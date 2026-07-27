"""Shared test authentication (#50).

Every `/api` route requires a session, so the existing suites need one. They get
it by minting a signed token and dropping it in the client's cookie jar rather
than POSTing to `/api/auth/login`: it skips a deliberately slow scrypt
verification per test, and it keeps those suites testing what they are about
(uploads, rate limits, persistence) instead of coupling them to credentials.

The login endpoint itself — password verification, rejection, cookie flags,
role enforcement — is covered end to end in `test_auth.py`.
"""

from __future__ import annotations

from typing import Protocol

from app.auth import Principal, issue_session, session_secret
from app.config import get_settings

REVIEWER = Principal(email="reviewer@test.local", role="reviewer")
ADMIN = Principal(email="admin@test.local", role="admin")

# The password behind both accounts, as configured into AUTH_USERS by conftest.
# Only `test_auth.py` needs it — everything else short-circuits the password step.
PASSWORD = "correct-horse-battery-staple"


def users_env() -> str:
    """An `AUTH_USERS` value for the accounts above, hashed at call time.

    Called once from conftest before the app is imported. Hashing here (rather
    than pasting a literal scrypt string into source) keeps the fixture honest:
    the suite authenticates through exactly the encoding `hash_password` produces.
    """
    from app.auth import hash_password

    return ",".join(
        f"{principal.email}:{principal.role}:{hash_password(PASSWORD)}"
        for principal in (REVIEWER, ADMIN)
    )


class _HasCookies(Protocol):
    """Both `TestClient` and `httpx.AsyncClient` expose a cookie jar this way."""

    @property
    def cookies(self) -> object: ...


def session_token(principal: Principal = REVIEWER) -> str:
    settings = get_settings()
    return issue_session(principal, session_secret(settings), settings.auth_session_ttl_seconds)


def sign_in(client: _HasCookies, principal: Principal = REVIEWER) -> None:
    """Give `client` a valid session for `principal`, as the browser would hold."""
    settings = get_settings()
    client.cookies.set(settings.auth_cookie_name, session_token(principal))  # type: ignore[attr-defined]


def bearer(principal: Principal = REVIEWER) -> dict[str, str]:
    """Header form of the same session, for the non-browser code path."""
    return {"Authorization": f"Bearer {session_token(principal)}"}
