"""Shared test setup.

Force the in-memory persistence backend before the app is imported anywhere, so
tests never touch a real sqlite file and each `TestClient` lifespan gets a fresh,
isolated store. Individual tests that exercise the graph still monkeypatch
`app.main.graph` with a fake; those that only exercise the store rely on this
process-local backend.
"""

import os

# Hard-set (not setdefault): an ambient CHECKPOINT_BACKEND from the developer's
# shell or CI must not leak in and turn the isolated in-memory store into a
# shared real database. Tests that need sqlite build their own Settings.
os.environ["CHECKPOINT_BACKEND"] = "memory"

# Rate limiting uses a process-wide in-memory counter keyed by client IP. Left
# on, it would accumulate across the many requests the suite makes from the same
# TestClient host and trip spuriously. Disable it globally; test_rate_limiting
# re-enables the limiter locally to prove it works.
os.environ["RATE_LIMIT_ENABLED"] = "false"

# Auth (#50) stays ON for the whole suite: it is the production default, and the
# existing API suites are what prove the guards don't break the real request
# paths. They authenticate via tests/auth_helpers.sign_in.
#
# A fixed signing key makes tokens reproducible across the Settings instances
# individual tests construct — without it each would mint its own random
# per-process secret and reject the others' cookies. Test-only value; production
# sets a real AUTH_SECRET.
os.environ["AUTH_SECRET"] = "test-only-session-signing-key-not-a-secret"

# Configure explicit test accounts rather than relying on the built-in demo
# users, so the suite neither depends on nor silently blesses the published demo
# passwords.
from app.config import get_settings  # noqa: E402
from tests.auth_helpers import users_env  # noqa: E402

os.environ["AUTH_USERS"] = users_env()
os.environ["AUTH_DEMO_USERS"] = "false"

# Importing anything under `app` (above, to reach `hash_password`) transitively
# imports app.logging_config, which calls configure_logging() -> get_settings() at
# import time. That caches a Settings built from a half-populated environment, so
# drop it: the next caller — app.main, at its own import — then builds one from
# the environment as finally configured here. Without this the suite silently
# falls back to the demo accounts.
get_settings.cache_clear()
