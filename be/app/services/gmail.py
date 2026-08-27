"""Gmail OAuth, API access, and message retrieval.

Two rules shape this module:

1. **Least privilege.** Only `gmail.readonly` is used for ingestion. `gmail.send` is
   provisioned for the later approval-gated sender but is never exercised here.
2. **Metadata before content.** Listing and metadata come first; a full body or an
   attachment is downloaded only after :mod:`app.services.relevance` says the message is
   finance-relevant. That is what keeps Topline from copying a mailbox.

The transport is wrapped in :class:`GmailClient` so the ingestion pipeline can be tested
against a fake without touching Google.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Iterator, Protocol, Sequence

from app.config import Settings
from app.logging_config import get_logger
from app.services.relevance import MessageMetadata

logger = get_logger(__name__)

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "Reply-To"]


class GmailHistoryExpired(Exception):
    """Gmail rejected the stored `historyId` (HTTP 404/410).

    Gmail only retains history for a limited window, so this is expected after an idle
    period rather than an error. The caller falls back to a date-scoped resync.
    """


class GmailAuthError(Exception):
    """The stored credentials cannot be used; the mailbox must be reconnected."""


# --------------------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------------------


def build_authorization_url(settings: Settings, state: str) -> str:
    """Build the Google consent URL.

    `access_type=offline` + `prompt=consent` guarantee a refresh token on first connect;
    without them Google returns one only once per client/user pair.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": settings.google_client_id or "",
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(settings.google_oauth_scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URI}?{urlencode(params)}"


@dataclass(slots=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: list[str] = field(default_factory=list)
    id_token: str | None = None


async def exchange_code_for_tokens(settings: Settings, code: str) -> GoogleTokens:
    """Swap an authorization code for tokens."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": settings.google_client_id or "",
                "client_secret": settings.google_client_secret or "",
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        raise GmailAuthError(f"token exchange failed ({response.status_code}): {response.text}")
    return _tokens_from_payload(response.json())


async def refresh_access_token(settings: Settings, refresh_token: str) -> GoogleTokens:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GOOGLE_TOKEN_URI,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id or "",
                "client_secret": settings.google_client_secret or "",
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        raise GmailAuthError(f"token refresh failed ({response.status_code}): {response.text}")
    tokens = _tokens_from_payload(response.json())
    # Google omits the refresh token on refresh; keep the one we already hold.
    tokens.refresh_token = tokens.refresh_token or refresh_token
    return tokens


def _tokens_from_payload(payload: dict[str, Any]) -> GoogleTokens:
    expires_in = payload.get("expires_in")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)) if expires_in else None
    )
    return GoogleTokens(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token"),
        expires_at=expires_at,
        scopes=(payload.get("scope") or "").split(),
        id_token=payload.get("id_token"),
    )


async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            GOOGLE_USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
        )
    if response.status_code != 200:
        raise GmailAuthError(f"userinfo failed ({response.status_code}): {response.text}")
    return response.json()


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------


class GmailTransport(Protocol):
    """The slice of the Gmail API this project uses. Fakes implement this in tests."""

    def list_messages(
        self, *, query: str, page_token: str | None, max_results: int
    ) -> dict[str, Any]: ...

    def get_message(self, message_id: str, *, format: str = "full") -> dict[str, Any]: ...

    def get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]: ...

    def list_history(
        self, *, start_history_id: str, page_token: str | None
    ) -> dict[str, Any]: ...

    def get_profile(self) -> dict[str, Any]: ...


class GoogleApiTransport:
    """`googleapiclient` transport. Synchronous; run it from a worker thread."""

    def __init__(self, access_token: str) -> None:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credentials = Credentials(token=access_token)
        self._service = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        self._users = self._service.users()

    def list_messages(
        self, *, query: str, page_token: str | None, max_results: int
    ) -> dict[str, Any]:
        return self._users.messages().list(
            userId="me", q=query, pageToken=page_token, maxResults=max_results
        ).execute()

    def get_message(self, message_id: str, *, format: str = "full") -> dict[str, Any]:
        request = self._users.messages().get(userId="me", id=message_id, format=format)
        if format == "metadata":
            request = self._users.messages().get(
                userId="me", id=message_id, format="metadata",
                metadataHeaders=METADATA_HEADERS,
            )
        return request.execute()

    def get_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any]:
        return self._users.messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id
        ).execute()

    def list_history(self, *, start_history_id: str, page_token: str | None) -> dict[str, Any]:
        from googleapiclient.errors import HttpError

        try:
            return self._users.history().list(
                userId="me",
                startHistoryId=start_history_id,
                pageToken=page_token,
                historyTypes=["messageAdded"],
            ).execute()
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                raise GmailHistoryExpired(str(exc)) from exc
            raise

    def get_profile(self) -> dict[str, Any]:
        return self._users.getProfile(userId="me").execute()


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AttachmentRef:
    attachment_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    #: Present when Gmail inlined small attachment bodies in the message payload.
    inline_data: bytes | None = None


@dataclass(slots=True)
class ParsedMessage:
    gmail_message_id: str
    gmail_thread_id: str | None
    history_id: str | None
    internal_date: datetime | None
    from_email: str
    from_name: str
    to_emails: list[str]
    cc_emails: list[str]
    subject: str
    snippet: str
    label_ids: list[str]
    body_text: str
    attachments: list[AttachmentRef] = field(default_factory=list)

    def to_metadata(self) -> MessageMetadata:
        return MessageMetadata(
            gmail_message_id=self.gmail_message_id,
            from_email=self.from_email,
            from_name=self.from_name,
            to_emails=self.to_emails,
            cc_emails=self.cc_emails,
            subject=self.subject,
            snippet=self.snippet,
            label_ids=self.label_ids,
            attachment_filenames=[a.filename for a in self.attachments],
            attachment_mime_types=[a.mime_type for a in self.attachments],
        )


class GmailClient:
    """Thin, testable wrapper over :class:`GmailTransport`."""

    def __init__(self, transport: GmailTransport) -> None:
        self._transport = transport

    # -- listing ------------------------------------------------------------------------

    def iter_message_ids(
        self, *, query: str, page_size: int = 100, max_messages: int = 2000
    ) -> Iterator[str]:
        """Yield message ids matching `query`, following pagination until the cap."""
        page_token: str | None = None
        seen = 0
        while seen < max_messages:
            page = self._transport.list_messages(
                query=query,
                page_token=page_token,
                max_results=min(page_size, max_messages - seen),
            )
            for item in page.get("messages", []) or []:
                if message_id := item.get("id"):
                    yield message_id
                    seen += 1
                    if seen >= max_messages:
                        return
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    # -- fetching -----------------------------------------------------------------------

    def get_metadata(self, message_id: str) -> ParsedMessage:
        """Headers, labels and attachment metadata. No body is transferred."""
        return parse_message(self._transport.get_message(message_id, format="metadata"))

    def get_full(self, message_id: str) -> ParsedMessage:
        """The full message. Called only for messages that scored as candidates."""
        return parse_message(self._transport.get_message(message_id, format="full"))

    def get_attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        payload = self._transport.get_attachment(message_id, attachment_id)
        return decode_b64url(payload.get("data", ""))

    def get_profile(self) -> dict[str, Any]:
        return self._transport.get_profile()

    # -- history ------------------------------------------------------------------------

    def iter_history_message_ids(self, start_history_id: str) -> tuple[list[str], str | None]:
        """Return ``(new_message_ids, latest_history_id)`` since `start_history_id`.

        Raises :class:`GmailHistoryExpired` when Gmail no longer has that cursor, which the
        caller turns into a scoped resync.
        """
        message_ids: list[str] = []
        latest = start_history_id
        page_token: str | None = None

        while True:
            page = self._transport.list_history(
                start_history_id=start_history_id, page_token=page_token
            )
            latest = page.get("historyId") or latest
            for record in page.get("history", []) or []:
                for added in record.get("messagesAdded", []) or []:
                    message = added.get("message") or {}
                    if message_id := message.get("id"):
                        message_ids.append(message_id)
            page_token = page.get("nextPageToken")
            if not page_token:
                break

        # Preserve order while dropping repeats across history pages.
        return list(dict.fromkeys(message_ids)), latest


# --------------------------------------------------------------------------------------
# MIME parsing
# --------------------------------------------------------------------------------------


def decode_b64url(data: str | bytes | None) -> bytes:
    if not data:
        return b""
    if isinstance(data, str):
        data = data.encode("ascii", errors="ignore")
    padding = b"=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (binascii.Error, ValueError):
        return b""


def _header(headers: Sequence[dict[str, str]], name: str) -> str:
    target = name.lower()
    for header in headers:
        if (header.get("name") or "").lower() == target:
            return header.get("value") or ""
    return ""


def _split_addresses(raw: str) -> list[str]:
    from email.utils import getaddresses

    return [addr.lower() for _, addr in getaddresses([raw]) if addr]


def parse_message(payload: dict[str, Any]) -> ParsedMessage:
    """Turn a Gmail API message resource into a flat, typed record."""
    body_payload = payload.get("payload") or {}
    headers = body_payload.get("headers") or []

    from_name, from_email = parseaddr(_header(headers, "From"))
    internal_date = None
    if raw_date := payload.get("internalDate"):
        try:
            internal_date = datetime.fromtimestamp(int(raw_date) / 1000, tz=timezone.utc)
        except (TypeError, ValueError):
            internal_date = None

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentRef] = []
    _walk_parts(body_payload, text_parts, html_parts, attachments)

    body = "\n".join(p for p in text_parts if p).strip()
    if not body and html_parts:
        body = strip_html("\n".join(html_parts))

    return ParsedMessage(
        gmail_message_id=payload.get("id", ""),
        gmail_thread_id=payload.get("threadId"),
        history_id=str(payload["historyId"]) if payload.get("historyId") else None,
        internal_date=internal_date,
        from_email=(from_email or "").strip().lower(),
        from_name=(from_name or "").strip(),
        to_emails=_split_addresses(_header(headers, "To")),
        cc_emails=_split_addresses(_header(headers, "Cc")),
        subject=_header(headers, "Subject"),
        snippet=payload.get("snippet", "") or "",
        label_ids=list(payload.get("labelIds") or []),
        body_text=body,
        attachments=attachments,
    )


def _walk_parts(
    part: dict[str, Any],
    text_parts: list[str],
    html_parts: list[str],
    attachments: list[AttachmentRef],
) -> None:
    mime_type = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}

    if filename:
        attachments.append(
            AttachmentRef(
                attachment_id=body.get("attachmentId"),
                filename=filename,
                mime_type=mime_type,
                size_bytes=int(body.get("size") or 0),
                inline_data=decode_b64url(body.get("data")) if body.get("data") else None,
            )
        )
    elif mime_type == "text/plain" and body.get("data"):
        text_parts.append(decode_b64url(body["data"]).decode("utf-8", errors="replace"))
    elif mime_type == "text/html" and body.get("data"):
        html_parts.append(decode_b64url(body["data"]).decode("utf-8", errors="replace"))

    for child in part.get("parts") or []:
        _walk_parts(child, text_parts, html_parts, attachments)


def strip_html(html: str) -> str:
    """Flatten HTML to readable text - enough for keyword extraction, not rendering."""
    import re
    from html import unescape

    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)</td>", "\t", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
