"""Symmetric encryption for OAuth tokens at rest.

Refresh tokens are long-lived mailbox credentials. They are Fernet-encrypted before they
reach a column, and the plaintext is never logged or serialised into an API response.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class TokenEncryptionError(RuntimeError):
    """Raised when a token cannot be encrypted or decrypted."""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with: python -m app.keygen"
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY must be a urlsafe-base64-encoded 32-byte Fernet key"
        ) from exc


def encrypt_token(plaintext: str | None) -> str | None:
    """Encrypt a token. ``None`` passes through so optional tokens stay optional."""
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str | None) -> str | None:
    if ciphertext is None:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # Usually means TOKEN_ENCRYPTION_KEY was rotated without re-linking the mailbox.
        raise TokenEncryptionError(
            "Stored token could not be decrypted; the account must be reconnected"
        ) from exc


def reset_cache() -> None:
    """Drop the cached Fernet - used by tests that swap the key."""
    _fernet.cache_clear()
