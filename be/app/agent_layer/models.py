"""SQLAlchemy mappings for agent-owned tables only.

Canonical owner/customer/invoice/source/payment data and the audit log remain in
``app.models``. These mappings share the platform ``Base`` so one metadata graph
covers the whole backend.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import JSONVariant


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )


class AgentDigest(Base):
    __tablename__ = "agent_digests"
    __table_args__ = (
        sa.UniqueConstraint("owner_id", "run_date", name="uq_agent_digests_owner_date"),
        sa.CheckConstraint("status IN ('building', 'sent', 'failed')", name="ck_agent_digests_status"),
        sa.CheckConstraint(
            "total_outstanding_paise >= 0", name="ck_agent_digests_total_non_negative"
        ),
        sa.CheckConstraint("customer_count >= 0", name="ck_agent_digests_count_non_negative"),
        sa.Index(
            "ux_agent_digests_owner_thread",
            "owner_id",
            "gmail_thread_id",
            unique=True,
            postgresql_where=sa.text("gmail_thread_id IS NOT NULL"),
            sqlite_where=sa.text("gmail_thread_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="building")
    gmail_thread_id: Mapped[str | None] = mapped_column(sa.Text)
    owner_message_id: Mapped[str | None] = mapped_column(sa.Text)
    total_outstanding_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, server_default="0")
    customer_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AgentDigestItem(Base):
    __tablename__ = "agent_digest_items"
    __table_args__ = (
        sa.UniqueConstraint("digest_id", "item_number", name="uq_agent_digest_items_number"),
        sa.UniqueConstraint("digest_id", "customer_id", name="uq_agent_digest_items_customer"),
        sa.CheckConstraint(
            "status IN ('actionable', 'drafted', 'skipped', 'paused')",
            name="ck_agent_digest_items_status",
        ),
        sa.CheckConstraint("item_number > 0", name="ck_agent_digest_items_number_positive"),
        sa.CheckConstraint("amount_paise > 0", name="ck_agent_digest_items_amount_positive"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("agent_digests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    invoice_ids: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    oldest_due_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    recommendation_reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_references: Mapped[list[dict[str, Any]]] = mapped_column(JSONVariant, nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="actionable")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AgentDraft(Base):
    __tablename__ = "agent_drafts"
    __table_args__ = (
        sa.UniqueConstraint("digest_id", "draft_number", name="uq_agent_drafts_number"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'sending', 'rejected', 'sent', 'failed', 'paused')",
            name="ck_agent_drafts_status",
        ),
        sa.CheckConstraint("draft_number > 0", name="ck_agent_drafts_number_positive"),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'sending', 'sent') OR "
            "(approved_by IS NOT NULL AND approved_at IS NOT NULL AND approved_content_hash IS NOT NULL)",
            name="ck_agent_drafts_approval",
        ),
        sa.CheckConstraint(
            "status <> 'sent' OR (sent_at IS NOT NULL AND send_result IS NOT NULL)",
            name="ck_agent_drafts_sent",
        ),
        sa.Index("ix_agent_drafts_owner_status", "owner_id", "status", "created_at"),
        sa.Index(
            "ix_agent_drafts_customer_thread",
            "owner_id",
            "customer_thread_id",
            postgresql_where=sa.text("customer_thread_id IS NOT NULL"),
            sqlite_where=sa.text("customer_thread_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("agent_digests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    digest_item_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("agent_digest_items.id", ondelete="RESTRICT"), nullable=False
    )
    draft_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    customer_email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    invoice_ids: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    subject: Mapped[str] = mapped_column(sa.Text, nullable=False)
    text_body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    tone: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="pending")
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    agent_decision: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rendered_html: Mapped[str] = mapped_column(sa.Text, nullable=False)
    customer_thread_id: Mapped[str | None] = mapped_column(sa.Text)
    approved_by: Mapped[str | None] = mapped_column(sa.Text)
    approval_source: Mapped[str | None] = mapped_column(sa.Text)
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_content_hash: Mapped[str | None] = mapped_column(sa.Text)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    send_result: Mapped[dict[str, Any] | None] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AgentReviewTask(Base):
    __tablename__ = "agent_review_tasks"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('owner_command_clarification', 'customer_reply', 'send_failure')",
            name="ck_agent_review_tasks_kind",
        ),
        sa.CheckConstraint("status IN ('open', 'resolved', 'dismissed')", name="ck_agent_review_tasks_status"),
        sa.Index(
            "ix_agent_review_tasks_owner_open",
            "owner_id",
            "created_at",
            postgresql_where=sa.text("status = 'open'"),
            sqlite_where=sa.text("status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONVariant, nullable=False, default=dict)
    digest_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("agent_digests.id", ondelete="CASCADE")
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("customers.id", ondelete="SET NULL")
    )
    invoice_ids: Mapped[list[str]] = mapped_column(JSONVariant, nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="open")
    created_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
