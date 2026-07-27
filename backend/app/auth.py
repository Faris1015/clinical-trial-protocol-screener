"""Authentication and role-based authorization (#50).

The human-in-the-loop gate is the point where patient data gets touched, so it
is gated to an authenticated, authorized reviewer — and the approver's identity
is recorded in the run's audit trail (see `app/services/screening.py`).

**Why the backend owns auth.** The frontend is a *static export* (no request-time
Node server — see `frontend/next.config.ts`), which rules out NextAuth/Auth.js:
its whole contract is server routes. So FastAPI is the identity authority and the
frontend is a pure client of it. That is also the topology-independent choice —
the same flow works behind nginx, in the single-service demo image, and on
`next dev`.

**Session transport.** A signed, expiring token delivered as an httpOnly cookie.
Cookies (not an `Authorization` header) are load-bearing here: the live pipeline
view is an `EventSource`, and the SSE spec gives no way to set request headers on
one. A cookie rides along automatically. `Authorization: Bearer <token>` is
*also* accepted for non-browser clients (the load-test harness, scripts).

**CSRF.** Cookie auth on state-changing POSTs (`/approve`) needs a CSRF answer.
This one is `SameSite=Strict`: every deployed topology serves the app and the API
from a single origin (nginx proxies `/api`; the demo image mounts the bundle in
the API process), so a strict cookie is never needed cross-site and a
cross-origin POST therefore arrives with no credentials at all. Bearer tokens are
immune by construction — a browser never attaches them on its own.

**Crypto.** Passwords: `hashlib.scrypt` (memory-hard, stdlib — no bcrypt/passlib
build dependency). Sessions: HMAC-SHA256 over a compact JSON payload. Both are
stdlib primitives used the sanctioned way (`secrets` for salts, `compare_digest`
for every comparison). The token format is deliberately *not* JWT: we issue and
verify with one fixed algorithm and never read an algorithm out of the token, so
the entire class of alg-confusion attacks is absent rather than mitigated.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, get_args

from fastapi import Request

from app.config import get_settings
from app.exceptions import AuthenticationRequiredError, AuthorizationDeniedError
from app.logging_config import get_logger

if TYPE_CHECKING:
    from app.config import Settings

log = get_logger("auth")

Role = Literal["reviewer", "admin"]

# Roles are a ladder, not a set: a route declares the *minimum* it accepts, so
# adding a rung later doesn't mean revisiting every guard. "reviewer" can work
# the approval gate; "admin" can do that plus manage rules and users.
_ROLE_RANK: dict[str, int] = {"reviewer": 1, "admin": 2}

ROLES: tuple[str, ...] = get_args(Role)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a request."""

    email: str
    role: Role


@dataclass(frozen=True)
class AuthUser:
    """A configured account: who they are, what they may do, and their verifier."""

    email: str
    role: Role
    password_hash: str


def has_role(actual: str, minimum: str) -> bool:
    """Whether `actual` clears the `minimum` rung of the role ladder."""
    return _ROLE_RANK.get(actual, 0) >= _ROLE_RANK.get(minimum, 0)


# --- password hashing -------------------------------------------------------

# scrypt cost. 2**14 * 8 * 128 = 16 MiB of memory per hash and ~50-100 ms on a
# modern core: enough to make offline cracking expensive without making a login
# feel slow. `maxmem` must be set explicitly — OpenSSL's default ceiling is
# below what these parameters need on some builds.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16

# Verified against when the email is unknown, so a bad email and a bad password
# cost the same wall-clock time and the login endpoint can't be turned into a
# user-enumeration oracle.
_DUMMY_PASSWORD = "trialgate-timing-equalizer"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    """Encode a password as `scrypt$n$r$p$salt$hash` (base64url, unpadded).

    The cost parameters travel inside the string, so raising them later doesn't
    invalidate hashes minted under the old ones.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of `password` against a `hash_password` string.

    A malformed or unknown-scheme hash verifies as False rather than raising: a
    typo'd `AUTH_USERS` entry must lock that account out, never authenticate it
    and never 500 the login endpoint.
    """
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        derived = _scrypt(password, _b64d(salt_b64), int(n), int(r), int(p))
        # Decoding the digest belongs inside the try too: base64 errors are
        # ValueErrors, and a hash with a good salt but a corrupt digest would
        # otherwise raise straight out of here and 500 the login endpoint.
        expected = _b64d(digest_b64)
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


# --- configured users -------------------------------------------------------

# Accounts for the zero-config public demo (`AUTH_DEMO_USERS`, on by default so
# `docker compose up` and the free demo image both land on a working login
# screen). Passwords live here in plaintext *on purpose*: they are published in
# the README, so an opaque hash in the source would only obscure that these are
# throwaway credentials, and hashing at startup keeps them on the same code path
# as real ones. Setting AUTH_USERS replaces this list outright — a real
# deployment never carries these accounts alongside its own.
_DEMO_USERS: tuple[tuple[str, Role, str], ...] = (
    ("reviewer@trialgate.local", "reviewer", "trialgate-reviewer"),
    ("admin@trialgate.local", "admin", "trialgate-admin"),
)


def parse_users(spec: str) -> dict[str, AuthUser]:
    """Parse `AUTH_USERS` — `email:role:hash` entries, comma- or newline-separated.

    Raises ValueError on anything malformed so a broken user list fails at
    startup, not on the first login attempt. `:` is a safe field separator: it
    can't appear in an email address, and a scrypt hash uses only `$` and
    base64url characters.
    """
    users: dict[str, AuthUser] = {}
    for entry in (e.strip() for e in spec.replace("\n", ",").split(",")):
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Malformed AUTH_USERS entry {entry!r}: expected 'email:role:password_hash'."
            )
        email, role, password_hash = (part.strip() for part in parts)
        if not email or not password_hash:
            raise ValueError(f"AUTH_USERS entry {entry!r} is missing an email or password hash.")
        if role not in ROLES:
            raise ValueError(
                f"AUTH_USERS entry for {email!r} has unknown role {role!r}; "
                f"expected one of {', '.join(ROLES)}."
            )
        key = email.lower()
        if key in users:
            raise ValueError(f"AUTH_USERS lists {email!r} more than once.")
        users[key] = AuthUser(email=key, role=role, password_hash=password_hash)  # type: ignore[arg-type]
    return users


@lru_cache(maxsize=1)
def _demo_users() -> dict[str, AuthUser]:
    """Hash the demo accounts once per process (scrypt is deliberately slow)."""
    log.warning(
        "auth.demo_users_active",
        detail=(
            "Built-in demo accounts are enabled with published passwords. "
            "Set AUTH_USERS (and AUTH_DEMO_USERS=false) for any real deployment."
        ),
        accounts=[email for email, _role, _password in _DEMO_USERS],
    )
    return {
        email: AuthUser(email=email, role=role, password_hash=hash_password(password))
        for email, role, password in _DEMO_USERS
    }


def configured_users(settings: Settings) -> dict[str, AuthUser]:
    """The accounts this instance accepts, keyed by lowercased email.

    `AUTH_USERS` wins outright when set; the demo accounts are a fallback for the
    zero-config demo, never an addition to a configured list.
    """
    if settings.auth_users.strip():
        return parse_users(settings.auth_users)
    if settings.auth_demo_users:
        return _demo_users()
    return {}


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """The decoy verifier, hashed once per process.

    Computing it per call would make the unknown-email path do *two* scrypt
    passes against a real account's one — turning the equalizer below into a
    louder enumeration oracle than having none at all.
    """
    return hash_password(_DUMMY_PASSWORD)


def authenticate(email: str, password: str, users: dict[str, AuthUser]) -> Principal | None:
    """Resolve a login to a Principal, or None when the credentials don't hold."""
    user = users.get(email.strip().lower())
    if user is None:
        # Burn one scrypt pass — the same work a real account costs — so response
        # time doesn't reveal whether the email is registered.
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash):
        return None
    return Principal(email=user.email, role=user.role)


# --- session tokens ---------------------------------------------------------


@lru_cache(maxsize=1)
def _ephemeral_secret() -> str:
    """A per-process signing key, used when AUTH_SECRET is unset.

    Safe by default — nothing secret ships in the repo — but sessions die on
    restart and two replicas reject each other's cookies, so a real deployment
    must set AUTH_SECRET. Warned about once, loudly.
    """
    log.warning(
        "auth.ephemeral_secret",
        detail=(
            "AUTH_SECRET is unset; signing sessions with a random per-process key. "
            "Sessions will not survive a restart and will not work across replicas."
        ),
    )
    return secrets.token_urlsafe(32)


def session_secret(settings: Settings) -> str:
    return settings.auth_secret or _ephemeral_secret()


def _sign(payload_b64: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def issue_session(principal: Principal, secret: str, ttl_seconds: int) -> str:
    """Mint `<payload>.<signature>` carrying the principal and an expiry."""
    payload = {
        "sub": principal.email,
        "role": principal.role,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    # Compact, key-sorted JSON so the signed bytes are reproducible.
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_session(token: str, secret: str) -> Principal | None:
    """Validate a session token, returning its Principal or None.

    None covers every rejection — malformed, wrong signature, expired, unknown
    role. The signature is checked *before* the payload is parsed, so unsigned
    input never reaches the JSON decoder.
    """
    payload_b64, _, signature = token.partition(".")
    if not payload_b64 or not signature:
        return None
    if not hmac.compare_digest(_sign(payload_b64, secret), signature):
        return None
    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    email, role, expires_at = payload.get("sub"), payload.get("role"), payload.get("exp")
    if not isinstance(email, str) or role not in ROLES:
        return None
    if not isinstance(expires_at, int) or expires_at <= time.time():
        return None
    return Principal(email=email, role=role)


# --- request-time resolution ------------------------------------------------

_BEARER_PREFIX = "bearer "

# Stands in for the caller when AUTH_ENABLED=false (single-user local runs, the
# load-test harness). Full privileges, and an identity that is unmistakable in an
# audit trail — so a run approved with auth off can never be mistaken for one a
# real reviewer signed off.
ANONYMOUS: Principal = Principal(email="anonymous@auth-disabled", role="admin")


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


def principal_from_request(request: Request, settings: Settings) -> Principal | None:
    """The verified caller behind this request, or None when unauthenticated.

    Cookie first (the browser path, and the only one an EventSource can use),
    then `Authorization: Bearer` for non-browser clients.
    """
    token = request.cookies.get(settings.auth_cookie_name) or _bearer_token(request)
    if not token:
        return None
    return verify_session(token, session_secret(settings))


class RoleGuard:
    """FastAPI dependency: authenticate the caller and enforce a minimum role.

    Declared per route rather than applied as blanket middleware, so the guard is
    visible in the signature and the handler receives the Principal it needs to
    write into the audit trail. `test_auth.py` asserts every non-auth `/api`
    route carries one, which is what keeps a future route from shipping open.
    """

    def __init__(self, minimum: Role) -> None:
        self.minimum = minimum

    async def __call__(self, request: Request) -> Principal:
        settings = get_settings()
        if not settings.auth_enabled:
            return ANONYMOUS
        principal = principal_from_request(request, settings)
        if principal is None:
            raise AuthenticationRequiredError("Sign in to use this endpoint.")
        if not has_role(principal.role, self.minimum):
            log.warning(
                "auth.forbidden",
                path=request.url.path,
                role=principal.role,
                required=self.minimum,
            )
            raise AuthorizationDeniedError(
                f"This action requires the '{self.minimum}' role or higher."
            )
        return principal


require_reviewer = RoleGuard("reviewer")
require_admin = RoleGuard("admin")


def _main() -> int:  # pragma: no cover - operator utility, not part of the request path
    """`python -m app.auth hash` — mint an AUTH_USERS password hash.

    The other half of "admin manages users": there is no way to hand-write a
    scrypt hash, so provisioning an account needs this.
    """
    import getpass
    import sys

    if len(sys.argv) != 2 or sys.argv[1] != "hash":
        sys.stderr.write(
            "usage: python -m app.auth hash\n"
            "  Prompts for a password and prints its AUTH_USERS hash.\n"
        )
        return 2
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm: "):
        sys.stderr.write("Passwords did not match.\n")
        return 1
    if not password:
        sys.stderr.write("Password must not be empty.\n")
        return 1
    sys.stdout.write(f"{hash_password(password)}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
