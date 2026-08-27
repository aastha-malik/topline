"""FastAPI application entrypoint.

Owns the app scaffold: settings, logging, CORS, the request-id middleware, error handling
and router registration. The agent layer mounts itself through
``app.agent_layer.api.create_agent_router`` and is attached here defensively, so the
platform API keeps serving even while that side is mid-development.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, health, ledger, sync, webhooks
from app.config import get_settings
from app.db import dispose_engine
from app.logging_config import configure_logging, get_logger, request_id_var

logger = get_logger(__name__)

DESCRIPTION = """
Topline is a Gmail-native revenue recovery agent.

This service covers the **platform and ingestion** half of the product:

* Google OAuth for Gmail with least-privilege scopes (`gmail.readonly`; `gmail.send` is
  provisioned for the approval-gated sender but never used here)
* historical backfill and incremental sync, filtered to finance-relevant mail
* invoice and payment-evidence extraction from email bodies and attached PDFs
* a source-backed receivables ledger with a deterministic state machine
* Razorpay test-mode webhooks as an optional payment-confirmation source

**Evidence policy:** Gmail evidence is never presented as guaranteed payment truth. An
email claiming payment produces `payment_claimed` and pauses follow-ups; only a payment
provider can produce `confirmed_paid`.

Digest, draft, approval and send endpoints are owned by the agent layer and appear under
their own tags when that module is available.
"""

TAGS_METADATA = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Google OAuth and Gmail connection management."},
    {"name": "sync", "description": "Gmail backfill/incremental sync and Razorpay reconciliation."},
    {"name": "ledger", "description": "Customers, invoices, evidence, source messages, audit trail."},
    {"name": "webhooks", "description": "Razorpay test-mode webhook receiver."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    logger.info(
        "topline api starting",
        extra={"environment": settings.environment, "scopes": settings.google_oauth_scopes},
    )
    if not settings.token_encryption_key:
        logger.warning(
            "TOKEN_ENCRYPTION_KEY is unset; Gmail token storage will fail. "
            "Generate one with: python -m app.keygen"
        )
    agent_scheduler = None
    if settings.enable_scheduler:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            from app.agent_layer.scheduler import register_daily_cycle_job
            from app.agent_layer.wiring import get_default_agent_orchestrator

            service = get_default_agent_orchestrator()
            agent_scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
            register_daily_cycle_job(
                scheduler=agent_scheduler,
                service=service,
                list_connected_owner_ids=service.repository.list_connected_owner_ids,
                hour_ist=settings.digest_hour_ist,
            )
            agent_scheduler.start()
            logger.info("agent daily scheduler started", extra={"hour_ist": settings.digest_hour_ist})
        except Exception as exc:  # noqa: BLE001 - optional scheduler must not block API startup
            logger.warning(
                "agent scheduler not started",
                extra={"reason": f"{type(exc).__name__}: {exc}"},
            )
    try:
        yield
    finally:
        if agent_scheduler is not None:
            agent_scheduler.shutdown(wait=False)
        await dispose_engine()
        logger.info("topline api stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Topline API",
        description=DESCRIPTION,
        version=health.API_VERSION,
        lifespan=lifespan,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id to every log line and response."""
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals (or decrypted tokens) into an HTTP response body.
        logger.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "error_code": "internal_error"},
        )

    prefix = settings.api_v1_prefix
    app.include_router(health.router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(sync.router, prefix=prefix)
    app.include_router(ledger.router, prefix=prefix)
    app.include_router(webhooks.router, prefix=prefix)

    _mount_agent_layer(app, prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": "topline-api",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": f"{prefix}/health",
        }

    return app


def _mount_agent_layer(app: FastAPI, prefix: str) -> None:
    """Attach the agent developer's router when it is importable.

    Kept optional on purpose: ingestion and the ledger must stay serviceable even if the
    Gemini/approval side is unavailable (missing SDK, missing API key, mid-refactor).
    """
    try:
        from app.agent_layer.api import (
            create_agent_router,
            register_agent_exception_handlers,
        )
        from app.agent_layer.wiring import get_default_agent_orchestrator
    except Exception as exc:  # noqa: BLE001 - optional layer must not block platform API
        logger.warning("agent layer not mounted", extra={"reason": f"{type(exc).__name__}: {exc}"})
        return

    try:
        from app.api.deps import resolve_owner_id_for_agent_layer

        router = create_agent_router(
            get_orchestrator=get_default_agent_orchestrator,
            get_owner_id=resolve_owner_id_for_agent_layer,
        )
        app.include_router(router, prefix=prefix)
        register_agent_exception_handlers(app)
        logger.info("agent layer mounted")
    except Exception as exc:  # noqa: BLE001 - optional layer must not block platform API
        logger.warning(
            "agent layer present but could not be mounted",
            extra={"reason": f"{type(exc).__name__}: {exc}"},
        )


app = create_app()
