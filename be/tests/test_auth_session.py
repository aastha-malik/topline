"""Session auth and cross-tenant isolation at the HTTP layer.

Unlike `test_api.py`, these tests do NOT override `resolve_owner` - they exercise the real
session-cookie path so that "invoices never get mixed up between owners" is verified
end to end.
"""

from __future__ import annotations

from datetime import date

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import get_db
from app.enums import PaymentState
from app.main import create_app
from app.models import Invoice
from app.services import ledger
from app.services.session import SESSION_COOKIE, issue_session


@pytest_asyncio.fixture
async def app_client(engine, session):
    app = create_app()

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


def _sign_in(http: AsyncClient, user_id) -> None:
    http.cookies.set(SESSION_COOKIE, issue_session(user_id))


async def _seed_invoice(session, *, workspace, owner, number, email, amount=4_000_000):
    customer = await ledger.upsert_customer(
        session, workspace_id=workspace.id, owner_id=owner.id,
        name=email.split("@")[0].title(), email=email,
    )
    invoice = Invoice(
        workspace_id=workspace.id, owner_id=owner.id, customer_id=customer.id,
        invoice_number=number, normalized_number=number,
        amount_paise=amount, balance_paise=amount, currency="INR",
        issued_date=date(2026, 7, 5), due_date=date(2026, 7, 20),
        payment_state=str(PaymentState.LIKELY_UNPAID), reminder_state="ready_for_reminder",
        dedupe_key=f"seed-{number}",
    )
    session.add(invoice)
    await session.flush()
    return customer, invoice


class TestSessionRequired:
    async def test_no_cookie_is_401(self, app_client, owner):
        for path in ("/api/v1/invoices", "/api/v1/ledger/summary", "/api/v1/customers"):
            assert (await app_client.get(path)).status_code == 401

    async def test_owner_header_is_ignored_without_the_flag(self, app_client, owner):
        resp = await app_client.get(
            "/api/v1/invoices", headers={"X-Owner-Id": str(owner.id)}
        )
        assert resp.status_code == 401

    async def test_unknown_user_in_a_valid_token_is_401(self, app_client):
        import uuid

        _sign_in(app_client, uuid.uuid4())
        assert (await app_client.get("/api/v1/invoices")).status_code == 401


class TestTenantIsolation:
    async def test_an_owner_sees_only_their_own_ledger(
        self, app_client, session, workspace, owner, second_workspace, other_owner,
    ):
        _, mine = await _seed_invoice(
            session, workspace=workspace, owner=owner,
            number="INV-MINE-1", email="ap@acmetraders.in",
        )
        _, theirs = await _seed_invoice(
            session, workspace=second_workspace, owner=other_owner,
            number="INV-THEIRS-1", email="ap@harborcustomer.in",
        )

        _sign_in(app_client, owner.id)

        invoices = (await app_client.get("/api/v1/invoices")).json()
        assert [i["invoice_number"] for i in invoices] == ["INV-MINE-1"]

        summary = (await app_client.get("/api/v1/ledger/summary")).json()
        assert summary["invoice_count"] == 1
        assert summary["customer_count"] == 1

        customers = (await app_client.get("/api/v1/customers")).json()
        assert [c["primary_email"] for c in customers] == ["ap@acmetraders.in"]

        # The other owner's invoice is invisible even by direct id.
        assert (
            await app_client.get(f"/api/v1/invoices/{theirs.id}")
        ).status_code == 404
        assert (
            await app_client.get(f"/api/v1/invoices/{mine.id}")
        ).status_code == 200

    async def test_each_owner_sees_their_side(
        self, app_client, session, workspace, owner, second_workspace, other_owner,
    ):
        await _seed_invoice(
            session, workspace=workspace, owner=owner,
            number="INV-MINE-1", email="ap@acmetraders.in",
        )
        await _seed_invoice(
            session, workspace=second_workspace, owner=other_owner,
            number="INV-THEIRS-1", email="ap@harborcustomer.in",
        )
        _sign_in(app_client, other_owner.id)
        theirs = (await app_client.get("/api/v1/invoices")).json()
        assert [i["invoice_number"] for i in theirs] == ["INV-THEIRS-1"]


class TestSessionEndpoints:
    async def test_session_shape_and_logout(self, app_client, owner, workspace):
        _sign_in(app_client, owner.id)
        me = await app_client.get("/api/v1/auth/session")
        assert me.status_code == 200
        body = me.json()
        assert body["authenticated"] is True
        assert body["user"]["email"] == owner.email
        assert body["workspace"]["id"] == str(workspace.id)

        out = await app_client.post("/api/v1/auth/logout")
        assert out.status_code == 204
        # delete_cookie sets an expired Set-Cookie for the session name.
        assert "topline_session=" in out.headers.get("set-cookie", "")

    async def test_session_without_a_cookie_is_401(self, app_client):
        assert (await app_client.get("/api/v1/auth/session")).status_code == 401
