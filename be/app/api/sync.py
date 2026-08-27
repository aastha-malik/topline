"""Gmail ingestion and Razorpay reconciliation endpoints.

Every scheduled job has a manual endpoint here, so a demo never waits on the clock.
"""

from __future__ import annotations


from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import AppSettings, DbSession, Owner, get_gmail_account
from app.enums import AccountStatus
from app.logging_config import get_logger
from app.models import GmailAccount, SyncRun
from app.schemas import (
    BackfillRequest,
    ReconcileResponse,
    SyncRequest,
    SyncResultResponse,
    SyncRunResponse,
)
from app.services import razorpay_sync
from app.services.crypto import TokenEncryptionError, decrypt_token, encrypt_token
from app.services.gmail import (
    GmailAuthError,
    GmailClient,
    GoogleApiTransport,
    refresh_access_token,
)
from app.services.ingest import IngestionPipeline

logger = get_logger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])


async def _build_client(session, settings, account: GmailAccount) -> GmailClient:
    """Build a Gmail client, refreshing the access token when it has expired."""
    from datetime import datetime, timedelta, timezone

    if account.status != AccountStatus.CONNECTED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Gmail account {account.email_address} is {account.status}; reconnect it.",
        )

    try:
        access_token = decrypt_token(account.access_token_encrypted)
        refresh_token = decrypt_token(account.refresh_token_encrypted)
    except TokenEncryptionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    expiry = account.token_expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    expired = expiry is None or expiry <= datetime.now(timezone.utc) + timedelta(seconds=60)

    if expired and refresh_token:
        try:
            tokens = await refresh_access_token(settings, refresh_token)
        except GmailAuthError as exc:
            account.status = str(AccountStatus.NEEDS_REAUTH)
            await session.flush()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Gmail authorization expired; reconnect the account. ({exc})",
            ) from exc
        access_token = tokens.access_token
        account.access_token_encrypted = encrypt_token(tokens.access_token)
        account.token_expiry = tokens.expires_at
        await session.flush()

    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No usable Gmail access token; reconnect the account.",
        )
    return GmailClient(GoogleApiTransport(access_token))


@router.post(
    "/backfill",
    response_model=SyncResultResponse,
    summary="Backfill finance-relevant Gmail history",
)
async def run_backfill(
    session: DbSession, settings: AppSettings, owner: Owner, payload: BackfillRequest
) -> SyncResultResponse:
    """Ingest the owner-selected history window.

    Metadata is fetched for every candidate; full content and PDF attachments are fetched
    only for messages that score as finance-relevant. Re-running is safe: already-processed
    messages are skipped and ledger upserts are keyed.
    """
    account = await get_gmail_account(session, owner, payload.gmail_account_id)
    client = await _build_client(session, settings, account)
    pipeline = IngestionPipeline(session, account=account, client=client, settings=settings)
    outcome = await pipeline.run_backfill(months=payload.months)
    return SyncResultResponse(**outcome.as_dict() | {"sync_run_id": outcome.sync_run_id})


@router.post(
    "/incremental",
    response_model=SyncResultResponse,
    summary="Incremental Gmail sync via stored history id",
)
async def run_incremental(
    session: DbSession, settings: AppSettings, owner: Owner, payload: SyncRequest
) -> SyncResultResponse:
    """Sync new mail since the stored `historyId`.

    If Gmail rejects the cursor - it expires after an idle period - this falls back to a
    date-scoped resync rather than a full mailbox re-read.
    """
    account = await get_gmail_account(session, owner, payload.gmail_account_id)
    client = await _build_client(session, settings, account)
    pipeline = IngestionPipeline(session, account=account, client=client, settings=settings)
    outcome = await pipeline.run_incremental()
    return SyncResultResponse(**outcome.as_dict() | {"sync_run_id": outcome.sync_run_id})


@router.get(
    "/runs",
    response_model=list[SyncRunResponse],
    summary="Recent sync runs",
)
async def list_sync_runs(
    session: DbSession, owner: Owner, limit: int = Query(default=20, ge=1, le=100)
) -> list[SyncRunResponse]:
    runs = (
        await session.scalars(
            select(SyncRun)
            .where(SyncRun.workspace_id == owner.workspace_id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        )
    ).all()
    return [SyncRunResponse.model_validate(r) for r in runs]


@router.post(
    "/razorpay/reconcile",
    response_model=ReconcileResponse,
    summary="Retry unmatched Razorpay payment events",
)
async def reconcile_razorpay(
    session: DbSession, settings: AppSettings, owner: Owner
) -> ReconcileResponse:
    """Match stored-but-unreconciled Razorpay events against the ledger.

    Worth running after every Gmail sync: a payment confirmation often arrives before the
    invoice it belongs to has been extracted from the mailbox.
    """
    results = await razorpay_sync.reconcile_pending(
        session,
        workspace_id=owner.workspace_id,
        owner_id=owner.user_id,
        settings=settings,
    )
    return ReconcileResponse(
        reconciled=len(results), results=[r.as_dict() for r in results]
    )
