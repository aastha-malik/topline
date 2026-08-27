"""Small shared helpers."""

from __future__ import annotations

from datetime import datetime, timezone


def ensure_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    Values that come back from the database can be naive - SQLite has no timezone type,
    and a `timestamp without time zone` column behaves the same way - while values from
    Gmail and Razorpay are always aware. Comparing the two raises, so every comparison
    between a stored timestamp and a live one goes through here first.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
