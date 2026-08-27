from __future__ import annotations

import base64
import unittest
from email import message_from_bytes
from email.policy import default

from app.agent_layer.domain import OwnerProfile
from app.agent_layer.gmail_gateway import GmailMailGateway
from app.config import Settings


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeGmailService:
    def __init__(self) -> None:
        self.sent_body = None

    def users(self):
        return self

    def threads(self):
        return self

    def messages(self):
        return self

    def get(self, **_kwargs):
        return _Request(
            {
                "messages": [
                    {
                        "payload": {
                            "headers": [
                                {"name": "Message-ID", "value": "<digest-1@example.com>"},
                                {"name": "References", "value": "<root@example.com>"},
                                {
                                    "name": "Subject",
                                    "value": "Topline daily receivables review — 2026-08-27",
                                },
                            ]
                        }
                    }
                ]
            }
        )

    def send(self, *, userId, body):
        self.sent_body = body
        return _Request({"id": "gmail-sent-1", "threadId": body.get("threadId", "new-thread")})


class GmailGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_uses_same_gmail_thread_and_rfc_reply_headers(self):
        fake = _FakeGmailService()
        gateway = GmailMailGateway(
            session_factory=None,  # token lookup is replaced below
            settings=Settings(gemini_api_key="test", debug=False),
        )

        async def access_token(_owner_id: str) -> str:
            return "access-token"

        gateway._access_token = access_token
        gateway._build_service = lambda _token: fake
        owner = OwnerProfile(
            id="00000000-0000-0000-0000-000000000001",
            email="owner@example.com",
            gmail_address="owner@gmail.com",
        )
        receipt = await gateway.reply_to_owner_thread(
            owner=owner,
            thread_id="digest-thread-1",
            subject="This subject is replaced by the real thread subject",
            text_body="Draft 1",
            html_body="<p>Draft 1</p>",
        )
        self.assertEqual(receipt.thread_id, "digest-thread-1")
        self.assertEqual(fake.sent_body["threadId"], "digest-thread-1")
        raw = base64.urlsafe_b64decode(fake.sent_body["raw"])
        message = message_from_bytes(raw, policy=default)
        self.assertEqual(
            message["Subject"], "Re: Topline daily receivables review — 2026-08-27"
        )
        self.assertEqual(message["In-Reply-To"], "<digest-1@example.com>")
        self.assertEqual(
            message["References"], "<root@example.com> <digest-1@example.com>"
        )
        self.assertTrue(message.is_multipart())

    async def test_header_injection_in_recipient_is_rejected(self):
        gateway = GmailMailGateway(
            session_factory=None,
            settings=Settings(gemini_api_key="test", debug=False),
        )
        owner = OwnerProfile(
            id="00000000-0000-0000-0000-000000000001",
            email="owner@example.com",
            gmail_address="owner@gmail.com",
        )
        with self.assertRaises(ValueError):
            await gateway.send_customer_email(
                owner=owner,
                recipient="victim@example.com\nBcc: attacker@example.com",
                subject="Invoice",
                text_body="Hello",
                html_body="<p>Hello</p>",
                reply_to=None,
                thread_id=None,
            )


if __name__ == "__main__":
    unittest.main()
