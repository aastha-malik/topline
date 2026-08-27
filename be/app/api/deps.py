"""Shared FastAPI dependencies.

Auth note: the demo is single-owner, so `resolve_owner` falls back to the workspace's owner
when no Supabase Auth context is present. The seam is deliberate - when Supabase Auth is
wired up, verifying the JWT here is the only change needed, and every route that already
depends on `CurrentOwner` becomes authenticated at once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.models import GmailAccount, User, Workspace

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@dataclass(slots=True)
class CurrentOwner:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    email: str


async def resolve_owner(
    session: DbSession,
    x_owner_id: Annotated[str | None, Header(alias="X-Owner-Id")] = None,
) -> CurrentOwner:
    """Resolve the acting owner.

    `X-Owner-Id` selects an owner explicitly (useful for the demo and for tests). Without
    it, the single seeded owner is used. Replace the fallback with Supabase JWT verification
    before this is exposed to more than one tenant.
    """
    if x_owner_id:
        try:
            owner_uuid = uuid.UUID(x_owner_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="X-Owner-Id must be a UUID"
            ) from None
        user = await session.scalar(select(User).where(User.id == owner_uuid))
    else:
        user = await session.scalar(
            select(User).where(User.role == "owner").order_by(User.created_at).limit(1)
        )

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No owner found. Connect a Gmail account first (POST /api/v1/auth/google/start).",
        )
    return CurrentOwner(user_id=user.id, workspace_id=user.workspace_id, email=user.email)


Owner = Annotated[CurrentOwner, Depends(resolve_owner)]


async def resolve_owner_id_for_agent_layer(owner: Owner) -> str:
    """Adapt the platform owner dependency to the agent router's tenant-safe ID."""

    return str(owner.user_id)


async def get_gmail_account(
    session: AsyncSession, owner: CurrentOwner, account_id: uuid.UUID | None = None
) -> GmailAccount:
    """Load a connected Gmail account for the owner's workspace."""
    stmt = select(GmailAccount).where(GmailAccount.workspace_id == owner.workspace_id)
    if account_id is not None:
        stmt = stmt.where(GmailAccount.id == account_id)
    else:
        stmt = stmt.order_by(GmailAccount.created_at).limit(1)

    account = await session.scalar(stmt)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No connected Gmail account. Complete the Google OAuth flow first.",
        )
    return account


async def ensure_workspace(session: AsyncSession, name: str = "Topline Workspace") -> Workspace:
    """Return the demo workspace, creating it on first connect."""
    workspace = await session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
    if workspace is None:
        workspace = Workspace(name=name, business_name=name, sender_name=name)
        session.add(workspace)
        await session.flush()
    return workspace
