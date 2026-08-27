"""The deterministic state machine, including the rule that Gmail cannot confirm payment."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.enums import (
    BLOCKING_PAYMENT_STATES,
    EffectiveState,
    EvidenceStrength,
    PaymentEventType,
    PaymentProvider,
    PaymentState,
    ReminderState,
    effective_state,
)
from app.services.decisions import (
    EvidenceSnapshot,
    PaymentEventView,
    decide_state,
    fold_payment_events,
)

TODAY = date(2026, 8, 27)
DUE = date(2026, 7, 20)


def snapshot(**overrides) -> EvidenceSnapshot:
    base = dict(
        amount_paise=4_000_000,
        due_date=DUE,
        customer_id="cust-1",
        customer_email="ap@acmetraders.in",
        invoice_number="INV-2026-0114",
    )
    return EvidenceSnapshot(**{**base, **overrides})


class TestAllSevenStates:
    """Every required state must be reachable from evidence alone."""

    def test_ready_for_reminder(self):
        decision = decide_state(snapshot(), today=TODAY)
        assert decision.effective_state == EffectiveState.READY_FOR_REMINDER
        assert "38 day(s) overdue" in decision.reason

    def test_likely_unpaid_when_not_yet_due(self):
        decision = decide_state(snapshot(due_date=date(2026, 9, 30)), today=TODAY)
        assert decision.payment_state == PaymentState.LIKELY_UNPAID
        assert decision.reminder_state == ReminderState.PAUSED

    def test_confirmed_paid_requires_a_provider(self):
        decision = decide_state(
            snapshot(has_provider_confirmation=True, confirmed_paid_paise=4_000_000),
            today=TODAY,
        )
        assert decision.effective_state == EffectiveState.CONFIRMED_PAID
        assert decision.evidence_strength == EvidenceStrength.PROVIDER_CONFIRMED
        assert decision.balance_paise == 0

    def test_payment_claimed(self):
        decision = decide_state(
            snapshot(has_payment_claim=True, payment_claim_note="We paid on the 3rd"),
            today=TODAY,
        )
        assert decision.effective_state == EffectiveState.PAYMENT_CLAIMED
        assert decision.evidence_strength == EvidenceStrength.GMAIL_EXPLICIT
        assert decision.blocks_followup

    def test_disputed(self):
        decision = decide_state(snapshot(has_dispute=True), today=TODAY)
        assert decision.effective_state == EffectiveState.DISPUTED
        assert decision.blocks_followup

    @pytest.mark.parametrize(
        "overrides,missing",
        [
            ({"amount_paise": 0}, "amount"),
            ({"due_date": None}, "due_date"),
            ({"customer_email": None}, "customer_email"),
            ({"customer_id": None}, "customer"),
        ],
    )
    def test_needs_information(self, overrides, missing):
        decision = decide_state(snapshot(**overrides), today=TODAY)
        assert decision.effective_state == EffectiveState.NEEDS_INFORMATION
        assert missing in decision.missing_fields

    def test_paused(self):
        decision = decide_state(
            snapshot(manually_paused=True, pause_reason="Owner asked to hold"), today=TODAY
        )
        assert decision.effective_state == EffectiveState.PAUSED
        assert decision.reason == "Owner asked to hold"

    def test_every_state_is_covered_by_this_class(self):
        """Guard against a state being added without a matching test."""
        reachable = set()
        for snap in (
            snapshot(),
            snapshot(due_date=date(2026, 9, 30)),
            snapshot(has_provider_confirmation=True, confirmed_paid_paise=4_000_000),
            snapshot(has_payment_claim=True),
            snapshot(has_dispute=True),
            snapshot(amount_paise=0),
            snapshot(manually_paused=True),
        ):
            reachable.add(decide_state(snap, today=TODAY).effective_state)
        assert reachable == set(EffectiveState)


class TestGmailIsNeverPaymentTruth:
    """The core safety property: mail alone can never assert settlement."""

    def test_email_claim_does_not_confirm_payment(self):
        decision = decide_state(
            snapshot(has_payment_claim=True, amount_paid_paise=4_000_000), today=TODAY
        )
        assert decision.payment_state != PaymentState.CONFIRMED_PAID
        assert decision.effective_state == EffectiveState.PAYMENT_CLAIMED

    def test_zero_balance_without_provider_is_not_confirmed_paid(self):
        """Recorded payments covering the amount still need provider confirmation."""
        decision = decide_state(
            snapshot(amount_paid_paise=4_000_000, has_provider_confirmation=False),
            today=TODAY,
        )
        assert decision.payment_state == PaymentState.LIKELY_UNPAID
        assert decision.reminder_state == ReminderState.PAUSED
        assert "no payment provider confirmed it" in decision.reason

    def test_gmail_event_claiming_capture_is_downgraded_to_a_claim(self):
        """Even a Gmail row tagged as a confirmation folds to an unverified claim."""
        folded = fold_payment_events(
            [
                PaymentEventView(
                    provider=PaymentProvider.GMAIL,
                    event_type=PaymentEventType.PAYMENT_CAPTURED,
                    amount_paise=4_000_000,
                    is_confirmation=True,  # a lie from upstream
                    evidence_snippet="we paid it",
                )
            ]
        )
        assert folded["has_provider_confirmation"] is False
        assert folded["confirmed_paid_paise"] == 0
        assert folded["has_payment_claim"] is True

        decision = decide_state(snapshot(**folded), today=TODAY)
        assert decision.effective_state == EffectiveState.PAYMENT_CLAIMED

    def test_evidence_strength_never_exceeds_gmail_without_a_provider(self):
        for snap in (snapshot(), snapshot(has_payment_claim=True), snapshot(has_dispute=True)):
            decision = decide_state(snap, today=TODAY)
            assert decision.evidence_strength != EvidenceStrength.PROVIDER_CONFIRMED


class TestPrecedence:
    def test_confirmed_paid_outranks_a_dispute(self):
        decision = decide_state(
            snapshot(
                has_provider_confirmation=True,
                confirmed_paid_paise=4_000_000,
                has_dispute=True,
            ),
            today=TODAY,
        )
        assert decision.effective_state == EffectiveState.CONFIRMED_PAID

    def test_dispute_outranks_a_payment_claim(self):
        decision = decide_state(
            snapshot(has_dispute=True, has_payment_claim=True), today=TODAY
        )
        assert decision.effective_state == EffectiveState.DISPUTED

    def test_claim_outranks_missing_information(self):
        decision = decide_state(
            snapshot(has_payment_claim=True, due_date=None), today=TODAY
        )
        assert decision.effective_state == EffectiveState.PAYMENT_CLAIMED

    def test_partial_provider_payment_leaves_a_balance(self):
        decision = decide_state(
            snapshot(has_provider_confirmation=True, confirmed_paid_paise=1_000_000),
            today=TODAY,
        )
        assert decision.payment_state == PaymentState.LIKELY_UNPAID
        assert decision.balance_paise == 3_000_000
        assert decision.effective_state == EffectiveState.READY_FOR_REMINDER

    def test_refund_reverses_a_confirmation(self):
        folded = fold_payment_events(
            [
                PaymentEventView(PaymentProvider.RAZORPAY, PaymentEventType.PAYMENT_CAPTURED,
                                 4_000_000, True),
                PaymentEventView(PaymentProvider.RAZORPAY, PaymentEventType.REFUND_CREATED,
                                 4_000_000, False),
            ]
        )
        assert folded["has_provider_confirmation"] is False
        assert decide_state(snapshot(**folded), today=TODAY).payment_state != (
            PaymentState.CONFIRMED_PAID
        )

    def test_failed_payment_is_not_a_confirmation(self):
        folded = fold_payment_events(
            [PaymentEventView(PaymentProvider.RAZORPAY, PaymentEventType.PAYMENT_FAILED,
                              4_000_000, False)]
        )
        assert folded["has_provider_confirmation"] is False


class TestReminderPacing:
    def test_cooldown_blocks_a_repeat_reminder(self):
        decision = decide_state(
            snapshot(last_reminder_at=datetime(2026, 8, 26, tzinfo=timezone.utc)),
            today=TODAY,
            reminder_cooldown_days=3,
        )
        assert decision.reminder_state == ReminderState.PAUSED
        assert "within the last 3 day(s)" in decision.reason

    def test_reminder_resumes_after_the_cooldown(self):
        decision = decide_state(
            snapshot(last_reminder_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
            today=TODAY,
            reminder_cooldown_days=3,
        )
        assert decision.reminder_state == ReminderState.READY_FOR_REMINDER

    def test_grace_period_delays_the_first_reminder(self):
        decision = decide_state(
            snapshot(due_date=date(2026, 8, 25)), today=TODAY, grace_days=5
        )
        assert decision.reminder_state == ReminderState.PAUSED

    def test_paused_until_a_future_date(self):
        decision = decide_state(snapshot(paused_until=date(2026, 9, 15)), today=TODAY)
        assert decision.effective_state == EffectiveState.PAUSED

    def test_expired_pause_releases_the_invoice(self):
        decision = decide_state(snapshot(paused_until=date(2026, 8, 1)), today=TODAY)
        assert decision.effective_state == EffectiveState.READY_FOR_REMINDER


class TestEffectiveStateMapping:
    def test_blocking_states_never_surface_as_ready(self):
        for state in BLOCKING_PAYMENT_STATES:
            assert effective_state(state, ReminderState.READY_FOR_REMINDER) == state

    def test_decisions_are_serialisable_for_the_audit_log(self):
        payload = decide_state(snapshot(), today=TODAY).as_dict()
        assert payload["effective_state"] == "ready_for_reminder"
        assert isinstance(payload["rules"], list)
        assert payload["balance_paise"] == 4_000_000
