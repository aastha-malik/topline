"""Application settings. Secrets come from the environment only - never from code."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Gmail scopes -----------------------------------------------------------------
# `gmail.readonly` powers ingestion. `gmail.send` is provisioned so the approval-gated
# sender (owned by the agent developer) does not need a second consent screen; nothing in
# this milestone sends mail. `gmail.modify` is deliberately absent - Topline never mutates
# labels, reads drafts, or trashes owner mail, so there is no unavoidable reason to hold
# write access to the mailbox. FORBIDDEN_SCOPES makes that a startup error, not a habit.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
FORBIDDEN_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
        "https://www.googleapis.com/auth/gmail.insert",
        "https://www.googleapis.com/auth/gmail.settings.basic",
        "https://www.googleapis.com/auth/gmail.settings.sharing",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "topline-api"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Database (Supabase Postgres via the session pooler) ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/topline",
        description="SQLAlchemy async DSN. Use the Supabase *session pooler* URI.",
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- Supabase project ---
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None
    supabase_jwt_secret: str | None = None

    # --- Secrets at rest ---
    token_encryption_key: str = Field(
        default="",
        description="Fernet key, urlsafe-base64 32 bytes. Generate with: python -m app.keygen",
    )

    # --- Owner sessions ---
    #: Fernet key that signs the session cookie. Falls back to `token_encryption_key` when
    #: blank, so an existing deployment needs no new secret. Rotating it signs everyone out.
    session_encryption_key: str = Field(default="")
    session_ttl_hours: int = Field(default=720, ge=1)
    #: Send the session cookie only over HTTPS. Keep false for plain-http local dev; set
    #: true in every deployed environment.
    session_cookie_secure: bool = False
    #: Development-only escape hatch: honour an `X-Owner-Id` header when there is no session.
    #: Ignored unless the environment is `local` or `test`.
    allow_owner_header: bool = False

    # --- Google OAuth / Gmail ---
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/api/v1/auth/google/callback"
    frontend_post_auth_redirect: str = "http://localhost:5173/connect"
    #: Provision `gmail.send` for the later approval-gated sender. Set false to drop it.
    enable_gmail_send_scope: bool = True
    oauth_state_ttl_seconds: int = Field(default=600, ge=60)

    # --- Gmail ingestion tuning ---
    backfill_months: int = Field(default=12, ge=1, le=60)
    gmail_list_page_size: int = Field(default=100, ge=1, le=500)
    gmail_max_messages_per_run: int = Field(default=2000, ge=1)
    gmail_max_attachment_bytes: int = Field(default=15 * 1024 * 1024, ge=1024)
    #: Days re-scanned when Gmail rejects the stored history id. Scoped on purpose: an
    #: expired cursor means "we may have missed a few days", not "re-read the mailbox".
    fallback_resync_days: int = Field(default=14, ge=1, le=365)
    #: Minimum relevance score (0-100) for a message to justify a full-content fetch.
    relevance_threshold: int = Field(default=40, ge=0, le=100)
    #: Characters of body retained. Topline keeps evidence, not whole mailboxes.
    max_stored_body_chars: int = Field(default=20_000, ge=500)
    max_stored_pdf_chars: int = Field(default=60_000, ge=500)
    #: Below this many extracted characters per page, a PDF is treated as scanned.
    ocr_trigger_chars_per_page: int = Field(default=40, ge=0)
    enable_ocr_fallback: bool = True

    # --- Ledger / decision engine ---
    default_payment_terms_days: int = Field(default=15, ge=0)
    reminder_grace_days: int = Field(default=0, ge=0)
    reminder_cooldown_days: int = Field(default=3, ge=0)
    default_currency: str = "INR"

    # --- Razorpay (optional confirmation source; test mode for the demo) ---
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None
    #: One global Razorpay integration means one business. Pin its workspace here and every
    #: webhook lands there. Left unset, the receiver resolves the workspace from the event
    #: and drops anything it cannot place - it never guesses an owner.
    razorpay_workspace_id: str | None = None

    # --- Owned by the agent developer; declared here so config lives in one place ---
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    digest_hour_ist: int = Field(default=9, ge=0, le=23)
    enable_scheduler: bool = False

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        # Supabase hands out a sync DSN; upgrade it rather than failing at connect time.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        return v

    @model_validator(mode="after")
    def _reject_overbroad_scopes(self) -> "Settings":
        overlap = FORBIDDEN_SCOPES & set(self.google_oauth_scopes)
        if overlap:
            raise ValueError(f"Refusing over-broad Gmail scopes: {sorted(overlap)}")
        return self

    @property
    def google_oauth_scopes(self) -> list[str]:
        scopes = ["openid", "email", "profile", GMAIL_READONLY_SCOPE]
        if self.enable_gmail_send_scope:
            scopes.append(GMAIL_SEND_SCOPE)
        return scopes

    @property
    def session_key(self) -> str:
        return self.session_encryption_key or self.token_encryption_key

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def razorpay_webhooks_configured(self) -> bool:
        return bool(self.razorpay_webhook_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
