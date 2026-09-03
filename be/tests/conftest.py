"""Test fixtures: an in-memory database, a seeded workspace, and a fake Gmail."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

# Settings are read at import time, so the test environment must be set up first.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("LOG_FORMAT", "console")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.enums import AccountStatus, SyncStatus  # noqa: E402
from app.models import GmailAccount, User, Workspace  # noqa: E402
from app.services.crypto import encrypt_token  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

OWNER_EMAIL = "owner@northwind.in"
ACME_EMAIL = "ap@acmetraders.in"
NOVA_EMAIL = "accounts@novafoods.co.in"


@pytest.fixture(scope="session")
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    """A fresh in-memory database per test, so no test can leak state into another."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[Any]:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def workspace(session) -> Workspace:
    ws = Workspace(name="Northwind Studio", business_name="Northwind Studio",
                   sender_name="Northwind Studio")
    session.add(ws)
    await session.flush()
    return ws


@pytest_asyncio.fixture
async def owner(session, workspace) -> User:
    user = User(workspace_id=workspace.id, email=OWNER_EMAIL, name="Nina", role="owner")
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def second_workspace(session) -> Workspace:
    ws = Workspace(name="Harbor Freight", business_name="Harbor Freight",
                   sender_name="Harbor Freight")
    session.add(ws)
    await session.flush()
    return ws


@pytest_asyncio.fixture
async def other_owner(session, second_workspace) -> User:
    user = User(workspace_id=second_workspace.id, email="rita@harborfreight.in",
                name="Rita", role="owner")
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def gmail_account(session, workspace, owner) -> GmailAccount:
    account = GmailAccount(
        workspace_id=workspace.id,
        user_id=owner.id,
        email_address=OWNER_EMAIL,
        access_token_encrypted=encrypt_token("access-token"),
        refresh_token_encrypted=encrypt_token("refresh-token"),
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        granted_scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        status=str(AccountStatus.CONNECTED),
        backfill_status=str(SyncStatus.PENDING),
        connected_at=datetime.now(timezone.utc),
    )
    session.add(account)
    await session.flush()
    return account


# --------------------------------------------------------------------------------------
# Fake Gmail
# --------------------------------------------------------------------------------------


def b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def build_message(
    *,
    message_id: str,
    thread_id: str = "thread-1",
    from_addr: str = f"Acme AP <{ACME_EMAIL}>",
    to_addr: str = OWNER_EMAIL,
    subject: str = "",
    body: str = "",
    snippet: str | None = None,
    labels: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    history_id: str = "1000",
    days_ago: int = 40,
) -> dict[str, Any]:
    """Build a Gmail API `messages.get` resource."""
    internal_date = int(
        (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000
    )
    parts: list[dict[str, Any]] = [
        {"mimeType": "text/plain", "filename": "", "body": {"data": b64(body), "size": len(body)}}
    ]
    for att in attachments or []:
        parts.append(
            {
                "mimeType": att.get("mime_type", "application/pdf"),
                "filename": att["filename"],
                "body": {"attachmentId": att["attachment_id"], "size": att.get("size", 1000)},
            }
        )
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": history_id,
        "internalDate": str(internal_date),
        "snippet": snippet if snippet is not None else body[:180],
        "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
            "parts": parts,
        },
    }


class FakeGmailTransport:
    """In-memory Gmail. Records every call so tests can assert on fetch behaviour."""

    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: dict[str, bytes] | None = None,
        history: list[dict[str, Any]] | None = None,
        history_expired: bool = False,
        profile_history_id: str = "2000",
    ) -> None:
        self.messages = {m["id"]: m for m in messages}
        self.attachments = attachments or {}
        self.history = history or []
        self.history_expired = history_expired
        self.profile_history_id = profile_history_id

        self.list_calls: list[str] = []
        self.metadata_fetches: list[str] = []
        self.full_fetches: list[str] = []
        self.attachment_fetches: list[str] = []

    def list_messages(self, *, query: str, page_token: str | None, max_results: int):
        self.list_calls.append(query)
        if page_token:
            return {"messages": []}
        return {"messages": [{"id": mid} for mid in self.messages]}

    def get_message(self, message_id: str, *, format: str = "full"):
        message = self.messages[message_id]
        if format == "metadata":
            self.metadata_fetches.append(message_id)
            # Gmail omits part bodies in metadata format; mirror that so a test fails if
            # the pipeline ever reads a body it did not pay for.
            stripped = {**message, "payload": {**message["payload"]}}
            stripped["payload"]["parts"] = [
                {
                    "mimeType": p["mimeType"],
                    "filename": p.get("filename", ""),
                    "body": {
                        k: v for k, v in p["body"].items() if k in ("attachmentId", "size")
                    },
                }
                for p in message["payload"].get("parts", [])
            ]
            return stripped
        self.full_fetches.append(message_id)
        return message

    def get_attachment(self, message_id: str, attachment_id: str):
        import base64

        self.attachment_fetches.append(attachment_id)
        data = self.attachments.get(attachment_id, b"")
        return {"data": base64.urlsafe_b64encode(data).decode("ascii"), "size": len(data)}

    def list_history(self, *, start_history_id: str, page_token: str | None):
        from app.services.gmail import GmailHistoryExpired

        if self.history_expired:
            raise GmailHistoryExpired(f"startHistoryId {start_history_id} is too old")
        if page_token:
            return {"historyId": self.profile_history_id}
        return {"history": self.history, "historyId": self.profile_history_id}

    def get_profile(self):
        return {"emailAddress": OWNER_EMAIL, "historyId": self.profile_history_id}


@pytest.fixture
def acme_invoice_pdf() -> bytes:
    return (FIXTURES / "invoice_acme.pdf").read_bytes()


@pytest.fixture
def nova_invoice_pdf() -> bytes:
    return (FIXTURES / "invoice_nova.pdf").read_bytes()


@pytest.fixture
def scanned_invoice_pdf() -> bytes:
    return (FIXTURES / "invoice_scanned.pdf").read_bytes()
