"""Razorpay webhook ingestion.

Razorpay retries any non-2xx delivery, so this endpoint returns 200 for duplicates and for
events it deliberately ignores. It returns a 4xx only when the request itself is not
trustworthy - an invalid signature or unparseable body - because retrying those is correct.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import AppSettings, DbSession
from app.logging_config import get_logger
from app.models import User, Workspace
from app.schemas import WebhookAckResponse
from app.services import razorpay_sync
from app.services.razorpay_sync import WebhookSignatureError

logger = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/razorpay",
    response_model=WebhookAckResponse,
    summary="Razorpay webhook receiver (test mode)",
)
async def razorpay_webhook(
    request: Request,
    session: DbSession,
    settings: AppSettings,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: str | None = Header(default=None, alias="X-Razorpay-Event-Id"),
) -> WebhookAckResponse:
    """Verify, store and reconcile a Razorpay event.

    The raw body is read before parsing because the HMAC is computed over the exact bytes
    Razorpay sent; re-serialising the JSON would break the signature.
    """
    raw_body = await request.body()

    try:
        razorpay_sync.verify_webhook_signature(
            raw_body, x_razorpay_signature, settings.razorpay_webhook_secret
        )
    except WebhookSignatureError as exc:
        logger.warning("rejected razorpay webhook", extra={"reason": str(exc)})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid webhook: {exc}"
        ) from exc

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not valid JSON"
        ) from exc

    event = razorpay_sync.parse_webhook_payload(payload, event_id=x_razorpay_event_id)
    if event is None:
        # Acknowledged so Razorpay stops retrying an event we have no rule for.
        return WebhookAckResponse(
            received=True,
            event_id=x_razorpay_event_id,
            processed=False,
            detail=f"Ignored unhandled event type: {payload.get('event')}",
        )

    workspace = await session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
    if workspace is None:
        # Nothing to reconcile against yet; 200 keeps Razorpay from retrying forever.
        return WebhookAckResponse(
            received=True,
            event_id=event.event_id,
            processed=False,
            detail="No workspace is provisioned yet; event discarded",
        )
    owner_id = await session.scalar(
        select(User.id)
        .where(User.workspace_id == workspace.id, User.role == "owner")
        .order_by(User.created_at)
        .limit(1)
    )

    result = await razorpay_sync.ingest_event(
        session,
        workspace_id=workspace.id,
        owner_id=owner_id,
        event=event,
        settings=settings,
    )
    return WebhookAckResponse(
        received=True,
        event_id=result.event_id,
        processed=result.accepted,
        duplicate=result.duplicate,
        matched_invoice_id=result.matched_invoice_id,
        resulting_state=result.resulting_state,
        detail=result.detail,
    )
