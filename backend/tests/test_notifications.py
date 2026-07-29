"""Gate/escalation notifications (#60).

Three properties matter, in this order:

1. **Opt-in.** Nothing leaves the process unless a deployment configured it.
2. **PHI-free.** The payload is metadata; no patient or criteria detail rides out
   to Slack or a mail relay.
3. **Non-fatal.** A dead webhook or a refused SMTP connection must not disturb the
   screening it fired from.

The webhook channel is exercised against a stubbed `httpx.AsyncClient` and the
email channel against a stubbed `smtplib.SMTP`, so no test opens a socket.
"""

from __future__ import annotations

import smtplib
from typing import Any

import pytest

from app.config import Settings
from app.services import notifications


@pytest.fixture(autouse=True)
def clean_notify_env(monkeypatch):
    """Drop any ambient NOTIFY_* so these tests describe only what they set.

    Explicit kwargs outrank the environment in pydantic-settings, but the fields a
    given test *doesn't* pass (SMTP host, credentials) would otherwise be filled
    from the developer's shell and quietly change which channels fire.
    """
    for var in (
        "NOTIFY_ENABLED",
        "NOTIFY_WEBHOOK_URL",
        "NOTIFY_EMAIL_TO",
        "NOTIFY_EMAIL_FROM",
        "NOTIFY_SMTP_HOST",
        "NOTIFY_SMTP_PORT",
        "NOTIFY_SMTP_USERNAME",
        "NOTIFY_SMTP_PASSWORD",
        "NOTIFY_SMTP_STARTTLS",
        "NOTIFY_TIMEOUT_SECONDS",
        "NOTIFY_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


def _settings(**overrides: Any) -> Settings:
    """A Settings with notifications on and a webhook configured, by default."""
    base: dict[str, Any] = {
        "notify_enabled": True,
        "notify_webhook_url": "https://hooks.example.com/T000/B000",
        "notify_base_url": "https://trialgate.example.com",
    }
    merged: dict[str, Any] = {**base, **overrides}
    return Settings(_env_file=None, **merged)


class _StubResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubClient:
    """Stands in for `httpx.AsyncClient`, recording the POSTs it was handed."""

    posts: list[tuple[str, dict]] = []

    def __init__(self, status_code: int = 200, error: Exception | None = None, **_kw: Any):
        self.status_code = status_code
        self.error = error

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def post(self, url: str, json: dict) -> _StubResponse:
        type(self).posts.append((url, json))
        if self.error:
            raise self.error
        return _StubResponse(self.status_code)


@pytest.fixture
def webhook(monkeypatch):
    """Capture webhook POSTs. Returns the shared list the stub appends to."""
    _StubClient.posts = []

    def factory(**kwargs: Any) -> _StubClient:
        return _StubClient(**kwargs)

    monkeypatch.setattr(notifications.httpx, "AsyncClient", factory)
    return _StubClient.posts


@pytest.fixture
def failing_webhook(monkeypatch):
    """A webhook client whose POST raises — the outage path."""
    _StubClient.posts = []
    monkeypatch.setattr(
        notifications.httpx,
        "AsyncClient",
        lambda **_kw: _StubClient(error=RuntimeError("connection refused")),
    )
    return _StubClient.posts


class _StubSMTP:
    """Stands in for `smtplib.SMTP`, recording the conversation."""

    sent: list[dict[str, Any]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None

    def __enter__(self) -> _StubSMTP:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_args = (username, password)

    def send_message(self, message: Any) -> None:
        type(self).sent.append(
            {
                "host": self.host,
                "port": self.port,
                "starttls": self.started_tls,
                "login": self.login_args,
                "subject": message["Subject"],
                "to": message["To"],
                "from": message["From"],
                "body": message.get_content(),
            }
        )


@pytest.fixture
def smtp(monkeypatch):
    """Capture SMTP sends. Returns the shared list the stub appends to."""
    _StubSMTP.sent = []
    monkeypatch.setattr(notifications.smtplib, "SMTP", _StubSMTP)
    return _StubSMTP.sent


def _email_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "notify_webhook_url": None,
        "notify_smtp_host": "smtp.example.com",
        "notify_email_to": "a@example.com, b@example.com",
        "notify_email_from": "trialgate@example.com",
    }
    merged: dict[str, Any] = {**base, **overrides}
    return _settings(**merged)


# --- opt-in --------------------------------------------------------------


async def test_disabled_by_default_sends_nothing(webhook):
    """The default Settings has notifications off, so a parked run is silent."""
    settings = Settings(_env_file=None)
    assert settings.notify_enabled is False
    await notifications.notify_gate(settings, thread_id="t1", status="awaiting_approval")
    assert webhook == []


async def test_enabled_without_a_channel_cannot_be_constructed():
    """Startup validation, not a silent no-op: NOTIFY_ENABLED with nothing to
    notify through looks configured while paging nobody."""
    with pytest.raises(ValueError, match="NOTIFY_ENABLED=true requires a channel"):
        Settings(_env_file=None, notify_enabled=True)


@pytest.mark.parametrize("missing", ["notify_smtp_host", "notify_email_to", "notify_email_from"])
async def test_email_needs_host_recipients_and_sender(missing):
    """A half-configured email channel is the likeliest way to end up silently
    unnotified, so each missing piece fails at startup."""
    overrides: dict[str, Any] = {
        "notify_enabled": True,
        "notify_smtp_host": "smtp.example.com",
        "notify_email_to": "a@example.com",
        "notify_email_from": "trialgate@example.com",
        missing: "" if missing == "notify_email_to" else None,
    }
    with pytest.raises(ValueError, match="requires a channel"):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize("status", ["done", "failed", "parsing", "critiquing", "matching"])
async def test_statuses_nobody_has_to_act_on_are_not_notified(webhook, status):
    """A run that finished or failed on its own isn't waiting for anybody. Paging
    on those is how a notification channel gets muted."""
    await notifications.notify_gate(_settings(), thread_id="t1", status=status)
    assert webhook == []


@pytest.mark.parametrize("status", sorted(notifications.NOTIFY_STATUSES))
async def test_both_stop_statuses_notify(webhook, status):
    await notifications.notify_gate(_settings(), thread_id="t1", status=status)
    assert len(webhook) == 1


# --- webhook payload -----------------------------------------------------


async def test_webhook_payload_is_slack_shaped_and_structured(webhook):
    await notifications.notify_gate(
        _settings(),
        thread_id="abc-123",
        status="awaiting_approval",
        source_filename="nsclc-protocol.pdf",
        criteria_count=7,
    )
    (url, body) = webhook[0]
    assert url == "https://hooks.example.com/T000/B000"
    # `text` is what Slack renders; `event` is the same information as fields for a
    # generic consumer. One payload has to serve both.
    assert "nsclc-protocol.pdf" in body["text"]
    assert "awaiting approval" in body["text"]
    # The rendered message has to be actionable on its own — what to do, and a link.
    assert "must approve the extracted criteria" in body["text"]
    assert "https://trialgate.example.com/runs/view/?id=abc-123" in body["text"]
    assert body["event"] == {
        "thread_id": "abc-123",
        "status": "awaiting_approval",
        "source_filename": "nsclc-protocol.pdf",
        "criteria_count": 7,
        "url": "https://trialgate.example.com/runs/view/?id=abc-123",
    }


async def test_payload_carries_no_patient_or_criteria_detail(webhook):
    """The allowlist is the PHI control: the payload is built from the call's
    arguments, so there is no path for cohort or criteria content to ride out."""
    await notifications.notify_gate(
        _settings(), thread_id="abc", status="escalated", source_filename="p.pdf", criteria_count=2
    )
    (_url, body) = webhook[0]
    assert set(body) == {"text", "event"}
    assert set(body["event"]) == {
        "thread_id",
        "status",
        "source_filename",
        "criteria_count",
        "url",
    }


async def test_escalation_text_says_what_happened(webhook):
    await notifications.notify_gate(
        _settings(), thread_id="abc", status="escalated", source_filename="p.pdf"
    )
    assert "escalated for human review" in webhook[0][1]["text"]


async def test_link_is_omitted_rather_than_guessed(webhook):
    """No NOTIFY_BASE_URL means no link — a wrong host in a notification is worse
    than none."""
    await notifications.notify_gate(
        _settings(notify_base_url=None), thread_id="abc", status="awaiting_approval"
    )
    assert webhook[0][1]["event"]["url"] is None


async def test_link_matches_the_frontend_static_export_route(webhook):
    """Regression: the id is a *query parameter* on `/runs/view/`, not a path
    segment. The frontend is a static export (`output: "export"`), so
    `/runs/<thread_id>` is an unexported path — a hard 404 for every recipient who
    clicked it. Must stay in step with `runHref` in frontend/src/lib/runs.ts,
    including the trailing slash before `?`.
    """
    await notifications.notify_gate(_settings(), thread_id="abc-123", status="awaiting_approval")
    assert webhook[0][1]["event"]["url"] == "https://trialgate.example.com/runs/view/?id=abc-123"


async def test_thread_id_is_percent_encoded_in_the_link(webhook):
    """Thread ids are server-minted UUIDs today, but the id lands in a query
    string — encoding it keeps a future id format from producing a broken link."""
    await notifications.notify_gate(_settings(), thread_id="a b&c=d", status="awaiting_approval")
    assert webhook[0][1]["event"]["url"].endswith("/runs/view/?id=a%20b%26c%3Dd")


async def test_base_url_trailing_slash_does_not_double_up(webhook):
    await notifications.notify_gate(
        _settings(notify_base_url="https://trialgate.example.com/"),
        thread_id="abc",
        status="awaiting_approval",
    )
    assert webhook[0][1]["event"]["url"] == "https://trialgate.example.com/runs/view/?id=abc"


async def test_summary_falls_back_to_the_thread_id(webhook):
    """A run whose checkpoint has no filename still has to be identifiable."""
    await notifications.notify_gate(
        _settings(), thread_id="abc-123", status="awaiting_approval", source_filename=None
    )
    assert "abc-123" in webhook[0][1]["text"]


# --- email ---------------------------------------------------------------


async def test_email_is_sent_over_starttls_to_every_recipient(smtp):
    await notifications.notify_gate(
        _email_settings(notify_smtp_username="user", notify_smtp_password="pw"),
        thread_id="abc",
        status="awaiting_approval",
        source_filename="p.pdf",
        criteria_count=3,
    )
    (sent,) = smtp
    assert (sent["host"], sent["port"]) == ("smtp.example.com", 587)
    assert sent["to"] == "a@example.com, b@example.com"
    assert sent["from"] == "trialgate@example.com"
    # STARTTLS is negotiated before the credentials go anywhere.
    assert sent["starttls"] is True
    assert sent["login"] == ("user", "pw")
    assert "p.pdf" in sent["subject"]
    assert "https://trialgate.example.com/runs/view/?id=abc" in sent["body"]
    assert "3 extracted" in sent["body"]


async def test_email_without_credentials_skips_login(smtp):
    """An open relay on a trusted network needs no AUTH — offering one anyway
    fails on servers that don't advertise it."""
    await notifications.notify_gate(_email_settings(), thread_id="abc", status="awaiting_approval")
    assert smtp[0]["login"] is None


async def test_starttls_can_be_disabled_for_a_plaintext_relay(smtp):
    await notifications.notify_gate(
        _email_settings(notify_smtp_starttls=False), thread_id="abc", status="escalated"
    )
    assert smtp[0]["starttls"] is False


async def test_email_body_states_it_carries_no_patient_data(smtp):
    await notifications.notify_gate(
        _email_settings(), thread_id="abc", status="awaiting_approval", source_filename="p.pdf"
    )
    assert "no patient data" in smtp[0]["body"]


async def test_both_channels_fire_when_both_are_configured(webhook, smtp):
    await notifications.notify_gate(
        _email_settings(notify_webhook_url="https://hooks.example.com/T000/B000"),
        thread_id="abc",
        status="awaiting_approval",
    )
    assert len(webhook) == 1
    assert len(smtp) == 1


# --- failure isolation ---------------------------------------------------


async def test_webhook_failure_is_swallowed(failing_webhook):
    """`notify_gate` is called from inside a screening's SSE generator: an
    exception here would abort the stream of a run that actually succeeded."""
    await notifications.notify_gate(_settings(), thread_id="abc", status="awaiting_approval")
    # No raise. The POST was attempted before it blew up.
    assert len(failing_webhook) == 1


async def test_webhook_error_status_is_swallowed(monkeypatch):
    monkeypatch.setattr(
        notifications.httpx, "AsyncClient", lambda **_kw: _StubClient(status_code=500)
    )
    await notifications.notify_gate(_settings(), thread_id="abc", status="awaiting_approval")


async def test_smtp_failure_is_swallowed(monkeypatch):
    def refuse(*_a: object, **_k: object) -> None:
        raise smtplib.SMTPConnectError(421, "service unavailable")

    monkeypatch.setattr(notifications.smtplib, "SMTP", refuse)
    await notifications.notify_gate(_email_settings(), thread_id="abc", status="awaiting_approval")


async def test_a_bug_on_the_dispatch_path_is_swallowed_too(monkeypatch):
    """The never-raises guarantee is structural, not a matter of the internals
    staying careful: a fault outside a channel's own try still must not reach the
    screening that fired this."""

    def explode(*_a: object, **_k: object) -> dict:
        raise KeyError("regression in payload assembly")

    monkeypatch.setattr(notifications, "_payload", explode)
    await notifications.notify_gate(_settings(), thread_id="abc", status="awaiting_approval")


async def test_a_hung_channel_is_bounded_by_the_timeout(monkeypatch):
    """A webhook that never answers must not park the run forever; the timeout is
    the ceiling on what a notification can cost a screening."""
    import asyncio

    async def never_returns(_settings: Settings, _event: dict) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(notifications, "_send_webhook", never_returns)
    settings = _settings(notify_timeout_seconds=0.05)
    await asyncio.wait_for(
        notifications.notify_gate(settings, thread_id="abc", status="awaiting_approval"),
        timeout=5,
    )


async def test_one_dead_channel_does_not_block_the_other(monkeypatch, smtp):
    """Channels are dispatched concurrently, so a hanging webhook must not eat the
    email's budget — the reviewer still gets paged."""
    import asyncio

    async def never_returns(_settings: Settings, _event: dict) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(notifications, "_send_webhook", never_returns)
    settings = _email_settings(
        notify_webhook_url="https://hooks.example.com/T000/B000",
        notify_timeout_seconds=0.05,
    )
    await notifications.notify_gate(settings, thread_id="abc", status="awaiting_approval")
    assert len(smtp) == 1
