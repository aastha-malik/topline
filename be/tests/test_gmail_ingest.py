"""End-to-end Gmail ingestion against a fake Gmail, including the unrelated-email case."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.config import Settings
from app.enums import (
    EffectiveState,
    MessageProcessingState,
    PaymentState,
    SyncStatus,
)
from app.models import (
    ActivityLog,
    Customer,
    Invoice,
    InvoiceSourceLink,
    SourceAttachment,
    SourceMessage,
    SyncRun,
)
from app.services.gmail import GmailClient
from app.services.ingest import IngestionPipeline
from tests.conftest import ACME_EMAIL, FakeGmailTransport, build_message

TODAY = date(2026, 8, 27)


def make_settings(**overrides) -> Settings:
    base = dict(
        token_encryption_key="unused-in-these-tests",
        relevance_threshold=40,
        default_payment_terms_days=15,
        reminder_cooldown_days=3,
        backfill_months=12,
        enable_ocr_fallback=True,
    )
    return Settings(_env_file=None, **{**base, **overrides})


def invoice_message(acme_pdf: bytes) -> dict:
    return build_message(
        message_id="msg-invoice",
        thread_id="thread-acme",
        subject="Tax Invoice INV-2026-0114 from Northwind",
        body="Hi, please find attached invoice INV-2026-0114 for Rs. 40,000. Due 20 Jul 2026.",
        attachments=[
            {"filename": "invoice_INV-2026-0114.pdf", "attachment_id": "att-acme", "size": len(acme_pdf)}
        ],
        days_ago=50,
    )


UNRELATED_MESSAGE = build_message(
    message_id="msg-lunch",
    thread_id="thread-lunch",
    from_addr="Rahul <rahul@friendsgroup.com>",
    subject="Team lunch on Friday?",
    body="Thinking of that new place near the office. Are you in? Bring the whole team.",
    days_ago=10,
)


async def run_pipeline(session, account, transport, settings=None, mode="backfill"):
    pipeline = IngestionPipeline(
        session,
        account=account,
        client=GmailClient(transport),
        settings=settings or make_settings(),
        today=TODAY,
    )
    return await (pipeline.run_backfill() if mode == "backfill" else pipeline.run_incremental())


class TestBackfill:
    async def test_invoice_email_becomes_a_ledger_row(self, session, gmail_account, acme_invoice_pdf):
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)], attachments={"att-acme": acme_invoice_pdf}
        )
        outcome = await run_pipeline(session, gmail_account, transport)

        assert outcome.status == SyncStatus.COMPLETED
        assert outcome.invoices_created == 1

        invoice = await session.scalar(select(Invoice))
        assert invoice.invoice_number == "INV-2026-0114"
        assert invoice.amount_paise == 4_000_000
        assert invoice.currency == "INR"
        assert invoice.due_date == date(2026, 7, 20)
        assert invoice.effective_state == EffectiveState.READY_FOR_REMINDER

        customer = await session.scalar(select(Customer))
        assert customer.primary_email == ACME_EMAIL
        assert customer.domain == "acmetraders.in"

    async def test_invoice_is_traceable_to_its_gmail_message_and_pdf(
        self, session, gmail_account, acme_invoice_pdf
    ):
        """Every extracted fact must point back at a source the owner can open."""
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)], attachments={"att-acme": acme_invoice_pdf}
        )
        await run_pipeline(session, gmail_account, transport)

        invoice = await session.scalar(select(Invoice))
        message = await session.scalar(select(SourceMessage))
        attachment = await session.scalar(select(SourceAttachment))

        assert invoice.source_message_id == message.id
        assert invoice.source_attachment_id == attachment.id
        assert message.gmail_message_id == "msg-invoice"
        assert message.gmail_thread_id == "thread-acme"

        links = (await session.scalars(select(InvoiceSourceLink))).all()
        assert links, "extraction must leave an evidence trail"
        assert any("INV-2026-0114" in (l.evidence_snippet or "") for l in links)
        for link in links:
            assert link.evidence_locator
            assert link.source_message_id == message.id

    async def test_pdf_text_is_extracted_without_ocr(
        self, session, gmail_account, acme_invoice_pdf
    ):
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)], attachments={"att-acme": acme_invoice_pdf}
        )
        await run_pipeline(session, gmail_account, transport)

        attachment = await session.scalar(select(SourceAttachment))
        assert attachment.extraction_status == "text_extracted"
        assert attachment.extraction_method == "pypdf"
        assert "INV-2026-0114" in attachment.extracted_text
        assert attachment.content_sha256

    async def test_history_id_is_stored_after_a_successful_run(
        self, session, gmail_account, acme_invoice_pdf
    ):
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            profile_history_id="5150",
        )
        outcome = await run_pipeline(session, gmail_account, transport)
        assert outcome.end_history_id == "5150"
        assert gmail_account.last_history_id == "5150"
        assert gmail_account.backfill_status == SyncStatus.COMPLETED


class TestUnrelatedEmailIsIgnored:
    """The required negative case: unrelated mail is skipped, logged, and never stored."""

    async def test_unrelated_email_creates_no_ledger_rows(self, session, gmail_account):
        transport = FakeGmailTransport([UNRELATED_MESSAGE])
        outcome = await run_pipeline(session, gmail_account, transport)

        assert outcome.messages_ignored == 1
        assert outcome.invoices_created == 0
        assert await session.scalar(select(func.count(Invoice.id))) == 0
        assert await session.scalar(select(func.count(Customer.id))) == 0

    async def test_unrelated_email_body_is_never_downloaded(self, session, gmail_account):
        """Cost and privacy: a message below threshold must not trigger a content fetch."""
        transport = FakeGmailTransport([UNRELATED_MESSAGE])
        await run_pipeline(session, gmail_account, transport)

        assert transport.metadata_fetches == ["msg-lunch"]
        assert transport.full_fetches == [], "ignored mail must not be fetched in full"
        assert transport.attachment_fetches == []

    async def test_ignore_decision_is_recorded_with_reasons(self, session, gmail_account):
        transport = FakeGmailTransport([UNRELATED_MESSAGE])
        await run_pipeline(session, gmail_account, transport)

        message = await session.scalar(select(SourceMessage))
        assert message.processing_state == MessageProcessingState.IGNORED
        assert message.is_finance_relevant is False
        assert message.body_text is None, "the body of ignored mail must not be retained"

        entry = await session.scalar(
            select(ActivityLog).where(ActivityLog.event_type == "gmail.message_ignored")
        )
        assert entry is not None
        assert entry.decision["score"] < entry.decision["threshold"]

    async def test_relevant_and_unrelated_mail_in_one_run(
        self, session, gmail_account, acme_invoice_pdf
    ):
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf), UNRELATED_MESSAGE],
            attachments={"att-acme": acme_invoice_pdf},
        )
        outcome = await run_pipeline(session, gmail_account, transport)

        assert outcome.messages_metadata_fetched == 2
        assert outcome.messages_content_fetched == 1
        assert outcome.messages_ignored == 1
        assert transport.full_fetches == ["msg-invoice"]
        assert await session.scalar(select(func.count(Invoice.id))) == 1


class TestIdempotency:
    async def test_rerunning_a_backfill_does_not_duplicate_anything(
        self, session, gmail_account, acme_invoice_pdf
    ):
        messages = [invoice_message(acme_invoice_pdf), UNRELATED_MESSAGE]
        attachments = {"att-acme": acme_invoice_pdf}

        first = await run_pipeline(
            session, gmail_account, FakeGmailTransport(messages, attachments=attachments)
        )
        counts_after_first = await _counts(session)

        second = await run_pipeline(
            session, gmail_account, FakeGmailTransport(messages, attachments=attachments)
        )
        counts_after_second = await _counts(session)

        assert first.invoices_created == 1
        assert second.invoices_created == 0, "a re-sync must not create a second invoice"
        assert counts_after_first == counts_after_second

    async def test_rerun_adds_only_a_new_sync_run_record_to_the_audit_log(
        self, session, gmail_account, acme_invoice_pdf
    ):
        """Each run is its own event, but the decisions inside it must not be re-logged."""
        messages = [invoice_message(acme_invoice_pdf), UNRELATED_MESSAGE]
        attachments = {"att-acme": acme_invoice_pdf}

        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))
        before = await _audit_counts(session)

        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))
        after = await _audit_counts(session)

        assert after["gmail.sync_completed"] == before["gmail.sync_completed"] + 1
        assert after["ledger.invoice_extracted"] == before["ledger.invoice_extracted"]
        assert after["gmail.message_ignored"] == before["gmail.message_ignored"]

    async def test_a_second_run_skips_already_decided_messages(
        self, session, gmail_account, acme_invoice_pdf
    ):
        messages = [invoice_message(acme_invoice_pdf), UNRELATED_MESSAGE]
        attachments = {"att-acme": acme_invoice_pdf}
        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))

        transport = FakeGmailTransport(messages, attachments=attachments)
        await run_pipeline(session, gmail_account, transport)

        assert transport.metadata_fetches == [], "already-decided messages must not refetch"
        assert transport.full_fetches == []

    async def test_audit_rows_are_not_duplicated(self, session, gmail_account, acme_invoice_pdf):
        messages = [invoice_message(acme_invoice_pdf)]
        attachments = {"att-acme": acme_invoice_pdf}
        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))
        invoice_events = await session.scalar(
            select(func.count(ActivityLog.id)).where(
                ActivityLog.event_type == "ledger.invoice_extracted"
            )
        )
        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))
        assert (
            await session.scalar(
                select(func.count(ActivityLog.id)).where(
                    ActivityLog.event_type == "ledger.invoice_extracted"
                )
            )
            == invoice_events
        )

    async def test_the_same_invoice_from_two_messages_collapses_to_one_row(
        self, session, gmail_account, acme_invoice_pdf
    ):
        """A forwarded or re-sent invoice PDF must not create a duplicate receivable."""
        resend = build_message(
            message_id="msg-invoice-resend",
            thread_id="thread-acme-2",
            subject="Re-sending: Tax Invoice INV-2026-0114",
            body="Resending invoice INV-2026-0114 for Rs. 40,000 as requested.",
            attachments=[
                {"filename": "invoice_INV-2026-0114.pdf", "attachment_id": "att-acme-2",
                 "size": len(acme_invoice_pdf)}
            ],
            days_ago=45,
        )
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf), resend],
            attachments={"att-acme": acme_invoice_pdf, "att-acme-2": acme_invoice_pdf},
        )
        outcome = await run_pipeline(session, gmail_account, transport)

        assert outcome.invoices_created == 1
        assert outcome.invoices_updated == 1
        assert await session.scalar(select(func.count(Invoice.id))) == 1


class TestPaymentClaimsPauseFollowUps:
    async def test_customer_saying_already_paid_pauses_reminders(
        self, session, gmail_account, acme_invoice_pdf
    ):
        claim = build_message(
            message_id="msg-claim",
            thread_id="thread-acme",
            subject="Re: Tax Invoice INV-2026-0114",
            body="Hi, we have already paid invoice INV-2026-0114 on the 3rd. UTR HDFC2026X8817.",
            days_ago=5,
        )
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf), claim],
            attachments={"att-acme": acme_invoice_pdf},
        )
        await run_pipeline(session, gmail_account, transport)

        invoice = await session.scalar(select(Invoice))
        assert invoice.payment_state == PaymentState.PAYMENT_CLAIMED
        assert invoice.effective_state == EffectiveState.PAYMENT_CLAIMED
        assert invoice.balance_paise == 4_000_000, "a claim must not zero the balance"

    async def test_dispute_hard_stops_the_invoice(
        self, session, gmail_account, acme_invoice_pdf
    ):
        dispute = build_message(
            message_id="msg-dispute",
            thread_id="thread-acme",
            subject="Re: Invoice INV-2026-0114",
            body="This invoice is wrong - we were overcharged for the retainer. Please revise.",
            days_ago=4,
        )
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf), dispute],
            attachments={"att-acme": acme_invoice_pdf},
        )
        await run_pipeline(session, gmail_account, transport)

        invoice = await session.scalar(select(Invoice))
        assert invoice.payment_state == PaymentState.DISPUTED
        assert invoice.dispute_note


class TestFailureHandling:
    async def test_one_bad_message_does_not_abort_the_run(
        self, session, gmail_account, acme_invoice_pdf
    ):
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)], attachments={"att-acme": acme_invoice_pdf}
        )
        broken = dict(transport.messages)
        broken["msg-broken"] = {"id": "msg-broken"}  # no payload at all
        transport.messages = broken

        outcome = await run_pipeline(session, gmail_account, transport)
        assert outcome.status == SyncStatus.COMPLETED
        assert outcome.invoices_created == 1

    async def test_a_failed_run_does_not_advance_the_history_cursor(
        self, session, gmail_account
    ):
        """The cursor must only move on success, or a crash silently skips messages."""
        gmail_account.last_history_id = "1000"

        class ExplodingTransport(FakeGmailTransport):
            def list_messages(self, **kwargs):
                raise RuntimeError("Gmail unavailable")

        outcome = await run_pipeline(session, gmail_account, ExplodingTransport([]))
        assert outcome.status == SyncStatus.FAILED
        assert gmail_account.last_history_id == "1000"

        run = await session.scalar(select(SyncRun))
        assert run.status == SyncStatus.FAILED
        assert "Gmail unavailable" in (run.error or "")


async def _counts(session) -> dict[str, int]:
    """Ledger entity counts. Excludes ActivityLog, which grows by one run record per sync."""
    out = {}
    for model in (Invoice, Customer, SourceMessage, SourceAttachment, InvoiceSourceLink):
        out[model.__name__] = await session.scalar(select(func.count(model.id)))
    return out


async def _audit_counts(session) -> dict[str, int]:
    rows = (
        await session.execute(
            select(ActivityLog.event_type, func.count(ActivityLog.id)).group_by(
                ActivityLog.event_type
            )
        )
    ).all()
    counts = {event: count for event, count in rows}
    for key in ("gmail.sync_completed", "ledger.invoice_extracted", "gmail.message_ignored"):
        counts.setdefault(key, 0)
    return counts
