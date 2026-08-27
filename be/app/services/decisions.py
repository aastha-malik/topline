"""The deterministic invoice state machine.

This module is the safety floor of the product. It runs *before* any model call and decides
the two stored state columns from evidence alone. Nothing here consults an LLM, and no LLM
may overwrite its output.

The rule that matters most:

    Gmail evidence can never produce `confirmed_paid`.

An email saying "we paid it" is a *claim*. It pauses follow-ups and asks the owner to verify,
which is the honest answer. Only a provider confirmation (Razorpay, or an explicit owner
override) is allowed to assert settlement - enforced here, in the model CHECK constraint, and
in the SQL migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from app.utils import ensure_utc
from app.enums import (
    BLOCKING_PAYMENT_STATES,
    CONFIRMING_EVENT_TYPES,
    EffectiveState,
    EvidenceStrength,
    PaymentEventType,
    PaymentProvider,
    PaymentState,
    ReminderState,
    effective_state,
)


@dataclass(slots=True, frozen=True)
class EvidenceSnapshot:
    """Everything the state machine is allowed to look at.

    Deliberately a plain value object rather than an ORM row, so the rules can be unit
    tested without a database and reused by the reconciliation path.
    """

    amount_paise: int
    amount_paid_paise: int = 0
    due_date: date | None = None
    issued_date: date | None = None
    customer_id: Any = None
    customer_email: str | None = None
    invoice_number: str | None = None
    missing_fields: Sequence[str] = ()

    #: Provider settlements observed for this invoice (Razorpay / manual owner override).
    confirmed_paid_paise: int = 0
    has_provider_confirmation: bool = False

    #: Unverified assertions found in email.
    has_payment_claim: bool = False
    payment_claim_note: str | None = None
    has_dispute: bool = False
    dispute_note: str | None = None

    #: Operational overrides.
    manually_paused: bool = False
    paused_until: date | None = None
    pause_reason: str | None = None

    reminder_count: int = 0
    last_reminder_at: datetime | None = None

    #: True when the invoice's own facts came only from Gmail (the normal case).
    gmail_only_source: bool = True


@dataclass(slots=True)
class StateDecision:
    payment_state: PaymentState
    reminder_state: ReminderState
    evidence_strength: EvidenceStrength
    reason: str
    balance_paise: int
    #: Ordered list of every rule considered, for the audit trail.
    rule_trail: list[str] = field(default_factory=list)
    #: Critical facts the extractor could not find. Non-empty -> `needs_information`.
    missing_fields: list[str] = field(default_factory=list)
    #: True when something deliberately holds this invoice (owner pause / paused_until),
    #: as opposed to it merely not being actionable yet. Drives `paused` vs `likely_unpaid`.
    is_on_hold: bool = False

    @property
    def effective_state(self) -> EffectiveState:
        return effective_state(
            self.payment_state, self.reminder_state, is_on_hold=self.is_on_hold
        )

    @property
    def blocks_followup(self) -> bool:
        return (
            self.payment_state in BLOCKING_PAYMENT_STATES
            or self.reminder_state == ReminderState.PAUSED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_state": str(self.payment_state),
            "reminder_state": str(self.reminder_state),
            "effective_state": str(self.effective_state),
            "evidence_strength": str(self.evidence_strength),
            "balance_paise": self.balance_paise,
            "reason": self.reason,
            "rules": self.rule_trail,
            "missing_fields": self.missing_fields,
            "is_on_hold": self.is_on_hold,
        }


REQUIRED_FACTS = ("amount", "customer_email", "due_date")


def decide_state(
    snapshot: EvidenceSnapshot,
    *,
    today: date | None = None,
    grace_days: int = 0,
    reminder_cooldown_days: int = 3,
) -> StateDecision:
    """Resolve an invoice's `(payment_state, reminder_state)` from its evidence.

    Precedence, highest first:

    1. `confirmed_paid`      - a provider confirmed settlement covering the balance
    2. `disputed`            - a hard stop; the owner must act
    3. `payment_claimed`     - someone says it is paid; pause and ask the owner to verify
    4. `needs_information`   - a critical fact is missing; do not draft anything
    5. `paused`              - an explicit operational hold
    6. `ready_for_reminder`  - overdue, complete, and off cooldown
    7. `likely_unpaid`       - the honest default
    """
    today = today or datetime.now(timezone.utc).date()
    trail: list[str] = []

    settled = max(snapshot.confirmed_paid_paise, snapshot.amount_paid_paise)
    balance = max(0, snapshot.amount_paise - settled)

    # --- 1. provider-confirmed payment -------------------------------------------------
    if snapshot.has_provider_confirmation and snapshot.amount_paise > 0 and balance == 0:
        trail.append("provider_confirmation_covers_balance")
        return StateDecision(
            PaymentState.CONFIRMED_PAID,
            ReminderState.PAUSED,
            EvidenceStrength.PROVIDER_CONFIRMED,
            "Payment confirmed by the payment provider; all follow-ups stopped.",
            balance,
            trail,
        )
    if snapshot.has_provider_confirmation and balance > 0:
        trail.append("provider_confirmation_partial")

    # --- 2. dispute --------------------------------------------------------------------
    if snapshot.has_dispute:
        trail.append("dispute_evidence_present")
        return StateDecision(
            PaymentState.DISPUTED,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            snapshot.dispute_note
            or "The customer disputed this invoice; no follow-up without owner action.",
            balance,
            trail,
        )

    # --- 3. unverified payment claim ---------------------------------------------------
    if snapshot.has_payment_claim:
        # Reached only without a provider confirmation, so this stays a claim on purpose.
        trail.append("payment_claim_without_provider_confirmation")
        return StateDecision(
            PaymentState.PAYMENT_CLAIMED,
            ReminderState.PAUSED,
            EvidenceStrength.GMAIL_EXPLICIT,
            snapshot.payment_claim_note
            or "The customer says this was paid. Reminders are paused pending owner "
               "verification - email alone is not proof of payment.",
            balance,
            trail,
        )

    # --- 4. missing critical facts -----------------------------------------------------
    missing = _missing_facts(snapshot)
    if missing:
        trail.append(f"missing_facts:{','.join(missing)}")
        return StateDecision(
            PaymentState.NEEDS_INFORMATION,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            f"Cannot follow up safely: missing {', '.join(missing)}.",
            balance,
            trail,
            missing_fields=missing,
        )

    # --- 5. explicit operational hold ---------------------------------------------------
    if snapshot.manually_paused:
        trail.append("manually_paused")
        return StateDecision(
            PaymentState.LIKELY_UNPAID,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            snapshot.pause_reason or "Follow-ups paused by the owner.",
            balance,
            trail,
            is_on_hold=True,
        )
    if snapshot.paused_until and snapshot.paused_until > today:
        trail.append("paused_until_future")
        return StateDecision(
            PaymentState.LIKELY_UNPAID,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            snapshot.pause_reason
            or f"Follow-ups paused until {snapshot.paused_until.isoformat()}.",
            balance,
            trail,
            is_on_hold=True,
        )

    # --- 6. ready for a reminder --------------------------------------------------------
    if balance <= 0:
        # Fully covered, but nothing from a provider confirmed it. Do not claim paid.
        trail.append("balance_zero_without_provider_confirmation")
        return StateDecision(
            PaymentState.LIKELY_UNPAID,
            ReminderState.PAUSED,
            EvidenceStrength.GMAIL_EXPLICIT,
            "Recorded payments cover the amount, but no payment provider confirmed it. "
            "Owner review required before marking this paid.",
            balance,
            trail,
        )

    assert snapshot.due_date is not None  # guaranteed by the missing-facts check above
    overdue_from = snapshot.due_date + timedelta(days=grace_days)
    if today < overdue_from:
        trail.append("not_yet_due")
        return StateDecision(
            PaymentState.LIKELY_UNPAID,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            f"Not due until {snapshot.due_date.isoformat()}.",
            balance,
            trail,
        )

    if _in_cooldown(snapshot, today, reminder_cooldown_days):
        trail.append("reminder_cooldown_active")
        return StateDecision(
            PaymentState.LIKELY_UNPAID,
            ReminderState.PAUSED,
            _gmail_strength(snapshot),
            f"A reminder was sent within the last {reminder_cooldown_days} day(s).",
            balance,
            trail,
        )

    days_overdue = (today - snapshot.due_date).days
    trail.append("overdue_and_clear_to_remind")
    return StateDecision(
        PaymentState.LIKELY_UNPAID,
        ReminderState.READY_FOR_REMINDER,
        _gmail_strength(snapshot),
        f"{days_overdue} day(s) overdue with no payment evidence on file.",
        balance,
        trail,
    )


def _missing_facts(snapshot: EvidenceSnapshot) -> list[str]:
    missing = list(dict.fromkeys(snapshot.missing_fields))
    if snapshot.amount_paise <= 0 and "amount" not in missing:
        missing.append("amount")
    if snapshot.due_date is None and "due_date" not in missing:
        missing.append("due_date")
    if not snapshot.customer_email and "customer_email" not in missing:
        missing.append("customer_email")
    if snapshot.customer_id is None and "customer" not in missing:
        missing.append("customer")
    # `invoice_number` is nice to have, not required: plenty of small businesses invoice
    # by email with an amount and a date only.
    return [m for m in missing if m in {"amount", "due_date", "customer_email", "customer"}]


def _in_cooldown(snapshot: EvidenceSnapshot, today: date, cooldown_days: int) -> bool:
    if snapshot.last_reminder_at is None or cooldown_days <= 0:
        return False
    last = ensure_utc(snapshot.last_reminder_at)
    last_date = last.date() if isinstance(last, datetime) else last
    return (today - last_date).days < cooldown_days


def _gmail_strength(snapshot: EvidenceSnapshot) -> EvidenceStrength:
    if snapshot.has_provider_confirmation:
        return EvidenceStrength.PROVIDER_CONFIRMED
    if snapshot.has_payment_claim or snapshot.has_dispute:
        return EvidenceStrength.GMAIL_EXPLICIT
    return EvidenceStrength.GMAIL_INFERRED


# --------------------------------------------------------------------------------------
# Payment-event folding
# --------------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PaymentEventView:
    """The subset of a payment event the state machine reads."""

    provider: str
    event_type: str
    amount_paise: int | None
    is_confirmation: bool
    evidence_snippet: str | None = None


def fold_payment_events(events: Iterable[PaymentEventView]) -> dict[str, Any]:
    """Reduce an invoice's payment events into state-machine inputs.

    Gmail events are folded as claims regardless of how they were tagged - the guard is
    applied here as well as at the database CHECK constraint, so a bad write upstream
    still cannot fabricate a confirmation.
    """
    confirmed_paise = 0
    refunded_paise = 0
    has_confirmation = False
    has_claim = False
    has_dispute = False
    claim_note: str | None = None
    dispute_note: str | None = None

    for event in events:
        is_gmail = event.provider == PaymentProvider.GMAIL

        if event.event_type == PaymentEventType.EMAIL_DISPUTE:
            has_dispute = True
            dispute_note = dispute_note or event.evidence_snippet
            continue

        if event.event_type in (
            PaymentEventType.EMAIL_PAYMENT_CLAIM,
            PaymentEventType.EMAIL_RECEIPT,
        ):
            has_claim = True
            claim_note = claim_note or event.evidence_snippet
            continue

        if event.event_type == PaymentEventType.REFUND_CREATED:
            refunded_paise += event.amount_paise or 0
            continue

        if event.event_type == PaymentEventType.PAYMENT_FAILED:
            continue

        if event.event_type in CONFIRMING_EVENT_TYPES and event.is_confirmation and not is_gmail:
            has_confirmation = True
            confirmed_paise += event.amount_paise or 0
        elif is_gmail:
            # A Gmail row that claims to confirm is downgraded, never trusted.
            has_claim = True
            claim_note = claim_note or event.evidence_snippet

    return {
        "confirmed_paid_paise": max(0, confirmed_paise - refunded_paise),
        "has_provider_confirmation": has_confirmation and confirmed_paise > refunded_paise,
        "has_payment_claim": has_claim,
        "payment_claim_note": claim_note,
        "has_dispute": has_dispute,
        "dispute_note": dispute_note,
    }
