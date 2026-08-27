"""Append-only activity log.

Audit writes are idempotent: a caller supplies a `dedupe_key` derived from the thing that
happened, so replaying a sync re-emits the same key and the duplicate insert is dropped.
Re-running a backfill must not double the owner's history.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ActorType
from app.models import ActivityLog
from app.services.db_utils import insert_ignore


def audit_key(*parts: Any) -> str:
    """Build a stable dedupe key from the identity of an event."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record_event(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    event_type: str,
    summary: str,
    owner_id: uuid.UUID | None = None,
    actor_type: str = ActorType.SYSTEM,
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    decision: dict[str, Any] | None = None,
    source_evidence: Sequence[dict[str, Any]] = (),
    model_name: str | None = None,
    prompt_version: str | None = None,
    dedupe_key: str | None = None,
) -> bool:
    """Append one audit row. Returns True when it was written (False = already recorded)."""
    values = {
        "id": uuid.uuid4(),
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "event_type": event_type,
        "actor_type": str(actor_type),
        "actor_id": actor_id,
        "entity_type": entity_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "summary": summary,
        "decision": decision or {},
        "source_evidence": list(source_evidence),
        "model_name": model_name,
        "prompt_version": prompt_version,
        "dedupe_key": dedupe_key,
    }
    if dedupe_key is None:
        session.add(ActivityLog(**values))
        return True
    return await insert_ignore(
        session,
        ActivityLog.__table__,
        values,
        index_elements=["workspace_id", "dedupe_key"],
    )
