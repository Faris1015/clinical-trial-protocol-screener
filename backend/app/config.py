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
    # "stub" is a zero-inference, deterministic in-process model used for load
    # testing (#10) and offline demos — it isolates app/pipeline performance
    # from real model latency. Never enable it in production: it returns canned
    # extractions, not real analysis.
    llm_provider: Literal["ollama", "anthropic", "stub"] = "ollama"
    ollama_model: str = "qwen2.5:7b"
    ollama_base_url: str = "http://localhost:11434"
    # Hard ceiling on tokens generated per LLM call. Bounds a degenerate
    # repetition loop (observed: a small model emitting 50k+ tokens on one
    # malformed structured-output attempt, hanging the whole screening) while
    # sitting well above any real parser/critic/matcher output. Ollama-only —
    # hosted providers cap output themselves.
    ollama_num_predict: int = Field(4096, ge=1)
    anthropic_model: str = "claude-sonnet-5"
    anthropic_api_key: str | None = None
    llm_temperature: float = Field(0.0, ge=0.0, le=1.0)
    # Artificial per-call latency (seconds) for LLM_PROVIDER=stub. 0 measures the
    # app's own overhead; a non-zero value models a slow backend so a load test
    # can see how inference latency interacts with the threadpool + concurrency
    # gate without needing a real GPU.
    stub_latency_seconds: float = Field(0.0, ge=0.0)
    # Per-model price table for the cost accounting (#101), as
    # "model=input/output" entries in USD per million tokens, comma- or
    # newline-separated. The separator is "=" rather than ":" because Ollama model
    # ids contain a colon ("qwen2.5:7b") and would otherwise be unaddressable.
    # A model with no entry reports its tokens at zero cost — which is the right
    # reading for a local model, and the reason the defaults name only the hosted
    # ones. An id is matched exactly first, then by the longest configured prefix,
    # so a dated snapshot inherits its family's price without needing a row.
    # Prices move; override this rather than editing the code when they do.
    llm_prices: str = (
        "claude-opus-5=5/25,claude-opus-4-8=5/25,claude-opus-4-7=5/25,"
        "claude-sonnet-5=3/15,claude-sonnet-4-6=3/15,claude-haiku-4-5=1/5,"
        "claude-fable-5=10/50"
    )

    # --- API ---
    # Comma-separated list, e.g. "http://localhost:3000,https://screener.example.com"
    # The default is the Next dev server's port. It is a fallback for hitting this
    # API directly from a browser page: `next dev` proxies /api itself, so the
    # normal dev flow is same-origin and never reaches CORS.
    cors_origins: str = "http://localhost:3000"
    # Single-service demo mode: point this at the frontend's built bundle (a
    # directory containing index.html — `frontend/out`, from `next build` with
    # `output: "export"`) and the API also serves the app from the same origin, so
    # one container hosts the whole demo with no CORS or second host. Unset (the
    # default) in the split production topology, where nginx serves the bundle and
    # reverse-proxies /api. See deploy/demo/Dockerfile and
    # docs/free-demo-deploy.md.
    frontend_dist: Path | None = None

    # --- API hardening (#15) ---
    # Reject uploads larger than this before buffering the whole body. 25 MiB
    # comfortably fits a 200-page protocol PDF while stopping 500 MB spam.
    max_upload_bytes: int = Field(25 * 1024 * 1024, ge=1)
    # Upper bound on the protocol text handed to the pipeline. The byte cap
    # above bounds the *transport*, but a 25 MiB markdown/txt upload — or a
    # text-dense PDF — decodes to millions of characters that are then fed to
    # the Parser *and* the Critic on every parse attempt. Truncate to a sane
    # budget so a crafted upload can't drive unbounded token spend / context
    # overflow. 200k chars (~50k tokens) dwarfs any real eligibility section.
    max_protocol_text_chars: int = Field(200_000, ge=1000)
    # Reject a PDF whose page count is implausible for a protocol *before*
    # rendering every page into memory (a decompression/"PDF bomb" defense).
    # Protocols run 80-200 pages; 2000 is generous headroom.
    max_pdf_pages: int = Field(2000, ge=1)
    # Comma-separated content-type allowlist for uploads. A generic type
    # (application/octet-stream or empty) falls back to a filename-extension
    # check so browser uploads of .md/.txt files aren't rejected spuriously.
    upload_content_types: str = "application/pdf,text/markdown,text/plain"
    # slowapi limits (see https://limits.readthedocs.io for the "N/unit" syntax).
    # Strict on the LLM-triggering create endpoint, generous on cheap reads.
    rate_limit_create: str = "10/minute"
    rate_limit_read: str = "120/minute"
    # Login is the one endpoint an attacker can guess against, so it gets its own
    # (tight) IP-keyed limit rather than inheriting the read budget.
    rate_limit_login: str = "10/minute"
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
    # The matcher streams progress between LLM calls (which reset the idle clock),
    # but a single cohort-mapping call on a slow local model can itself run for a
    # while, so the approve stream gets a longer idle window than the pre-approval
    # phase before the reaper trips.
    sse_matcher_idle_timeout_seconds: float = Field(300.0, gt=0)

    # --- Auth (#50) ---
    # Off only for single-user local runs and the load-test harness: with auth
    # disabled every request is treated as a synthetic admin principal
    # (app/auth.py ANONYMOUS), which is unmistakable in an audit trail.
    auth_enabled: bool = True
    # HMAC key for session cookies. Unset means a random per-process key: safe by
    # default (no secret in the repo) but sessions die on restart and replicas
    # reject each other's cookies, so production must set it.
    auth_secret: str | None = None
    # Accounts, as comma- or newline-separated "email:role:password_hash" entries.
    # Mint a hash with `python -m app.auth hash`. When set, this replaces the demo
    # accounts entirely — a configured deployment never carries them too.
    auth_users: str = ""
    # Seed the built-in demo accounts (published passwords, see README) when
    # AUTH_USERS is empty. On by default so `docker compose up` and the zero-config
    # demo image land on a working login screen; turn it off in production.
    auth_demo_users: bool = True
    # Session lifetime. 8 hours covers a reviewer's shift without leaving a
    # forgotten browser authenticated overnight.
    auth_session_ttl_seconds: int = Field(8 * 3600, ge=60)
    auth_cookie_name: str = "trialgate_session"
    # Add the Secure attribute so the cookie is only ever sent over HTTPS. Off by
    # default because local dev and the compose stack are plain HTTP (a Secure
    # cookie would simply never be stored there); set true behind TLS.
    auth_cookie_secure: bool = False

    # --- Notify on gate / escalation (#60) ---
    # Off by default: a deployment opts in, and until it does nothing leaves the
    # process. When on, a run that parks at the approval gate or escalates pushes
    # one notification per configured channel (see app/services/notifications.py).
    notify_enabled: bool = False
    # Slack-compatible incoming webhook (or any endpoint that accepts a JSON POST).
    notify_webhook_url: str | None = None
    # Email recipients, comma-separated. Requires the SMTP settings below.
    notify_email_to: str = ""
    notify_email_from: str | None = None
    notify_smtp_host: str | None = None
    notify_smtp_port: int = Field(587, ge=1, le=65535)
    notify_smtp_username: str | None = None
    notify_smtp_password: str | None = None
    # STARTTLS on the default submission port. Set false only for a plaintext
    # relay on a trusted network (a local MTA, or MailHog in dev).
    notify_smtp_starttls: bool = True
    # Per-channel ceiling. A notification is dispatched on the run's own task, so
    # this is also the worst case it can add to a run's completion — small enough
    # that an unreachable webhook costs a beat, not a stalled reviewer.
    notify_timeout_seconds: float = Field(5.0, gt=0)
    # Public base URL of the frontend, e.g. "https://trialgate.example.com". Used
    # to turn a thread_id into a link a reviewer can click. Unset omits the link
    # rather than guessing a host.
    notify_base_url: str | None = None

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
    # Expose Prometheus metrics at GET /metrics (#7). Custom domain metrics are
    # always recorded (negligible cost); this only gates the scrape endpoint so a
    # deployment can keep it off the public surface if it scrapes out-of-band.
    metrics_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # "console" = human-readable, colorized (dev); "json" = one object per line (prod).
    log_format: Literal["console", "json"] = "console"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def upload_content_type_set(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.upload_content_types.split(",") if t.strip())

    @property
    def notify_email_recipient_list(self) -> list[str]:
        return [addr.strip() for addr in self.notify_email_to.split(",") if addr.strip()]

    @property
    def notify_email_configured(self) -> bool:
        """Whether the email channel has everything it needs to send.

        All three are required: a relay to hand the message to, at least one
        recipient, and an envelope sender (SMTP servers reject a message without
        one, so defaulting it would only move the failure to the relay).
        """
        return bool(
            self.notify_smtp_host and self.notify_email_recipient_list and self.notify_email_from
        )

    @property
    def notify_webhook_configured(self) -> bool:
        return bool(self.notify_webhook_url)

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
    def _require_auth_accounts(self) -> "Settings":
        # Auth on with no accounts is a locked-out app — nobody can reach the
        # approval gate. Catch it at startup rather than at the login screen.
        if self.auth_enabled and not self.auth_users.strip() and not self.auth_demo_users:
            raise ValueError(
                "AUTH_ENABLED=true requires accounts: set AUTH_USERS "
                "('email:role:hash' entries — mint a hash with `python -m app.auth hash`), "
                "or AUTH_DEMO_USERS=true for the demo accounts, "
                "or AUTH_ENABLED=false to run without authentication."
            )
        return self

    @model_validator(mode="after")
    def _require_a_notification_channel(self) -> "Settings":
        # NOTIFY_ENABLED with nothing to notify *through* is the failure mode this
        # feature has: it looks configured, reviewers assume they'll be paged, and
        # every run silently no-ops. Catch it at startup, like the auth-accounts
        # check — the half-configured email case (a host but no recipients) is the
        # one most likely to slip through, so name what is missing.
        if self.notify_enabled and not (
            self.notify_webhook_configured or self.notify_email_configured
        ):
            raise ValueError(
                "NOTIFY_ENABLED=true requires a channel: set NOTIFY_WEBHOOK_URL, or all of "
                "NOTIFY_SMTP_HOST + NOTIFY_EMAIL_TO + NOTIFY_EMAIL_FROM for email. "
                "Set NOTIFY_ENABLED=false to disable notifications."
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
