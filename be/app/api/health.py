"""Liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import AppSettings, DbSession
from app.schemas import HealthResponse, ReadinessResponse
from app.services.crypto import TokenEncryptionError, encrypt_token

router = APIRouter(tags=["health"])

API_VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(settings: AppSettings) -> HealthResponse:
    """Process is up. Does not touch the database, so it stays fast under load."""
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        environment=settings.environment,
        version=API_VERSION,
    )


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(session: DbSession, settings: AppSettings) -> ReadinessResponse:
    """Report whether each dependency is usable. Never raises - it reports."""
    try:
        await session.execute(text("SELECT 1"))
        database = "ok"
    except Exception as exc:
        database = f"error: {type(exc).__name__}"

    try:
        encrypt_token("probe")
        token_encryption = "ok"
    except TokenEncryptionError as exc:
        token_encryption = f"error: {exc}"

    google = "configured" if settings.google_oauth_configured else "not_configured"
    razorpay = "configured" if settings.razorpay_configured else "not_configured"
    passed = database == "ok" and token_encryption == "ok"

    return ReadinessResponse(
        status="ready" if passed else "degraded",
        database=database,
        google_oauth=google,
        razorpay=razorpay,
        token_encryption=token_encryption,
        checks_passed=passed,
    )
