"""Authentication and role-based authorization (#50).

Covers the four acceptance criteria of the issue:

- Unauthenticated callers are turned away; authenticated ones get through
  (``test_unauthenticated_*``, ``test_login_*``).
- ``/approve`` rejects unauthenticated and under-privileged requests
  (``test_unauthenticated_request_is_401``, ``test_reviewer_is_forbidden_*``).
- The approver's identity lands in the run's audit trail (asserted end to end in
  ``test_api_integration.py``, and at the service layer in
  ``test_screening_service.py``).
- Roles are enforced in the API (``test_reviewer_is_forbidden_from_admin_route``)
  as well as the UI.

Plus the primitives underneath: password hashing, token signing/expiry, and the
route-coverage guard that keeps a future endpoint from shipping unguarded.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import (
    ANONYMOUS,
    Principal,
    RoleGuard,
    authenticate,
    configured_users,
    has_role,
    hash_password,
    issue_session,
    parse_users,
    session_secret,
    verify_password,
    verify_session,
)
from app.config import Settings
from tests.auth_helpers import ADMIN, PASSWORD, REVIEWER, bearer, sign_in

# Endpoints that must stay reachable without a session: the login exchange, the
# logout that clears an already-dead cookie, and the operator probes.
PUBLIC_PATHS = frozenset({"/api/auth/login", "/api/auth/logout", "/health", "/ready", "/metrics"})


@pytest.fixture
def client():
    """An unauthenticated client — this suite opts in to a session per test."""
    with TestClient(main.app, raise_server_exceptions=False) as c:
        yield c


def _upload(client, **kwargs):
    return client.post(
        "/api/screenings",
        files={"file": ("p.md", b"Inclusion criteria: age >= 18", "text/markdown")},
        **kwargs,
    )


# --- password hashing -------------------------------------------------------


def test_password_round_trips():
    encoded = hash_password("s3cret-passphrase")
    assert verify_password("s3cret-passphrase", encoded)
    assert not verify_password("s3cret-passphras", encoded)
    assert not verify_password("", encoded)


def test_hash_is_salted_per_call():
    # Two hashes of the same password must differ, or identical passwords would be
    # identifiable across accounts from the stored value alone.
    assert hash_password("same") != hash_password("same")


def test_hash_encodes_its_cost_parameters():
    # The parameters travel with the hash so they can be raised later without
    # invalidating existing ones.
    scheme, n, r, p, salt, digest = hash_password("x").split("$")
    assert scheme == "scrypt"
    assert int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1
    assert salt and digest


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-hash",
        "scrypt$only$three$parts",
        "bcrypt$16384$8$1$c2FsdA$aGFzaA",  # unknown scheme
        "scrypt$notanumber$8$1$c2FsdA$aGFzaA",
        "scrypt$16384$8$1$!!!$!!!",  # undecodable base64
    ],
)
def test_malformed_hash_never_authenticates(malformed):
    """A typo'd AUTH_USERS entry must lock that account out — never authenticate
    it, and never 500 the login endpoint."""
    assert verify_password("anything", malformed) is False


def test_hash_with_a_good_salt_but_corrupt_digest_fails_closed():
    """Regression: the digest is decoded *inside* the guarded block.

    With a decodable salt the scrypt call succeeds, so a corrupt digest is the one
    malformed shape that reaches the final comparison — and base64 errors are
    ValueErrors, which would otherwise propagate and 500 the login endpoint
    instead of rejecting the credentials.
    """
    good_salt = hash_password("x").split("$")[4]
    assert verify_password("x", f"scrypt$16384$8$1${good_salt}$!!!not-base64!!!") is False


def test_unknown_email_costs_the_same_work_as_a_wrong_password():
    """Regression: the timing equalizer must burn exactly one scrypt pass.

    Hashing the decoy per call made the unknown-email path cost *two* passes
    against a real account's one — a louder user-enumeration oracle than having no
    equalizer at all. Asserted as a ratio with generous slack, so this catches the
    2x regression without being a latency benchmark.
    """
    users = parse_users(f"known@example.com:reviewer:{hash_password('pw')}")

    def elapsed(email: str) -> float:
        start = time.perf_counter()
        for _ in range(3):
            assert authenticate(email, "wrong-password", users) is None
        return time.perf_counter() - start

    # Warm the cached decoy hash so its one-off cost isn't attributed to the call.
    authenticate("ghost@example.com", "x", users)
    known, unknown = elapsed("known@example.com"), elapsed("ghost@example.com")
    assert unknown < known * 1.7, f"unknown-email path took {unknown / known:.2f}x a known one"


# --- user configuration -----------------------------------------------------


def test_parse_users_reads_roles_and_lowercases_emails():
    users = parse_users(
        f"Reviewer@Example.COM:reviewer:{hash_password('a')}\n"
        f"boss@example.com:admin:{hash_password('b')}"
    )
    assert set(users) == {"reviewer@example.com", "boss@example.com"}
    assert users["reviewer@example.com"].role == "reviewer"
    assert users["boss@example.com"].role == "admin"


@pytest.mark.parametrize(
    "spec",
    [
        "missing-role@example.com:hash-only",
        "who@example.com:superuser:hash",  # role not on the ladder
        "who@example.com::hash",
        ":reviewer:hash",
        "who@example.com:reviewer:",
        "dupe@example.com:reviewer:h,dupe@example.com:admin:h",
    ],
)
def test_malformed_user_spec_fails_loudly(spec):
    """Configuration errors surface at startup, not on the first login attempt."""
    with pytest.raises(ValueError):
        parse_users(spec)


def test_configured_users_prefers_auth_users_over_demo_accounts():
    """A configured deployment must not silently carry the published demo
    accounts alongside its own — that would be a backdoor."""
    settings = Settings(
        _env_file=None,
        auth_users=f"only@example.com:admin:{hash_password('x')}",
        auth_demo_users=True,
    )
    assert set(configured_users(settings)) == {"only@example.com"}


def test_demo_accounts_are_the_fallback_when_unconfigured():
    settings = Settings(_env_file=None, auth_users="", auth_demo_users=True)
    users = configured_users(settings)
    assert set(users) == {"reviewer@trialgate.local", "admin@trialgate.local"}
    assert users["admin@trialgate.local"].role == "admin"
    # Demo passwords are published in the README; they must actually work.
    assert verify_password("trialgate-admin", users["admin@trialgate.local"].password_hash)


def test_auth_on_with_no_accounts_is_a_startup_error():
    """Auth enabled with no way to sign in is a locked-out app. Fail at startup."""
    with pytest.raises(ValueError, match="requires accounts"):
        Settings(_env_file=None, auth_enabled=True, auth_users="", auth_demo_users=False)


def test_auth_off_with_no_accounts_is_fine():
    settings = Settings(_env_file=None, auth_enabled=False, auth_users="", auth_demo_users=False)
    assert settings.auth_enabled is False
    assert configured_users(settings) == {}


def test_user_spec_tolerates_blank_entries_and_whitespace():
    """A trailing comma or a newline-per-account list (the readable way to write
    this in a compose file or Kubernetes secret) must not be a config error."""
    users = parse_users(f"\n  a@example.com:reviewer:{hash_password('p')} ,\n\n")
    assert set(users) == {"a@example.com"}


# --- authenticate -----------------------------------------------------------


def test_authenticate_accepts_correct_credentials():
    users = parse_users(f"r@example.com:reviewer:{hash_password('pw')}")
    assert authenticate("r@example.com", "pw", users) == Principal("r@example.com", "reviewer")


def test_authenticate_is_case_insensitive_on_email():
    users = parse_users(f"r@example.com:reviewer:{hash_password('pw')}")
    assert authenticate("  R@Example.com ", "pw", users) is not None


@pytest.mark.parametrize(
    ("email", "password"),
    [("r@example.com", "wrong"), ("nobody@example.com", "pw"), ("", "")],
)
def test_authenticate_rejects_bad_credentials(email, password):
    users = parse_users(f"r@example.com:reviewer:{hash_password('pw')}")
    assert authenticate(email, password, users) is None


# --- session tokens ---------------------------------------------------------


def test_session_round_trips():
    token = issue_session(REVIEWER, "shhh", 3600)
    assert verify_session(token, "shhh") == REVIEWER


def test_session_signed_with_another_key_is_rejected():
    token = issue_session(REVIEWER, "key-a", 3600)
    assert verify_session(token, "key-b") is None


def test_expired_session_is_rejected():
    assert verify_session(issue_session(REVIEWER, "k", -1), "k") is None


def test_tampered_payload_is_rejected():
    """The role can't be edited upward: the signature covers the payload, and it
    is checked before the payload is even parsed."""
    payload, _, signature = issue_session(REVIEWER, "k", 3600).partition(".")
    forged = issue_session(Principal(REVIEWER.email, "admin"), "k", 3600).partition(".")[0]
    assert verify_session(f"{forged}.{signature}", "k") is None
    assert verify_session(f"{payload}.{signature}x", "k") is None


@pytest.mark.parametrize("token", ["", ".", "no-dot", "a.b", "....", "!!!.???"])
def test_malformed_token_is_rejected(token):
    assert verify_session(token, "k") is None


@pytest.mark.parametrize("payload", ["[1, 2]", '"a string"', "null", "42"])
def test_correctly_signed_non_object_payload_is_rejected(payload):
    """Signed but structurally wrong: a JSON scalar or array must not reach the
    field lookups as though it were a session."""
    from app.auth import _b64e, _sign

    encoded = _b64e(payload.encode())
    assert verify_session(f"{encoded}.{_sign(encoded, 'k')}", "k") is None


def test_session_without_an_expiry_is_rejected():
    from app.auth import _b64e, _sign

    encoded = _b64e(b'{"sub":"a@b.c","role":"admin"}')
    assert verify_session(f"{encoded}.{_sign(encoded, 'k')}", "k") is None


@pytest.mark.parametrize("body", [b"not json at all", b"{unclosed", b"\xff\xfe binary"])
def test_correctly_signed_garbage_payload_is_rejected(body):
    """Reached only with a valid signature (a corrupted cookie, or a holder of the
    key): the JSON decode must fail closed rather than raise out of the guard."""
    from app.auth import _b64e, _sign

    encoded = _b64e(body)
    assert verify_session(f"{encoded}.{_sign(encoded, 'k')}", "k") is None


# --- signing key ------------------------------------------------------------


def test_configured_secret_is_used_verbatim():
    settings = Settings(_env_file=None, auth_secret="my-key")
    assert session_secret(settings) == "my-key"


def test_unset_secret_falls_back_to_a_stable_random_per_process_key():
    """Safe by default — no signing key ships in the repo — but the same key for
    the life of the process, or every request would invalidate the last one's
    cookie."""
    settings = Settings(_env_file=None, auth_secret=None)
    first = session_secret(settings)
    assert first and first != "my-key"
    assert session_secret(settings) == first
    # And it actually works as a signing key.
    assert verify_session(issue_session(REVIEWER, first, 60), first) == REVIEWER


def test_session_with_an_unknown_role_is_rejected():
    """A token minted before a role was retired (or hand-crafted) must not
    resolve to a principal with an off-ladder role."""
    forged = issue_session(Principal("x@example.com", "superuser"), "k", 3600)  # type: ignore[arg-type]
    assert verify_session(forged, "k") is None


# --- the role ladder --------------------------------------------------------


@pytest.mark.parametrize(
    ("actual", "minimum", "allowed"),
    [
        ("reviewer", "reviewer", True),
        ("admin", "reviewer", True),
        ("admin", "admin", True),
        ("reviewer", "admin", False),
        ("nonsense", "reviewer", False),
    ],
)
def test_role_ladder(actual, minimum, allowed):
    assert has_role(actual, minimum) is allowed


# --- login / logout / me ----------------------------------------------------


def test_login_sets_a_hardened_session_cookie(client):
    response = client.post("/api/auth/login", json={"email": REVIEWER.email, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"email": REVIEWER.email, "role": "reviewer"}

    cookie = response.headers["set-cookie"].lower()
    # httponly: no JS (and so no XSS foothold) can read the session.
    assert "httponly" in cookie
    # samesite=strict is the CSRF defense for the POSTs below.
    assert "samesite=strict" in cookie
    assert f"{main.settings.auth_cookie_name}=" in cookie

    # And the session it issued actually works.
    assert client.get("/api/auth/me").status_code == 200


def test_login_is_case_insensitive_and_trims(client):
    response = client.post(
        "/api/auth/login", json={"email": f"  {REVIEWER.email.upper()} ", "password": PASSWORD}
    )
    assert response.status_code == 200
    assert response.json()["email"] == REVIEWER.email


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("reviewer@test.local", "wrong-password"),
        ("ghost@test.local", PASSWORD),
    ],
)
def test_login_rejects_bad_credentials_identically(client, email, password):
    """One message for both failures — the response must not confirm whether an
    email is registered."""
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json() == {
        "error": "InvalidCredentialsError",
        "detail": "Incorrect email or password.",
    }
    assert "set-cookie" not in response.headers


def test_login_validates_its_payload(client):
    assert client.post("/api/auth/login", json={"email": "a@b.c"}).status_code == 422
    # Bounded so a login attempt can't hand scrypt a huge input to chew on.
    oversized = client.post("/api/auth/login", json={"email": "a@b.c", "password": "x" * 5000})
    assert oversized.status_code == 422


@pytest.mark.parametrize(
    "email",
    [
        "no-at-sign",
        "spaces in@example.com",
        "newline@example.com\nfake.log.line=1",  # log forging via the failure log
        "tab\t@example.com",
        "",
        "a" * 400 + "@example.com",  # over the length cap
    ],
)
def test_login_rejects_malformed_emails_before_hashing(client, email):
    """422, not 401: the value is echoed into the failed-login log line, so it gets
    the same shape constraint X-Request-ID gets — and junk never costs a scrypt
    pass."""
    response = client.post("/api/auth/login", json={"email": email, "password": "x"})
    assert response.status_code == 422


def test_me_reports_the_role(client):
    sign_in(client, ADMIN)
    assert client.get("/api/auth/me").json() == {"email": ADMIN.email, "role": "admin"}


def test_me_is_401_without_a_session(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"] == "AuthenticationRequiredError"


def test_logout_clears_the_session(client):
    # Signs in through the real endpoint rather than tests.auth_helpers: the point
    # here is that the server's Set-Cookie deletion undoes the server's own
    # Set-Cookie, which only holds if both went through the same cookie jar keys.
    client.post("/api/auth/login", json={"email": REVIEWER.email, "password": PASSWORD})
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_logout_without_a_session_still_succeeds(client):
    """Signing out of an already-expired session must clear the stale cookie
    rather than 401."""
    assert client.post("/api/auth/logout").status_code == 200


def test_a_forged_cookie_does_not_authenticate(client):
    client.cookies.set(main.settings.auth_cookie_name, "totally.forged")
    assert client.get("/api/auth/me").status_code == 401


def test_expired_cookie_does_not_authenticate(client):
    expired = issue_session(REVIEWER, session_secret(main.settings), -1)
    client.cookies.set(main.settings.auth_cookie_name, expired)
    assert client.get("/api/auth/me").status_code == 401


# --- bearer tokens (non-browser clients) ------------------------------------


def test_bearer_token_authenticates(client):
    """EventSource can't set headers, so the browser uses a cookie — but scripts
    and the load-test harness need a header path."""
    assert client.get("/api/auth/me", headers=bearer()).status_code == 200


def test_bearer_is_ignored_when_malformed(client):
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer"}).status_code == 401


def test_cookie_wins_over_a_bearer_header(client):
    """Precedence must be deterministic, so a stale header can't silently
    downgrade an active browser session."""
    sign_in(client, ADMIN)
    response = client.get("/api/auth/me", headers=bearer(REVIEWER))
    assert response.json()["role"] == "admin"


# --- every screening route is gated -----------------------------------------

PROTECTED_REQUESTS = [
    ("POST", "/api/screenings"),
    ("GET", "/api/screenings"),
    ("GET", "/api/screenings/any-id/stream"),
    ("POST", "/api/screenings/any-id/approve"),
    # The gate's other decision (#91) carries the same authority as approval, so
    # it is guarded identically — an anonymous caller must not be able to end a run.
    ("POST", "/api/screenings/any-id/reject"),
    ("PATCH", "/api/screenings/any-id/criteria"),
    ("GET", "/api/screenings/any-id/state"),
    ("GET", "/api/admin/users"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
def test_unauthenticated_request_is_401(client, method, path):
    """Including /approve and /state — an anonymous caller must not learn whether
    a thread exists, so this 401 precedes the 404/409 checks."""
    response = client.request(method, path)
    assert response.status_code == 401, path
    assert response.json()["error"] == "AuthenticationRequiredError"


@pytest.mark.parametrize(("method", "path"), PROTECTED_REQUESTS)
def test_authenticated_request_is_not_401(client, method, path):
    """The guards don't reject a valid session: whatever these return (404 for an
    unknown thread, 200, 422), it isn't an auth failure."""
    sign_in(client, ADMIN)
    response = client.request(method, path)
    assert response.status_code not in (401, 403), path


def test_reviewer_reaches_the_screening_routes(client):
    sign_in(client, REVIEWER)
    upload = _upload(client)
    assert upload.status_code == 200
    assert client.get("/api/screenings").status_code == 200


def test_reviewer_is_forbidden_from_admin_route(client):
    """Role enforced in the API, not just by hiding the nav entry."""
    sign_in(client, REVIEWER)
    response = client.get("/api/admin/users")
    assert response.status_code == 403
    assert response.json()["error"] == "AuthorizationDeniedError"


def test_admin_lists_accounts_without_password_hashes(client):
    sign_in(client, ADMIN)
    response = client.get("/api/admin/users")
    assert response.status_code == 200
    body = response.json()
    assert {u["email"] for u in body} == {REVIEWER.email, ADMIN.email}
    assert all(set(u) == {"email", "role"} for u in body)
    # The verifier must never leave the server.
    assert "scrypt" not in response.text


def test_admin_also_reaches_the_reviewer_routes(client):
    """The ladder is inclusive: an admin can work the approval gate too."""
    sign_in(client, ADMIN)
    assert _upload(client).status_code == 200


# --- coverage guard ---------------------------------------------------------


def test_every_api_route_declares_an_auth_guard():
    """The requirement that can't be met by review alone.

    Guards are per-route dependencies, which means a new endpoint is unprotected
    by *omission* — nothing fails, it just silently ships open. This walks the
    real route table so that mistake breaks the build instead.
    """
    unguarded = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api") or path in PUBLIC_PATHS:
            continue
        dependant = getattr(route, "dependant", None)
        guards = [
            d.call
            for d in (dependant.dependencies if dependant else [])
            if isinstance(d.call, RoleGuard)
        ]
        if not guards:
            unguarded.append(path)
    assert not unguarded, (
        f"These /api routes have no RoleGuard dependency: {unguarded}. "
        "Add Depends(require_reviewer) / Depends(require_admin), or list the path "
        "in PUBLIC_PATHS if it is deliberately open."
    )


# --- auth disabled ----------------------------------------------------------


def test_auth_can_be_disabled_for_local_runs(client, monkeypatch):
    """AUTH_ENABLED=false is the single-user / load-test escape hatch."""
    monkeypatch.setattr(main.settings, "auth_enabled", False)
    # get_settings() is what RoleGuard reads, and it returns this same cached
    # instance — so patching the attribute covers both.
    assert client.get("/api/auth/me").json() == {"email": ANONYMOUS.email, "role": "admin"}
    assert _upload(client).status_code == 200


def test_disabled_auth_principal_is_identifiable_in_an_audit_trail():
    """A run approved with auth off must never look like a real reviewer signed
    it off."""
    assert "anonymous" in ANONYMOUS.email
    assert "@" in ANONYMOUS.email
    assert ANONYMOUS.email not in {REVIEWER.email, ADMIN.email}


async def test_role_guard_allows_anonymous_when_auth_is_disabled(monkeypatch):
    """The guard itself short-circuits, so this holds for every route at once."""
    monkeypatch.setattr(main.settings, "auth_enabled", False)

    class _Request:
        cookies: dict[str, str] = {}
        headers: dict[str, str] = {}

    assert await RoleGuard("admin")(_Request()) == ANONYMOUS  # type: ignore[arg-type]
