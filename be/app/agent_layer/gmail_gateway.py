from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import GMAIL_SEND_SCOPE, Settings, get_settings
from app.models import GmailAccount
from app.services.crypto import decrypt_token, encrypt_token
from app.services.gmail import GmailAuthError, refresh_access_token

from .domain import MailReceipt, OwnerProfile
from .templates import normalize_subject


class GmailMailGateway:
    """Concrete Gmail sender used only behind :class:`AgentOrchestrator`.

    OAuth/history ingestion remains in ``app.services.gmail``. This adapter adds
    the approval-path MIME/send behavior and preserves Gmail threads by setting
    both Gmail's ``threadId`` and RFC reply headers.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings or get_settings()

    async def send_owner_digest(
        self,
        *,
        owner: OwnerProfile,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> MailReceipt:
        return await self._send(
            owner=owner,
            recipient=owner.gmail_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            reply_to=None,
            thread_id=None,
        )

    async def reply_to_owner_thread(
        self,
        *,
        owner: OwnerProfile,
        thread_id: str,
        subject: str,
        text_body: str,
        html_body: str,
    ) -> MailReceipt:
        return await self._send(
            owner=owner,
            recipient=owner.gmail_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            reply_to=None,
            thread_id=thread_id,
        )

    async def send_customer_email(
        self,
        *,
        owner: OwnerProfile,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        reply_to: str | None,
        thread_id: str | None,
    ) -> MailReceipt:
        return await self._send(
            owner=owner,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            reply_to=reply_to,
            thread_id=thread_id,
        )

    async def notify_owner(
        self,
        *,
        owner: OwnerProfile,
        subject: str,
        text_body: str,
        html_body: str,
        thread_id: str | None,
    ) -> MailReceipt:
        return await self._send(
            owner=owner,
            recipient=owner.gmail_address,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            reply_to=None,
            thread_id=thread_id,
        )

    async def _send(
        self,
        *,
        owner: OwnerProfile,
        recipient: str,
        subject: str,
        text_body: str,
        html_body: str,
        reply_to: str | None,
        thread_id: str | None,
    ) -> MailReceipt:
        sender = self._safe_address(owner.gmail_address)
        recipient = self._safe_address(recipient)
        reply_to = self._safe_address(reply_to) if reply_to else None
        access_token = await self._access_token(owner.id)
        service = await asyncio.to_thread(self._build_service, access_token)

        reply_headers: dict[str, str] = {}
        if thread_id:
            reply_headers = await asyncio.to_thread(
                self._get_thread_reply_headers, service, thread_id
            )
        resolved_subject = reply_headers.get("subject") or normalize_subject(subject)
        if thread_id and not resolved_subject.lower().startswith("re:"):
            resolved_subject = f"Re: {resolved_subject}"

        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = resolved_subject
        domain = sender.rsplit("@", 1)[-1]
        message["Message-ID"] = make_msgid(domain=domain)
        if reply_to:
            message["Reply-To"] = reply_to
        if reply_headers.get("message_id"):
            message["In-Reply-To"] = reply_headers["message_id"]
            references = " ".join(
                value
                for value in (reply_headers.get("references"), reply_headers["message_id"])
                if value
            )
            message["References"] = references
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        body: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        }
        if thread_id:
            body["threadId"] = thread_id

        response = await asyncio.to_thread(self._execute_send, service, body)
        return MailReceipt(
            message_id=response["id"],
            thread_id=response.get("threadId") or thread_id or "",
            provider_payload={
                "id": response.get("id"),
                "threadId": response.get("threadId"),
                "labelIds": response.get("labelIds", []),
            },
        )

    async def _access_token(self, owner_id: str) -> str:
        async with self._sessions() as session:
            account = await session.scalar(
                sa.select(GmailAccount)
                .where(
                    GmailAccount.user_id == uuid.UUID(owner_id),
                    GmailAccount.status == "connected",
                )
                .order_by(GmailAccount.connected_at.desc())
                .limit(1)
            )
            if account is None:
                raise GmailAuthError("Owner has no connected Gmail account")
            if GMAIL_SEND_SCOPE not in set(account.granted_scopes or []):
                raise GmailAuthError("Connected Gmail account lacks gmail.send; reconnect it")
            access_token = decrypt_token(account.access_token_encrypted)
            refresh_token = decrypt_token(account.refresh_token_encrypted)
            expires_soon = account.token_expiry is not None and account.token_expiry <= (
                datetime.now(timezone.utc) + timedelta(seconds=60)
            )
            if access_token and not expires_soon:
                return access_token
            if not refresh_token:
                raise GmailAuthError("Gmail access expired and no refresh token is available")
            refreshed = await refresh_access_token(self._settings, refresh_token)
            if not refreshed.access_token:
                raise GmailAuthError("Google returned no access token")
            account.access_token_encrypted = encrypt_token(refreshed.access_token)
            account.refresh_token_encrypted = encrypt_token(refreshed.refresh_token)
            account.token_expiry = refreshed.expires_at
            if refreshed.scopes:
                account.granted_scopes = refreshed.scopes
            await session.commit()
            return refreshed.access_token

    @staticmethod
    def _safe_address(value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Email address contains a newline")
        _name, address = parseaddr(value)
        if not address or "@" not in address:
            raise ValueError("Invalid email address")
        return address.lower()

    @staticmethod
    def _build_service(access_token: str) -> Any:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        return build(
            "gmail",
            "v1",
            credentials=Credentials(token=access_token),
            cache_discovery=False,
        )

    @staticmethod
    def _get_thread_reply_headers(service: Any, thread_id: str) -> dict[str, str]:
        thread = (
            service.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["Message-ID", "References", "Subject"],
            )
            .execute()
        )
        messages = thread.get("messages") or []
        if not messages:
            return {}
        headers = {
            (item.get("name") or "").lower(): item.get("value") or ""
            for item in (messages[-1].get("payload", {}).get("headers") or [])
        }
        return {
            "message_id": headers.get("message-id", ""),
            "references": headers.get("references", ""),
            "subject": headers.get("subject", ""),
        }

    @staticmethod
    def _execute_send(service: Any, body: dict[str, Any]) -> dict[str, Any]:
        return service.users().messages().send(userId="me", body=body).execute()
