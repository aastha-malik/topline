"""Google OAuth for Gmail.

Scopes requested: `openid`, `email`, `profile`, `gmail.readonly`, and optionally
`gmail.send`. `gmail.modify` is never requested - Topline reads mail and (later, behind an
approval gate) sends new mail; it has no reason to mutate the owner's mailbox.

The callback stores encrypted tokens and creates the workspace/owner records, but does not
start a backfill: ingestion is an explicit action (`POST /api/v1/sync/backfill`).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.deps import AppSettings, DbSession, Owner, ensure_workspace
from app.enums import AccountStatus, SyncStatus
from app.logging_config import get_logger
from app.models import GmailAccount, OAuthState, User
from app.schemas import AuthStartResponse, ConnectionStatusResponse, GmailAccountResponse
from app.services.audit import audit_key, record_event
from app.services.crypto import encrypt_token
from app.services.gmail import (
    GmailAuthError,
    build_authorization_url,
    exchange_code_for_tokens,
    fetch_userinfo,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/google/start",
    response_model=AuthStartResponse,
    summary="Begin the Google OAuth flow",
)
async def start_google_oauth(
    session: DbSession, settings: AppSettings
) -> AuthStartResponse:
    """Return the Google consent URL and persist a single-use CSRF state."""
    if not settings.google_oauth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )

    state = secrets.token_urlsafe(32)
    session.add(
        OAuthState(
            state=state,
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.oauth_state_ttl_seconds),
        )
    )
    await session.flush()

    return AuthStartResponse(
        authorization_url=build_authorization_url(settings, state),
        state=state,
        scopes=settings.google_oauth_scopes,
    )


@router.get("/google/callback", summary="Google OAuth redirect target")
async def google_oauth_callback(
    session: DbSession,
    settings: AppSettings,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Exchange the code for tokens and store the connected mailbox."""
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Google returned: {error}"
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Missing `code` or `state`"
        )

    stored = await session.scalar(select(OAuthState).where(OAuthState.state == state))
    now = datetime.now(timezone.utc)
    if stored is None or stored.consumed_at is not None or _expired(stored, now):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid, expired or already-used OAuth state. Restart the connection flow.",
        )
    stored.consumed_at = now  # single use

    try:
        tokens = await exchange_code_for_tokens(settings, code)
        userinfo = await fetch_userinfo(tokens.access_token)
    except GmailAuthError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    email = (userinfo.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google did not return an email address for this account",
        )

    account = await _link_account(session, settings, email, userinfo, tokens)

    await record_event(
        session,
        workspace_id=account.workspace_id,
        owner_id=account.user_id,
        event_type="gmail.account_connected",
        summary=f"Connected Gmail account {email}",
        entity_type="gmail_account",
        entity_id=str(account.id),
        decision={"scopes": account.granted_scopes, "status": account.status},
        dedupe_key=audit_key("gmail.account_connected", account.id, account.connected_at),
    )

    redirect = f"{settings.frontend_post_auth_redirect}?connected={email}"
    return RedirectResponse(url=redirect, status_code=status.HTTP_302_FOUND)


def _expired(stored: OAuthState, now: datetime) -> bool:
    expires_at = stored.expires_at
    if expires_at.tzinfo is None:  # SQLite round-trips naive datetimes
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at < now


async def _link_account(session, settings, email: str, userinfo: dict, tokens) -> GmailAccount:
    """Create or refresh the workspace, owner and Gmail account rows."""
    workspace = await ensure_workspace(session, name=userinfo.get("name") or "Topline Workspace")

    user = await session.scalar(
        select(User).where(User.workspace_id == workspace.id, User.email == email)
    )
    if user is None:
        user = User(
            workspace_id=workspace.id,
            email=email,
            name=userinfo.get("name"),
            role="owner",
            supabase_user_id=None,
        )
        session.add(user)
        await session.flush()

    account = await session.scalar(
        select(GmailAccount).where(
            GmailAccount.workspace_id == workspace.id, GmailAccount.email_address == email
        )
    )
    if account is None:
        account = GmailAccount(
            workspace_id=workspace.id,
            user_id=user.id,
            email_address=email,
            backfill_status=str(SyncStatus.PENDING),
        )
        session.add(account)

    account.google_sub = userinfo.get("sub") or account.google_sub
    account.access_token_encrypted = encrypt_token(tokens.access_token)
    # Google omits the refresh token on re-consent; keep the stored one if so.
    if tokens.refresh_token:
        account.refresh_token_encrypted = encrypt_token(tokens.refresh_token)
    account.token_expiry = tokens.expires_at
    account.granted_scopes = tokens.scopes or settings.google_oauth_scopes
    account.status = str(AccountStatus.CONNECTED)
    account.connected_at = datetime.now(timezone.utc)
    await session.flush()
    return account


@router.get(
    "/connection",
    response_model=ConnectionStatusResponse,
    summary="Gmail/Razorpay connection status",
)
async def connection_status(
    session: DbSession, settings: AppSettings, owner: Owner
) -> ConnectionStatusResponse:
    accounts = (
        await session.scalars(
            select(GmailAccount).where(GmailAccount.workspace_id == owner.workspace_id)
        )
    ).all()
    return ConnectionStatusResponse(
        connected=any(a.status == AccountStatus.CONNECTED for a in accounts),
        google_oauth_configured=settings.google_oauth_configured,
        razorpay_configured=settings.razorpay_configured,
        accounts=[GmailAccountResponse.model_validate(a) for a in accounts],
    )


@router.delete(
    "/connection/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Disconnect a Gmail account",
)
async def disconnect(session: DbSession, owner: Owner, account_id: uuid.UUID) -> None:
    """Revoke local access by discarding the stored tokens. Ledger data is retained."""
    account = await session.scalar(
        select(GmailAccount).where(
            GmailAccount.id == account_id, GmailAccount.workspace_id == owner.workspace_id
        )
    )
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    account.access_token_encrypted = None
    account.refresh_token_encrypted = None
    account.status = str(AccountStatus.DISCONNECTED)
    await session.flush()

    await record_event(
        session,
        workspace_id=owner.workspace_id,
        owner_id=owner.user_id,
        event_type="gmail.account_disconnected",
        summary=f"Disconnected Gmail account {account.email_address}",
        actor_type="user",
        actor_id=str(owner.user_id),
        entity_type="gmail_account",
        entity_id=str(account.id),
    )
