from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import enums
from app.models import (
    Customer,
    GmailAccount,
    Invoice,
    InvoiceSourceLink,
    PaymentEvent,
    SourceAttachment,
    SourceMessage,
    User,
    Workspace,
)
from app.services.audit import record_event
from app.services.ledger import mark_invoices_paused

from .domain import (
    AuditEvent,
    BrandProfile,
    CustomerRecord,
    DigestItemRecord,
    DigestItemStatus,
    DigestRecord,
    DigestStatus,
    DraftRecord,
    DraftStatus,
    EvidenceRecord,
    InvoiceRecord,
    OwnerProfile,
    PaymentState,
    ReminderOutcome,
    ReminderState,
    ReviewTaskRecord,
)
from .errors import NotFoundError, UnsafeActionError
from .models import AgentDigest, AgentDigestItem, AgentDraft, AgentReviewTask


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)


class SqlAlchemyAgentRepository:
    """Direct adapter over the live ledger plus agent-owned workflow tables."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _user(self, session: AsyncSession, owner_id: str) -> User:
        row = await session.get(User, _uuid(owner_id))
        if row is None:
            raise NotFoundError("Owner not found")
        return row

    async def get_owner(self, owner_id: str) -> OwnerProfile:
        async with self._sessions() as session:
            user = await self._user(session, owner_id)
            gmail = await session.scalar(
                sa.select(GmailAccount)
                .where(
                    GmailAccount.user_id == user.id,
                    GmailAccount.status == enums.AccountStatus.CONNECTED.value,
                )
                .order_by(GmailAccount.connected_at.desc())
                .limit(1)
            )
        if gmail is None:
            raise NotFoundError("Owner has no connected Gmail account")
        return OwnerProfile(
            id=str(user.id),
            email=user.email,
            gmail_address=gmail.email_address,
            name=user.name or "",
        )

    async def list_connected_owner_ids(self) -> Sequence[str]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    sa.select(User.id)
                    .join(GmailAccount, GmailAccount.user_id == User.id)
                    .where(
                        User.role == "owner",
                        GmailAccount.status == enums.AccountStatus.CONNECTED.value,
                    )
                    .distinct()
                )
            ).all()
        return [str(value) for value in rows]

    async def get_brand(self, owner_id: str) -> BrandProfile:
        async with self._sessions() as session:
            user = await self._user(session, owner_id)
            workspace = await session.get(Workspace, user.workspace_id)
        if workspace is None:
            raise NotFoundError("Owner workspace not found")
        return BrandProfile(
            business_name=workspace.business_name or workspace.name,
            sender_name=workspace.sender_name or user.name or workspace.name,
            primary_color=workspace.primary_color,
            logo_url=workspace.logo_url,
            reply_to=workspace.reply_to,
        )

    async def list_actionable_invoices(
        self, owner_id: str, run_date: date
    ) -> Sequence[InvoiceRecord]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    sa.select(Invoice)
                    .where(
                        Invoice.owner_id == _uuid(owner_id),
                        Invoice.customer_id.is_not(None),
                        Invoice.payment_state == enums.PaymentState.LIKELY_UNPAID.value,
                        Invoice.reminder_state == enums.ReminderState.READY_FOR_REMINDER.value,
                        Invoice.balance_paise > 0,
                        Invoice.due_date.is_not(None),
                        Invoice.due_date <= run_date,
                        Invoice.manually_paused.is_(False),
                    )
                    .order_by(Invoice.due_date, Invoice.customer_id)
                )
            ).all()
        return [self._invoice(row) for row in rows]

    async def get_customer(self, owner_id: str, customer_id: str) -> CustomerRecord:
        async with self._sessions() as session:
            row = await session.get(Customer, _uuid(customer_id))
        if row is None or str(row.owner_id) != owner_id or row.is_archived:
            raise NotFoundError("Customer not found")
        return self._customer(row)

    async def list_customer_invoices(
        self, owner_id: str, customer_id: str
    ) -> Sequence[InvoiceRecord]:
        await self.get_customer(owner_id, customer_id)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    sa.select(Invoice)
                    .where(
                        Invoice.owner_id == _uuid(owner_id),
                        Invoice.customer_id == _uuid(customer_id),
                    )
                    .order_by(Invoice.due_date.desc())
                )
            ).all()
        return [self._invoice(row) for row in rows]

    async def list_finance_evidence(
        self, owner_id: str, customer_id: str, invoice_ids: Sequence[str]
    ) -> Sequence[EvidenceRecord]:
        customer = await self.get_customer(owner_id, customer_id)
        ids = [_uuid(value) for value in invoice_ids]
        if not ids:
            return []
        async with self._sessions() as session:
            user = await self._user(session, owner_id)
            # Never surface evidence for an invoice the owner does not own, even if the
            # caller passes its id.
            ids = list(
                (
                    await session.scalars(
                        sa.select(Invoice.id).where(
                            Invoice.owner_id == _uuid(owner_id), Invoice.id.in_(ids)
                        )
                    )
                ).all()
            )
            if not ids:
                return []
            linked_rows = (
                await session.execute(
                    sa.select(InvoiceSourceLink, SourceMessage, SourceAttachment, PaymentEvent)
                    .outerjoin(SourceMessage, InvoiceSourceLink.source_message_id == SourceMessage.id)
                    .outerjoin(
                        SourceAttachment,
                        InvoiceSourceLink.source_attachment_id == SourceAttachment.id,
                    )
                    .outerjoin(PaymentEvent, InvoiceSourceLink.payment_event_id == PaymentEvent.id)
                    .where(
                        InvoiceSourceLink.workspace_id == user.workspace_id,
                        InvoiceSourceLink.invoice_id.in_(ids),
                    )
                    .order_by(InvoiceSourceLink.created_at.desc())
                )
            ).all()
            message_rows = (
                await session.scalars(
                    sa.select(SourceMessage)
                    .where(
                        SourceMessage.workspace_id == user.workspace_id,
                        SourceMessage.is_finance_relevant.is_(True),
                        SourceMessage.from_email == customer.email.lower(),
                    )
                    .order_by(SourceMessage.internal_date.desc())
                    .limit(20)
                )
            ).all()

        evidence: list[EvidenceRecord] = []
        seen: set[str] = set()
        for link, message, attachment, payment in linked_rows:
            excerpt = (
                link.evidence_snippet
                or (message.snippet if message else None)
                or (message.body_text[:800] if message and message.body_text else None)
                or (attachment.extracted_text[:800] if attachment and attachment.extracted_text else None)
                or (payment.evidence_snippet if payment else None)
                or "Linked finance evidence"
            )
            evidence.append(
                EvidenceRecord(
                    id=str(link.id),
                    kind=link.link_type,
                    excerpt=excerpt,
                    source_date=(
                        (message.internal_date if message else None)
                        or (payment.observed_at if payment else None)
                        or link.created_at
                    ),
                    source_message_id=str(link.source_message_id) if link.source_message_id else None,
                    source_attachment_id=(
                        str(link.source_attachment_id) if link.source_attachment_id else None
                    ),
                    gmail_thread_id=message.gmail_thread_id if message else None,
                )
            )
            seen.add(str(link.source_message_id) if link.source_message_id else "")
        for message in message_rows:
            if str(message.id) in seen:
                continue
            excerpt = message.snippet or (message.body_text[:800] if message.body_text else "")
            if not excerpt:
                continue
            evidence.append(
                EvidenceRecord(
                    id=str(message.id),
                    kind="finance_message",
                    excerpt=excerpt,
                    source_date=message.internal_date or message.created_at,
                    source_message_id=str(message.id),
                    gmail_thread_id=message.gmail_thread_id,
                )
            )
        return evidence

    async def list_prior_reminders(
        self, owner_id: str, customer_id: str, invoice_ids: Sequence[str]
    ) -> Sequence[ReminderOutcome]:
        invoice_set = set(invoice_ids)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    sa.select(AgentDraft).where(
                        AgentDraft.owner_id == _uuid(owner_id),
                        AgentDraft.customer_id == _uuid(customer_id),
                    )
                )
            ).all()
        return [
            ReminderOutcome(
                id=str(row.id),
                draft_id=str(row.id),
                sent_at=row.sent_at,
                tone=row.tone,
                outcome=(row.send_result or {}).get("outcome"),
                status=row.status,
            )
            for row in rows
            if invoice_set.intersection(row.invoice_ids)
        ]

    async def get_or_create_digest(self, owner_id: str, run_date: date) -> DigestRecord:
        owner_uuid = _uuid(owner_id)
        async with self._sessions() as session:
            user = await self._user(session, owner_id)
            existing = await session.scalar(
                sa.select(AgentDigest).where(
                    AgentDigest.owner_id == owner_uuid, AgentDigest.run_date == run_date
                )
            )
            if existing:
                return self._digest(existing)
            row = AgentDigest(
                workspace_id=user.workspace_id, owner_id=owner_uuid, run_date=run_date
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    sa.select(AgentDigest).where(
                        AgentDigest.owner_id == owner_uuid, AgentDigest.run_date == run_date
                    )
                )
                if existing is None:
                    raise
                return self._digest(existing)
            await session.refresh(row)
            return self._digest(row)

    async def save_digest(self, digest: DigestRecord) -> None:
        async with self._sessions() as session:
            row = await session.get(AgentDigest, _uuid(digest.id))
            if row is None or str(row.owner_id) != digest.owner_id:
                raise NotFoundError("Digest not found")
            row.status = str(digest.status)
            row.gmail_thread_id = digest.gmail_thread_id
            row.owner_message_id = digest.owner_message_id
            row.total_outstanding_paise = digest.total_outstanding_paise
            row.customer_count = digest.customer_count
            await session.commit()

    async def add_digest_item(self, item: DigestItemRecord) -> None:
        async with self._sessions() as session:
            digest = await session.get(AgentDigest, _uuid(item.digest_id))
            if digest is None:
                raise NotFoundError("Digest not found")
            session.add(
                AgentDigestItem(
                    id=_uuid(item.id),
                    workspace_id=digest.workspace_id,
                    digest_id=digest.id,
                    item_number=item.item_number,
                    customer_id=_uuid(item.customer_id),
                    customer_name=item.customer_name,
                    invoice_ids=list(item.invoice_ids),
                    amount_paise=item.amount_paise,
                    oldest_due_date=item.oldest_due_date,
                    recommendation_reason=item.recommendation_reason,
                    source_references=list(item.source_references),
                    status=str(item.status),
                )
            )
            await session.commit()

    async def get_digest(self, owner_id: str, digest_id: str) -> DigestRecord:
        async with self._sessions() as session:
            row = await session.get(AgentDigest, _uuid(digest_id))
        if row is None or str(row.owner_id) != owner_id:
            raise NotFoundError("Digest not found")
        return self._digest(row)

    async def get_digest_by_thread(
        self, owner_id: str, gmail_thread_id: str
    ) -> DigestRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                sa.select(AgentDigest).where(
                    AgentDigest.owner_id == _uuid(owner_id),
                    AgentDigest.gmail_thread_id == gmail_thread_id,
                )
            )
        return self._digest(row) if row else None

    async def list_digest_items(
        self, owner_id: str, digest_id: str
    ) -> Sequence[DigestItemRecord]:
        await self.get_digest(owner_id, digest_id)
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    sa.select(AgentDigestItem)
                    .where(AgentDigestItem.digest_id == _uuid(digest_id))
                    .order_by(AgentDigestItem.item_number)
                )
            ).all()
        return [self._item(row) for row in rows]

    async def save_digest_item(self, item: DigestItemRecord) -> None:
        async with self._sessions() as session:
            row = await session.get(AgentDigestItem, _uuid(item.id))
            if row is None:
                raise NotFoundError("Digest item not found")
            row.status = str(item.status)
            row.recommendation_reason = item.recommendation_reason
            row.source_references = list(item.source_references)
            await session.commit()

    async def create_draft(self, draft: DraftRecord) -> None:
        async with self._sessions() as session:
            user = await self._user(session, draft.owner_id)
            session.add(self._draft_model(draft, user.workspace_id))
            await session.commit()

    async def get_draft(self, owner_id: str, draft_id: str) -> DraftRecord:
        async with self._sessions() as session:
            row = await session.get(AgentDraft, _uuid(draft_id))
        if row is None or str(row.owner_id) != owner_id:
            raise NotFoundError("Draft not found")
        return self._draft(row)

    async def list_drafts(
        self, owner_id: str, digest_id: str | None = None
    ) -> Sequence[DraftRecord]:
        query = sa.select(AgentDraft).where(AgentDraft.owner_id == _uuid(owner_id))
        if digest_id:
            query = query.where(AgentDraft.digest_id == _uuid(digest_id))
        async with self._sessions() as session:
            rows = (await session.scalars(query.order_by(AgentDraft.created_at))).all()
        return [self._draft(row) for row in rows]

    async def save_draft(self, draft: DraftRecord) -> None:
        values = {
            field: getattr(draft, field)
            for field in (
                "subject",
                "text_body",
                "rationale",
                "tone",
                "rendered_html",
                "customer_thread_id",
                "approved_by",
                "approval_source",
                "approved_at",
                "approved_content_hash",
                "sent_at",
                "send_result",
                "updated_at",
            )
        }
        values["status"] = str(draft.status)
        allowed_from: dict[DraftStatus, tuple[str, ...]] = {
            DraftStatus.PENDING: (
                DraftStatus.PENDING.value,
                DraftStatus.APPROVED.value,
                DraftStatus.FAILED.value,
                DraftStatus.PAUSED.value,
            ),
            DraftStatus.APPROVED: (DraftStatus.PENDING.value,),
            DraftStatus.REJECTED: (
                DraftStatus.PENDING.value,
                DraftStatus.APPROVED.value,
                DraftStatus.FAILED.value,
                DraftStatus.PAUSED.value,
            ),
            DraftStatus.SENT: (DraftStatus.SENDING.value,),
            DraftStatus.FAILED: (DraftStatus.SENDING.value,),
            DraftStatus.PAUSED: (
                DraftStatus.PENDING.value,
                DraftStatus.APPROVED.value,
                DraftStatus.PAUSED.value,
            ),
        }
        current_statuses = allowed_from.get(draft.status)
        if current_statuses is None:
            raise UnsafeActionError(f"Unsupported draft transition to {draft.status}")
        conditions = [
            AgentDraft.id == _uuid(draft.id),
            AgentDraft.owner_id == _uuid(draft.owner_id),
            AgentDraft.status.in_(current_statuses),
        ]
        if draft.status == DraftStatus.APPROVED:
            # A stale approval must not overwrite a dashboard edit that landed
            # after the approval request loaded the pending draft.
            conditions.extend(
                [
                    AgentDraft.subject == draft.subject,
                    AgentDraft.text_body == draft.text_body,
                ]
            )
        async with self._sessions() as session:
            result = await session.execute(
                sa.update(AgentDraft).where(*conditions).values(**values)
            )
            if result.rowcount != 1:
                await session.rollback()
                raise UnsafeActionError(
                    "Draft changed concurrently or is already sending/sent; reload before retrying"
                )
            await session.commit()

    async def claim_approved_draft_for_send(
        self, owner_id: str, draft_id: str, approved_content_hash: str
    ) -> bool:
        async with self._sessions() as session:
            claimed = await session.scalar(
                sa.update(AgentDraft)
                .where(
                    AgentDraft.id == _uuid(draft_id),
                    AgentDraft.owner_id == _uuid(owner_id),
                    AgentDraft.status == DraftStatus.APPROVED.value,
                    AgentDraft.approved_content_hash == approved_content_hash,
                )
                .values(status=DraftStatus.SENDING.value)
                .returning(AgentDraft.id)
            )
            await session.commit()
        return claimed is not None

    async def find_draft_by_customer_thread(
        self, owner_id: str, gmail_thread_id: str
    ) -> DraftRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(
                sa.select(AgentDraft)
                .where(
                    AgentDraft.owner_id == _uuid(owner_id),
                    AgentDraft.customer_thread_id == gmail_thread_id,
                )
                .order_by(AgentDraft.sent_at.desc())
                .limit(1)
            )
        return self._draft(row) if row else None

    async def create_review_task(self, task: ReviewTaskRecord) -> None:
        async with self._sessions() as session:
            user = await self._user(session, task.owner_id)
            session.add(
                AgentReviewTask(
                    id=_uuid(task.id), workspace_id=user.workspace_id,
                    owner_id=user.id, kind=str(task.kind), reason=task.reason,
                    payload=task.payload,
                    digest_id=_uuid(task.digest_id) if task.digest_id else None,
                    customer_id=_uuid(task.customer_id) if task.customer_id else None,
                    invoice_ids=list(task.invoice_ids), status=task.status, created_at=task.created_at,
                )
            )
            await session.commit()

    async def pause_invoices(
        self,
        owner_id: str,
        invoice_ids: Sequence[str],
        *,
        payment_state: str | None,
        reason: str,
    ) -> None:
        ids = [_uuid(value) for value in invoice_ids]
        async with self._sessions() as session:
            user = await self._user(session, owner_id)
            await mark_invoices_paused(
                session,
                workspace_id=user.workspace_id,
                invoice_ids=ids,
                reason=reason,
                payment_state=payment_state,
            )
            items = (
                await session.scalars(
                    sa.select(AgentDigestItem).where(
                        AgentDigestItem.workspace_id
                        == sa.select(User.workspace_id).where(User.id == _uuid(owner_id)).scalar_subquery()
                    )
                )
            ).all()
            for item in items:
                if set(invoice_ids).intersection(item.invoice_ids):
                    item.status = DigestItemStatus.PAUSED.value
            drafts = (
                await session.scalars(
                    sa.select(AgentDraft).where(
                        AgentDraft.owner_id == _uuid(owner_id),
                        AgentDraft.status.in_([DraftStatus.PENDING.value, DraftStatus.APPROVED.value]),
                    )
                )
            ).all()
            for draft in drafts:
                if set(invoice_ids).intersection(draft.invoice_ids):
                    draft.status = DraftStatus.PAUSED.value
                    draft.approved_content_hash = None
            await session.commit()

    async def mark_reminder_sent(
        self, owner_id: str, invoice_ids: Sequence[str], sent_at: datetime
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                sa.update(Invoice)
                .where(
                    Invoice.owner_id == _uuid(owner_id),
                    Invoice.id.in_([_uuid(value) for value in invoice_ids]),
                )
                .values(
                    reminder_count=Invoice.reminder_count + 1,
                    last_reminder_at=sent_at,
                )
            )
            await session.commit()

    async def append_audit(self, event: AuditEvent) -> None:
        actor_type = {
            "owner": enums.ActorType.USER.value,
            "customer": enums.ActorType.PROVIDER.value,
        }.get(event.actor_type, enums.ActorType.SYSTEM.value)
        async with self._sessions() as session:
            user = await self._user(session, event.owner_id)
            await record_event(
                session,
                workspace_id=user.workspace_id,
                owner_id=user.id,
                event_type=event.event_type,
                actor_type=actor_type,
                actor_id=event.actor_id,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                summary=event.event_type.replace("_", " "),
                decision=event.decision,
                source_evidence=event.source_evidence,
                model_name=event.model_name,
                prompt_version=event.prompt_version,
            )
            await session.commit()

    @staticmethod
    def _customer(row: Customer) -> CustomerRecord:
        return CustomerRecord(
            id=str(row.id), owner_id=str(row.owner_id), name=row.name,
            email=row.primary_email, phone=row.phone, match_confidence=row.match_confidence,
        )

    @staticmethod
    def _invoice(row: Invoice) -> InvoiceRecord:
        return InvoiceRecord(
            id=str(row.id), owner_id=str(row.owner_id), customer_id=str(row.customer_id),
            invoice_number=row.invoice_number or row.normalized_number or "unknown",
            amount_paise=row.amount_paise, balance_paise=row.balance_paise,
            currency=row.currency, issued_date=row.issued_date, due_date=row.due_date,
            payment_state=PaymentState(row.payment_state),
            reminder_state=ReminderState(row.reminder_state),
            source_message_id=str(row.source_message_id) if row.source_message_id else None,
            source_attachment_id=(str(row.source_attachment_id) if row.source_attachment_id else None),
            dispute_note=row.dispute_note, payment_claim_note=row.payment_claim_note,
        )

    @staticmethod
    def _digest(row: AgentDigest) -> DigestRecord:
        return DigestRecord(
            id=str(row.id), owner_id=str(row.owner_id), run_date=row.run_date,
            status=DigestStatus(row.status), gmail_thread_id=row.gmail_thread_id,
            owner_message_id=row.owner_message_id,
            total_outstanding_paise=row.total_outstanding_paise,
            customer_count=row.customer_count, created_at=row.created_at,
        )

    @staticmethod
    def _item(row: AgentDigestItem) -> DigestItemRecord:
        return DigestItemRecord(
            id=str(row.id), digest_id=str(row.digest_id), item_number=row.item_number,
            customer_id=str(row.customer_id), customer_name=row.customer_name,
            invoice_ids=tuple(row.invoice_ids), amount_paise=row.amount_paise,
            oldest_due_date=row.oldest_due_date,
            recommendation_reason=row.recommendation_reason,
            source_references=tuple(row.source_references), status=DigestItemStatus(row.status),
        )

    @staticmethod
    def _draft(row: AgentDraft) -> DraftRecord:
        return DraftRecord(
            id=str(row.id), owner_id=str(row.owner_id), digest_id=str(row.digest_id),
            digest_item_id=str(row.digest_item_id), draft_number=row.draft_number,
            customer_id=str(row.customer_id), customer_email=row.customer_email,
            invoice_ids=tuple(row.invoice_ids), subject=row.subject, text_body=row.text_body,
            rationale=row.rationale, tone=row.tone, status=DraftStatus(row.status),
            source_snapshot=row.source_snapshot, agent_decision=row.agent_decision,
            prompt_version=row.prompt_version, model_name=row.model_name,
            rendered_html=row.rendered_html, customer_thread_id=row.customer_thread_id,
            approved_by=row.approved_by, approval_source=row.approval_source,
            approved_at=row.approved_at, approved_content_hash=row.approved_content_hash,
            sent_at=row.sent_at, send_result=row.send_result,
            created_at=row.created_at, updated_at=row.updated_at,
        )

    @staticmethod
    def _draft_model(draft: DraftRecord, workspace_id: uuid.UUID) -> AgentDraft:
        return AgentDraft(
            id=_uuid(draft.id), workspace_id=workspace_id, owner_id=_uuid(draft.owner_id),
            digest_id=_uuid(draft.digest_id), digest_item_id=_uuid(draft.digest_item_id),
            draft_number=draft.draft_number, customer_id=_uuid(draft.customer_id),
            customer_email=draft.customer_email, invoice_ids=list(draft.invoice_ids),
            subject=draft.subject, text_body=draft.text_body, rationale=draft.rationale,
            tone=draft.tone, status=str(draft.status), source_snapshot=draft.source_snapshot,
            agent_decision=draft.agent_decision, prompt_version=draft.prompt_version,
            model_name=draft.model_name, rendered_html=draft.rendered_html,
            customer_thread_id=draft.customer_thread_id, approved_by=draft.approved_by,
            approval_source=draft.approval_source, approved_at=draft.approved_at,
            approved_content_hash=draft.approved_content_hash, sent_at=draft.sent_at,
            send_result=draft.send_result, created_at=draft.created_at, updated_at=draft.updated_at,
        )
