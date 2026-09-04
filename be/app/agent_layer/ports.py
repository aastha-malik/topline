from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from .domain import (
    AuditEvent,
    BrandProfile,
    CustomerRecord,
    CustomerReplyDecision,
    DigestItemRecord,
    DigestRecord,
    DraftRecord,
    EvidenceRecord,
    InvoiceRecord,
    MailReceipt,
    OwnerCommandDecision,
    OwnerProfile,
    ReminderDraftDecision,
    ReminderOutcome,
    ReviewTaskRecord,
)


class LedgerRepository(Protocol):
    """Contract implemented by the developer-owned database layer.

    The agent layer deliberately does not own Gmail ingestion, invoice parsing,
    payment reconciliation, or the canonical customer/invoice tables.
    """

    async def get_owner(self, owner_id: str) -> OwnerProfile: ...

    async def list_connected_owner_ids(self) -> Sequence[str]: ...

    async def get_brand(self, owner_id: str) -> BrandProfile: ...

    async def list_actionable_invoices(
        self, owner_id: str, run_date: date
    ) -> Sequence[InvoiceRecord]: ...

    async def get_customer(self, owner_id: str, customer_id: str) -> CustomerRecord: ...

    async def list_customer_invoices(
        self, owner_id: str, customer_id: str
    ) -> Sequence[InvoiceRecord]: ...

    async def list_finance_evidence(
        self, owner_id: str, customer_id: str, invoice_ids: Sequence[str]
    ) -> Sequence[EvidenceRecord]: ...

    async def list_prior_reminders(
        self, owner_id: str, customer_id: str, invoice_ids: Sequence[str]
    ) -> Sequence[ReminderOutcome]: ...

    async def get_or_create_digest(self, owner_id: str, run_date: date) -> DigestRecord: ...

    async def save_digest(self, digest: DigestRecord) -> None: ...

    async def add_digest_item(self, item: DigestItemRecord) -> None: ...

    async def get_digest(self, owner_id: str, digest_id: str) -> DigestRecord: ...

    async def get_digest_by_thread(
        self, owner_id: str, gmail_thread_id: str
    ) -> DigestRecord | None: ...

    async def list_digest_items(
        self, owner_id: str, digest_id: str
    ) -> Sequence[DigestItemRecord]: ...

    async def get_digest_item(
        self, owner_id: str, digest_item_id: str
    ) -> DigestItemRecord: ...

    async def save_digest_item(self, item: DigestItemRecord) -> None: ...

    async def create_draft(self, draft: DraftRecord) -> None: ...

    async def get_draft(self, owner_id: str, draft_id: str) -> DraftRecord: ...

    async def list_drafts(
        self, owner_id: str, digest_id: str | None = None
    ) -> Sequence[DraftRecord]: ...

    async def save_draft(self, draft: DraftRecord) -> None: ...

    async def claim_approved_draft_for_send(
        self, owner_id: str, draft_id: str, approved_content_hash: str
    ) -> bool: ...

    async def find_draft_by_customer_thread(
        self, owner_id: str, gmail_thread_id: str
    ) -> DraftRecord | None: ...

    async def create_review_task(self, task: ReviewTaskRecord) -> None: ...

    async def pause_invoices(
        self,
        owner_id: str,
        invoice_ids: Sequence[str],
        *,
        payment_state: str | None,
        reason: str,
    ) -> None: ...

    async def mark_reminder_sent(
        self, owner_id: str, invoice_ids: Sequence[str], sent_at: datetime
    ) -> None: ...

    async def append_audit(self, event: AuditEvent) -> None: ...


class MailGateway(Protocol):
    """Adapter over the developer-owned Gmail service."""

    async def send_owner_digest(
        self,
        *,
        owner: OwnerProfile,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> MailReceipt: ...

    async def reply_to_owner_thread(
        self,
        *,
        owner: OwnerProfile,
        thread_id: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> MailReceipt: ...

    async def send_customer_email(
        self,
        *,
        owner: OwnerProfile,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        reply_to: str | None,
        thread_id: str | None,
    ) -> MailReceipt: ...

    async def notify_owner(
        self,
        *,
        owner: OwnerProfile,
        subject: str,
        text_body: str,
        html_body: str,
        thread_id: str | None,
    ) -> MailReceipt: ...


class AgentGateway(Protocol):
    """Bounded structured-output model calls. It has no send capability."""

    @property
    def model_name(self) -> str: ...

    async def parse_owner_command(
        self,
        *,
        owner_text: str,
        digest_items: Sequence[DigestItemRecord],
    ) -> OwnerCommandDecision: ...

    async def draft_reminder(
        self,
        *,
        dossier_payload: dict,
        tone: str,
        owner_note: str | None,
    ) -> ReminderDraftDecision: ...

    async def classify_customer_reply(
        self,
        *,
        new_reply_text: str,
        customer: CustomerRecord,
        invoices: Sequence[InvoiceRecord],
    ) -> CustomerReplyDecision: ...
