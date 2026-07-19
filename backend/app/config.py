"""Centralized runtime configuration.

Every knob the app has lives here, loaded from environment variables with an
optional `.env` file for local development (see `.env.example` for the
authoritative list). Nothing elsewhere in the codebase reads `os.environ`
directly. Validation runs at first access, so a misconfigured deployment
fails at startup with a clear message instead of mid-screening.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    # Anchor the .env lookup to backend/ so it works regardless of the
    # directory the server is launched from.
    model_config = SettingsConfigDict(
        env_file=APP_DIR.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM backend ---
    llm_provider: Literal["ollama", "anthropic"] = "ollama"
    ollama_model: str = "llama3.1:8b"
    ollama_base_url: str = "http://localhost:11434"
    anthropic_model: str = "claude-sonnet-5"
    anthropic_api_key: str | None = None
    llm_temperature: float = Field(0.0, ge=0.0, le=1.0)

    # --- API ---
    # Comma-separated list, e.g. "http://localhost:5173,https://screener.example.com"
    cors_origins: str = "http://localhost:5173"

    # --- API hardening (#15) ---
    # Reject uploads larger than this before buffering the whole body. 25 MiB
    # comfortably fits a 200-page protocol PDF while stopping 500 MB spam.
    max_upload_bytes: int = Field(25 * 1024 * 1024, ge=1)
    # Comma-separated content-type allowlist for uploads. A generic type
    # (application/octet-stream or empty) falls back to a filename-extension
    # check so browser uploads of .md/.txt files aren't rejected spuriously.
    upload_content_types: str = "application/pdf,text/markdown,text/plain"
    # slowapi limits (see https://limits.readthedocs.io for the "N/unit" syntax).
    # Strict on the LLM-triggering create endpoint, generous on cheap reads.
    rate_limit_create: str = "10/minute"
    rate_limit_read: str = "120/minute"
    # Toggle the limiter off entirely (tests set RATE_LIMIT_ENABLED=false so the
    # suite isn't throttled by a process-wide in-memory counter).
    rate_limit_enabled: bool = True
    # Concurrent in-flight screenings (graph runs) per instance. Once saturated,
    # new stream/approve requests get 429 + Retry-After instead of queueing.
    max_concurrent_screenings: int = Field(4, ge=1)
    # Retry-After (seconds) advertised when the concurrency gate is saturated.
    concurrency_retry_after_seconds: int = Field(5, ge=1)
    # SSE hygiene: emit a heartbeat comment every N seconds of silence, and
    # reap a stream that produces nothing for the idle window (dead client or a
    # wedged graph). idle must be a multiple-ish of heartbeat to be meaningful.
    sse_heartbeat_seconds: float = Field(15.0, gt=0)
    sse_idle_timeout_seconds: float = Field(120.0, gt=0)

    # --- Pipeline ---
    max_parse_attempts: int = Field(3, ge=1, le=10)
    rules_path: Path = APP_DIR / "rules" / "compliance_rules.yaml"
    patients_path: Path = APP_DIR / "data" / "patients.json"

    # --- Persistence ---
    # Where LangGraph execution state and screening metadata live. "memory" is
    # process-local and lost on restart (tests only); "sqlite" is the durable
    # single-node default; "postgres" is the multi-replica production target.
    checkpoint_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    # sqlite file for both the checkpointer and the screening store (one DB).
    sqlite_path: Path = APP_DIR.parent / "screenings.sqlite"
    # Required when CHECKPOINT_BACKEND=postgres, e.g.
    # "postgresql://user:pass@host:5432/screener".
    postgres_dsn: str | None = None

    # --- Build metadata ---
    # Short commit SHA, injected at image build (Docker ARG -> GIT_SHA env) so
    # /health and /ready can report exactly which build is running.
    git_sha: str | None = None

    # --- Observability ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # "console" = human-readable, colorized (dev); "json" = one object per line (prod).
    log_format: Literal["console", "json"] = "console"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def upload_content_type_set(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.upload_content_types.split(",") if t.strip())

    @model_validator(mode="after")
    def _require_anthropic_key(self) -> "Settings":
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic. "
                "Set it in the environment or in backend/.env."
            )
        return self

    @model_validator(mode="after")
    def _require_postgres_dsn(self) -> "Settings":
        if self.checkpoint_backend == "postgres" and not self.postgres_dsn:
            raise ValueError(
                "POSTGRES_DSN is required when CHECKPOINT_BACKEND=postgres. "
                "Set it in the environment or in backend/.env."
            )
        return self

    @model_validator(mode="after")
    def _sse_idle_covers_a_heartbeat(self) -> "Settings":
        # A stream must get at least one heartbeat before it can be reaped;
        # otherwise the idle timer trips on the first tick and the heartbeat
        # (dead-client detector) never fires.
        if self.sse_idle_timeout_seconds < self.sse_heartbeat_seconds:
            raise ValueError(
                "SSE_IDLE_TIMEOUT_SECONDS must be >= SSE_HEARTBEAT_SECONDS "
                f"({self.sse_idle_timeout_seconds} < {self.sse_heartbeat_seconds})."
            )
        return self

    @model_validator(mode="after")
    def _require_rules_file(self) -> "Settings":
        if not self.rules_path.is_file():
            raise ValueError(
                f"Compliance rules file not found: {self.rules_path}. "
                "Set RULES_PATH or restore backend/app/rules/compliance_rules.yaml."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
