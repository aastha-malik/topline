from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any

from app.enums import PaymentState, ReminderState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DigestStatus(StrEnum):
    BUILDING = "building"
    SENT = "sent"
    FAILED = "failed"


class DigestItemStatus(StrEnum):
    ACTIONABLE = "actionable"
    DRAFTED = "drafted"
    SKIPPED = "skipped"
    PAUSED = "paused"


class DraftStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SENDING = "sending"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"
    PAUSED = "paused"


class OwnerActionKind(StrEnum):
    DRAFT = "draft"
    SKIP = "skip"


class CustomerReplyIntent(StrEnum):
    ALREADY_PAID = "already_paid"
    DISPUTE = "dispute"
    PROMISE_TO_PAY = "promise_to_pay"
    QUESTION = "question"
    UNCLEAR = "unclear"


class ReviewTaskKind(StrEnum):
    OWNER_COMMAND_CLARIFICATION = "owner_command_clarification"
    CUSTOMER_REPLY = "customer_reply"
    SEND_FAILURE = "send_failure"


@dataclass(slots=True, frozen=True)
class OwnerProfile:
    id: str
    email: str
    gmail_address: str
    name: str = ""


@dataclass(slots=True, frozen=True)
class BrandProfile:
    business_name: str
    sender_name: str
    primary_color: str = "#155EEF"
    logo_url: str | None = None
    reply_to: str | None = None


@dataclass(slots=True, frozen=True)
class CustomerRecord:
    id: str
    owner_id: str
    name: str
    email: str
    phone: str | None = None
    match_confidence: float | None = None


@dataclass(slots=True, frozen=True)
class InvoiceRecord:
    id: str
    owner_id: str
    customer_id: str
    invoice_number: str
    amount_paise: int
    balance_paise: int
    currency: str
    issued_date: date | None
    due_date: date | None
    payment_state: PaymentState
    reminder_state: ReminderState
    source_message_id: str | None = None
    source_attachment_id: str | None = None
    dispute_note: str | None = None
    payment_claim_note: str | None = None

    def is_actionable(self, run_date: date) -> bool:
        return (
            self.payment_state == PaymentState.LIKELY_UNPAID
            and self.reminder_state == ReminderState.READY_FOR_REMINDER
            and self.balance_paise > 0
            and self.due_date is not None
            and self.due_date <= run_date
        )


@dataclass(slots=True, frozen=True)
class EvidenceRecord:
    id: str
    kind: str
    excerpt: str
    source_date: datetime
    source_message_id: str | None = None
    source_attachment_id: str | None = None
    gmail_thread_id: str | None = None


@dataclass(slots=True, frozen=True)
class ReminderOutcome:
    id: str
    draft_id: str
    sent_at: datetime | None
    tone: str
    outcome: str | None
    status: str


@dataclass(slots=True, frozen=True)
class CustomerDossier:
    customer: CustomerRecord
    invoices: tuple[InvoiceRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    reminders: tuple[ReminderOutcome, ...]
    recommendation_reason: str
    source_references: tuple[dict[str, str], ...]

    def prompt_payload(self) -> dict[str, Any]:
        def value(item: Any) -> Any:
            if isinstance(item, (date, datetime)):
                return item.isoformat()
            if isinstance(item, StrEnum):
                return str(item)
            if isinstance(item, tuple):
                return [value(entry) for entry in item]
            if isinstance(item, dict):
                return {key: value(entry) for key, entry in item.items()}
            if hasattr(item, "__dataclass_fields__"):
                return {key: value(entry) for key, entry in asdict(item).items()}
            return item

        return value(self)


@dataclass(slots=True)
class DigestRecord:
    id: str
    owner_id: str
    run_date: date
    status: DigestStatus = DigestStatus.BUILDING
    gmail_thread_id: str | None = None
    owner_message_id: str | None = None
    total_outstanding_paise: int = 0
    customer_count: int = 0
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class DigestItemRecord:
    id: str
    digest_id: str
    item_number: int
    customer_id: str
    customer_name: str
    invoice_ids: tuple[str, ...]
    amount_paise: int
    oldest_due_date: date
    recommendation_reason: str
    source_references: tuple[dict[str, str], ...]
    status: DigestItemStatus = DigestItemStatus.ACTIONABLE


@dataclass(slots=True)
class DraftRecord:
    id: str
    owner_id: str
    digest_id: str
    digest_item_id: str
    draft_number: int
    customer_id: str
    customer_email: str
    invoice_ids: tuple[str, ...]
    subject: str
    text_body: str
    rationale: str
    tone: str
    status: DraftStatus
    source_snapshot: dict[str, Any]
    agent_decision: dict[str, Any]
    prompt_version: str
    model_name: str
    rendered_html: str
    customer_thread_id: str | None = None
    approved_by: str | None = None
    approval_source: str | None = None
    approved_at: datetime | None = None
    approved_content_hash: str | None = None
    sent_at: datetime | None = None
    send_result: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def content_hash(self) -> str:
        payload = json.dumps(
            {
                "customer_email": self.customer_email.strip().lower(),
                "subject": self.subject,
                "text_body": self.text_body,
                "invoice_ids": list(self.invoice_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ReviewTaskRecord:
    id: str
    owner_id: str
    kind: ReviewTaskKind
    reason: str
    payload: dict[str, Any]
    digest_id: str | None = None
    customer_id: str | None = None
    invoice_ids: tuple[str, ...] = ()
    status: str = "open"
    created_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True, frozen=True)
class OwnerAction:
    action: OwnerActionKind
    confidence: float
    reason: str
    item_number: int | None = None
    customer_id: str | None = None
    tone: str | None = None
    note: str | None = None


@dataclass(slots=True, frozen=True)
class OwnerCommandDecision:
    actions: tuple[OwnerAction, ...]
    confidence: float
    explanation: str
    ambiguous: bool = False
    prompt_version: str = "owner-command-v1"
    model_name: str = ""


@dataclass(slots=True, frozen=True)
class ReminderDraftDecision:
    subject: str
    text_body: str
    rationale: str
    tone: str
    confidence: float
    cited_source_ids: tuple[str, ...]
    prompt_version: str = "reminder-draft-v1"
    model_name: str = ""


@dataclass(slots=True, frozen=True)
class CustomerReplyDecision:
    intent: CustomerReplyIntent
    confidence: float
    explanation: str
    requires_review: bool
    cited_invoice_ids: tuple[str, ...]
    prompt_version: str = "customer-reply-v1"
    model_name: str = ""


@dataclass(slots=True, frozen=True)
class MailReceipt:
    message_id: str
    thread_id: str
    provider_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DailyCycleResult:
    digest: DigestRecord
    items: tuple[DigestItemRecord, ...]


@dataclass(slots=True, frozen=True)
class OwnerReplyResult:
    kind: str
    drafts: tuple[DraftRecord, ...] = ()
    sent_draft_ids: tuple[str, ...] = ()
    review_task: ReviewTaskRecord | None = None


@dataclass(slots=True, frozen=True)
class CustomerReplyResult:
    decision: CustomerReplyDecision
    review_task: ReviewTaskRecord
    paused_invoice_ids: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class AuditEvent:
    owner_id: str
    event_type: str
    actor_type: str
    actor_id: str | None
    entity_type: str
    entity_id: str
    decision: dict[str, Any]
    source_evidence: tuple[dict[str, Any], ...] = ()
    model_name: str | None = None
    prompt_version: str | None = None
    occurred_at: datetime = field(default_factory=utcnow)
