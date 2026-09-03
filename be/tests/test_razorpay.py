"""Razorpay webhook verification, idempotency and reconciliation."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.enums import EffectiveState, EvidenceStrength, PaymentProvider, PaymentState
from app.models import ActivityLog, Customer, Invoice, InvoiceSourceLink, PaymentEvent
from app.services import ledger, razorpay_sync
from app.services.razorpay_sync import (
    WebhookSignatureError,
    build_test_signature,
    parse_webhook_payload,
    verify_webhook_signature,
)

SECRET = "test_webhook_secret"


def settings() -> Settings:
    return Settings(_env_file=None, token_encryption_key="x", razorpay_webhook_secret=SECRET)


def captured_payload(
    *, amount: int = 4_000_000, invoice_number: str | None = "INV-2026-0114",
    email: str = "ap@acmetraders.in", payment_id: str = "pay_TEST0001",
) -> dict:
    notes = {"invoice_number": invoice_number} if invoice_number else {}
    return {
        "event": "payment.captured",
        "created_at": 1785000000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "currency": "INR",
                    "email": email,
                    "notes": notes,
                }
            }
        },
    }


async def seed_invoice(session, workspace, owner, *, number="INV-2026-0114", amount=4_000_000):
    customer = await ledger.upsert_customer(
        session,
        workspace_id=workspace.id,
        owner_id=owner.id,
        name="Acme Traders",
        email="ap@acmetraders.in",
    )
    invoice = Invoice(
        workspace_id=workspace.id,
        owner_id=owner.id,
        customer_id=customer.id,
        invoice_number=number,
        normalized_number=number.upper(),
        amount_paise=amount,
        balance_paise=amount,
        currency="INR",
        issued_date=date(2026, 7, 5),
        due_date=date(2026, 7, 20),
        payment_state=str(PaymentState.LIKELY_UNPAID),
        reminder_state="ready_for_reminder",
        dedupe_key=ledger.invoice_dedupe_key(
            customer_email="ap@acmetraders.in", invoice_number=number
        ),
    )
    session.add(invoice)
    await session.flush()
    return customer, invoice


class TestSignatureVerification:
    def test_valid_signature_passes(self):
        body = json.dumps(captured_payload()).encode()
        verify_webhook_signature(body, build_test_signature(body, SECRET), SECRET)

    def test_tampered_body_is_rejected(self):
        body = json.dumps(captured_payload()).encode()
        signature = build_test_signature(body, SECRET)
        tampered = json.dumps(captured_payload(amount=1)).encode()
        with pytest.raises(WebhookSignatureError, match="mismatch"):
            verify_webhook_signature(tampered, signature, SECRET)

    def test_missing_signature_is_rejected(self):
        with pytest.raises(WebhookSignatureError, match="missing"):
            verify_webhook_signature(b"{}", None, SECRET)

    def test_unconfigured_secret_is_rejected(self):
        """There is no "skip verification" path, even when the secret is absent."""
        with pytest.raises(WebhookSignatureError, match="not configured"):
            verify_webhook_signature(b"{}", "abc", None)

    def test_wrong_secret_is_rejected(self):
        body = b'{"event":"payment.captured"}'
        with pytest.raises(WebhookSignatureError):
            verify_webhook_signature(body, build_test_signature(body, "other"), SECRET)


class TestPayloadParsing:
    def test_extracts_the_fields_reconciliation_needs(self):
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        assert event.event_id == "evt_1"
        assert event.event_type == "payment_captured"
        assert event.amount_paise == 4_000_000
        assert event.customer_email == "ap@acmetraders.in"
        assert event.invoice_number == "INV-2026-0114"

    def test_unhandled_events_are_dropped(self):
        assert parse_webhook_payload({"event": "payment.authorized"}) is None

    def test_invoice_paid_event(self):
        event = parse_webhook_payload(
            {
                "event": "invoice.paid",
                "payload": {"invoice": {"entity": {"id": "inv_R1", "amount": 500000,
                                                   "currency": "INR"}}},
            },
            event_id="evt_2",
        )
        assert event.event_type == "invoice_paid"
        assert event.razorpay_invoice_id == "inv_R1"


class TestWorkspaceRouting:
    async def test_pinned_workspace_wins(self, session, workspace, owner):
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        resolved = await razorpay_sync.resolve_workspace_for_event(
            session, event, pinned=str(workspace.id)
        )
        assert resolved == workspace.id

    async def test_routes_by_a_matching_invoice(self, session, workspace, owner):
        await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        resolved = await razorpay_sync.resolve_workspace_for_event(session, event)
        assert resolved == workspace.id

    async def test_unroutable_event_returns_none(self, session, workspace, owner):
        event = parse_webhook_payload(
            captured_payload(invoice_number=None, email="stranger@nowhere.in"),
            event_id="evt_1",
        )
        assert await razorpay_sync.resolve_workspace_for_event(session, event) is None

    async def test_ambiguous_customer_email_is_not_guessed(
        self, session, workspace, owner, second_workspace, other_owner
    ):
        await ledger.upsert_customer(
            session, workspace_id=workspace.id, owner_id=owner.id,
            name="Shared", email="shared@customer.in",
        )
        await ledger.upsert_customer(
            session, workspace_id=second_workspace.id, owner_id=other_owner.id,
            name="Shared", email="shared@customer.in",
        )
        event = parse_webhook_payload(
            captured_payload(invoice_number=None, email="shared@customer.in"),
            event_id="evt_1",
        )
        assert await razorpay_sync.resolve_workspace_for_event(session, event) is None


class TestReconciliation:
    async def test_capture_confirms_payment_and_stops_reminders(
        self, session, workspace, owner
    ):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")

        result = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )

        assert result.matched_invoice_id == invoice.id
        assert result.match_method == "invoice_number_note"
        await session.refresh(invoice)
        assert invoice.payment_state == PaymentState.CONFIRMED_PAID
        assert invoice.effective_state == EffectiveState.CONFIRMED_PAID
        assert invoice.evidence_strength == EvidenceStrength.PROVIDER_CONFIRMED
        assert invoice.balance_paise == 0
        assert invoice.razorpay_payment_id == "pay_TEST0001"

    async def test_confirmation_is_linked_as_evidence(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        link = await session.scalar(
            select(InvoiceSourceLink).where(
                InvoiceSourceLink.link_type == "payment_confirmation"
            )
        )
        assert link is not None
        assert link.evidence_locator == "razorpay:evt_1"
        assert link.confidence == 1.0

    async def test_partial_payment_leaves_the_invoice_open(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(amount=1_000_000), event_id="evt_1")
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        await session.refresh(invoice)
        assert invoice.payment_state == PaymentState.LIKELY_UNPAID
        assert invoice.balance_paise == 3_000_000

    async def test_matches_on_customer_email_and_amount_without_a_note(
        self, session, workspace, owner
    ):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(invoice_number=None), event_id="evt_1")
        result = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        assert result.match_method == "customer_email+amount"
        assert result.matched_invoice_id == invoice.id

    async def test_ambiguous_amount_is_not_guessed(self, session, workspace, owner):
        """Two identical open invoices must not be resolved by guessing."""
        await seed_invoice(session, workspace, owner, number="INV-A")
        customer = await session.scalar(select(Customer))
        twin = Invoice(
            workspace_id=workspace.id, owner_id=owner.id, customer_id=customer.id,
            invoice_number="INV-B", normalized_number="INV-B",
            amount_paise=4_000_000, balance_paise=4_000_000,
            due_date=date(2026, 7, 20), payment_state=str(PaymentState.LIKELY_UNPAID),
            dedupe_key="twin-key",
        )
        session.add(twin)
        await session.flush()

        event = parse_webhook_payload(captured_payload(invoice_number=None), event_id="evt_1")
        result = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        assert result.matched_invoice_id is None
        assert "no invoice matched" in result.detail

    async def test_unmatched_event_is_retained_for_later(self, session, workspace, owner):
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        result = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        assert result.accepted and result.matched_invoice_id is None
        stored = await session.scalar(select(PaymentEvent))
        assert stored.provider == PaymentProvider.RAZORPAY
        assert stored.invoice_id is None

    async def test_pending_events_reconcile_once_the_invoice_appears(
        self, session, workspace, owner
    ):
        """A confirmation often arrives before the invoice is extracted from Gmail."""
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        _, invoice = await seed_invoice(session, workspace, owner)

        results = await razorpay_sync.reconcile_pending(
            session, workspace_id=workspace.id, owner_id=owner.id, settings=settings()
        )
        assert len(results) == 1
        await session.refresh(invoice)
        assert invoice.payment_state == PaymentState.CONFIRMED_PAID

    async def test_failed_payment_does_not_confirm(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        payload = captured_payload()
        payload["event"] = "payment.failed"
        event = parse_webhook_payload(payload, event_id="evt_fail")
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        await session.refresh(invoice)
        assert invoice.payment_state != PaymentState.CONFIRMED_PAID

    async def test_refund_reopens_the_invoice(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id,
            event=parse_webhook_payload(captured_payload(), event_id="evt_pay"),
            settings=settings(),
        )
        await session.refresh(invoice)
        assert invoice.payment_state == PaymentState.CONFIRMED_PAID

        refund = {
            "event": "refund.created",
            "payload": {
                "refund": {"entity": {"id": "rfnd_1", "amount": 4_000_000,
                                      "payment_id": "pay_TEST0001", "currency": "INR"}}
            },
        }
        await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id,
            event=parse_webhook_payload(refund, event_id="evt_refund"),
            settings=settings(),
        )
        await session.refresh(invoice)
        assert invoice.payment_state != PaymentState.CONFIRMED_PAID


class TestIdempotency:
    async def test_replayed_webhook_creates_one_event(self, session, workspace, owner):
        """Razorpay retries on any non-2xx, so duplicate delivery is the normal case."""
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")

        first = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )
        second = await razorpay_sync.ingest_event(
            session, workspace_id=workspace.id, owner_id=owner.id, event=event,
            settings=settings(),
        )

        assert first.duplicate is False
        assert second.duplicate is True
        assert await session.scalar(select(func.count(PaymentEvent.id))) == 1

    async def test_replay_does_not_double_count_the_payment(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(amount=2_000_000), event_id="evt_1")
        for _ in range(3):
            await razorpay_sync.ingest_event(
                session, workspace_id=workspace.id, owner_id=owner.id, event=event,
                settings=settings(),
            )
        await session.refresh(invoice)
        assert invoice.amount_paid_paise == 2_000_000
        assert invoice.balance_paise == 2_000_000

    async def test_replay_does_not_duplicate_audit_or_evidence(self, session, workspace, owner):
        _, invoice = await seed_invoice(session, workspace, owner)
        event = parse_webhook_payload(captured_payload(), event_id="evt_1")
        for _ in range(3):
            await razorpay_sync.ingest_event(
                session, workspace_id=workspace.id, owner_id=owner.id, event=event,
                settings=settings(),
            )
        assert await session.scalar(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.event_type == "razorpay.event_reconciled"
            )
        ) == 1
        assert await session.scalar(select(func.count(InvoiceSourceLink.id))) == 1
