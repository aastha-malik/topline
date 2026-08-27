from __future__ import annotations

import copy
import unittest
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from app.agent_layer.domain import (
    AuditEvent,
    BrandProfile,
    CustomerRecord,
    CustomerReplyDecision,
    CustomerReplyIntent,
    DigestItemRecord,
    DigestRecord,
    DraftRecord,
    DraftStatus,
    EvidenceRecord,
    InvoiceRecord,
    MailReceipt,
    OwnerAction,
    OwnerActionKind,
    OwnerCommandDecision,
    OwnerProfile,
    PaymentState,
    ReminderDraftDecision,
    ReminderOutcome,
    ReminderState,
    ReviewTaskRecord,
)
from app.agent_layer.errors import ApprovalRequiredError, NotFoundError
from app.agent_layer.scheduler import register_daily_cycle_job
from app.agent_layer.service import AgentOrchestrator
from app.services.pdf import extract_pdf_text


def uid() -> str:
    return str(uuid.uuid4())


class LedgerFixtureRepository:
    """In-memory implementation of the canonical ledger/agent repository contract."""

    def __init__(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures"
        acme_pdf = extract_pdf_text(
            (fixture_dir / "invoice_acme.pdf").read_bytes(), enable_ocr=False
        ).text
        nova_pdf = extract_pdf_text(
            (fixture_dir / "invoice_nova.pdf").read_bytes(), enable_ocr=False
        ).text
        self.owner = OwnerProfile(id=uid(), email="owner@example.com", gmail_address="owner@gmail.com")
        self.brand = BrandProfile(
            business_name="Northstar Components",
            sender_name="Aastha",
            primary_color="#155EEF",
            reply_to="accounts@northstar.example",
        )
        self.acme = CustomerRecord(
            id=uid(),
            owner_id=self.owner.id,
            name="Acme Traders Pvt Ltd",
            email="ap@acmetraders.in",
        )
        self.nova = CustomerRecord(
            id=uid(),
            owner_id=self.owner.id,
            name="Nova Foods LLP",
            email="accounts@novafoods.co.in",
        )
        self.customers = {self.acme.id: self.acme, self.nova.id: self.nova}
        self.invoices = {
            "acme": InvoiceRecord(
                id=uid(),
                owner_id=self.owner.id,
                customer_id=self.acme.id,
                invoice_number="INV-2026-0114",
                amount_paise=4_000_000,
                balance_paise=4_000_000,
                currency="INR",
                issued_date=date(2026, 7, 5),
                due_date=date(2026, 7, 20),
                payment_state=PaymentState.LIKELY_UNPAID,
                reminder_state=ReminderState.READY_FOR_REMINDER,
                source_message_id=uid(),
                source_attachment_id=uid(),
            ),
            "nova": InvoiceRecord(
                id=uid(),
                owner_id=self.owner.id,
                customer_id=self.nova.id,
                invoice_number="TI/26-27/0042",
                amount_paise=525_050,
                balance_paise=525_050,
                currency="INR",
                issued_date=date(2026, 6, 1),
                due_date=date(2026, 6, 16),
                payment_state=PaymentState.LIKELY_UNPAID,
                reminder_state=ReminderState.READY_FOR_REMINDER,
                source_message_id=uid(),
                source_attachment_id=uid(),
            ),
            "paid": InvoiceRecord(
                id=uid(),
                owner_id=self.owner.id,
                customer_id=self.acme.id,
                invoice_number="AC-1001",
                amount_paise=100_000,
                balance_paise=0,
                currency="INR",
                issued_date=date(2026, 5, 1),
                due_date=date(2026, 5, 31),
                payment_state=PaymentState.CONFIRMED_PAID,
                reminder_state=ReminderState.PAUSED,
                source_message_id=uid(),
            ),
        }
        now = datetime(2026, 8, 20, tzinfo=timezone.utc)
        self.evidence = {
            self.acme.id: [
                EvidenceRecord(
                    id=uid(),
                    kind="invoice_document",
                    excerpt=acme_pdf,
                    source_date=now,
                    source_message_id=uid(),
                )
            ],
            self.nova.id: [
                EvidenceRecord(
                    id=uid(),
                    kind="invoice_document",
                    excerpt=nova_pdf,
                    source_date=now,
                    source_message_id=uid(),
                )
            ],
        }
        self.digests: dict[str, DigestRecord] = {}
        self.items: dict[str, DigestItemRecord] = {}
        self.drafts: dict[str, DraftRecord] = {}
        self.tasks: dict[str, ReviewTaskRecord] = {}
        self.audit: list[AuditEvent] = []

    async def get_owner(self, owner_id: str) -> OwnerProfile:
        if owner_id != self.owner.id:
            raise NotFoundError("Owner not found")
        return self.owner

    async def list_connected_owner_ids(self):
        return [self.owner.id]

    async def get_brand(self, owner_id: str) -> BrandProfile:
        await self.get_owner(owner_id)
        return self.brand

    async def list_actionable_invoices(self, owner_id: str, run_date: date):
        await self.get_owner(owner_id)
        # The fixture intentionally returns a paid row too; the service must re-check it.
        return list(self.invoices.values())

    async def get_customer(self, owner_id: str, customer_id: str) -> CustomerRecord:
        await self.get_owner(owner_id)
        customer = self.customers.get(customer_id)
        if customer is None:
            raise NotFoundError("Customer not found")
        return customer

    async def list_customer_invoices(self, owner_id: str, customer_id: str):
        await self.get_customer(owner_id, customer_id)
        return [invoice for invoice in self.invoices.values() if invoice.customer_id == customer_id]

    async def list_finance_evidence(self, owner_id: str, customer_id: str, invoice_ids):
        await self.get_customer(owner_id, customer_id)
        return list(self.evidence.get(customer_id, []))

    async def list_prior_reminders(self, owner_id: str, customer_id: str, invoice_ids):
        invoice_set = set(invoice_ids)
        return [
            ReminderOutcome(
                id=draft.id,
                draft_id=draft.id,
                sent_at=draft.sent_at,
                tone=draft.tone,
                outcome=None,
                status=str(draft.status),
            )
            for draft in self.drafts.values()
            if draft.owner_id == owner_id
            and draft.customer_id == customer_id
            and invoice_set.intersection(draft.invoice_ids)
        ]

    async def get_or_create_digest(self, owner_id: str, run_date: date):
        for digest in self.digests.values():
            if digest.owner_id == owner_id and digest.run_date == run_date:
                return digest
        digest = DigestRecord(id=uid(), owner_id=owner_id, run_date=run_date)
        self.digests[digest.id] = digest
        return digest

    async def save_digest(self, digest: DigestRecord) -> None:
        self.digests[digest.id] = digest

    async def add_digest_item(self, item: DigestItemRecord) -> None:
        self.items[item.id] = item

    async def get_digest(self, owner_id: str, digest_id: str):
        digest = self.digests.get(digest_id)
        if digest is None or digest.owner_id != owner_id:
            raise NotFoundError("Digest not found")
        return digest

    async def get_digest_by_thread(self, owner_id: str, gmail_thread_id: str):
        return next(
            (
                digest
                for digest in self.digests.values()
                if digest.owner_id == owner_id and digest.gmail_thread_id == gmail_thread_id
            ),
            None,
        )

    async def list_digest_items(self, owner_id: str, digest_id: str):
        await self.get_digest(owner_id, digest_id)
        return sorted(
            [item for item in self.items.values() if item.digest_id == digest_id],
            key=lambda item: item.item_number,
        )

    async def save_digest_item(self, item: DigestItemRecord) -> None:
        self.items[item.id] = item

    async def create_draft(self, draft: DraftRecord) -> None:
        self.drafts[draft.id] = draft

    async def get_draft(self, owner_id: str, draft_id: str):
        draft = self.drafts.get(draft_id)
        if draft is None or draft.owner_id != owner_id:
            raise NotFoundError("Draft not found")
        return draft

    async def list_drafts(self, owner_id: str, digest_id: str | None = None):
        return sorted(
            [
                draft
                for draft in self.drafts.values()
                if draft.owner_id == owner_id and (digest_id is None or draft.digest_id == digest_id)
            ],
            key=lambda draft: draft.draft_number,
        )

    async def save_draft(self, draft: DraftRecord) -> None:
        self.drafts[draft.id] = draft

    async def claim_approved_draft_for_send(
        self, owner_id: str, draft_id: str, approved_content_hash: str
    ) -> bool:
        draft = await self.get_draft(owner_id, draft_id)
        if (
            draft.status != DraftStatus.APPROVED
            or draft.approved_content_hash != approved_content_hash
        ):
            return False
        draft.status = DraftStatus.SENDING
        return True

    async def find_draft_by_customer_thread(self, owner_id: str, gmail_thread_id: str):
        return next(
            (
                draft
                for draft in self.drafts.values()
                if draft.owner_id == owner_id and draft.customer_thread_id == gmail_thread_id
            ),
            None,
        )

    async def create_review_task(self, task: ReviewTaskRecord) -> None:
        self.tasks[task.id] = task

    async def pause_invoices(
        self, owner_id: str, invoice_ids, *, payment_state: str | None, reason: str
    ) -> None:
        for invoice in self.invoices.values():
            if invoice.owner_id == owner_id and invoice.id in set(invoice_ids):
                object.__setattr__(invoice, "reminder_state", ReminderState.PAUSED)
                if payment_state:
                    object.__setattr__(invoice, "payment_state", PaymentState(payment_state))
                object.__setattr__(invoice, "payment_claim_note", reason)

    async def mark_reminder_sent(self, owner_id: str, invoice_ids, sent_at) -> None:
        # Canonical fixtures do not expose reminder_count, but this call proves the
        # send path updates the ledger-owned reminder outcome fields.
        self.last_reminder_update = {
            "owner_id": owner_id,
            "invoice_ids": tuple(invoice_ids),
            "sent_at": sent_at,
        }

    async def append_audit(self, event: AuditEvent) -> None:
        self.audit.append(copy.deepcopy(event))


class FakeMailGateway:
    def __init__(self) -> None:
        self.owner_digests: list[dict] = []
        self.owner_replies: list[dict] = []
        self.customer_emails: list[dict] = []
        self.owner_notifications: list[dict] = []

    async def send_owner_digest(self, **kwargs):
        self.owner_digests.append(kwargs)
        return MailReceipt(message_id=uid(), thread_id=f"digest-thread-{len(self.owner_digests)}")

    async def reply_to_owner_thread(self, **kwargs):
        self.owner_replies.append(kwargs)
        return MailReceipt(message_id=uid(), thread_id=kwargs["thread_id"])

    async def send_customer_email(self, **kwargs):
        self.customer_emails.append(kwargs)
        return MailReceipt(
            message_id=uid(),
            thread_id=kwargs["thread_id"] or f"customer-thread-{len(self.customer_emails)}",
            provider_payload={"labelIds": ["SENT"]},
        )

    async def notify_owner(self, **kwargs):
        self.owner_notifications.append(kwargs)
        return MailReceipt(message_id=uid(), thread_id=kwargs["thread_id"] or uid())


class ContextAwareFakeAgent:
    model_name = "gemini-test-flash"

    def __init__(self) -> None:
        self.ambiguous = False

    async def parse_owner_command(self, *, owner_text: str, digest_items):
        if self.ambiguous or "maybe" in owner_text.lower():
            return OwnerCommandDecision(
                actions=(),
                confidence=0.35,
                explanation="The customer reference is ambiguous.",
                ambiguous=True,
                model_name=self.model_name,
            )
        actions = tuple(
            OwnerAction(
                action=OwnerActionKind.DRAFT,
                confidence=0.99,
                reason=f"Owner requested a reminder for {item.customer_name}",
                item_number=item.item_number,
                customer_id=item.customer_id,
                tone="firm" if item.item_number == 1 else "polite",
            )
            for item in digest_items
        )
        return OwnerCommandDecision(
            actions=actions,
            confidence=0.99,
            explanation="Each named digest item was matched exactly.",
            model_name=self.model_name,
        )

    async def draft_reminder(self, *, dossier_payload: dict, tone: str, owner_note: str | None):
        customer = dossier_payload["customer"]
        invoice = dossier_payload["invoices"][0]
        evidence = dossier_payload["evidence"][0]
        return ReminderDraftDecision(
            subject=f"Follow-up on invoice {invoice['invoice_number']}",
            text_body=(
                f"Hello {customer['name']},\n\nThis is a {tone} follow-up on invoice "
                f"{invoice['invoice_number']} for INR {invoice['balance_paise'] / 100:,.2f}. "
                f"Context noted: {evidence['excerpt']}"
            ),
            rationale=(
                f"Uses {customer['name']}'s actual invoice and recent finance message; "
                "it does not state payment failure as confirmed."
            ),
            tone=tone,
            confidence=0.99,
            cited_source_ids=(evidence["id"],),
            model_name=self.model_name,
        )

    async def classify_customer_reply(self, *, new_reply_text: str, customer, invoices):
        return CustomerReplyDecision(
            intent=CustomerReplyIntent.UNCLEAR,
            confidence=0.60,
            explanation="The reply needs owner interpretation.",
            requires_review=True,
            cited_invoice_ids=tuple(invoice.id for invoice in invoices),
            model_name=self.model_name,
        )


class AgentWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = LedgerFixtureRepository()
        self.mail = FakeMailGateway()
        self.agent = ContextAwareFakeAgent()
        self.service = AgentOrchestrator(
            repository=self.repo, mail=self.mail, agent=self.agent
        )
        self.cycle = await self.service.run_daily_cycle(
            owner_id=self.repo.owner.id, run_date=date(2026, 8, 27)
        )

    async def create_two_drafts(self):
        result = await self.service.process_owner_reply(
            owner_id=self.repo.owner.id,
            digest_id=self.cycle.digest.id,
            body="Acme ko firm bhejo, Nova ko polite bhejo",
            actor_message_id="owner-command-1",
        )
        self.assertEqual(result.kind, "drafts_created")
        self.assertEqual(len(result.drafts), 2)
        return result.drafts

    async def test_daily_cycle_selects_only_actionable_and_sends_one_owner_digest(self):
        self.assertEqual(len(self.cycle.items), 2)
        selected_ids = {invoice_id for item in self.cycle.items for invoice_id in item.invoice_ids}
        self.assertNotIn(self.repo.invoices["paid"].id, selected_ids)
        self.assertEqual(len(self.mail.owner_digests), 1)
        self.assertEqual(self.cycle.digest.gmail_thread_id, "digest-thread-1")

    async def test_scheduler_registers_one_9am_ist_job(self):
        class Scheduler:
            call = None

            def add_job(self, function, **kwargs):
                self.call = (function, kwargs)

        scheduler = Scheduler()
        register_daily_cycle_job(
            scheduler=scheduler,
            service=self.service,
            list_connected_owner_ids=self.repo.list_connected_owner_ids,
        )
        _function, kwargs = scheduler.call
        self.assertEqual(kwargs["id"], "topline-agent-daily-cycle")
        self.assertEqual(kwargs["hour"], 9)
        self.assertEqual(kwargs["timezone"], "Asia/Kolkata")
        self.assertEqual(kwargs["max_instances"], 1)

    async def test_customer_specific_drafts_use_actual_dossier_context(self):
        first, second = await self.create_two_drafts()
        self.assertIn("INV-2026-0114", first.text_body)
        self.assertIn("Acme Traders", first.text_body)
        self.assertIn("TI/26-27/0042", second.text_body)
        self.assertIn("Nova Foods", second.text_body)
        self.assertNotEqual(first.text_body, second.text_body)
        self.assertNotEqual(first.rationale, second.rationale)

    async def test_ambiguous_owner_reply_never_sends(self):
        self.agent.ambiguous = True
        result = await self.service.process_owner_reply(
            owner_id=self.repo.owner.id,
            digest_id=self.cycle.digest.id,
            body="Maybe usko bhej do",
            actor_message_id="owner-command-ambiguous",
        )
        self.assertEqual(result.kind, "clarification_required")
        self.assertEqual(len(self.mail.customer_emails), 0)
        self.assertEqual(len(self.repo.tasks), 1)
        self.assertIn(
            "owner_command_needs_clarification", [event.event_type for event in self.repo.audit]
        )

    async def test_unapproved_draft_cannot_send_and_block_is_audited(self):
        drafts = await self.create_two_drafts()
        with self.assertRaises(ApprovalRequiredError):
            await self.service.send_approved_draft(
                owner_id=self.repo.owner.id,
                draft_id=drafts[0].id,
                actor_id=self.repo.owner.id,
            )
        self.assertEqual(len(self.mail.customer_emails), 0)
        self.assertEqual(self.repo.drafts[drafts[0].id].status, DraftStatus.PENDING)
        self.assertIn("draft_send_blocked", [event.event_type for event in self.repo.audit])

    async def test_send_1_approves_and_sends_only_draft_1(self):
        first, second = await self.create_two_drafts()
        result = await self.service.process_owner_reply(
            owner_id=self.repo.owner.id,
            digest_id=self.cycle.digest.id,
            body="send 1",
            actor_message_id="owner-send-command-1",
        )
        self.assertEqual(result.sent_draft_ids, (first.id,))
        self.assertEqual(self.repo.drafts[first.id].status, DraftStatus.SENT)
        self.assertEqual(self.repo.drafts[second.id].status, DraftStatus.PENDING)
        self.assertEqual(len(self.mail.customer_emails), 1)
        self.assertEqual(self.mail.customer_emails[0]["recipient"], first.customer_email)
        self.assertEqual(first.approval_source, "owner_email_command")

    async def test_already_paid_pauses_reminders_and_never_replies_to_customer(self):
        first, _second = await self.create_two_drafts()
        await self.service.process_owner_reply(
            owner_id=self.repo.owner.id,
            digest_id=self.cycle.digest.id,
            body="send 1",
            actor_message_id="owner-send-command-1",
        )
        customer_send_count = len(self.mail.customer_emails)
        result = await self.service.process_customer_reply(
            owner_id=self.repo.owner.id,
            gmail_thread_id=self.repo.drafts[first.id].customer_thread_id or "",
            body="Already paid on the 3rd. Please check.\n\n> Previous reminder",
            source_message_id="customer-reply-1",
        )
        invoice = self.repo.invoices["acme"]
        self.assertEqual(result.decision.intent, CustomerReplyIntent.ALREADY_PAID)
        self.assertEqual(invoice.payment_state, PaymentState.PAYMENT_CLAIMED)
        self.assertEqual(invoice.reminder_state, ReminderState.PAUSED)
        self.assertEqual(result.paused_invoice_ids, (invoice.id,))
        self.assertEqual(len(self.mail.customer_emails), customer_send_count)
        self.assertEqual(len(self.mail.owner_notifications), 1)

    async def test_audit_records_source_decision_approval_final_render_and_send_result(self):
        first, _second = await self.create_two_drafts()
        await self.service.process_owner_reply(
            owner_id=self.repo.owner.id,
            digest_id=self.cycle.digest.id,
            body="send 1",
            actor_message_id="owner-send-command-audit",
        )
        by_type = {event.event_type: event for event in self.repo.audit}
        for event_type in (
            "daily_item_selected",
            "owner_command_decided",
            "draft_created",
            "draft_approved",
            "draft_send_attempted",
            "draft_sent",
        ):
            self.assertIn(event_type, by_type)
        sent = next(
            event
            for event in self.repo.audit
            if event.event_type == "draft_sent" and event.entity_id == first.id
        )
        self.assertTrue(sent.source_evidence)
        self.assertIn("final_rendered_email", sent.decision)
        self.assertIn("send_result", sent.decision)
        self.assertTrue(sent.decision["send_result"]["ok"])
        self.assertEqual(sent.model_name, self.agent.model_name)
        self.assertEqual(sent.prompt_version, "reminder-draft-v1")


if __name__ == "__main__":
    unittest.main()
