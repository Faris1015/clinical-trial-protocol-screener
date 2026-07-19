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
