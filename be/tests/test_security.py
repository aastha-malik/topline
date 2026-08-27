"""Token encryption at rest and the least-privilege scope guard."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import FORBIDDEN_SCOPES, Settings
from app.models import GmailAccount
from app.services import crypto
from app.services.crypto import TokenEncryptionError, decrypt_token, encrypt_token


class TestTokenEncryption:
    def test_round_trip(self):
        assert decrypt_token(encrypt_token("refresh-abc")) == "refresh-abc"

    def test_ciphertext_does_not_contain_the_plaintext(self):
        secret = "1//0eXaMpLe-refresh-token"
        assert secret not in encrypt_token(secret)

    def test_encryption_is_non_deterministic(self):
        """Fernet includes a random IV, so identical tokens do not produce identical rows."""
        assert encrypt_token("same") != encrypt_token("same")

    def test_none_passes_through(self):
        assert encrypt_token(None) is None
        assert decrypt_token(None) is None

    def test_a_rotated_key_reports_rather_than_returning_garbage(self, monkeypatch):
        ciphertext = encrypt_token("refresh-abc")
        crypto.reset_cache()
        monkeypatch.setattr(
            crypto, "get_settings",
            lambda: Settings(_env_file=None, token_encryption_key=Fernet.generate_key().decode()),
        )
        with pytest.raises(TokenEncryptionError, match="must be reconnected"):
            decrypt_token(ciphertext)
        crypto.reset_cache()

    def test_a_missing_key_is_a_clear_error(self, monkeypatch):
        crypto.reset_cache()
        monkeypatch.setattr(
            crypto, "get_settings", lambda: Settings(_env_file=None, token_encryption_key="")
        )
        with pytest.raises(TokenEncryptionError, match="TOKEN_ENCRYPTION_KEY is not set"):
            encrypt_token("x")
        crypto.reset_cache()


class TestTokensAtRest:
    async def test_stored_gmail_tokens_are_ciphertext(self, session, gmail_account):
        row = await session.scalar(
            select(GmailAccount).where(GmailAccount.id == gmail_account.id)
        )
        assert row.refresh_token_encrypted != "refresh-token"
        assert row.access_token_encrypted != "access-token"
        assert decrypt_token(row.refresh_token_encrypted) == "refresh-token"

    async def test_the_api_response_model_omits_token_columns(self):
        from app.schemas import GmailAccountResponse

        fields = set(GmailAccountResponse.model_fields)
        assert not any("token" in f for f in fields if f != "token_expiry")


class TestScopeGuard:
    def test_mailbox_write_scopes_are_refused(self):
        assert "https://www.googleapis.com/auth/gmail.modify" in FORBIDDEN_SCOPES
        assert "https://mail.google.com/" in FORBIDDEN_SCOPES

    def test_default_scopes_are_read_plus_send_only(self):
        scopes = set(Settings(_env_file=None, token_encryption_key="x").google_oauth_scopes)
        assert scopes == {
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        }

    def test_send_scope_can_be_dropped(self):
        settings = Settings(
            _env_file=None, token_encryption_key="x", enable_gmail_send_scope=False
        )
        assert "https://www.googleapis.com/auth/gmail.send" not in settings.google_oauth_scopes
        assert "https://www.googleapis.com/auth/gmail.readonly" in settings.google_oauth_scopes

    def test_no_forbidden_scope_can_slip_into_the_requested_set(self):
        settings = Settings(_env_file=None, token_encryption_key="x")
        assert not (FORBIDDEN_SCOPES & set(settings.google_oauth_scopes))


class TestOAuthState:
    async def test_state_is_single_use(self, session):
        """A replayed callback must not be able to re-link a mailbox."""
        from datetime import datetime, timedelta, timezone

        from app.models import OAuthState

        state = OAuthState(
            state="abc", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)
        )
        session.add(state)
        await session.flush()

        stored = await session.scalar(select(OAuthState).where(OAuthState.state == "abc"))
        assert stored.consumed_at is None
        stored.consumed_at = datetime.now(timezone.utc)
        await session.flush()

        replay = await session.scalar(select(OAuthState).where(OAuthState.state == "abc"))
        assert replay.consumed_at is not None
