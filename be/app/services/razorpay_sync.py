"""Razorpay webhook ingestion and payment reconciliation.

Razorpay is the *optional confirmation* source. Gmail remains the primary source of
invoices and context; Razorpay's job is narrow but important - it is the only thing in the
system allowed to turn `likely_unpaid` into `confirmed_paid`.

Safety properties:

* Signatures are verified with a constant-time HMAC-SHA256 compare before the body is
  parsed as anything meaningful. An unsigned or mis-signed request never reaches the ledger.
* Delivery is idempotent on the Razorpay event id, because Razorpay retries on non-2xx.
* An event that cannot be matched to an invoice is still stored, unmatched, so a later
  reconciliation pass can pick it up rather than losing the confirmation.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.enums import PaymentEventType, PaymentProvider
from app.logging_config import get_logger
from app.models import Invoice, PaymentEvent
from app.services import ledger
from app.services.audit import audit_key, record_event

logger = get_logger(__name__)


class WebhookSignatureError(Exception):
    """The webhook signature is missing or does not match."""


#: Razorpay event name -> our internal event type. Anything unmapped is ignored.
EVENT_TYPE_MAP: dict[str, PaymentEventType] = {
    "payment.captured": PaymentEventType.PAYMENT_CAPTURED,
    "payment.failed": PaymentEventType.PAYMENT_FAILED,
    "invoice.paid": PaymentEventType.INVOICE_PAID,
    "order.paid": PaymentEventType.PAYMENT_CAPTURED,
    "refund.created": PaymentEventType.REFUND_CREATED,
    "refund.processed": PaymentEventType.REFUND_CREATED,
}


def verify_webhook_signature(body: bytes, signature: str | None, secret: str | None) -> None:
    """Verify Razorpay's `X-Razorpay-Signature` header.

    Raises :class:`WebhookSignatureError` on any failure. There is no "skip verification"
    path - an unverified webhook must never be able to mark an invoice paid.
    """
    if not secret:
        raise WebhookSignatureError("RAZORPAY_WEBHOOK_SECRET is not configured")
    if not signature:
        raise WebhookSignatureError("missing X-Razorpay-Signature header")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise WebhookSignatureError("signature mismatch")


# --------------------------------------------------------------------------------------
# Payload normalisation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class RazorpayEvent:
    event_id: str
    event_name: str
    event_type: PaymentEventType
    amount_paise: int | None
    currency: str
    occurred_at: datetime
    razorpay_payment_id: str | None = None
    razorpay_invoice_id: str | None = None
    razorpay_order_id: str | None = None
    customer_email: str | None = None
    customer_contact: str | None = None
    #: `notes.invoice_number` is the cleanest join back to a Gmail-extracted invoice.
    invoice_number: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_webhook_payload(payload: dict[str, Any], *, event_id: str | None = None) -> RazorpayEvent | None:
    """Normalise a Razorpay webhook body. Returns None for events we do not act on."""
    event_name = payload.get("event") or ""
    mapped = EVENT_TYPE_MAP.get(event_name)
    if mapped is None:
        return None

    entities = payload.get("payload") or {}
    payment = ((entities.get("payment") or {}).get("entity")) or {}
    invoice = ((entities.get("invoice") or {}).get("entity")) or {}
    order = ((entities.get("order") or {}).get("entity")) or {}
    refund = ((entities.get("refund") or {}).get("entity")) or {}
    primary = payment or invoice or order or refund

    notes = {**(invoice.get("notes") or {}), **(payment.get("notes") or {}),
             **(order.get("notes") or {})}

    created_at = payload.get("created_at") or primary.get("created_at")
    occurred_at = (
        datetime.fromtimestamp(int(created_at), tz=timezone.utc)
        if created_at
        else datetime.now(timezone.utc)
    )

    resolved_id = (
        event_id
        or payload.get("id")
        or f"{event_name}:{primary.get('id') or uuid.uuid4()}"
    )

    return RazorpayEvent(
        event_id=str(resolved_id),
        event_name=event_name,
        event_type=mapped,
        # Razorpay already speaks in the smallest currency unit, so paise passes through.
        amount_paise=_as_int(refund.get("amount") or primary.get("amount")),
        currency=(primary.get("currency") or "INR").upper(),
        occurred_at=occurred_at,
        razorpay_payment_id=payment.get("id") or refund.get("payment_id"),
        razorpay_invoice_id=invoice.get("id") or payment.get("invoice_id"),
        razorpay_order_id=order.get("id") or payment.get("order_id"),
        customer_email=(payment.get("email") or invoice.get("customer_details", {}).get("email")
                        or "").strip().lower() or None,
        customer_contact=payment.get("contact") or None,
        invoice_number=(
            notes.get("invoice_number")
            or notes.get("invoice_no")
            or notes.get("invoice")
            or invoice.get("invoice_number")
        ),
        raw=payload,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ReconciliationResult:
    event_id: str
    accepted: bool
    duplicate: bool = False
    matched_invoice_id: uuid.UUID | None = None
    match_method: str | None = None
    resulting_state: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "matched_invoice_id": str(self.matched_invoice_id) if self.matched_invoice_id else None,
            "match_method": self.match_method,
            "resulting_state": self.resulting_state,
            "detail": self.detail,
        }


async def ingest_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    event: RazorpayEvent,
    settings: Settings,
) -> ReconciliationResult:
    """Store a Razorpay event and reconcile it against the ledger. Idempotent."""
    invoice, method = await match_invoice(session, workspace_id=workspace_id, event=event)
    customer_id = invoice.customer_id if invoice is not None else None

    payment_event, created = await ledger.record_payment_event(
        session,
        workspace_id=workspace_id,
        provider=PaymentProvider.RAZORPAY,
        provider_event_id=event.event_id,
        event_type=event.event_type,
        amount_paise=event.amount_paise,
        currency=event.currency,
        observed_at=event.occurred_at,
        invoice_id=invoice.id if invoice else None,
        customer_id=customer_id,
        evidence_snippet=(
            f"Razorpay {event.event_name}"
            + (f" payment {event.razorpay_payment_id}" if event.razorpay_payment_id else "")
        ),
        payload=event.raw,
        reconciliation_method=method,
    )

    if invoice is None:
        # Keep the confirmation rather than dropping it; `reconcile_pending` retries later.
        result = ReconciliationResult(
            event.event_id,
            accepted=True,
            duplicate=not created,
            detail="stored unmatched: no invoice matched this event",
        )
        await _audit(session, workspace_id, owner_id, event, result, None)
        return result

    if event.razorpay_payment_id and not invoice.razorpay_payment_id:
        invoice.razorpay_payment_id = event.razorpay_payment_id
    if event.razorpay_invoice_id and not invoice.razorpay_invoice_id:
        invoice.razorpay_invoice_id = event.razorpay_invoice_id

    if payment_event is not None and payment_event.invoice_id is None:
        await ledger.attach_event_to_invoice(session, payment_event, invoice, method=method or "")

    decision = await ledger.refresh_invoice_state(
        session,
        invoice,
        grace_days=settings.reminder_grace_days,
        reminder_cooldown_days=settings.reminder_cooldown_days,
    )

    if payment_event is not None:
        await ledger.link_evidence(
            session,
            workspace_id=workspace_id,
            invoice_id=invoice.id,
            link_type="payment_confirmation"
            if payment_event.is_confirmation
            else "reminder_context",
            snippet=payment_event.evidence_snippet,
            locator=f"razorpay:{event.event_id}",
            payment_event_id=payment_event.id,
            confidence=1.0 if payment_event.is_confirmation else 0.5,
        )

    result = ReconciliationResult(
        event.event_id,
        accepted=True,
        duplicate=not created,
        matched_invoice_id=invoice.id,
        match_method=method,
        resulting_state=str(decision.effective_state),
        detail=decision.reason,
    )
    await _audit(session, workspace_id, owner_id, event, result, decision.as_dict())
    return result


async def match_invoice(
    session: AsyncSession, *, workspace_id: uuid.UUID, event: RazorpayEvent
) -> tuple[Invoice | None, str | None]:
    """Find the invoice a Razorpay event belongs to.

    Strategies run strongest-first and stop at the first unambiguous hit. Anything weaker
    than "one clear match" returns None: a wrong match would mark the wrong invoice paid,
    which is worse than leaving the event unreconciled for the owner to resolve.
    """
    base = select(Invoice).where(Invoice.workspace_id == workspace_id)

    # 1. Razorpay invoice id already stored on the invoice.
    if event.razorpay_invoice_id:
        if found := await session.scalar(
            base.where(Invoice.razorpay_invoice_id == event.razorpay_invoice_id)
        ):
            return found, "razorpay_invoice_id"

    # 2. Razorpay payment id already stored.
    if event.razorpay_payment_id:
        if found := await session.scalar(
            base.where(Invoice.razorpay_payment_id == event.razorpay_payment_id)
        ):
            return found, "razorpay_payment_id"

    # 3. `notes.invoice_number` matched against the Gmail-extracted number.
    if event.invoice_number:
        normalized = event.invoice_number.strip().upper().replace(" ", "")
        matches = (
            await session.scalars(base.where(Invoice.normalized_number == normalized))
        ).all()
        if len(matches) == 1:
            return matches[0], "invoice_number_note"
        if len(matches) > 1 and event.customer_email:
            customer = await ledger.find_customer_by_email(
                session, workspace_id, event.customer_email
            )
            if customer:
                scoped = [m for m in matches if m.customer_id == customer.id]
                if len(scoped) == 1:
                    return scoped[0], "invoice_number_note+customer"

    # 4. Customer email + exact outstanding amount.
    if event.customer_email and event.amount_paise:
        customer = await ledger.find_customer_by_email(
            session, workspace_id, event.customer_email
        )
        if customer is not None:
            matches = (
                await session.scalars(
                    base.where(
                        Invoice.customer_id == customer.id,
                        Invoice.amount_paise == event.amount_paise,
                        Invoice.payment_state != "confirmed_paid",
                    )
                )
            ).all()
            if len(matches) == 1:
                return matches[0], "customer_email+amount"

    return None, None


async def reconcile_pending(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    settings: Settings,
) -> list[ReconciliationResult]:
    """Retry every stored-but-unmatched Razorpay event.

    Run after a Gmail sync: a confirmation often arrives before the invoice it belongs to
    has been extracted from the mailbox.
    """
    pending = (
        await session.scalars(
            select(PaymentEvent).where(
                PaymentEvent.workspace_id == workspace_id,
                PaymentEvent.provider == str(PaymentProvider.RAZORPAY),
                PaymentEvent.invoice_id.is_(None),
            )
        )
    ).all()

    results: list[ReconciliationResult] = []
    for stored in pending:
        event = parse_webhook_payload(stored.payload or {}, event_id=stored.provider_event_id)
        if event is None:
            continue
        invoice, method = await match_invoice(session, workspace_id=workspace_id, event=event)
        if invoice is None:
            continue

        await ledger.attach_event_to_invoice(session, stored, invoice, method=method or "")
        decision = await ledger.refresh_invoice_state(
            session,
            invoice,
            grace_days=settings.reminder_grace_days,
            reminder_cooldown_days=settings.reminder_cooldown_days,
        )
        result = ReconciliationResult(
            stored.provider_event_id,
            accepted=True,
            matched_invoice_id=invoice.id,
            match_method=method,
            resulting_state=str(decision.effective_state),
            detail=decision.reason,
        )
        results.append(result)
        await _audit(session, workspace_id, owner_id, event, result, decision.as_dict())

    return results


async def _audit(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    event: RazorpayEvent,
    result: ReconciliationResult,
    decision: dict[str, Any] | None,
) -> None:
    await record_event(
        session,
        workspace_id=workspace_id,
        owner_id=owner_id,
        event_type="razorpay.event_reconciled",
        summary=(
            f"Razorpay {event.event_name}: "
            + (
                f"matched invoice via {result.match_method} -> {result.resulting_state}"
                if result.matched_invoice_id
                else "stored without an invoice match"
            )
        ),
        actor_type="provider",
        actor_id="razorpay",
        entity_type="invoice" if result.matched_invoice_id else "payment_event",
        entity_id=str(result.matched_invoice_id) if result.matched_invoice_id else event.event_id,
        decision={**result.as_dict(), "state_decision": decision},
        source_evidence=[
            {
                "provider": "razorpay",
                "event": event.event_name,
                "event_id": event.event_id,
                "payment_id": event.razorpay_payment_id,
                "amount_paise": event.amount_paise,
            }
        ],
        dedupe_key=audit_key("razorpay.event", workspace_id, event.event_id, result.resulting_state),
    )


def build_test_signature(body: bytes, secret: str) -> str:
    """Helper for tests and local webhook replay."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
