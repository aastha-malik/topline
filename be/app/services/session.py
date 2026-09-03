"""Signed session cookies for owner authentication.

The Google OAuth callback already proves who the person is (Google verifies the account and
returns a stable `sub`). This module turns that into a session: a Fernet-encrypted,
timestamped token carrying nothing but the `users.id` it belongs to. Fernet gives integrity
(the value cannot be tampered with) and, via its embedded timestamp, expiry.

There is no separate signing key by default - `SESSION_ENCRYPTION_KEY` falls back to
`TOKEN_ENCRYPTION_KEY` so an existing deployment needs no new secret. Rotating either key
simply signs everyone out.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Response

from app.config import Settings, get_settings

SESSION_COOKIE = "topline_session"

#: Bumped if the token payload shape changes, so old cookies are rejected rather than
#: misread.
_TOKEN_VERSION = 1


class SessionKeyError(RuntimeError):
    """Neither SESSION_ENCRYPTION_KEY nor TOKEN_ENCRYPTION_KEY is usable."""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().session_key
    if not key:
        raise SessionKeyError(
            "SESSION_ENCRYPTION_KEY (or TOKEN_ENCRYPTION_KEY) is not set. "
            "Generate one with: python -m app.keygen"
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise SessionKeyError(
            "SESSION_ENCRYPTION_KEY must be a urlsafe-base64-encoded 32-byte Fernet key"
        ) from exc


def reset_cache() -> None:
    """Drop the cached Fernet - used by tests that swap the key."""
    _fernet.cache_clear()


def issue_session(user_id: uuid.UUID) -> str:
    """Mint a session token for a user."""
    payload = json.dumps({"uid": str(user_id), "v": _TOKEN_VERSION}).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def read_session(token: str | None, settings: Settings | None = None) -> uuid.UUID | None:
    """Return the user id a token belongs to, or ``None``.

    ``None`` covers every failure mode - absent, malformed, expired, wrong key, wrong
    version - so callers never have to distinguish "no session" from "bad session".
    """
    if not token:
        return None
    ttl = (settings or get_settings()).session_ttl_hours * 3600
    try:
        raw = _fernet().decrypt(token.encode("ascii"), ttl=ttl)
    except (InvalidToken, ValueError):
        return None
    try:
        data = json.loads(raw)
        if data.get("v") != _TOKEN_VERSION:
            return None
        return uuid.UUID(str(data["uid"]))
    except (ValueError, KeyError, TypeError):
        return None


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Attach the session cookie with the right hardening for the environment."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
