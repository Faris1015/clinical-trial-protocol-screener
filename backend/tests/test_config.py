"""Settings behavior: defaults, env overrides, and fail-fast validation."""

import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate each test from the developer's real environment and the cache."""
    for var in (
        "LLM_PROVIDER",
        "OLLAMA_MODEL",
        "OLLAMA_NUM_PREDICT",
        "OLLAMA_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "LLM_TEMPERATURE",
        "CORS_ORIGINS",
        "MAX_PARSE_ATTEMPTS",
        "RULES_PATH",
        "PATIENTS_PATH",
        "LOG_LEVEL",
        "MAX_UPLOAD_BYTES",
        "UPLOAD_CONTENT_TYPES",
        "RATE_LIMIT_CREATE",
        "RATE_LIMIT_READ",
        "RATE_LIMIT_ENABLED",
        "MAX_CONCURRENT_SCREENINGS",
        "CONCURRENCY_RETRY_AFTER_SECONDS",
        "SSE_HEARTBEAT_SECONDS",
        "SSE_IDLE_TIMEOUT_SECONDS",
        "SSE_MATCHER_IDLE_TIMEOUT_SECONDS",
        "NOTIFY_ENABLED",
        "NOTIFY_WEBHOOK_URL",
        "NOTIFY_EMAIL_TO",
        "NOTIFY_EMAIL_FROM",
        "NOTIFY_SMTP_HOST",
        "NOTIFY_TIMEOUT_SECONDS",
        "NOTIFY_BASE_URL",
        "NOTIFY_STALE_AFTER_SECONDS",
        "NOTIFY_REMINDER_INTERVAL_SECONDS",
        "NOTIFY_REMINDER_CHECK_INTERVAL_SECONDS",
        "NOTIFY_DIGEST_ENABLED",
        "NOTIFY_DIGEST_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_load_without_any_env():
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.max_parse_attempts == 3
    assert s.cors_origin_list == ["http://localhost:3000"]
    assert s.rules_path.is_file()
    # Generation cap defaults high enough for any real extraction, low enough to
    # bound a runaway loop.
    assert s.ollama_num_predict == 4096
    # The approve/matcher stream gets a longer idle window than the base stream.
    assert s.sse_idle_timeout_seconds == 120.0
    assert s.sse_matcher_idle_timeout_seconds == 300.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "mistral:7b")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "1024")
    monkeypatch.setenv("MAX_PARSE_ATTEMPTS", "5")
    s = Settings(_env_file=None)
    assert s.ollama_model == "mistral:7b"
    assert s.ollama_num_predict == 1024
    assert s.max_parse_attempts == 5


def test_cors_origins_comma_separated(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000, https://screener.example.com")
    s = Settings(_env_file=None)
    assert s.cors_origin_list == [
        "http://localhost:3000",
        "https://screener.example.com",
    ]


def test_anthropic_without_key_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY is required"):
        Settings(_env_file=None)


def test_anthropic_with_key_passes(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    s = Settings(_env_file=None)
    assert s.llm_provider == "anthropic"


def test_missing_rules_file_fails_fast(monkeypatch):
    monkeypatch.setenv("RULES_PATH", "/nonexistent/rules.yaml")
    with pytest.raises(ValueError, match="rules file not found"):
        Settings(_env_file=None)


def test_invalid_provider_rejected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_parse_attempts_bounds(monkeypatch):
    monkeypatch.setenv("MAX_PARSE_ATTEMPTS", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


# --- API hardening settings (#15) ------------------------------------------


def test_hardening_defaults():
    s = Settings(_env_file=None)
    assert s.max_upload_bytes == 25 * 1024 * 1024
    assert s.rate_limit_enabled is True
    assert s.max_concurrent_screenings == 4
    assert s.upload_content_type_set == frozenset(
        {"application/pdf", "text/markdown", "text/plain"}
    )


def test_upload_content_types_override(monkeypatch):
    monkeypatch.setenv("UPLOAD_CONTENT_TYPES", "application/pdf, text/plain ")
    s = Settings(_env_file=None)
    assert s.upload_content_type_set == frozenset({"application/pdf", "text/plain"})


def test_rate_limit_enabled_override(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    assert Settings(_env_file=None).rate_limit_enabled is False


def test_max_upload_bytes_must_be_positive(monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_idle_timeout_below_heartbeat_fails_fast(monkeypatch):
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "30")
    monkeypatch.setenv("SSE_IDLE_TIMEOUT_SECONDS", "10")
    with pytest.raises(ValueError, match="SSE_IDLE_TIMEOUT_SECONDS"):
        Settings(_env_file=None)


# --- notifications (#60) ---------------------------------------------------


def test_notifications_are_off_by_default():
    """Nothing leaves the process until a deployment asks for it."""
    s = Settings(_env_file=None)
    assert s.notify_enabled is False
    assert s.notify_webhook_configured is False
    assert s.notify_email_configured is False


def test_notify_enabled_without_a_channel_fails_fast(monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    with pytest.raises(ValueError, match="NOTIFY_ENABLED=true requires a channel"):
        Settings(_env_file=None)


def test_notify_webhook_alone_is_a_valid_channel(monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/x")
    s = Settings(_env_file=None)
    assert (s.notify_webhook_configured, s.notify_email_configured) == (True, False)


def test_notify_email_recipients_are_comma_separated(monkeypatch):
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "a@example.com, b@example.com ")
    monkeypatch.setenv("NOTIFY_EMAIL_FROM", "trialgate@example.com")
    monkeypatch.setenv("NOTIFY_SMTP_HOST", "smtp.example.com")
    s = Settings(_env_file=None)
    assert s.notify_email_recipient_list == ["a@example.com", "b@example.com"]
    assert s.notify_email_configured is True


def test_notify_smtp_host_without_recipients_is_not_a_channel(monkeypatch):
    """The half-configured case: a relay but nobody to send to fails at startup
    rather than looking wired up and notifying no one."""
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_SMTP_HOST", "smtp.example.com")
    with pytest.raises(ValueError, match="requires a channel"):
        Settings(_env_file=None)


def test_stale_reminder_and_digest_defaults():
    s = Settings(_env_file=None)
    assert s.notify_stale_after_seconds == 86400
    assert s.notify_reminder_interval_seconds == 86400
    assert s.notify_reminder_check_interval_seconds == 300
    assert s.notify_digest_enabled is False
    assert s.notify_digest_interval_seconds == 86400


def test_stale_reminder_and_digest_env_overrides(monkeypatch):
    monkeypatch.setenv("NOTIFY_STALE_AFTER_SECONDS", "3600")
    monkeypatch.setenv("NOTIFY_REMINDER_INTERVAL_SECONDS", "7200")
    monkeypatch.setenv("NOTIFY_REMINDER_CHECK_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("NOTIFY_DIGEST_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_DIGEST_INTERVAL_SECONDS", "43200")
    s = Settings(_env_file=None)
    assert s.notify_stale_after_seconds == 3600
    assert s.notify_reminder_interval_seconds == 7200
    assert s.notify_reminder_check_interval_seconds == 60
    assert s.notify_digest_enabled is True
    assert s.notify_digest_interval_seconds == 43200
