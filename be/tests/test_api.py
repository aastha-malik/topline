"""HTTP-level tests: health, the OpenAPI inventory, the ledger reads and the webhook route."""

from __future__ import annotations

import json
from datetime import date

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.deps import CurrentOwner, resolve_owner
from app.config import GMAIL_READONLY_SCOPE, GMAIL_SEND_SCOPE, get_settings
from app.db import get_db
from app.enums import PaymentState
from app.main import create_app
from app.models import Invoice
from app.services import ledger
from app.services.razorpay_sync import build_test_signature

SECRET = "test_webhook_secret"


@pytest_asyncio.fixture
async def client(engine, workspace, owner, session):
    """An app bound to the test session and authenticated as the seeded owner."""
    app = create_app()

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[resolve_owner] = lambda: CurrentOwner(
        user_id=owner.id, workspace_id=workspace.id, email=owner.email
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


class TestHealth:
    async def test_liveness(self, client):
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["environment"] == "test"

    async def test_readiness_reports_each_dependency(self, client):
        response = await client.get("/api/v1/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["database"] == "ok"
        assert body["token_encryption"] == "ok"
        assert body["checks_passed"] is True

    async def test_request_id_is_echoed(self, client):
        response = await client.get("/api/v1/health", headers={"X-Request-Id": "abc123"})
        assert response.headers["X-Request-Id"] == "abc123"


class TestOpenAPI:
    async def test_the_endpoint_inventory_is_published(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        for expected in (
            "/api/v1/health",
            "/api/v1/auth/google/start",
            "/api/v1/auth/google/callback",
            "/api/v1/sync/backfill",
            "/api/v1/sync/incremental",
            "/api/v1/sync/razorpay/reconcile",
            "/api/v1/customers",
            "/api/v1/invoices",
            "/api/v1/messages",
            "/api/v1/activity",
            "/api/v1/webhooks/razorpay",
        ):
            assert expected in paths, f"{expected} missing from the OpenAPI schema"

    async def test_docs_are_served(self, client):
        assert (await client.get("/docs")).status_code == 200


class TestScopes:
    def test_least_privilege_scopes(self):
        scopes = get_settings().google_oauth_scopes
        assert GMAIL_READONLY_SCOPE in scopes
        assert GMAIL_SEND_SCOPE in scopes  # provisioned for the approval-gated sender
        assert "https://www.googleapis.com/auth/gmail.modify" not in scopes
        assert "https://mail.google.com/" not in scopes

    def test_over_broad_scopes_are_refused_at_startup(self):
        from app.config import FORBIDDEN_SCOPES

        assert "https://www.googleapis.com/auth/gmail.modify" in FORBIDDEN_SCOPES

    async def test_oauth_start_requires_configuration(self, client, monkeypatch):
        response = await client.post("/api/v1/auth/google/start")
        # Configured in the test environment, so this should produce a consent URL.
        assert response.status_code == 200
        body = response.json()
        assert body["authorization_url"].startswith("https://accounts.google.com/")
        assert "access_type=offline" in body["authorization_url"]
        assert GMAIL_READONLY_SCOPE.replace(":", "%3A").replace("/", "%2F") in body[
            "authorization_url"
        ] or "gmail.readonly" in body["authorization_url"]


class TestLedgerReads:
    @pytest_asyncio.fixture
    async def seeded(self, session, workspace, owner):
        customer = await ledger.upsert_customer(
            session, workspace_id=workspace.id, owner_id=owner.id,
            name="Acme Traders", email="ap@acmetraders.in",
        )
        invoice = Invoice(
            workspace_id=workspace.id, owner_id=owner.id, customer_id=customer.id,
            invoice_number="INV-2026-0114", normalized_number="INV-2026-0114",
            amount_paise=4_000_000, balance_paise=4_000_000, currency="INR",
            issued_date=date(2026, 7, 5), due_date=date(2026, 7, 20),
            payment_state=str(PaymentState.LIKELY_UNPAID),
            reminder_state="ready_for_reminder",
            dedupe_key="seed-key",
        )
        session.add(invoice)
        await session.flush()
        return customer, invoice

    async def test_list_customers(self, client, seeded):
        response = await client.get("/api/v1/customers")
        assert response.status_code == 200
        assert response.json()[0]["primary_email"] == "ap@acmetraders.in"

    async def test_list_invoices_exposes_the_derived_state(self, client, seeded):
        response = await client.get("/api/v1/invoices")
        assert response.status_code == 200
        row = response.json()[0]
        assert row["effective_state"] == "ready_for_reminder"
        assert row["payment_state"] == "likely_unpaid"
        assert row["amount_paise"] == 4_000_000

    async def test_filter_by_state(self, client, seeded):
        assert len((await client.get("/api/v1/invoices?state=ready_for_reminder")).json()) == 1
        assert len((await client.get("/api/v1/invoices?state=confirmed_paid")).json()) == 0

    async def test_invoice_detail_carries_its_evidence_list(self, client, seeded):
        _, invoice = seeded
        response = await client.get(f"/api/v1/invoices/{invoice.id}")
        assert response.status_code == 200
        assert "evidence" in response.json()

    async def test_customer_dossier(self, client, seeded):
        customer, _ = seeded
        response = await client.get(f"/api/v1/customers/{customer.id}/dossier")
        assert response.status_code == 200
        body = response.json()
        assert body["total_outstanding_paise"] == 4_000_000
        assert body["open_invoice_count"] == 1

    async def test_ledger_summary(self, client, seeded):
        body = (await client.get("/api/v1/ledger/summary")).json()
        assert body["invoice_count"] == 1
        assert body["by_state"]["ready_for_reminder"] == 1

    async def test_unknown_invoice_is_404(self, client, seeded):
        import uuid

        response = await client.get(f"/api/v1/invoices/{uuid.uuid4()}")
        assert response.status_code == 404


class TestWebhookEndpoint:
    async def test_valid_signature_is_accepted(self, client, workspace, owner):
        payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_1",
                        "amount": 100,
                        "currency": "INR",
                        "notes": {"workspace_id": str(workspace.id)},
                    }
                }
            },
        }
        body = json.dumps(payload).encode()
        response = await client.post(
            "/api/v1/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": build_test_signature(body, SECRET),
                "X-Razorpay-Event-Id": "evt_http_1",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["received"] is True

    async def test_invalid_signature_is_401(self, client):
        body = b'{"event":"payment.captured"}'
        response = await client.post(
            "/api/v1/webhooks/razorpay",
            content=body,
            headers={"X-Razorpay-Signature": "deadbeef", "Content-Type": "application/json"},
        )
        assert response.status_code == 401

    async def test_missing_signature_is_401(self, client):
        response = await client.post(
            "/api/v1/webhooks/razorpay", content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401

    async def test_unroutable_event_is_dropped_not_charged_to_an_owner(self, client, session):
        from app.models import PaymentEvent

        payload = {
            "event": "payment.captured",
            "payload": {"payment": {"entity": {
                "id": "pay_nomatch", "amount": 999, "currency": "INR",
                "email": "stranger@nowhere.example",
            }}},
        }
        body = json.dumps(payload).encode()
        response = await client.post(
            "/api/v1/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": build_test_signature(body, SECRET),
                "X-Razorpay-Event-Id": "evt_nomatch",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["processed"] is False
        assert await session.scalar(select(func.count(PaymentEvent.id))) == 0

    async def test_unhandled_event_is_acknowledged_not_retried(self, client, workspace):
        """A 200 stops Razorpay retrying an event we intentionally do not act on."""
        body = json.dumps({"event": "payment.authorized"}).encode()
        response = await client.post(
            "/api/v1/webhooks/razorpay",
            content=body,
            headers={
                "X-Razorpay-Signature": build_test_signature(body, SECRET),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        assert response.json()["processed"] is False


class TestSyncGuards:
    async def test_backfill_without_a_connected_account_is_404(self, client, owner):
        response = await client.post("/api/v1/sync/backfill", json={})
        assert response.status_code == 404
        assert "Gmail" in response.json()["detail"]

    async def test_sync_runs_list_is_empty_initially(self, client, owner):
        assert (await client.get("/api/v1/sync/runs")).json() == []
