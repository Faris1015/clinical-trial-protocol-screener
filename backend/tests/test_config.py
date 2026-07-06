"""Settings behavior: defaults, env overrides, and fail-fast validation."""
import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Isolate each test from the developer's real environment and the cache."""
    for var in (
        "LLM_PROVIDER", "OLLAMA_MODEL", "OLLAMA_BASE_URL", "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL", "LLM_TEMPERATURE", "CORS_ORIGINS",
        "MAX_PARSE_ATTEMPTS", "RULES_PATH", "PATIENTS_PATH", "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_load_without_any_env():
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.max_parse_attempts == 3
    assert s.cors_origin_list == ["http://localhost:5173"]
    assert s.rules_path.is_file()


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "mistral:7b")
    monkeypatch.setenv("MAX_PARSE_ATTEMPTS", "5")
    s = Settings(_env_file=None)
    assert s.ollama_model == "mistral:7b"
    assert s.max_parse_attempts == 5


def test_cors_origins_comma_separated(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS", "http://localhost:5173, https://screener.example.com"
    )
    s = Settings(_env_file=None)
    assert s.cors_origin_list == [
        "http://localhost:5173", "https://screener.example.com",
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
