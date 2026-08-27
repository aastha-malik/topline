"""The receivables ledger: customer matching, invoice/evidence upserts, state refresh.

Every write here is idempotent. Re-running a backfill over the same mailbox must converge
on the same rows rather than growing the ledger, so each entity has an explicit natural key:

===================== ==========================================================
 entity                idempotency key
===================== ==========================================================
 customer              (workspace_id, primary_email)
 invoice               (workspace_id, dedupe_key)  - see `invoice_dedupe_key`
 evidence link         (invoice_id, link_type, evidence_hash)
 payment event         (workspace_id, provider, provider_event_id)
 audit row             (workspace_id, dedupe_key)
===================== ==========================================================
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    CustomerMatchMethod,
    EvidenceStrength,
    PaymentEventType,
    PaymentProvider,
    PaymentState,
    ReminderState,
)
from app.logging_config import get_logger
from app.models import Customer, Invoice, InvoiceSourceLink, PaymentEvent
from app.services.db_utils import insert_ignore
from app.services.decisions import (
    EvidenceSnapshot,
    PaymentEventView,
    StateDecision,
    decide_state,
    fold_payment_events,
)
from app.services.extraction import Evidence, InvoiceFacts
from app.services.relevance import email_domain
from app.utils import ensure_utc

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------


def invoice_dedupe_key(
    *,
    customer_email: str | None,
    invoice_number: str | None,
    amount_paise: int | None = None,
    issued_date: date | None = None,
) -> str:
    """Stable identity for an invoice.

    An invoice number scoped to a customer is the real key. Without a number - common for
    small businesses that invoice in the email body - fall back to the customer, amount and
    issue date, which is stable enough that the same invoice seen twice collapses to one row
    while two genuinely different invoices stay separate.
    """
    email = (customer_email or "").strip().lower()
    if invoice_number:
        raw = f"num|{email}|{invoice_number.strip().upper()}"
    else:
        raw = f"amt|{email}|{amount_paise or 0}|{issued_date.isoformat() if issued_date else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evidence_hash(link_type: str, snippet: str | None, locator: str | None) -> str:
    raw = f"{link_type}|{(snippet or '').strip()}|{locator or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------------------


async def upsert_customer(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    name: str,
    email: str,
    phone: str | None = None,
    seen_at: datetime | None = None,
    match_method: str = CustomerMatchMethod.EMAIL_EXACT,
    match_confidence: float = 1.0,
) -> Customer:
    """Find or create a customer by email, keeping first/last-seen fresh."""
    normalized = email.strip().lower()
    seen_at = seen_at or datetime.now(timezone.utc)

    existing = await session.scalar(
        select(Customer).where(
            Customer.workspace_id == workspace_id, Customer.primary_email == normalized
        )
    )
    if existing is not None:
        last_seen = ensure_utc(existing.last_seen_at)
        first_seen = ensure_utc(existing.first_seen_at)
        if seen_at and (last_seen is None or seen_at > last_seen):
            existing.last_seen_at = seen_at
        if seen_at and (first_seen is None or seen_at < first_seen):
            existing.first_seen_at = seen_at
        # Prefer a real display name over a domain-derived placeholder.
        if name and existing.name != name and _is_placeholder_name(existing.name, normalized):
            existing.name = name
        if phone and not existing.phone:
            existing.phone = phone
        await session.flush()
        return existing

    customer = Customer(
        workspace_id=workspace_id,
        owner_id=owner_id,
        name=name or normalized,
        primary_email=normalized,
        domain=email_domain(normalized) or None,
        phone=phone,
        match_method=str(match_method),
        match_confidence=match_confidence,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
    )
    session.add(customer)
    await session.flush()
    return customer


def _is_placeholder_name(current: str, email: str) -> bool:
    domain_stub = (email_domain(email).split(".")[0] or "").lower()
    return current.strip().lower() in {email, domain_stub, ""}


async def find_customer_by_email(
    session: AsyncSession, workspace_id: uuid.UUID, email: str | None
) -> Customer | None:
    if not email:
        return None
    return await session.scalar(
        select(Customer).where(
            Customer.workspace_id == workspace_id,
            Customer.primary_email == email.strip().lower(),
        )
    )


# --------------------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class InvoiceUpsertResult:
    invoice: Invoice
    created: bool
    decision: StateDecision


async def upsert_invoice_from_facts(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID,
    customer: Customer | None,
    facts: InvoiceFacts,
    source_message_id: uuid.UUID | None = None,
    source_attachment_id: uuid.UUID | None = None,
    today: date | None = None,
    grace_days: int = 0,
    reminder_cooldown_days: int = 3,
) -> InvoiceUpsertResult:
    """Create or refresh an invoice from extracted facts, then re-run the state machine.

    Existing invoices are only *upgraded*: a later, thinner extraction never blanks a fact
    the ledger already has, and never rewinds a state the decision engine owns.
    """
    key = invoice_dedupe_key(
        customer_email=facts.customer_email or (customer.primary_email if customer else None),
        invoice_number=facts.invoice_number,
        amount_paise=facts.amount_paise,
        issued_date=facts.issued_date,
    )

    invoice = await session.scalar(
        select(Invoice).where(Invoice.workspace_id == workspace_id, Invoice.dedupe_key == key)
    )
    created = invoice is None

    if invoice is None:
        invoice = Invoice(
            workspace_id=workspace_id,
            owner_id=owner_id,
            customer_id=customer.id if customer else None,
            dedupe_key=key,
            currency=facts.currency or "INR",
            amount_paise=facts.amount_paise or 0,
            balance_paise=facts.amount_paise or 0,
            payment_state=str(PaymentState.LIKELY_UNPAID),
            reminder_state=str(ReminderState.PAUSED),
            evidence_strength=str(EvidenceStrength.GMAIL_INFERRED),
        )
        session.add(invoice)

    # Fill gaps; never overwrite a known fact with None.
    if facts.invoice_number:
        invoice.invoice_number = facts.invoice_number
        invoice.normalized_number = facts.invoice_number.strip().upper()
    if facts.amount_paise:
        invoice.amount_paise = facts.amount_paise
    if facts.currency:
        invoice.currency = facts.currency
    if facts.issued_date:
        invoice.issued_date = facts.issued_date
    if facts.due_date and (invoice.due_date is None or not facts.due_date_inferred):
        invoice.due_date = facts.due_date
        invoice.due_date_inferred = facts.due_date_inferred
    if customer is not None and invoice.customer_id is None:
        invoice.customer_id = customer.id
    if source_message_id and invoice.source_message_id is None:
        invoice.source_message_id = source_message_id
    if source_attachment_id and invoice.source_attachment_id is None:
        invoice.source_attachment_id = source_attachment_id
    invoice.confidence = max(invoice.confidence or 0.0, facts.confidence)

    await session.flush()

    decision = await refresh_invoice_state(
        session,
        invoice,
        today=today,
        grace_days=grace_days,
        reminder_cooldown_days=reminder_cooldown_days,
    )
    return InvoiceUpsertResult(invoice, created, decision)


async def refresh_invoice_state(
    session: AsyncSession,
    invoice: Invoice,
    *,
    today: date | None = None,
    grace_days: int = 0,
    reminder_cooldown_days: int = 3,
) -> StateDecision:
    """Re-derive an invoice's state from every payment event currently on file."""
    events = (
        await session.scalars(
            select(PaymentEvent).where(PaymentEvent.invoice_id == invoice.id)
        )
    ).all()
    folded = fold_payment_events(
        PaymentEventView(
            provider=e.provider,
            event_type=e.event_type,
            amount_paise=e.amount_paise,
            is_confirmation=e.is_confirmation,
            evidence_snippet=e.evidence_snippet,
        )
        for e in events
    )

    customer_email: str | None = None
    if invoice.customer_id is not None:
        customer_email = await session.scalar(
            select(Customer.primary_email).where(Customer.id == invoice.customer_id)
        )

    snapshot = EvidenceSnapshot(
        amount_paise=invoice.amount_paise or 0,
        amount_paid_paise=invoice.amount_paid_paise or 0,
        due_date=invoice.due_date,
        issued_date=invoice.issued_date,
        customer_id=invoice.customer_id,
        customer_email=customer_email,
        invoice_number=invoice.invoice_number,
        manually_paused=invoice.manually_paused,
        paused_until=invoice.paused_until,
        pause_reason=invoice.pause_reason,
        reminder_count=invoice.reminder_count or 0,
        last_reminder_at=invoice.last_reminder_at,
        **folded,
    )
    decision = decide_state(
        snapshot,
        today=today,
        grace_days=grace_days,
        reminder_cooldown_days=reminder_cooldown_days,
    )

    invoice.payment_state = str(decision.payment_state)
    invoice.reminder_state = str(decision.reminder_state)
    invoice.evidence_strength = str(decision.evidence_strength)
    invoice.state_reason = decision.reason
    invoice.balance_paise = decision.balance_paise
    invoice.amount_paid_paise = max(
        invoice.amount_paid_paise or 0, folded["confirmed_paid_paise"]
    )
    invoice.missing_fields = list(decision.missing_fields)
    if folded["dispute_note"]:
        invoice.dispute_note = folded["dispute_note"]
    if folded["payment_claim_note"]:
        invoice.payment_claim_note = folded["payment_claim_note"]

    await session.flush()
    return decision


# --------------------------------------------------------------------------------------
# Evidence links
# --------------------------------------------------------------------------------------


async def link_evidence(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invoice_id: uuid.UUID,
    link_type: str,
    snippet: str | None,
    locator: str | None,
    source_message_id: uuid.UUID | None = None,
    source_attachment_id: uuid.UUID | None = None,
    payment_event_id: uuid.UUID | None = None,
    confidence: float = 0.5,
) -> bool:
    """Attach one evidence reference to an invoice. Returns True when newly written."""
    if not any((source_message_id, source_attachment_id, payment_event_id)):
        raise ValueError("evidence link needs a message, attachment or payment event")

    return await insert_ignore(
        session,
        InvoiceSourceLink.__table__,
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "invoice_id": invoice_id,
            "source_message_id": source_message_id,
            "source_attachment_id": source_attachment_id,
            "payment_event_id": payment_event_id,
            "link_type": str(link_type),
            "evidence_snippet": (snippet or "")[:2000] or None,
            "evidence_locator": locator,
            "evidence_hash": evidence_hash(str(link_type), snippet, locator),
            "confidence": confidence,
        },
        index_elements=["invoice_id", "link_type", "evidence_hash"],
    )


async def link_extraction_evidence(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invoice_id: uuid.UUID,
    evidence: Sequence[Evidence],
    link_type: str,
    source_message_id: uuid.UUID | None = None,
    source_attachment_id: uuid.UUID | None = None,
    confidence: float = 0.6,
) -> int:
    """Persist every snippet from an extraction pass. Returns the number newly written."""
    written = 0
    for item in evidence:
        if await link_evidence(
            session,
            workspace_id=workspace_id,
            invoice_id=invoice_id,
            link_type=link_type,
            snippet=item.snippet,
            locator=item.locator,
            source_message_id=source_message_id,
            source_attachment_id=source_attachment_id,
            confidence=confidence,
        ):
            written += 1
    return written


# --------------------------------------------------------------------------------------
# Payment events
# --------------------------------------------------------------------------------------


async def record_payment_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    provider: str,
    provider_event_id: str,
    event_type: str,
    amount_paise: int | None = None,
    currency: str = "INR",
    observed_at: datetime | None = None,
    invoice_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    source_message_id: uuid.UUID | None = None,
    evidence_snippet: str | None = None,
    payload: dict[str, Any] | None = None,
    reconciliation_method: str | None = None,
) -> tuple[PaymentEvent | None, bool]:
    """Record a payment observation exactly once.

    Returns ``(event, created)``. `is_confirmation` is derived here rather than passed in:
    only a non-Gmail provider emitting a settlement event may set it.
    """
    is_confirmation = (
        provider != PaymentProvider.GMAIL
        and event_type
        in (
            PaymentEventType.PAYMENT_CAPTURED,
            PaymentEventType.INVOICE_PAID,
            PaymentEventType.MANUAL_CONFIRMATION,
        )
    )

    created = await insert_ignore(
        session,
        PaymentEvent.__table__,
        {
            "id": uuid.uuid4(),
            "workspace_id": workspace_id,
            "invoice_id": invoice_id,
            "customer_id": customer_id,
            "source_message_id": source_message_id,
            "provider": str(provider),
            "provider_event_id": provider_event_id,
            "event_type": str(event_type),
            "amount_paise": amount_paise,
            "currency": currency,
            "observed_at": observed_at or datetime.now(timezone.utc),
            "is_confirmation": is_confirmation,
            "evidence_snippet": (evidence_snippet or "")[:2000] or None,
            "reconciled_at": datetime.now(timezone.utc) if invoice_id else None,
            "reconciliation_method": reconciliation_method,
            "payload": payload or {},
        },
        index_elements=["workspace_id", "provider", "provider_event_id"],
    )

    event = await session.scalar(
        select(PaymentEvent).where(
            PaymentEvent.workspace_id == workspace_id,
            PaymentEvent.provider == str(provider),
            PaymentEvent.provider_event_id == provider_event_id,
        )
    )
    # A retry that arrives after the invoice match is known should still attach itself.
    if event is not None and not created and invoice_id and event.invoice_id is None:
        event.invoice_id = invoice_id
        event.reconciled_at = datetime.now(timezone.utc)
        event.reconciliation_method = reconciliation_method
        await session.flush()
    return event, created


async def attach_event_to_invoice(
    session: AsyncSession,
    event: PaymentEvent,
    invoice: Invoice,
    *,
    method: str,
) -> None:
    event.invoice_id = invoice.id
    event.customer_id = invoice.customer_id
    event.reconciled_at = datetime.now(timezone.utc)
    event.reconciliation_method = method
    await session.flush()


async def mark_invoices_paused(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    invoice_ids: Sequence[uuid.UUID],
    reason: str,
    payment_state: str | None = None,
) -> int:
    """Pause follow-ups for a set of invoices. Used by the agent layer's `pause_invoices`."""
    if not invoice_ids:
        return 0
    values: dict[str, Any] = {
        "reminder_state": str(ReminderState.PAUSED),
        "pause_reason": reason,
        "manually_paused": True,
    }
    if payment_state:
        values["payment_state"] = str(payment_state)
        if payment_state == PaymentState.DISPUTED:
            values["dispute_note"] = reason
        elif payment_state == PaymentState.PAYMENT_CLAIMED:
            values["payment_claim_note"] = reason
            values["evidence_strength"] = str(EvidenceStrength.GMAIL_EXPLICIT)
    result = await session.execute(
        update(Invoice)
        .where(Invoice.workspace_id == workspace_id, Invoice.id.in_(list(invoice_ids)))
        .values(**values)
    )
    return result.rowcount or 0
