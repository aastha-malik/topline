"""Dialect-aware helpers for the idempotent write paths.

Production is Postgres and the test suite is SQLite; both support
``INSERT ... ON CONFLICT``, but through different dialect modules. These helpers pick the
right one so sync code can express "insert unless it already exists" once.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession


def _insert_for(session: AsyncSession, table: Table):
    name = session.bind.dialect.name if session.bind is not None else "postgresql"
    if name == "sqlite":
        return sqlite_insert(table)
    return pg_insert(table)


async def insert_ignore(
    session: AsyncSession,
    table: Table,
    values: dict[str, Any],
    *,
    index_elements: Sequence[str],
) -> bool:
    """Insert a row unless the unique key already exists.

    Returns True when a row was actually written, so callers can keep honest counters.
    """
    stmt = _insert_for(session, table).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=list(index_elements))
    result = await session.execute(stmt)
    return bool(result.rowcount)


async def upsert(
    session: AsyncSession,
    table: Table,
    values: dict[str, Any],
    *,
    index_elements: Sequence[str],
    update_columns: Sequence[str],
) -> None:
    """Insert, or update the named columns when the unique key already exists."""
    stmt = _insert_for(session, table).values(**values)
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=list(index_elements),
        set_={col: excluded[col] for col in update_columns},
    )
    await session.execute(stmt)
