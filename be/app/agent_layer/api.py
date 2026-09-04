# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from .errors import ApprovalRequiredError, NotFoundError, UnsafeActionError
from .service import AgentOrchestrator

# FastAPI dependencies are intentionally declared as default values. The router
# factory closes over integration-provided callables, so postponed Annotated
# dependencies cannot resolve them from module globals.


class RunDailyCycleRequest(BaseModel):
    run_date: date = Field(default_factory=date.today)


class BuildDailyQueueRequest(BaseModel):
    # Omitted by the dashboard so the service resolves "today" in IST itself.
    run_date: date | None = None


class DraftDigestItemRequest(BaseModel):
    tone: str = Field(default="normal", max_length=40)
    note: str | None = Field(default=None, max_length=2_000)


class OwnerReplyRequest(BaseModel):
    body: str = Field(min_length=1)
    digest_id: str | None = None
    gmail_thread_id: str | None = None
    source_message_id: str | None = None

    @model_validator(mode="after")
    def identify_digest(self) -> OwnerReplyRequest:
        if not self.digest_id and not self.gmail_thread_id:
            raise ValueError("digest_id or gmail_thread_id is required")
        return self


class CustomerReplyRequest(BaseModel):
    gmail_thread_id: str = Field(min_length=1)
    body: str
    source_message_id: str = Field(min_length=1)


class EditDraftRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    text_body: str = Field(min_length=1, max_length=20_000)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2_000)


def _payload(value: Any) -> Any:
    return jsonable_encoder(asdict(value) if is_dataclass(value) else value)


def create_agent_router(
    *,
    get_orchestrator: Callable[[], AgentOrchestrator | Awaitable[AgentOrchestrator]],
    get_owner_id: Callable[[], str | Awaitable[str]],
) -> APIRouter:
    """Create a router mounted by the developer-owned FastAPI app.

    ``get_owner_id`` must derive the owner from authenticated Supabase context;
    no endpoint trusts an owner id supplied by the client.
    """

    router = APIRouter(prefix="/agent", tags=["agent"])

    @router.post("/daily-cycles/run", status_code=status.HTTP_201_CREATED)
    async def run_daily_cycle(
        request: RunDailyCycleRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(await service.run_daily_cycle(owner_id=owner_id, run_date=request.run_date))

    @router.post("/daily-queue")
    async def build_daily_queue(
        request: BuildDailyQueueRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        """The dashboard's daily review: build today's actionable queue, send nothing."""
        result = await service.get_daily_queue(owner_id=owner_id, run_date=request.run_date)
        drafts = await service.repository.list_drafts(owner_id, result.digest.id)
        return _payload(
            {"digest": result.digest, "items": list(result.items), "drafts": drafts}
        )

    @router.post("/digest-items/{digest_item_id}/draft", status_code=status.HTTP_201_CREATED)
    async def draft_digest_item(
        digest_item_id: str,
        request: DraftDigestItemRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.draft_digest_item(
                owner_id=owner_id,
                digest_item_id=digest_item_id,
                tone=request.tone,
                note=request.note,
                actor_id=owner_id,
            )
        )

    @router.get("/daily-cycles/{digest_id}")
    async def view_daily_cycle(
        digest_id: str,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        digest = await service.repository.get_digest(owner_id, digest_id)
        items = await service.repository.list_digest_items(owner_id, digest_id)
        drafts = await service.repository.list_drafts(owner_id, digest_id)
        return _payload({"digest": digest, "items": items, "drafts": drafts})

    @router.get("/daily-cycles/{digest_id}/items")
    async def list_digest_items(
        digest_id: str,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(await service.repository.list_digest_items(owner_id, digest_id))

    @router.get("/drafts")
    async def list_drafts(
        digest_id: str | None = Query(default=None),
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(await service.repository.list_drafts(owner_id, digest_id))

    @router.patch("/drafts/{draft_id}")
    async def edit_draft(
        draft_id: str,
        request: EditDraftRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.edit_draft(
                owner_id=owner_id,
                draft_id=draft_id,
                subject=request.subject,
                text_body=request.text_body,
                actor_id=owner_id,
            )
        )

    @router.post("/drafts/{draft_id}/approve")
    async def approve_draft(
        draft_id: str,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.approve_draft(
                owner_id=owner_id,
                draft_id=draft_id,
                actor_id=owner_id,
                source="dashboard",
            )
        )

    @router.post("/drafts/{draft_id}/reject")
    async def reject_draft(
        draft_id: str,
        request: RejectRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.reject_draft(
                owner_id=owner_id,
                draft_id=draft_id,
                actor_id=owner_id,
                reason=request.reason,
            )
        )

    @router.post("/drafts/{draft_id}/send")
    async def send_draft(
        draft_id: str,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.send_approved_draft(
                owner_id=owner_id, draft_id=draft_id, actor_id=owner_id
            )
        )

    @router.post("/owner-replies")
    async def process_owner_reply(
        request: OwnerReplyRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.process_owner_reply(
                owner_id=owner_id,
                body=request.body,
                digest_id=request.digest_id,
                gmail_thread_id=request.gmail_thread_id,
                actor_message_id=request.source_message_id,
            )
        )

    @router.post("/customer-replies")
    async def process_customer_reply(
        request: CustomerReplyRequest,
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.process_customer_reply(
                owner_id=owner_id,
                gmail_thread_id=request.gmail_thread_id,
                body=request.body,
                source_message_id=request.source_message_id,
            )
        )

    @router.get("/customers/{customer_id}/dossier")
    async def get_customer_dossier(
        customer_id: str,
        invoice_id: list[str] | None = Query(default=None),
        owner_id: str = Depends(get_owner_id),
        service: AgentOrchestrator = Depends(get_orchestrator),
    ) -> Any:
        return _payload(
            await service.get_customer_dossier(
                owner_id=owner_id, customer_id=customer_id, invoice_ids=invoice_id
            )
        )

    return router


def register_agent_exception_handlers(app: FastAPI) -> None:
    """Register service-error mappings on the platform-owned FastAPI app."""

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ApprovalRequiredError)
    async def approval_handler(_request: Request, exc: ApprovalRequiredError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(UnsafeActionError)
    async def unsafe_handler(_request: Request, exc: UnsafeActionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
