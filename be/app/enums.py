"""Canonical string enums shared by the models, the API schemas and the SQL migrations.

Every value here is persisted as text in Postgres and guarded by a CHECK constraint in
`supabase/migrations`. `tests/test_schema_parity.py` fails the build when the two drift.

State factoring
---------------
The seven required invoice states are stored as **two orthogonal columns** rather than one,
matching the contract the agent layer already builds against
(``app/agent_layer/domain.py::InvoiceRecord``):

* ``invoices.payment_state``  -> what the evidence says about money
  (`confirmed_paid`, `likely_unpaid`, `payment_claimed`, `disputed`, `needs_information`)
* ``invoices.reminder_state`` -> what the follow-up queue may do
  (`ready_for_reminder`, `paused`)

:func:`effective_state` collapses the pair back into the single seven-valued label used by
the API and the dashboard.
"""

from __future__ import annotations

from enum import StrEnum


class PaymentState(StrEnum):
    """What the evidence says about the money. Precedence order, highest first."""

    CONFIRMED_PAID = "confirmed_paid"
    DISPUTED = "disputed"
    PAYMENT_CLAIMED = "payment_claimed"
    NEEDS_INFORMATION = "needs_information"
    LIKELY_UNPAID = "likely_unpaid"


class ReminderState(StrEnum):
    """What the daily follow-up queue is allowed to do with the invoice."""

    READY_FOR_REMINDER = "ready_for_reminder"
    PAUSED = "paused"


class EffectiveState(StrEnum):
    """The seven-valued label presented over the API. Derived, never stored."""

    CONFIRMED_PAID = "confirmed_paid"
    DISPUTED = "disputed"
    PAYMENT_CLAIMED = "payment_claimed"
    NEEDS_INFORMATION = "needs_information"
    PAUSED = "paused"
    READY_FOR_REMINDER = "ready_for_reminder"
    LIKELY_UNPAID = "likely_unpaid"


#: Payment states in which no customer-facing follow-up may be produced or sent.
#: `reminder_state` is forced to `paused` whenever the invoice is in one of these.
BLOCKING_PAYMENT_STATES: frozenset[str] = frozenset(
    {
        PaymentState.CONFIRMED_PAID,
        PaymentState.DISPUTED,
        PaymentState.PAYMENT_CLAIMED,
        PaymentState.NEEDS_INFORMATION,
    }
)


def effective_state(
    payment_state: str, reminder_state: str, *, is_on_hold: bool = False
) -> EffectiveState:
    """Collapse the stored state columns into the single seven-valued label.

    A blocking payment state always outranks the reminder state, so an invoice that is
    `confirmed_paid` never surfaces as `ready_for_reminder`.

    `paused` and `likely_unpaid` both map to ``reminder_state = paused`` in storage, but
    they mean different things to the owner, so `is_on_hold` separates them:

    * `paused`         - something is deliberately holding this invoice back (the owner
                         paused it, or `paused_until` has not elapsed)
    * `likely_unpaid`  - nothing is holding it; it is simply not actionable yet
                         (not yet due, or inside the reminder cooldown)

    That distinction is the honest default the product depends on: an unconfirmed invoice
    reads as *likely* unpaid rather than as a certainty or a deliberate hold.
    """
    if payment_state in BLOCKING_PAYMENT_STATES:
        return EffectiveState(payment_state)
    if reminder_state == ReminderState.READY_FOR_REMINDER:
        return EffectiveState.READY_FOR_REMINDER
    if is_on_hold:
        return EffectiveState.PAUSED
    return EffectiveState.LIKELY_UNPAID


class EvidenceStrength(StrEnum):
    """How far an invoice's payment status can be trusted.

    Gmail tops out at ``gmail_explicit``: someone *stated* payment in writing, which is a
    claim, not a settlement. Presenting that as payment truth is a product-level bug, so
    :mod:`app.services.decisions` requires ``provider_confirmed`` before `confirmed_paid`.
    """

    GMAIL_INFERRED = "gmail_inferred"  # keyword/heuristic read of an email or PDF
    GMAIL_EXPLICIT = "gmail_explicit"  # payment asserted in writing; still only a claim
    PROVIDER_CONFIRMED = "provider_confirmed"  # Razorpay, or an explicit owner override


class PaymentProvider(StrEnum):
    GMAIL = "gmail"
    RAZORPAY = "razorpay"
    MANUAL = "manual"


class PaymentEventType(StrEnum):
    # Razorpay - settlement confirmations
    PAYMENT_CAPTURED = "payment_captured"
    PAYMENT_FAILED = "payment_failed"
    INVOICE_PAID = "invoice_paid"
    REFUND_CREATED = "refund_created"
    # Gmail - claims and context only, never confirmations
    EMAIL_PAYMENT_CLAIM = "email_payment_claim"
    EMAIL_RECEIPT = "email_receipt"
    EMAIL_DISPUTE = "email_dispute"
    # Owner action
    MANUAL_CONFIRMATION = "manual_confirmation"


#: The only event types permitted to move an invoice to `confirmed_paid`.
CONFIRMING_EVENT_TYPES: frozenset[str] = frozenset(
    {
        PaymentEventType.PAYMENT_CAPTURED,
        PaymentEventType.INVOICE_PAID,
        PaymentEventType.MANUAL_CONFIRMATION,
    }
)


class LinkType(StrEnum):
    """Why a source message / attachment / payment event is attached to an invoice."""

    INVOICE_DOCUMENT = "invoice_document"  # the PDF the invoice was extracted from
    INVOICE_MENTION = "invoice_mention"  # an email body referencing the invoice
    PAYMENT_CLAIM = "payment_claim"
    PAYMENT_CONFIRMATION = "payment_confirmation"
    DISPUTE = "dispute"
    REMINDER_CONTEXT = "reminder_context"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    TEXT_EXTRACTED = "text_extracted"
    OCR_EXTRACTED = "ocr_extracted"
    FAILED = "failed"
    SKIPPED = "skipped"  # not a PDF, oversized, or password protected
    OCR_UNAVAILABLE = "ocr_unavailable"  # needed OCR but no OCR backend is installed


class ExtractionMethod(StrEnum):
    NONE = "none"
    PYPDF = "pypdf"
    PYMUPDF = "pymupdf"
    OCR_TESSERACT = "ocr_tesseract"


class MessageProcessingState(StrEnum):
    METADATA_ONLY = "metadata_only"  # headers seen, body deliberately not fetched
    FETCHED = "fetched"  # full body pulled because the message scored as a candidate
    EXTRACTED = "extracted"  # facts promoted into the ledger
    IGNORED = "ignored"  # scored below threshold - retained as a decision record
    FAILED = "failed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SyncMode(StrEnum):
    BACKFILL = "backfill"
    INCREMENTAL = "incremental"
    FALLBACK_RESYNC = "fallback_resync"  # history id expired -> scoped re-scan


class SyncStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AccountStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    DISCONNECTED = "disconnected"


class CustomerMatchMethod(StrEnum):
    EMAIL_EXACT = "email_exact"
    DOMAIN = "domain"
    NAME_FUZZY = "name_fuzzy"
    MANUAL = "manual"


class ActorType(StrEnum):
    SYSTEM = "system"
    USER = "user"
    PROVIDER = "provider"
