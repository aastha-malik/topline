from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .domain import DailyCycleResult
from .service import AgentOrchestrator


async def run_scheduled_daily_cycles(
    *,
    service: AgentOrchestrator,
    list_connected_owner_ids: Callable[[], Awaitable[Sequence[str]]],
    run_date: date | None = None,
) -> list[DailyCycleResult]:
    """Hook for the platform-owned APScheduler registration.

    The enclosing application remains responsible for registering this at
    09:00 Asia/Kolkata and for ensuring one scheduler leader in production.
    """

    cycle_date = run_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    results: list[DailyCycleResult] = []
    for owner_id in await list_connected_owner_ids():
        results.append(await service.run_daily_cycle(owner_id=owner_id, run_date=cycle_date))
    return results


def register_daily_cycle_job(
    *,
    scheduler: Any,
    service: AgentOrchestrator,
    list_connected_owner_ids: Callable[[], Awaitable[Sequence[str]]],
    hour_ist: int = 9,
) -> None:
    """Register the single daily job on the platform-owned APScheduler."""

    scheduler.add_job(
        run_scheduled_daily_cycles,
        trigger="cron",
        id="topline-agent-daily-cycle",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60 * 60,
        hour=hour_ist,
        minute=0,
        timezone="Asia/Kolkata",
        kwargs={
            "service": service,
            "list_connected_owner_ids": list_connected_owner_ids,
        },
    )
