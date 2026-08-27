"""Incremental Gmail sync and the scoped-resync fallback when the history id expires."""

from __future__ import annotations

from sqlalchemy import func, select

from app.enums import SyncMode, SyncStatus
from app.models import Invoice, SourceMessage, SyncRun
from tests.conftest import FakeGmailTransport
from tests.test_gmail_ingest import UNRELATED_MESSAGE, invoice_message, run_pipeline


def history_record(message_id: str) -> dict:
    return {"id": "h1", "messagesAdded": [{"message": {"id": message_id}}]}


class TestIncrementalSync:
    async def test_uses_the_stored_history_id(self, session, gmail_account, acme_invoice_pdf):
        gmail_account.last_history_id = "1000"
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            history=[history_record("msg-invoice")],
            profile_history_id="1200",
        )
        outcome = await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert outcome.mode == SyncMode.INCREMENTAL
        assert outcome.status == SyncStatus.COMPLETED
        assert outcome.start_history_id == "1000"
        assert outcome.history_fallback_used is False
        assert outcome.invoices_created == 1

    async def test_cursor_advances_only_after_success(
        self, session, gmail_account, acme_invoice_pdf
    ):
        gmail_account.last_history_id = "1000"
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            history=[history_record("msg-invoice")],
            profile_history_id="1200",
        )
        await run_pipeline(session, gmail_account, transport, mode="incremental")
        assert gmail_account.last_history_id == "1200"

    async def test_no_new_history_is_a_clean_no_op(self, session, gmail_account):
        gmail_account.last_history_id = "1000"
        transport = FakeGmailTransport([], history=[], profile_history_id="1000")
        outcome = await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert outcome.status == SyncStatus.COMPLETED
        assert outcome.messages_listed == 0
        assert outcome.invoices_created == 0

    async def test_incremental_still_ignores_unrelated_mail(self, session, gmail_account):
        gmail_account.last_history_id = "1000"
        transport = FakeGmailTransport(
            [UNRELATED_MESSAGE], history=[history_record("msg-lunch")], profile_history_id="1100"
        )
        outcome = await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert outcome.messages_ignored == 1
        assert transport.full_fetches == []
        assert await session.scalar(select(func.count(Invoice.id))) == 0


class TestHistoryExpiryFallback:
    """Gmail retains history for a limited window; an expired cursor is expected, not fatal."""

    async def test_expired_history_falls_back_to_a_scoped_resync(
        self, session, gmail_account, acme_invoice_pdf
    ):
        gmail_account.last_history_id = "999"
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            history_expired=True,
            profile_history_id="3000",
        )
        outcome = await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert outcome.status == SyncStatus.COMPLETED
        assert outcome.history_fallback_used is True
        assert outcome.mode == SyncMode.FALLBACK_RESYNC
        assert any("history id rejected" in note for note in outcome.notes)
        assert gmail_account.last_history_id == "3000", "the cursor must be repaired"

    async def test_the_fallback_is_date_scoped_not_a_full_mailbox_read(
        self, session, gmail_account, acme_invoice_pdf
    ):
        """An expired cursor means "we may have missed a few days", not "re-read everything"."""
        gmail_account.last_history_id = "999"
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            history_expired=True,
        )
        await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert transport.list_calls, "the fallback must issue a scoped list query"
        query = transport.list_calls[0]
        assert "after:" in query
        assert "-in:spam" in query and "-in:trash" in query

    async def test_the_fallback_run_is_recorded_for_audit(
        self, session, gmail_account, acme_invoice_pdf
    ):
        gmail_account.last_history_id = "999"
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)],
            attachments={"att-acme": acme_invoice_pdf},
            history_expired=True,
        )
        await run_pipeline(session, gmail_account, transport, mode="incremental")

        run = await session.scalar(select(SyncRun))
        assert run.history_fallback_used is True
        assert run.mode == SyncMode.FALLBACK_RESYNC
        assert run.status == SyncStatus.COMPLETED

    async def test_missing_history_id_also_uses_the_scoped_resync(
        self, session, gmail_account, acme_invoice_pdf
    ):
        assert gmail_account.last_history_id is None
        transport = FakeGmailTransport(
            [invoice_message(acme_invoice_pdf)], attachments={"att-acme": acme_invoice_pdf}
        )
        outcome = await run_pipeline(session, gmail_account, transport, mode="incremental")

        assert outcome.status == SyncStatus.COMPLETED
        assert any("no stored history id" in note for note in outcome.notes)
        assert outcome.invoices_created == 1

    async def test_fallback_after_a_backfill_does_not_duplicate_the_ledger(
        self, session, gmail_account, acme_invoice_pdf
    ):
        """The scoped resync re-lists messages already seen; they must collapse, not double."""
        messages = [invoice_message(acme_invoice_pdf)]
        attachments = {"att-acme": acme_invoice_pdf}
        await run_pipeline(session, gmail_account, FakeGmailTransport(messages, attachments=attachments))

        gmail_account.last_history_id = "999"
        outcome = await run_pipeline(
            session,
            gmail_account,
            FakeGmailTransport(messages, attachments=attachments, history_expired=True),
            mode="incremental",
        )

        assert outcome.history_fallback_used is True
        assert outcome.invoices_created == 0
        assert await session.scalar(select(func.count(Invoice.id))) == 1
        assert await session.scalar(select(func.count(SourceMessage.id))) == 1
