"""Shared FastAPI dependencies.

Auth: every owner-facing route depends on `CurrentOwner`, resolved from the session cookie
minted by the Google OAuth callback (`app.services.session`). There is no unauthenticated
fallback - a request without a valid session is rejected. `X-Owner-Id` is honoured only as
a local/test convenience, and only when `ALLOW_OWNER_HEADER=true`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.models import GmailAccount, User
from app.services.session import SESSION_COOKIE, read_session

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


@dataclass(slots=True)
class CurrentOwner:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    email: str


def _owner_header_id(request: Request, settings: Settings) -> uuid.UUID | None:
    """The dev-only `X-Owner-Id` seam. Off unless explicitly enabled outside production."""
    if not settings.allow_owner_header or settings.environment not in ("local", "test"):
        return None
    raw = request.headers.get("X-Owner-Id")
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="X-Owner-Id must be a UUID"
        ) from None


async def resolve_owner(
    session: DbSession,
    request: Request,
    settings: AppSettings,
) -> CurrentOwner:
    """Resolve the acting owner from the session cookie (or the dev header seam)."""
    user_id = read_session(request.cookies.get(SESSION_COOKIE), settings)
    if user_id is None:
        user_id = _owner_header_id(request, settings)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not signed in. Connect a Google account to continue.",
        )

    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session is no longer valid. Sign in again.",
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
