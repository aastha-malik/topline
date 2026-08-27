"""SQLAlchemy models for the Topline receivables ledger.

These mirror `supabase/migrations/*.sql`, which stays the authoritative schema for the
Supabase project. `tests/test_schema_parity.py` diffs the two so they cannot drift.

Types are chosen to be portable: the suite runs the same models on SQLite while production
runs Postgres, so JSON columns declare a JSONB variant rather than importing JSONB directly.

Tenancy: `workspace_id` is the tenant key; `owner_id` (a `users.id`) is carried alongside it
on the ledger tables because the agent layer addresses everything by owner.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import enums
from app.db import Base

#: JSON that becomes JSONB on Postgres and plain JSON on SQLite.
JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")

#: Server-side defaults for the NOT NULL JSON columns. Without these, only the ORM can
#: insert a row - psql, Supabase Studio and edge functions would hit a not-null violation.
#: Postgres coerces the literal to jsonb; SQLite stores it as text.
EMPTY_JSON_LIST = sa.text("'[]'")
EMPTY_JSON_OBJECT = sa.text("'{}'")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=_uuid)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


def _check(column: str, allowed: type[enums.StrEnum], name: str) -> sa.CheckConstraint:
    values = ", ".join(f"'{v.value}'" for v in allowed)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


# --------------------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------------------


class Workspace(Base):
    """One business. Single-owner for the demo, multi-tenant-shaped for later."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Brand profile consumed by the agent layer's `LedgerRepository.get_brand`.
    business_name: Mapped[str | None] = mapped_column(sa.Text)
    sender_name: Mapped[str | None] = mapped_column(sa.Text)
    primary_color: Mapped[str] = mapped_column(sa.Text, server_default="#155EEF")
    logo_url: Mapped[str | None] = mapped_column(sa.Text)
    reply_to: Mapped[str | None] = mapped_column(sa.Text)

    default_currency: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="INR"
    )
    timezone: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="Asia/Kolkata"
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    users: Mapped[list["User"]] = relationship(back_populates="workspace")


class User(Base):
    """A person in a workspace. The `owner` is the one whose Gmail is connected."""

    __tablename__ = "users"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "email", name="uq_users_workspace_email"),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_users_role"),
        sa.Index("ix_users_email", "email"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Lowercased on write by the service layer so lookups are case-insensitive.
    email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str | None] = mapped_column(sa.Text)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="owner")
    #: Links this row to a Supabase Auth user when Supabase Auth is wired up.
    supabase_user_id: Mapped[str | None] = mapped_column(sa.Text, unique=True)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    workspace: Mapped[Workspace] = relationship(back_populates="users")
    gmail_accounts: Mapped[list["GmailAccount"]] = relationship(back_populates="user")


# --------------------------------------------------------------------------------------
# Gmail connection
# --------------------------------------------------------------------------------------


class GmailAccount(Base):
    """A connected Gmail mailbox and its encrypted OAuth tokens.

    Tokens are stored Fernet-encrypted (see :mod:`app.services.crypto`); the plaintext
    never touches a column, a log line or an API response.
    """

    __tablename__ = "gmail_accounts"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "email_address", name="uq_gmail_accounts_workspace_email"
        ),
        _check("status", enums.AccountStatus, "ck_gmail_accounts_status"),
        _check("backfill_status", enums.SyncStatus, "ck_gmail_accounts_backfill_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    email_address: Mapped[str] = mapped_column(sa.Text, nullable=False)
    google_sub: Mapped[str | None] = mapped_column(sa.Text, index=True)

    access_token_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(sa.Text)
    token_expiry: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )

    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.AccountStatus.CONNECTED.value
    )

    #: Gmail's cursor. Persisted only after a sync commits, so a crash re-reads rather
    #: than silently skipping messages.
    last_history_id: Mapped[str | None] = mapped_column(sa.Text)
    backfill_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.SyncStatus.PENDING.value
    )
    backfill_window_start: Mapped[date | None] = mapped_column(sa.Date)
    backfill_window_end: Mapped[date | None] = mapped_column(sa.Date)
    last_backfill_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_incremental_sync_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    connected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    user: Mapped[User] = relationship(back_populates="gmail_accounts")


class OAuthState(Base):
    """Short-lived CSRF state for the Google OAuth handshake (single use)."""

    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = _pk()
    state: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    code_verifier: Mapped[str | None] = mapped_column(sa.Text)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)
    redirect_after: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class SyncRun(Base):
    """One ingestion run. The audit surface for "why does the ledger look like this"."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        _check("mode", enums.SyncMode, "ck_sync_runs_mode"),
        _check("status", enums.SyncStatus, "ck_sync_runs_status"),
        sa.Index("ix_sync_runs_account_started", "gmail_account_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gmail_account_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("gmail_accounts.id", ondelete="CASCADE")
    )
    mode: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.SyncStatus.RUNNING.value
    )

    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    #: Counters that make "we did not copy the mailbox" auditable.
    messages_listed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    messages_metadata_fetched: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    messages_content_fetched: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    messages_ignored: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    attachments_processed: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    invoices_upserted: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    payment_events_upserted: Mapped[int] = mapped_column(sa.Integer, server_default="0")

    start_history_id: Mapped[str | None] = mapped_column(sa.Text)
    end_history_id: Mapped[str | None] = mapped_column(sa.Text)
    #: True when Gmail rejected the stored history id and a scoped resync was run instead.
    history_fallback_used: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, default=dict, server_default=EMPTY_JSON_OBJECT
    )


# --------------------------------------------------------------------------------------
# Source evidence
# --------------------------------------------------------------------------------------


class SourceMessage(Base):
    """A Gmail message Topline looked at.

    Rows are kept for ignored messages too - with `body_text` left null - so the ledger can
    show *why* a message was skipped without retaining its contents.
    """

    __tablename__ = "source_messages"
    __table_args__ = (
        # The idempotency key for every Gmail sync.
        sa.UniqueConstraint(
            "gmail_account_id", "gmail_message_id", name="uq_source_messages_account_msg"
        ),
        _check("processing_state", enums.MessageProcessingState, "ck_source_messages_state"),
        _check("direction", enums.MessageDirection, "ck_source_messages_direction"),
        sa.Index("ix_source_messages_thread", "workspace_id", "gmail_thread_id"),
        sa.Index("ix_source_messages_from", "workspace_id", "from_email"),
        sa.Index("ix_source_messages_internal_date", "workspace_id", "internal_date"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gmail_account_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )

    gmail_message_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    gmail_thread_id: Mapped[str | None] = mapped_column(sa.Text)
    gmail_history_id: Mapped[str | None] = mapped_column(sa.Text)
    internal_date: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    from_email: Mapped[str | None] = mapped_column(sa.Text)
    from_name: Mapped[str | None] = mapped_column(sa.Text)
    from_domain: Mapped[str | None] = mapped_column(sa.Text, index=True)
    to_emails: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    cc_emails: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    subject: Mapped[str | None] = mapped_column(sa.Text)
    snippet: Mapped[str | None] = mapped_column(sa.Text)

    #: Truncated to `max_stored_body_chars`; null while the message is metadata-only.
    body_text: Mapped[str | None] = mapped_column(sa.Text)
    #: sha256 of the normalised body - lets a re-sync detect "unchanged" without a diff.
    body_hash: Mapped[str | None] = mapped_column(sa.Text)

    label_ids: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    has_attachments: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    direction: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.MessageDirection.INBOUND.value
    )

    is_finance_relevant: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    relevance_score: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    #: The scoring rules that fired, so a skip decision is explainable.
    relevance_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    processing_state: Mapped[str] = mapped_column(
        sa.Text,
        nullable=False,
        server_default=enums.MessageProcessingState.METADATA_ONLY.value,
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    attachments: Mapped[list["SourceAttachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class SourceAttachment(Base):
    """A PDF (or other file) hanging off a finance-relevant message."""

    __tablename__ = "source_attachments"
    __table_args__ = (
        sa.UniqueConstraint(
            "source_message_id",
            "gmail_attachment_id",
            name="uq_source_attachments_message_att",
        ),
        _check("extraction_status", enums.ExtractionStatus, "ck_source_attachments_status"),
        _check("extraction_method", enums.ExtractionMethod, "ck_source_attachments_method"),
        sa.Index("ix_source_attachments_sha", "workspace_id", "content_sha256"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("source_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )

    gmail_attachment_id: Mapped[str | None] = mapped_column(sa.Text)
    filename: Mapped[str | None] = mapped_column(sa.Text)
    mime_type: Mapped[str | None] = mapped_column(sa.Text)
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer)
    #: Lets the same invoice PDF forwarded twice resolve to one document.
    content_sha256: Mapped[str | None] = mapped_column(sa.Text)

    extraction_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.ExtractionStatus.PENDING.value
    )
    extraction_method: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.ExtractionMethod.NONE.value
    )
    extracted_text: Mapped[str | None] = mapped_column(sa.Text)
    page_count: Mapped[int | None] = mapped_column(sa.Integer)
    extraction_error: Mapped[str | None] = mapped_column(sa.Text)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    message: Mapped[SourceMessage] = relationship(back_populates="attachments")


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id", "primary_email", name="uq_customers_workspace_email"
        ),
        _check("match_method", enums.CustomerMatchMethod, "ck_customers_match_method"),
        sa.Index("ix_customers_domain", "workspace_id", "domain"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Lowercased. The natural key for a customer within a workspace.
    primary_email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    alt_emails: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    domain: Mapped[str | None] = mapped_column(sa.Text)
    phone: Mapped[str | None] = mapped_column(sa.Text)

    match_confidence: Mapped[float] = mapped_column(
        sa.Float, nullable=False, server_default="1.0"
    )
    match_method: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.CustomerMatchMethod.EMAIL_EXACT.value
    )
    is_archived: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    first_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")


class Invoice(Base):
    """A receivable.

    Money is stored in paise (integer) - never floats. The seven required states live in
    two columns; see :func:`app.enums.effective_state`.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        # Idempotency: one invoice per (workspace, dedupe_key). See ledger.invoice_dedupe_key.
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_invoices_workspace_dedupe"),
        _check("payment_state", enums.PaymentState, "ck_invoices_payment_state"),
        _check("reminder_state", enums.ReminderState, "ck_invoices_reminder_state"),
        _check("evidence_strength", enums.EvidenceStrength, "ck_invoices_evidence_strength"),
        sa.CheckConstraint("amount_paise >= 0", name="ck_invoices_amount_non_negative"),
        sa.CheckConstraint("balance_paise >= 0", name="ck_invoices_balance_non_negative"),
        # The rule that keeps Gmail honest: mail alone can never mean "paid".
        sa.CheckConstraint(
            "payment_state <> 'confirmed_paid' OR evidence_strength = 'provider_confirmed'",
            name="ck_invoices_confirmed_paid_requires_provider",
        ),
        sa.Index("ix_invoices_customer", "workspace_id", "customer_id"),
        sa.Index("ix_invoices_queue", "workspace_id", "payment_state", "reminder_state"),
        sa.Index("ix_invoices_due_date", "workspace_id", "due_date"),
        sa.Index("ix_invoices_razorpay", "workspace_id", "razorpay_invoice_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL")
    )

    invoice_number: Mapped[str | None] = mapped_column(sa.Text)
    #: Uppercased/stripped invoice number used for matching and deduplication.
    normalized_number: Mapped[str | None] = mapped_column(sa.Text, index=True)

    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    amount_paid_paise: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default="0"
    )
    balance_paise: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default="0"
    )
    currency: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="INR")

    issued_date: Mapped[date | None] = mapped_column(sa.Date)
    due_date: Mapped[date | None] = mapped_column(sa.Date)
    #: True when `due_date` was derived from payment terms rather than read off the invoice.
    due_date_inferred: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    payment_state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.PaymentState.LIKELY_UNPAID.value
    )
    reminder_state: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.ReminderState.PAUSED.value
    )
    state_reason: Mapped[str | None] = mapped_column(sa.Text)
    evidence_strength: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.EvidenceStrength.GMAIL_INFERRED.value
    )
    #: Extraction confidence 0..1 from the deterministic parser.
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0.5")
    #: Facts the parser could not find. Non-empty -> `needs_information`.
    missing_fields: Mapped[list[str]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )

    dispute_note: Mapped[str | None] = mapped_column(sa.Text)
    payment_claim_note: Mapped[str | None] = mapped_column(sa.Text)
    paused_until: Mapped[date | None] = mapped_column(sa.Date)
    pause_reason: Mapped[str | None] = mapped_column(sa.Text)
    #: Set by the owner/agent layer; blocks the queue regardless of dates.
    manually_paused: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    #: Primary provenance. Every invoice traces to a Gmail message, and usually a PDF.
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("source_messages.id", ondelete="SET NULL")
    )
    source_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("source_attachments.id", ondelete="SET NULL")
    )

    razorpay_invoice_id: Mapped[str | None] = mapped_column(sa.Text)
    razorpay_payment_id: Mapped[str | None] = mapped_column(sa.Text)

    #: Written by the agent layer's send path; read by the decision engine's cooldown.
    reminder_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    dedupe_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    customer: Mapped[Customer | None] = relationship(back_populates="invoices")
    source_links: Mapped[list["InvoiceSourceLink"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )

    @property
    def is_on_hold(self) -> bool:
        """A deliberate hold, as opposed to "not actionable yet".

        Uses UTC like the rest of the codebase; the decision engine takes an explicit
        `today` so date-sensitive logic is never left to the server's local clock.
        """
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date()
        return bool(self.manually_paused) or bool(
            self.paused_until and self.paused_until > today
        )

    @property
    def effective_state(self) -> enums.EffectiveState:
        return enums.effective_state(
            self.payment_state, self.reminder_state, is_on_hold=self.is_on_hold
        )


class PaymentEvent(Base):
    """Any observation about money moving, from Gmail or from Razorpay.

    `is_confirmation` is the load-bearing field: Gmail rows are always False, so no amount
    of email can flip an invoice to `confirmed_paid`.
    """

    __tablename__ = "payment_events"
    __table_args__ = (
        # Webhook idempotency: Razorpay retries deliver the same provider_event_id.
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "provider_event_id",
            name="uq_payment_events_provider_event",
        ),
        _check("provider", enums.PaymentProvider, "ck_payment_events_provider"),
        _check("event_type", enums.PaymentEventType, "ck_payment_events_type"),
        sa.CheckConstraint(
            "provider <> 'gmail' OR is_confirmation = false",
            name="ck_payment_events_gmail_never_confirms",
        ),
        sa.Index("ix_payment_events_invoice", "workspace_id", "invoice_id"),
        sa.Index("ix_payment_events_observed", "workspace_id", "observed_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Null while an event is received but not yet matched to an invoice.
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("invoices.id", ondelete="SET NULL")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("source_messages.id", ondelete="SET NULL")
    )

    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_event_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)

    amount_paise: Mapped[int | None] = mapped_column(sa.BigInteger)
    currency: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="INR")
    observed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    #: True only for provider settlements. Enforced False for Gmail by CHECK constraint.
    is_confirmation: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    evidence_snippet: Mapped[str | None] = mapped_column(sa.Text)
    reconciled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    reconciliation_method: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, default=dict, server_default=EMPTY_JSON_OBJECT
    )

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class InvoiceSourceLink(Base):
    """The evidence trail: why this invoice believes what it believes.

    Every row points at a Gmail message, a PDF attachment, or a payment event, and carries
    the verbatim snippet plus its location inside the source.
    """

    __tablename__ = "invoice_source_links"
    __table_args__ = (
        # Idempotency: re-running extraction over the same evidence is a no-op.
        sa.UniqueConstraint(
            "invoice_id", "link_type", "evidence_hash", name="uq_invoice_source_links_evidence"
        ),
        _check("link_type", enums.LinkType, "ck_invoice_source_links_type"),
        sa.CheckConstraint(
            "source_message_id IS NOT NULL"
            " OR source_attachment_id IS NOT NULL"
            " OR payment_event_id IS NOT NULL",
            name="ck_invoice_source_links_has_source",
        ),
        sa.Index("ix_invoice_source_links_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )

    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("source_messages.id", ondelete="CASCADE")
    )
    source_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("source_attachments.id", ondelete="CASCADE")
    )
    payment_event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("payment_events.id", ondelete="CASCADE")
    )

    link_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Verbatim text from the source - what the owner is shown as proof.
    evidence_snippet: Mapped[str | None] = mapped_column(sa.Text)
    #: Where the snippet came from, e.g. "pdf:page=1:chars=120-260".
    evidence_locator: Mapped[str | None] = mapped_column(sa.Text)
    #: sha256 of (link_type, snippet, locator) - the dedupe key for this evidence row.
    evidence_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False, server_default="0.5")

    created_at: Mapped[datetime] = _created_at()

    invoice: Mapped[Invoice] = relationship(back_populates="source_links")


class ActivityLog(Base):
    """Append-only audit trail. Shape matches the agent layer's `AuditEvent`.

    `dedupe_key` makes audit writes idempotent: replaying a sync re-emits the same key and
    the insert is dropped rather than duplicating history.
    """

    __tablename__ = "activity_log"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "dedupe_key", name="uq_activity_log_dedupe"),
        _check("actor_type", enums.ActorType, "ck_activity_log_actor_type"),
        sa.Index("ix_activity_log_occurred", "workspace_id", "occurred_at"),
        sa.Index("ix_activity_log_entity", "workspace_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )

    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=enums.ActorType.SYSTEM.value
    )
    actor_id: Mapped[str | None] = mapped_column(sa.Text)
    entity_type: Mapped[str | None] = mapped_column(sa.Text)
    entity_id: Mapped[str | None] = mapped_column(sa.Text)

    summary: Mapped[str | None] = mapped_column(sa.Text)
    decision: Mapped[dict[str, Any]] = mapped_column(
        JSONVariant, nullable=False, default=dict, server_default=EMPTY_JSON_OBJECT
    )
    source_evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONVariant, nullable=False, default=list, server_default=EMPTY_JSON_LIST
    )
    model_name: Mapped[str | None] = mapped_column(sa.Text)
    prompt_version: Mapped[str | None] = mapped_column(sa.Text)

    #: Null means "always insert"; a value makes the write idempotent.
    dedupe_key: Mapped[str | None] = mapped_column(sa.Text)
    occurred_at: Mapped[datetime] = _created_at()
    created_at: Mapped[datetime] = _created_at()
