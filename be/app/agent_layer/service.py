from __future__ import annotations

import html
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.logging_config import get_logger

from .domain import (
    AuditEvent,
    CustomerDossier,
    CustomerReplyDecision,
    CustomerReplyIntent,
    CustomerReplyResult,
    DailyCycleResult,
    DigestItemRecord,
    DigestItemStatus,
    DigestStatus,
    DraftRecord,
    DraftStatus,
    InvoiceRecord,
    OwnerAction,
    OwnerActionKind,
    OwnerReplyResult,
    PaymentState,
    ReviewTaskKind,
    ReviewTaskRecord,
    utcnow,
)
from .errors import ApprovalRequiredError, NotFoundError, UnsafeActionError
from .ports import AgentGateway, LedgerRepository, MailGateway
from .templates import (
    normalize_subject,
    render_branded_email,
    render_digest,
    render_draft_review,
)

_SEND_COMMAND = re.compile(
    r"^\s*send\s+(?P<selection>all|\d+(?:\s*(?:,|and)\s*\d+)*)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_QUOTED_MARKERS = (
    re.compile(r"^On .+wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^From:\s+.+$", re.IGNORECASE),
)
_ALREADY_PAID = re.compile(
    r"\b(already\s+paid|payment\s+(?:is\s+)?done|payment\s+kar\s+diya|paid\s+on|"
    r"transferred|remitted|settled)\b",
    re.IGNORECASE,
)
_DISPUTE = re.compile(
    r"\b(dispute|wrong\s+invoice|incorrect\s+(?:invoice|amount)|invoice\s+galat|"
    r"amount\s+galat|not\s+our\s+invoice|service\s+issue|not\s+accepted)\b",
    re.IGNORECASE,
)

logger = get_logger(__name__)


def _business_today() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def strip_quoted_history(text: str) -> str:
    """Keep only the newly authored part of an email reply."""

    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(">") or any(marker.match(line.strip()) for marker in _QUOTED_MARKERS):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _uuid() -> str:
    return str(uuid.uuid4())


def _serialize(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class AgentOrchestrator:
    def __init__(
        self,
        *,
        repository: LedgerRepository,
        mail: MailGateway,
        agent: AgentGateway,
        confidence_threshold: float = 0.80,
    ) -> None:
        self.repository = repository
        self.mail = mail
        self.agent = agent
        self.confidence_threshold = confidence_threshold

    async def get_customer_dossier(
        self,
        *,
        owner_id: str,
        customer_id: str,
        invoice_ids: Sequence[str] | None = None,
        as_of: date | None = None,
    ) -> CustomerDossier:
        customer = await self.repository.get_customer(owner_id, customer_id)
        all_invoices = list(await self.repository.list_customer_invoices(owner_id, customer_id))
        requested = set(invoice_ids or ())
        invoices = [invoice for invoice in all_invoices if not requested or invoice.id in requested]
        if requested != {invoice.id for invoice in invoices}:
            raise NotFoundError("One or more requested invoices are outside this customer dossier")
        evidence = tuple(
            await self.repository.list_finance_evidence(
                owner_id, customer_id, [invoice.id for invoice in invoices]
            )
        )
        reminders = tuple(
            await self.repository.list_prior_reminders(
                owner_id, customer_id, [invoice.id for invoice in invoices]
            )
        )
        source_references: list[dict[str, str]] = []
        for invoice in invoices:
            if invoice.source_message_id:
                source_references.append(
                    {"kind": "source_message", "id": invoice.source_message_id, "invoice_id": invoice.id}
                )
            if invoice.source_attachment_id:
                source_references.append(
                    {
                        "kind": "source_attachment",
                        "id": invoice.source_attachment_id,
                        "invoice_id": invoice.id,
                    }
                )
        for item in evidence:
            source_references.append({"kind": item.kind, "id": item.id})
            if item.source_message_id:
                source_references.append({"kind": "source_message", "id": item.source_message_id})
            if item.source_attachment_id:
                source_references.append({"kind": "source_attachment", "id": item.source_attachment_id})
        unique_references = tuple(
            dict(item) for item in {tuple(sorted(reference.items())) for reference in source_references}
        )
        reason = self._recommendation_reason(
            invoices=invoices,
            reminders_count=len(reminders),
            as_of=as_of or _business_today(),
        )
        return CustomerDossier(
            customer=customer,
            invoices=tuple(invoices),
            evidence=evidence,
            reminders=reminders,
            recommendation_reason=reason,
            source_references=unique_references,
        )

    @staticmethod
    def _recommendation_reason(
        *, invoices: Sequence[InvoiceRecord], reminders_count: int, as_of: date
    ) -> str:
        actionable = [invoice for invoice in invoices if invoice.is_actionable(as_of)]
        if not actionable:
            return "No invoice currently passes Topline's deterministic reminder checks."
        oldest = min(invoice.due_date for invoice in actionable if invoice.due_date is not None)
        days_overdue = max((as_of - oldest).days, 0)
        total_paise = sum(invoice.balance_paise for invoice in actionable)
        numbers = ", ".join(invoice.invoice_number for invoice in actionable)
        previous = (
            f" {reminders_count} prior reminder(s) are recorded."
            if reminders_count
            else " No prior reminder is recorded."
        )
        return (
            f"Invoice(s) {numbers} have {total_paise / 100:,.2f} outstanding; "
            f"the oldest due date is {oldest.isoformat()} ({days_overdue} days overdue). "
            f"Payment is not confirmed and no payment claim or dispute blocks follow-up.{previous}"
        )

    async def run_daily_cycle(self, *, owner_id: str, run_date: date) -> DailyCycleResult:
        owner = await self.repository.get_owner(owner_id)
        digest = await self.repository.get_or_create_digest(owner_id, run_date)
        existing_items = list(await self.repository.list_digest_items(owner_id, digest.id))
        if digest.status == DigestStatus.SENT:
            return DailyCycleResult(digest=digest, items=tuple(existing_items))

        items = existing_items
        if not items:
            candidates = await self.repository.list_actionable_invoices(owner_id, run_date)
            actionable = [invoice for invoice in candidates if invoice.is_actionable(run_date)]
            by_customer: dict[str, list[InvoiceRecord]] = defaultdict(list)
            for invoice in actionable:
                by_customer[invoice.customer_id].append(invoice)

            for item_number, (customer_id, invoices) in enumerate(by_customer.items(), start=1):
                dossier = await self.get_customer_dossier(
                    owner_id=owner_id,
                    customer_id=customer_id,
                    invoice_ids=[invoice.id for invoice in invoices],
                    as_of=run_date,
                )
                oldest_due_date = min(
                    invoice.due_date for invoice in invoices if invoice.due_date is not None
                )
                item = DigestItemRecord(
                    id=_uuid(),
                    digest_id=digest.id,
                    item_number=item_number,
                    customer_id=customer_id,
                    customer_name=dossier.customer.name,
                    invoice_ids=tuple(invoice.id for invoice in invoices),
                    amount_paise=sum(invoice.balance_paise for invoice in invoices),
                    oldest_due_date=oldest_due_date,
                    recommendation_reason=dossier.recommendation_reason,
                    source_references=dossier.source_references,
                )
                await self.repository.add_digest_item(item)
                items.append(item)
                await self.repository.append_audit(
                    AuditEvent(
                        owner_id=owner_id,
                        event_type="daily_item_selected",
                        actor_type="rules",
                        actor_id=None,
                        entity_type="digest_item",
                        entity_id=item.id,
                        decision={
                            "actionable": True,
                            "reason": item.recommendation_reason,
                            "invoice_ids": list(item.invoice_ids),
                        },
                        source_evidence=tuple(item.source_references),
                    )
                )

        digest.customer_count = len(items)
        digest.total_outstanding_paise = sum(item.amount_paise for item in items)
        text_body, html_body = render_digest(items, run_date.isoformat())
        try:
            receipt = await self.mail.send_owner_digest(
                owner=owner,
                subject=f"Topline daily receivables review — {run_date.isoformat()}",
                text_body=text_body,
                html_body=html_body,
            )
        except Exception as exc:
            digest.status = DigestStatus.FAILED
            await self.repository.save_digest(digest)
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="digest_send_failed",
                    actor_type="system",
                    actor_id=None,
                    entity_type="digest",
                    entity_id=digest.id,
                    decision={"error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            raise
        digest.status = DigestStatus.SENT
        digest.gmail_thread_id = receipt.thread_id
        digest.owner_message_id = receipt.message_id
        await self.repository.save_digest(digest)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="digest_sent",
                actor_type="system",
                actor_id=None,
                entity_type="digest",
                entity_id=digest.id,
                decision={
                    "gmail_message_id": receipt.message_id,
                    "gmail_thread_id": receipt.thread_id,
                    "item_count": len(items),
                },
                source_evidence=tuple(
                    reference for item in items for reference in item.source_references
                ),
            )
        )
        return DailyCycleResult(digest=digest, items=tuple(items))

    async def process_owner_reply(
        self,
        *,
        owner_id: str,
        body: str,
        digest_id: str | None = None,
        gmail_thread_id: str | None = None,
        actor_message_id: str | None = None,
    ) -> OwnerReplyResult:
        if digest_id:
            digest = await self.repository.get_digest(owner_id, digest_id)
        elif gmail_thread_id:
            digest = await self.repository.get_digest_by_thread(owner_id, gmail_thread_id)
            if digest is None:
                raise NotFoundError("Owner reply is not in a Topline digest thread")
        else:
            raise ValueError("digest_id or gmail_thread_id is required")
        new_text = strip_quoted_history(body)
        if not new_text:
            return await self._owner_clarification(
                owner_id=owner_id,
                digest_id=digest.id,
                reason="The owner reply contained no new command after quoted history was removed.",
                payload={"body": body, "actor_message_id": actor_message_id},
            )

        send_match = _SEND_COMMAND.fullmatch(new_text)
        if send_match:
            return await self._process_send_command(
                owner_id=owner_id,
                digest_id=digest.id,
                selection=send_match.group("selection"),
                actor_message_id=actor_message_id,
            )

        items = list(await self.repository.list_digest_items(owner_id, digest.id))
        try:
            decision = await self.agent.parse_owner_command(
                owner_text=new_text, digest_items=items
            )
        except Exception as exc:  # noqa: BLE001 - model/provider errors fail closed
            return await self._owner_clarification(
                owner_id=owner_id,
                digest_id=digest.id,
                reason="Topline could not safely parse the owner command.",
                payload={
                    "new_text": new_text,
                    "error_type": type(exc).__name__,
                    "actor_message_id": actor_message_id,
                },
            )

        validation_error = self._validate_owner_decision(decision.actions, items)
        unsafe = (
            decision.ambiguous
            or decision.confidence < self.confidence_threshold
            or validation_error is not None
        )
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="owner_command_decided",
                actor_type="agent",
                actor_id=actor_message_id,
                entity_type="digest",
                entity_id=digest.id,
                decision={
                    "input": new_text,
                    "output": _serialize(decision),
                    "accepted": not unsafe,
                    "validation_error": validation_error,
                },
                model_name=decision.model_name,
                prompt_version=decision.prompt_version,
            )
        )
        if unsafe:
            return await self._owner_clarification(
                owner_id=owner_id,
                digest_id=digest.id,
                reason=validation_error or decision.explanation or "The owner command was ambiguous.",
                payload={"new_text": new_text, "decision": _serialize(decision)},
            )

        by_number = {item.item_number: item for item in items}
        by_customer = {item.customer_id: item for item in items}
        drafts: list[DraftRecord] = []
        for action in decision.actions:
            item = by_number.get(action.item_number) if action.item_number else by_customer[action.customer_id or ""]
            if action.action == OwnerActionKind.SKIP:
                item.status = DigestItemStatus.SKIPPED
                await self.repository.save_digest_item(item)
                await self.repository.append_audit(
                    AuditEvent(
                        owner_id=owner_id,
                        event_type="digest_item_skipped",
                        actor_type="owner",
                        actor_id=actor_message_id,
                        entity_type="digest_item",
                        entity_id=item.id,
                        decision={"reason": action.reason, "input": new_text},
                        source_evidence=tuple(item.source_references),
                    )
                )
                continue
            try:
                draft = await self._create_customer_draft(
                    owner_id=owner_id,
                    digest_id=digest.id,
                    item=item,
                    action=action,
                    actor_message_id=actor_message_id,
                )
            except Exception as exc:  # noqa: BLE001 - drafting errors create review tasks
                return await self._owner_clarification(
                    owner_id=owner_id,
                    digest_id=digest.id,
                    reason=f"Draft {item.item_number} needs review before Topline can propose it.",
                    payload={
                        "new_text": new_text,
                        "digest_item_id": item.id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            drafts.append(draft)

        if drafts:
            owner = await self.repository.get_owner(owner_id)
            text_body, html_body = render_draft_review(drafts)
            await self.mail.reply_to_owner_thread(
                owner=owner,
                thread_id=digest.gmail_thread_id or "",
                subject="Re: Topline daily receivables review — drafts for approval",
                text_body=text_body,
                html_body=html_body,
            )
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="draft_review_sent_to_owner",
                    actor_type="system",
                    actor_id=None,
                    entity_type="digest",
                    entity_id=digest.id,
                    decision={"draft_ids": [draft.id for draft in drafts]},
                )
            )
        return OwnerReplyResult(kind="drafts_created", drafts=tuple(drafts))

    def _validate_owner_decision(
        self, actions: Sequence[OwnerAction], items: Sequence[DigestItemRecord]
    ) -> str | None:
        if not actions:
            return "No actionable instruction was found."
        by_number = {item.item_number: item for item in items}
        by_customer = {item.customer_id: item for item in items}
        selected_ids: set[str] = set()
        for action in actions:
            if action.confidence < self.confidence_threshold:
                return "At least one requested action has low confidence."
            from_number = by_number.get(action.item_number) if action.item_number else None
            from_customer = by_customer.get(action.customer_id or "") if action.customer_id else None
            if from_number is None and from_customer is None:
                return "A command references an item or customer outside this digest."
            if from_number and from_customer and from_number.id != from_customer.id:
                return "A command's item number and customer reference disagree."
            selected = from_number or from_customer
            if selected is None or selected.status not in {
                DigestItemStatus.ACTIONABLE,
                DigestItemStatus.DRAFTED,
            }:
                return "A command targets an item that is no longer actionable."
            if selected.id in selected_ids:
                return "The command produced multiple actions for the same digest item."
            selected_ids.add(selected.id)
        return None

    async def _create_customer_draft(
        self,
        *,
        owner_id: str,
        digest_id: str,
        item: DigestItemRecord,
        action: OwnerAction,
        actor_message_id: str | None,
    ) -> DraftRecord:
        dossier = await self.get_customer_dossier(
            owner_id=owner_id,
            customer_id=item.customer_id,
            invoice_ids=item.invoice_ids,
        )
        if any(not invoice.is_actionable(_business_today()) for invoice in dossier.invoices):
            raise UnsafeActionError("The invoice state changed and no longer permits drafting")
        decision = await self.agent.draft_reminder(
            dossier_payload=dossier.prompt_payload(),
            tone=action.tone or "normal",
            owner_note=action.note,
        )
        if decision.confidence < self.confidence_threshold:
            raise UnsafeActionError("Gemini draft confidence is below the review threshold")
        allowed_source_ids = {
            reference["id"] for reference in dossier.source_references if "id" in reference
        }
        if not set(decision.cited_source_ids).issubset(allowed_source_ids):
            raise UnsafeActionError("Gemini cited evidence outside the customer dossier")
        existing = await self.repository.list_drafts(owner_id, digest_id)
        draft_number = max((draft.draft_number for draft in existing), default=0) + 1
        brand = await self.repository.get_brand(owner_id)
        draft = DraftRecord(
            id=_uuid(),
            owner_id=owner_id,
            digest_id=digest_id,
            digest_item_id=item.id,
            draft_number=draft_number,
            customer_id=dossier.customer.id,
            customer_email=dossier.customer.email,
            invoice_ids=tuple(invoice.id for invoice in dossier.invoices),
            subject=normalize_subject(decision.subject),
            text_body=decision.text_body.strip(),
            rationale=decision.rationale.strip(),
            tone=decision.tone,
            status=DraftStatus.PENDING,
            source_snapshot={
                "dossier": dossier.prompt_payload(),
                "cited_source_ids": list(decision.cited_source_ids),
            },
            agent_decision=_serialize(decision),
            prompt_version=decision.prompt_version,
            model_name=decision.model_name,
            rendered_html=render_branded_email(brand, decision.text_body.strip()),
        )
        await self.repository.create_draft(draft)
        item.status = DigestItemStatus.DRAFTED
        await self.repository.save_digest_item(item)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_created",
                actor_type="agent",
                actor_id=actor_message_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={
                    "agent_output": _serialize(decision),
                    "status": str(draft.status),
                    "rendered_html": draft.rendered_html,
                },
                source_evidence=tuple(dossier.source_references),
                model_name=decision.model_name,
                prompt_version=decision.prompt_version,
            )
        )
        return draft

    async def edit_draft(
        self,
        *,
        owner_id: str,
        draft_id: str,
        subject: str,
        text_body: str,
        actor_id: str,
    ) -> DraftRecord:
        draft = await self.repository.get_draft(owner_id, draft_id)
        if draft.status in {DraftStatus.SENT, DraftStatus.REJECTED}:
            raise UnsafeActionError(f"Cannot edit a {draft.status} draft")
        draft.subject = normalize_subject(subject)
        draft.text_body = text_body.strip()
        if not draft.text_body:
            raise ValueError("Draft body cannot be empty")
        brand = await self.repository.get_brand(owner_id)
        draft.rendered_html = render_branded_email(brand, draft.text_body)
        draft.status = DraftStatus.PENDING
        draft.approved_by = None
        draft.approval_source = None
        draft.approved_at = None
        draft.approved_content_hash = None
        draft.updated_at = utcnow()
        await self.repository.save_draft(draft)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_edited",
                actor_type="owner",
                actor_id=actor_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={
                    "status": str(draft.status),
                    "subject": draft.subject,
                    "text_body": draft.text_body,
                    "approval_reset": True,
                },
            )
        )
        return draft

    async def approve_draft(
        self,
        *,
        owner_id: str,
        draft_id: str,
        actor_id: str,
        source: str,
    ) -> DraftRecord:
        draft = await self.repository.get_draft(owner_id, draft_id)
        if draft.status != DraftStatus.PENDING:
            raise UnsafeActionError(f"Only pending drafts can be approved; current status is {draft.status}")
        draft.status = DraftStatus.APPROVED
        draft.approved_by = actor_id
        draft.approval_source = source
        draft.approved_at = utcnow()
        draft.approved_content_hash = draft.content_hash()
        draft.updated_at = utcnow()
        await self.repository.save_draft(draft)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_approved",
                actor_type="owner",
                actor_id=actor_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={
                    "approval_source": source,
                    "approved_content_hash": draft.approved_content_hash,
                    "subject": draft.subject,
                    "text_body": draft.text_body,
                },
            )
        )
        return draft

    async def reject_draft(
        self, *, owner_id: str, draft_id: str, actor_id: str, reason: str | None
    ) -> DraftRecord:
        draft = await self.repository.get_draft(owner_id, draft_id)
        if draft.status == DraftStatus.SENT:
            raise UnsafeActionError("A sent draft cannot be rejected")
        draft.status = DraftStatus.REJECTED
        draft.approved_content_hash = None
        draft.updated_at = utcnow()
        await self.repository.save_draft(draft)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_rejected",
                actor_type="owner",
                actor_id=actor_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={"reason": reason},
            )
        )
        return draft

    async def send_approved_draft(
        self, *, owner_id: str, draft_id: str, actor_id: str
    ) -> DraftRecord:
        draft = await self.repository.get_draft(owner_id, draft_id)
        if draft.status != DraftStatus.APPROVED or not draft.approved_at:
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="draft_send_blocked",
                    actor_type="owner",
                    actor_id=actor_id,
                    entity_type="draft",
                    entity_id=draft.id,
                    decision={"reason": "explicit_approval_required", "status": str(draft.status)},
                )
            )
            raise ApprovalRequiredError("Customer email requires explicit owner approval")
        if draft.approved_content_hash != draft.content_hash():
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="draft_send_blocked",
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="draft",
                    entity_id=draft.id,
                    decision={"reason": "content_changed_after_approval"},
                )
            )
            raise ApprovalRequiredError("Draft changed after approval and must be approved again")
        claimed = await self.repository.claim_approved_draft_for_send(
            owner_id, draft.id, draft.approved_content_hash
        )
        if not claimed:
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="draft_send_blocked",
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="draft",
                    entity_id=draft.id,
                    decision={"reason": "draft_already_claimed_or_changed"},
                )
            )
            raise UnsafeActionError("Draft is already sending, sent, or no longer matches its approval")
        draft.status = DraftStatus.SENDING
        owner = await self.repository.get_owner(owner_id)
        brand = await self.repository.get_brand(owner_id)
        rendered_html = render_branded_email(brand, draft.text_body)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_send_attempted",
                actor_type="owner",
                actor_id=actor_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={
                    "approval_source": draft.approval_source,
                    "approved_at": draft.approved_at.isoformat(),
                    "recipient": draft.customer_email,
                    "subject": draft.subject,
                    "text_body": draft.text_body,
                    "rendered_html": rendered_html,
                },
                source_evidence=tuple(draft.source_snapshot.get("dossier", {}).get("source_references", [])),
                model_name=draft.model_name,
                prompt_version=draft.prompt_version,
            )
        )
        try:
            receipt = await self.mail.send_customer_email(
                owner=owner,
                recipient=draft.customer_email,
                subject=draft.subject,
                text_body=draft.text_body,
                html_body=rendered_html,
                reply_to=brand.reply_to,
                thread_id=draft.customer_thread_id,
            )
        except Exception as exc:
            draft.status = DraftStatus.FAILED
            draft.send_result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
            draft.updated_at = utcnow()
            await self.repository.save_draft(draft)
            task = ReviewTaskRecord(
                id=_uuid(),
                owner_id=owner_id,
                kind=ReviewTaskKind.SEND_FAILURE,
                reason="An approved customer email failed to send and was not retried automatically.",
                payload={"draft_id": draft.id, "send_result": draft.send_result},
                digest_id=draft.digest_id,
                customer_id=draft.customer_id,
                invoice_ids=draft.invoice_ids,
            )
            await self.repository.create_review_task(task)
            await self.repository.append_audit(
                AuditEvent(
                    owner_id=owner_id,
                    event_type="draft_send_failed",
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="draft",
                    entity_id=draft.id,
                    decision=draft.send_result,
                )
            )
            try:
                await self.mail.notify_owner(
                    owner=owner,
                    subject="Topline send failed — owner review required",
                    text_body=(
                        "An approved customer email failed to send. Topline did not retry it "
                        "automatically; please review the draft in the dashboard."
                    ),
                    html_body=(
                        "<p>An approved customer email failed to send.</p>"
                        "<p>Topline did not retry it automatically; please review the draft "
                        "in the dashboard.</p>"
                    ),
                    thread_id=None,
                )
            except Exception as notification_error:  # noqa: BLE001 - durable task already exists
                # The review task and audit event are already durable; Gmail may be the
                # component that failed, so owner notification is best-effort here.
                logger.warning(
                    "owner send-failure notification also failed",
                    extra={
                        "draft_id": draft.id,
                        "error_type": type(notification_error).__name__,
                    },
                )
            raise
        draft.status = DraftStatus.SENT
        draft.rendered_html = rendered_html
        draft.customer_thread_id = receipt.thread_id
        sent_at = utcnow()
        draft.sent_at = sent_at
        draft.send_result = {
            "ok": True,
            "message_id": receipt.message_id,
            "thread_id": receipt.thread_id,
            "provider_payload": receipt.provider_payload,
        }
        draft.updated_at = utcnow()
        await self.repository.save_draft(draft)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="draft_sent",
                actor_type="system",
                actor_id=actor_id,
                entity_type="draft",
                entity_id=draft.id,
                decision={
                    "final_rendered_email": {
                        "recipient": draft.customer_email,
                        "subject": draft.subject,
                        "text_body": draft.text_body,
                        "html_body": rendered_html,
                    },
                    "send_result": draft.send_result,
                },
                source_evidence=tuple(draft.source_snapshot.get("dossier", {}).get("source_references", [])),
                model_name=draft.model_name,
                prompt_version=draft.prompt_version,
            )
        )
        await self.repository.mark_reminder_sent(owner_id, draft.invoice_ids, sent_at)
        return draft

    async def _process_send_command(
        self,
        *,
        owner_id: str,
        digest_id: str,
        selection: str,
        actor_message_id: str | None,
    ) -> OwnerReplyResult:
        candidates = [
            draft
            for draft in await self.repository.list_drafts(owner_id, digest_id)
            if draft.status in {DraftStatus.PENDING, DraftStatus.APPROVED}
        ]
        by_number = {draft.draft_number: draft for draft in candidates}
        if selection.lower() == "all":
            selected_numbers = sorted(by_number)
        else:
            selected_numbers = [int(value) for value in re.findall(r"\d+", selection)]
        invalid = [number for number in selected_numbers if number not in by_number]
        if not selected_numbers or invalid:
            return await self._owner_clarification(
                owner_id=owner_id,
                digest_id=digest_id,
                reason="The send command references no pending draft or an unknown draft number.",
                payload={"selection": selection, "invalid_draft_numbers": invalid},
            )
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="owner_send_command_accepted",
                actor_type="owner",
                actor_id=actor_message_id,
                entity_type="digest",
                entity_id=digest_id,
                decision={"selection": selection, "draft_numbers": selected_numbers},
            )
        )
        sent_ids: list[str] = []
        actor = actor_message_id or "owner-email-command"
        for number in selected_numbers:
            draft = by_number[number]
            if draft.status == DraftStatus.PENDING:
                draft = await self.approve_draft(
                    owner_id=owner_id,
                    draft_id=draft.id,
                    actor_id=actor,
                    source="owner_email_command",
                )
            sent = await self.send_approved_draft(
                owner_id=owner_id, draft_id=draft.id, actor_id=actor
            )
            sent_ids.append(sent.id)
        return OwnerReplyResult(kind="drafts_sent", sent_draft_ids=tuple(sent_ids))

    async def _owner_clarification(
        self,
        *,
        owner_id: str,
        digest_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> OwnerReplyResult:
        task = ReviewTaskRecord(
            id=_uuid(),
            owner_id=owner_id,
            kind=ReviewTaskKind.OWNER_COMMAND_CLARIFICATION,
            reason=reason,
            payload=payload,
            digest_id=digest_id,
        )
        await self.repository.create_review_task(task)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="owner_command_needs_clarification",
                actor_type="system",
                actor_id=None,
                entity_type="review_task",
                entity_id=task.id,
                decision={"reason": reason, "payload": payload, "sent": False},
            )
        )
        digest = await self.repository.get_digest(owner_id, digest_id)
        owner = await self.repository.get_owner(owner_id)
        await self.mail.notify_owner(
            owner=owner,
            subject="Topline needs clarification",
            text_body=f"Topline did not send anything. {reason}",
            html_body=(
                "<p><strong>Topline did not send anything.</strong></p>"
                f"<p>{html.escape(reason)}</p>"
            ),
            thread_id=digest.gmail_thread_id,
        )
        return OwnerReplyResult(kind="clarification_required", review_task=task)

    async def process_customer_reply(
        self,
        *,
        owner_id: str,
        gmail_thread_id: str,
        body: str,
        source_message_id: str,
    ) -> CustomerReplyResult:
        draft = await self.repository.find_draft_by_customer_thread(owner_id, gmail_thread_id)
        if draft is None:
            raise NotFoundError("Customer reply is not in a known Topline reminder thread")
        new_text = strip_quoted_history(body)
        customer = await self.repository.get_customer(owner_id, draft.customer_id)
        all_invoices = await self.repository.list_customer_invoices(owner_id, draft.customer_id)
        invoices = [invoice for invoice in all_invoices if invoice.id in set(draft.invoice_ids)]
        if _ALREADY_PAID.search(new_text):
            decision = CustomerReplyDecision(
                intent=CustomerReplyIntent.ALREADY_PAID,
                confidence=1.0,
                explanation="Deterministic payment-claim phrase matched; payment is not marked confirmed.",
                requires_review=True,
                cited_invoice_ids=tuple(draft.invoice_ids),
                prompt_version="customer-reply-rules-v1",
                model_name="deterministic-rules",
            )
        elif _DISPUTE.search(new_text):
            decision = CustomerReplyDecision(
                intent=CustomerReplyIntent.DISPUTE,
                confidence=1.0,
                explanation="Deterministic dispute phrase matched.",
                requires_review=True,
                cited_invoice_ids=tuple(draft.invoice_ids),
                prompt_version="customer-reply-rules-v1",
                model_name="deterministic-rules",
            )
        elif not new_text:
            decision = CustomerReplyDecision(
                intent=CustomerReplyIntent.UNCLEAR,
                confidence=1.0,
                explanation="No new reply text remained after quoted history was removed.",
                requires_review=True,
                cited_invoice_ids=tuple(draft.invoice_ids),
                prompt_version="customer-reply-rules-v1",
                model_name="deterministic-rules",
            )
        else:
            try:
                decision = await self.agent.classify_customer_reply(
                    new_reply_text=new_text, customer=customer, invoices=invoices
                )
                if not set(decision.cited_invoice_ids).issubset(set(draft.invoice_ids)):
                    decision = CustomerReplyDecision(
                        intent=CustomerReplyIntent.UNCLEAR,
                        confidence=0.0,
                        explanation="Reply classification cited an invoice outside this reminder thread.",
                        requires_review=True,
                        cited_invoice_ids=tuple(draft.invoice_ids),
                        prompt_version=decision.prompt_version,
                        model_name=decision.model_name,
                    )
            except Exception as exc:  # noqa: BLE001 - classification failures pause safely
                decision = CustomerReplyDecision(
                    intent=CustomerReplyIntent.UNCLEAR,
                    confidence=0.0,
                    explanation=f"Reply classification failed safely: {type(exc).__name__}",
                    requires_review=True,
                    cited_invoice_ids=tuple(draft.invoice_ids),
                    prompt_version="customer-reply-fallback-v1",
                    model_name=self.agent.model_name,
                )

        unsafe_reply = (
            decision.intent
            in {
                CustomerReplyIntent.ALREADY_PAID,
                CustomerReplyIntent.DISPUTE,
                CustomerReplyIntent.UNCLEAR,
            }
            or decision.confidence < self.confidence_threshold
        )
        paused_invoice_ids: tuple[str, ...] = ()
        if unsafe_reply:
            payment_state = None
            if decision.intent == CustomerReplyIntent.ALREADY_PAID:
                payment_state = PaymentState.PAYMENT_CLAIMED.value
            elif decision.intent == CustomerReplyIntent.DISPUTE:
                payment_state = PaymentState.DISPUTED.value
            await self.repository.pause_invoices(
                owner_id,
                draft.invoice_ids,
                payment_state=payment_state,
                reason=decision.explanation,
            )
            paused_invoice_ids = tuple(draft.invoice_ids)
            if draft.status not in {DraftStatus.SENT, DraftStatus.REJECTED}:
                draft.status = DraftStatus.PAUSED
                draft.updated_at = utcnow()
                await self.repository.save_draft(draft)

        task = ReviewTaskRecord(
            id=_uuid(),
            owner_id=owner_id,
            kind=ReviewTaskKind.CUSTOMER_REPLY,
            reason=decision.explanation,
            payload={
                "source_message_id": source_message_id,
                "gmail_thread_id": gmail_thread_id,
                "new_reply_text": new_text,
                "decision": _serialize(decision),
                "automatic_customer_reply_sent": False,
            },
            customer_id=customer.id,
            invoice_ids=tuple(draft.invoice_ids),
        )
        await self.repository.create_review_task(task)
        await self.repository.append_audit(
            AuditEvent(
                owner_id=owner_id,
                event_type="customer_reply_decided",
                actor_type="customer",
                actor_id=source_message_id,
                entity_type="review_task",
                entity_id=task.id,
                decision={
                    "classification": _serialize(decision),
                    "paused_invoice_ids": list(paused_invoice_ids),
                    "automatic_customer_reply_sent": False,
                },
                source_evidence=(
                    {
                        "kind": "source_message",
                        "id": source_message_id,
                        "gmail_thread_id": gmail_thread_id,
                    },
                ),
                model_name=decision.model_name,
                prompt_version=decision.prompt_version,
            )
        )
        owner = await self.repository.get_owner(owner_id)
        await self.mail.notify_owner(
            owner=owner,
            subject=f"Topline review needed: reply from {customer.name}",
            text_body=(
                f"Topline received a customer reply and sent no automatic response.\n\n"
                f"Classification: {decision.intent}\nReason: {decision.explanation}\n\n{new_text}"
            ),
            html_body=(
                "<p><strong>Topline sent no automatic customer response.</strong></p>"
                f"<p>Classification: {html.escape(str(decision.intent))}<br>"
                f"Reason: {html.escape(decision.explanation)}</p>"
            ),
            thread_id=None,
        )
        return CustomerReplyResult(
            decision=decision,
            review_task=task,
            paused_invoice_ids=paused_invoice_ids,
        )
