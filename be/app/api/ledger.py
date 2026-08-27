"""Read endpoints over the receivables ledger.

These are the evidence surface: every invoice can be traced back to the Gmail message or
PDF it came from, which is what makes the agent's later recommendations auditable.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Sequence

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import DbSession, Owner
from app.enums import EffectiveState, PaymentState, ReminderState
from app.models import (
    ActivityLog,
    Customer,
    Invoice,
    InvoiceSourceLink,
    SourceAttachment,
    SourceMessage,
)
from app.schemas import (
    ActivityLogResponse,
    AttachmentResponse,
    CustomerDossierResponse,
    CustomerResponse,
    EvidenceResponse,
    InvoiceResponse,
    InvoiceWithEvidenceResponse,
    LedgerSummaryResponse,
    SourceMessageResponse,
)

router = APIRouter(tags=["ledger"])


def _to_invoice_response(invoice: Invoice) -> InvoiceResponse:
    # `effective_state` is a property on the model, so from_attributes picks it up.
    return InvoiceResponse.model_validate(invoice)


def _to_invoice_with_evidence(
    invoice: Invoice, links: Sequence[InvoiceSourceLink]
) -> InvoiceWithEvidenceResponse:
    return InvoiceWithEvidenceResponse(
        **InvoiceResponse.model_validate(invoice).model_dump(),
        evidence=[EvidenceResponse.model_validate(link) for link in links],
    )


@router.get("/customers", response_model=list[CustomerResponse], summary="List customers")
async def list_customers(
    session: DbSession, owner: Owner, limit: int = Query(default=100, ge=1, le=500)
) -> list[CustomerResponse]:
    customers = (
        await session.scalars(
            select(Customer)
            .where(Customer.workspace_id == owner.workspace_id, Customer.is_archived.is_(False))
            .order_by(Customer.name)
            .limit(limit)
        )
    ).all()
    return [CustomerResponse.model_validate(c) for c in customers]


@router.get(
    "/customers/{customer_id}/dossier",
    response_model=CustomerDossierResponse,
    summary="Customer dossier: invoices, evidence and recent correspondence",
)
async def customer_dossier(
    session: DbSession, owner: Owner, customer_id: uuid.UUID
) -> CustomerDossierResponse:
    """The full context for one customer.

    This is the read model the agent layer's `draft_reminder` is expected to build on, so
    it deliberately ships the evidence alongside the numbers.
    """
    customer = await session.scalar(
        select(Customer).where(
            Customer.id == customer_id, Customer.workspace_id == owner.workspace_id
        )
    )
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    invoices = (
        await session.scalars(
            select(Invoice)
            .where(Invoice.workspace_id == owner.workspace_id, Invoice.customer_id == customer.id)
            .order_by(Invoice.due_date.desc().nullslast())
        )
    ).all()

    enriched: list[InvoiceWithEvidenceResponse] = []
    for invoice in invoices:
        links = (
            await session.scalars(
                select(InvoiceSourceLink)
                .where(InvoiceSourceLink.invoice_id == invoice.id)
                .order_by(InvoiceSourceLink.created_at)
            )
        ).all()
        enriched.append(_to_invoice_with_evidence(invoice, links))

    # Correspondence for this customer: mail they sent, plus any message already cited as
    # evidence on one of their invoices (which covers invoices the owner sent out).
    cited_message_ids = select(InvoiceSourceLink.source_message_id).where(
        InvoiceSourceLink.invoice_id.in_([i.id for i in invoices] or [uuid.uuid4()]),
        InvoiceSourceLink.source_message_id.is_not(None),
    )
    messages = (
        await session.scalars(
            select(SourceMessage)
            .where(
                SourceMessage.workspace_id == owner.workspace_id,
                SourceMessage.is_finance_relevant.is_(True),
                (SourceMessage.from_email == customer.primary_email)
                | SourceMessage.id.in_(cited_message_ids),
            )
            .order_by(SourceMessage.internal_date.desc().nullslast())
            .limit(20)
        )
    ).all()

    outstanding = sum(
        i.balance_paise or 0
        for i in invoices
        if i.payment_state != PaymentState.CONFIRMED_PAID
    )
    return CustomerDossierResponse(
        customer=CustomerResponse.model_validate(customer),
        invoices=enriched,
        total_outstanding_paise=outstanding,
        open_invoice_count=sum(
            1 for i in invoices if i.payment_state != PaymentState.CONFIRMED_PAID
        ),
        recent_messages=[SourceMessageResponse.model_validate(m) for m in messages],
    )


@router.get("/invoices", response_model=list[InvoiceResponse], summary="List invoices")
async def list_invoices(
    session: DbSession,
    owner: Owner,
    state: EffectiveState | None = Query(
        default=None, description="Filter by the derived seven-valued state."
    ),
    customer_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[InvoiceResponse]:
    """List invoices, optionally filtered by the derived state.

    `ready_for_reminder` is the daily queue; `likely_unpaid` is everything not yet due or
    on cooldown. Nothing here is ever presented as confirmed payment without a provider
    confirmation behind it.
    """
    stmt = select(Invoice).where(Invoice.workspace_id == owner.workspace_id)
    if customer_id is not None:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if state is EffectiveState.READY_FOR_REMINDER:
        stmt = stmt.where(
            Invoice.payment_state == str(PaymentState.LIKELY_UNPAID),
            Invoice.reminder_state == str(ReminderState.READY_FOR_REMINDER),
        )
    elif state in (EffectiveState.PAUSED, EffectiveState.LIKELY_UNPAID):
        # Both are stored as `paused`; `is_on_hold` separates a deliberate hold from an
        # invoice that is merely not actionable yet, so the split happens after loading.
        stmt = stmt.where(
            Invoice.payment_state == str(PaymentState.LIKELY_UNPAID),
            Invoice.reminder_state == str(ReminderState.PAUSED),
        )
    elif state is not None:
        stmt = stmt.where(Invoice.payment_state == str(state))

    invoices = (
        await session.scalars(stmt.order_by(Invoice.due_date.asc().nullslast()).limit(limit))
    ).all()
    if state in (EffectiveState.PAUSED, EffectiveState.LIKELY_UNPAID):
        invoices = [i for i in invoices if i.effective_state == state]
    return [_to_invoice_response(i) for i in invoices]


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceWithEvidenceResponse,
    summary="One invoice with its full evidence trail",
)
async def get_invoice(
    session: DbSession, owner: Owner, invoice_id: uuid.UUID
) -> InvoiceWithEvidenceResponse:
    invoice = await session.scalar(
        select(Invoice).where(
            Invoice.id == invoice_id, Invoice.workspace_id == owner.workspace_id
        )
    )
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")

    links = (
        await session.scalars(
            select(InvoiceSourceLink)
            .where(InvoiceSourceLink.invoice_id == invoice.id)
            .order_by(InvoiceSourceLink.created_at)
        )
    ).all()
    return _to_invoice_with_evidence(invoice, links)


@router.get(
    "/invoices/{invoice_id}/source",
    response_model=list[AttachmentResponse],
    summary="Attachments behind an invoice",
)
async def invoice_source_attachments(
    session: DbSession, owner: Owner, invoice_id: uuid.UUID
) -> list[AttachmentResponse]:
    rows = (
        await session.scalars(
            select(SourceAttachment)
            .join(
                InvoiceSourceLink,
                InvoiceSourceLink.source_attachment_id == SourceAttachment.id,
            )
            .where(
                InvoiceSourceLink.invoice_id == invoice_id,
                SourceAttachment.workspace_id == owner.workspace_id,
            )
        )
    ).all()
    return [AttachmentResponse.model_validate(r) for r in rows]


@router.get("/ledger/summary", response_model=LedgerSummaryResponse, summary="Ledger totals")
async def ledger_summary(session: DbSession, owner: Owner) -> LedgerSummaryResponse:
    invoices = (
        await session.scalars(
            select(Invoice).where(Invoice.workspace_id == owner.workspace_id)
        )
    ).all()
    customer_count = await session.scalar(
        select(func.count(Customer.id)).where(Customer.workspace_id == owner.workspace_id)
    )
    by_state = Counter(str(i.effective_state) for i in invoices)
    return LedgerSummaryResponse(
        total_outstanding_paise=sum(
            i.balance_paise or 0
            for i in invoices
            if i.payment_state != PaymentState.CONFIRMED_PAID
        ),
        customer_count=customer_count or 0,
        invoice_count=len(invoices),
        by_state=dict(by_state),
    )


@router.get(
    "/messages",
    response_model=list[SourceMessageResponse],
    summary="Source messages, including ones deliberately ignored",
)
async def list_source_messages(
    session: DbSession,
    owner: Owner,
    relevant_only: bool = Query(
        default=False, description="False also returns messages that were ignored, with reasons."
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SourceMessageResponse]:
    """Inspect what ingestion looked at.

    Ignored messages are listed with their scoring reasons and no body, which is how
    "unrelated mail is ignored and logged" stays verifiable.
    """
    stmt = select(SourceMessage).where(SourceMessage.workspace_id == owner.workspace_id)
    if relevant_only:
        stmt = stmt.where(SourceMessage.is_finance_relevant.is_(True))
    rows = (
        await session.scalars(
            stmt.order_by(SourceMessage.internal_date.desc().nullslast()).limit(limit)
        )
    ).all()
    return [SourceMessageResponse.model_validate(r) for r in rows]


@router.get(
    "/activity",
    response_model=list[ActivityLogResponse],
    summary="Audit trail, newest first",
)
async def list_activity(
    session: DbSession,
    owner: Owner,
    event_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ActivityLogResponse]:
    stmt = select(ActivityLog).where(ActivityLog.workspace_id == owner.workspace_id)
    if event_type:
        stmt = stmt.where(ActivityLog.event_type == event_type)
    rows = (
        await session.scalars(stmt.order_by(ActivityLog.occurred_at.desc()).limit(limit))
    ).all()
    return [ActivityLogResponse.model_validate(r) for r in rows]
