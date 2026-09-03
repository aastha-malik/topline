"""Pydantic request/response models for the platform API.

Scope note: these cover health, Gmail connection, sync, the receivables ledger and Razorpay
webhooks. Digest/draft/approval schemas live in `app/agent_layer/api.py` and are owned by
the agent developer.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.enums import EffectiveState


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    app: str
    environment: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    google_oauth: str
    razorpay: str
    token_encryption: str
    checks_passed: bool


# --------------------------------------------------------------------------------------
# Auth / Gmail connection
# --------------------------------------------------------------------------------------


class AuthStartResponse(BaseModel):
    authorization_url: str
    state: str
    scopes: list[str]


class SessionUser(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None = None


class SessionWorkspace(BaseModel):
    id: uuid.UUID
    business_name: str


class SessionResponse(BaseModel):
    authenticated: bool = True
    user: SessionUser
    workspace: SessionWorkspace


class GmailAccountResponse(ORMModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    email_address: str
    status: str
    granted_scopes: list[str]
    backfill_status: str
    backfill_window_start: date | None
    backfill_window_end: date | None
    last_history_id: str | None
    last_backfill_at: datetime | None
    last_incremental_sync_at: datetime | None
    connected_at: datetime | None


class ConnectionStatusResponse(BaseModel):
    connected: bool
    google_oauth_configured: bool
    razorpay_configured: bool
    accounts: list[GmailAccountResponse] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------------


class BackfillRequest(BaseModel):
    gmail_account_id: uuid.UUID | None = None
    months: int | None = Field(
        default=None, ge=1, le=60, description="History window; defaults to BACKFILL_MONTHS."
    )


class SyncRequest(BaseModel):
    gmail_account_id: uuid.UUID | None = None


class SyncResultResponse(BaseModel):
    sync_run_id: uuid.UUID
    mode: str
    status: str
    messages_listed: int
    messages_metadata_fetched: int
    messages_content_fetched: int
    messages_ignored: int
    attachments_processed: int
    invoices_created: int
    invoices_updated: int
    payment_events_recorded: int
    history_fallback_used: bool
    start_history_id: str | None
    end_history_id: str | None
    error: str | None
    notes: list[str]


class SyncRunResponse(ORMModel):
    id: uuid.UUID
    mode: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    messages_listed: int
    messages_content_fetched: int
    messages_ignored: int
    attachments_processed: int
    invoices_upserted: int
    history_fallback_used: bool
    error: str | None


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


class CustomerResponse(ORMModel):
    id: uuid.UUID
    name: str
    primary_email: str
    domain: str | None
    phone: str | None
    match_confidence: float
    match_method: str
    first_seen_at: datetime | None
    last_seen_at: datetime | None


class EvidenceResponse(ORMModel):
    id: uuid.UUID
    link_type: str
    evidence_snippet: str | None
    evidence_locator: str | None
    confidence: float
    source_message_id: uuid.UUID | None
    source_attachment_id: uuid.UUID | None
    payment_event_id: uuid.UUID | None
    created_at: datetime


class InvoiceResponse(ORMModel):
    id: uuid.UUID
    customer_id: uuid.UUID | None
    invoice_number: str | None
    amount_paise: int
    amount_paid_paise: int
    balance_paise: int
    currency: str
    issued_date: date | None
    due_date: date | None
    due_date_inferred: bool
    payment_state: str
    reminder_state: str
    #: The seven-valued label derived from the two stored state columns.
    effective_state: EffectiveState
    state_reason: str | None
    evidence_strength: str
    confidence: float
    missing_fields: list[str]
    dispute_note: str | None
    payment_claim_note: str | None
    razorpay_invoice_id: str | None
    razorpay_payment_id: str | None
    reminder_count: int
    last_reminder_at: datetime | None
    source_message_id: uuid.UUID | None
    source_attachment_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class InvoiceWithEvidenceResponse(InvoiceResponse):
    evidence: list[EvidenceResponse] = Field(default_factory=list)


class SourceMessageResponse(ORMModel):
    id: uuid.UUID
    gmail_message_id: str
    gmail_thread_id: str | None
    internal_date: datetime | None
    from_email: str | None
    from_name: str | None
    subject: str | None
    snippet: str | None
    direction: str
    is_finance_relevant: bool
    relevance_score: int
    relevance_reasons: list[dict[str, Any]]
    processing_state: str
    has_attachments: bool


class AttachmentResponse(ORMModel):
    id: uuid.UUID
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    extraction_status: str
    extraction_method: str
    page_count: int | None
    extraction_error: str | None


class CustomerDossierResponse(BaseModel):
    """Everything the agent layer needs to reason about one customer."""

    customer: CustomerResponse
    invoices: list[InvoiceWithEvidenceResponse]
    total_outstanding_paise: int
    open_invoice_count: int
    recent_messages: list[SourceMessageResponse]


class LedgerSummaryResponse(BaseModel):
    total_outstanding_paise: int
    customer_count: int
    invoice_count: int
    by_state: dict[str, int]


class ActivityLogResponse(ORMModel):
    id: uuid.UUID
    event_type: str
    actor_type: str
    actor_id: str | None
    entity_type: str | None
    entity_id: str | None
    summary: str | None
    decision: dict[str, Any]
    source_evidence: list[dict[str, Any]]
    occurred_at: datetime


# --------------------------------------------------------------------------------------
# Razorpay
# --------------------------------------------------------------------------------------


class WebhookAckResponse(BaseModel):
    """Razorpay retries on any non-2xx, so this is returned for handled duplicates too."""

    received: bool
    event_id: str | None = None
    processed: bool
    duplicate: bool = False
    matched_invoice_id: uuid.UUID | None = None
    resulting_state: str | None = None
    detail: str = ""


class ReconcileResponse(BaseModel):
    reconciled: int
    results: list[dict[str, Any]]


class ErrorResponse(BaseModel):
    detail: str
    error_code: str | None = None
