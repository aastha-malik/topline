from __future__ import annotations

import unittest
import uuid
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app import enums
from app.agent_layer import (
    models as _agent_models,  # noqa: F401 - registers shared metadata
)
from app.agent_layer.domain import AuditEvent, PaymentState, ReminderState
from app.agent_layer.errors import NotFoundError
from app.agent_layer.repository import SqlAlchemyAgentRepository
from app.agent_layer.service import AgentOrchestrator
from app.db import Base
from app.models import (
    ActivityLog,
    Customer,
    GmailAccount,
    Invoice,
    InvoiceSourceLink,
    SourceMessage,
    User,
    Workspace,
)


class AgentRepositoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with self.sessions() as session:
            workspace = Workspace(
                name="Northstar",
                business_name="Northstar Components",
                sender_name="Aastha",
            )
            session.add(workspace)
            await session.flush()
            owner = User(
                workspace_id=workspace.id,
                email="owner@example.com",
                name="Aastha",
                role="owner",
            )
            session.add(owner)
            await session.flush()
            gmail = GmailAccount(
                workspace_id=workspace.id,
                user_id=owner.id,
                email_address="owner@gmail.com",
                granted_scopes=["gmail.readonly", "gmail.send"],
                status=enums.AccountStatus.CONNECTED.value,
                connected_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            customer = Customer(
                workspace_id=workspace.id,
                owner_id=owner.id,
                name="Acme Retail",
                primary_email="finance@acme.example",
                alt_emails=[],
                match_confidence=0.99,
                match_method=enums.CustomerMatchMethod.EMAIL_EXACT.value,
            )
            session.add_all([gmail, customer])
            await session.flush()
            message = SourceMessage(
                workspace_id=workspace.id,
                gmail_account_id=gmail.id,
                gmail_message_id="gmail-message-1",
                gmail_thread_id="gmail-source-thread-1",
                internal_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
                from_email=customer.primary_email,
                to_emails=[gmail.email_address],
                cc_emails=[],
                subject="Invoice AC-1042",
                snippet="Please find AC-1042. PO 88 is included for accounts payable.",
                label_ids=["INBOX"],
                is_finance_relevant=True,
                relevance_reasons=[{"rule": "invoice_number"}],
                processing_state=enums.MessageProcessingState.EXTRACTED.value,
            )
            session.add(message)
            await session.flush()
            invoice = Invoice(
                workspace_id=workspace.id,
                owner_id=owner.id,
                customer_id=customer.id,
                invoice_number="AC-1042",
                normalized_number="AC-1042",
                amount_paise=4_000_000,
                amount_paid_paise=0,
                balance_paise=4_000_000,
                currency="INR",
                issued_date=date(2026, 6, 20),
                due_date=date(2026, 7, 20),
                payment_state=enums.PaymentState.LIKELY_UNPAID.value,
                reminder_state=enums.ReminderState.READY_FOR_REMINDER.value,
                evidence_strength=enums.EvidenceStrength.GMAIL_INFERRED.value,
                missing_fields=[],
                source_message_id=message.id,
                dedupe_key="invoice:ac-1042",
            )
            session.add(invoice)
            await session.flush()
            session.add(
                InvoiceSourceLink(
                    workspace_id=workspace.id,
                    invoice_id=invoice.id,
                    source_message_id=message.id,
                    link_type=enums.LinkType.INVOICE_MENTION.value,
                    evidence_snippet="Invoice AC-1042 references PO 88.",
                    evidence_locator="email:snippet",
                    evidence_hash="evidence-ac-1042",
                    confidence=0.99,
                )
            )
            await session.commit()
            self.owner_id = str(owner.id)
            self.customer_id = str(customer.id)
            self.invoice_id = str(invoice.id)

        self.repo = SqlAlchemyAgentRepository(session_factory=self.sessions)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_dossier_reads_live_customer_invoice_and_source_link(self):
        service = AgentOrchestrator(repository=self.repo, mail=object(), agent=object())
        dossier = await service.get_customer_dossier(
            owner_id=self.owner_id,
            customer_id=self.customer_id,
            invoice_ids=[self.invoice_id],
            as_of=date(2026, 8, 27),
        )
        self.assertEqual(dossier.customer.email, "finance@acme.example")
        self.assertEqual(dossier.invoices[0].invoice_number, "AC-1042")
        self.assertTrue(any("PO 88" in evidence.excerpt for evidence in dossier.evidence))
        self.assertTrue(
            any(reference["kind"] == "source_message" for reference in dossier.source_references)
        )
        self.assertIn("Payment is not confirmed", dossier.recommendation_reason)

    async def test_daily_queue_persists_items_and_get_digest_item_scopes_to_owner(self):
        service = AgentOrchestrator(repository=self.repo, mail=object(), agent=object())

        queue = await service.get_daily_queue(
            owner_id=self.owner_id, run_date=date(2026, 8, 27)
        )

        self.assertEqual(len(queue.items), 1)
        item = queue.items[0]
        self.assertEqual(item.customer_name, "Acme Retail")

        fetched = await self.repo.get_digest_item(self.owner_id, item.id)
        self.assertEqual(fetched.id, item.id)
        self.assertEqual(tuple(fetched.invoice_ids), (self.invoice_id,))

        with self.assertRaises(NotFoundError):
            await self.repo.get_digest_item(str(uuid.uuid4()), item.id)

    async def test_pause_and_audit_update_canonical_ledger_tables(self):
        await self.repo.pause_invoices(
            self.owner_id,
            [self.invoice_id],
            payment_state=PaymentState.PAYMENT_CLAIMED.value,
            reason="Customer says payment was already made",
        )
        await self.repo.append_audit(
            AuditEvent(
                owner_id=self.owner_id,
                event_type="customer_reply_decided",
                actor_type="customer",
                actor_id="gmail-reply-1",
                entity_type="invoice",
                entity_id=self.invoice_id,
                decision={"automatic_customer_reply_sent": False},
            )
        )
        async with self.sessions() as session:
            invoice = await session.get(Invoice, uuid.UUID(self.invoice_id))
            audit = await session.scalar(
                sa.select(ActivityLog).where(ActivityLog.entity_id == self.invoice_id)
            )
        self.assertEqual(invoice.payment_state, PaymentState.PAYMENT_CLAIMED.value)
        self.assertEqual(invoice.reminder_state, ReminderState.PAUSED.value)
        self.assertTrue(invoice.manually_paused)
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_type, enums.ActorType.PROVIDER.value)


if __name__ == "__main__":
    unittest.main()
