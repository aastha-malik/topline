"""`_link_account`: one workspace per Google identity, additive mailbox linking.

This is the mechanism that stops two different businesses landing in the same workspace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.api.auth import _link_account
from app.models import GmailAccount, User, Workspace
from app.services.crypto import decrypt_token
from app.services.gmail import GoogleTokens


def _tokens(access="access-1", refresh="refresh-1"):
    return GoogleTokens(
        access_token=access,
        refresh_token=refresh,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def _userinfo(sub, email, name="Someone"):
    return {"sub": sub, "email": email, "name": name}


async def test_two_identities_get_separate_workspaces(session, settings):
    await _link_account(
        session, settings, "nina@northwind.in",
        _userinfo("google-sub-A", "nina@northwind.in", "Nina"), _tokens(),
        linked_workspace_id=None,
    )
    await _link_account(
        session, settings, "rita@harbor.in",
        _userinfo("google-sub-B", "rita@harbor.in", "Rita"), _tokens(),
        linked_workspace_id=None,
    )

    assert await session.scalar(select(func.count(Workspace.id))) == 2
    assert await session.scalar(select(func.count(User.id))) == 2
    owners = (await session.scalars(select(User))).all()
    assert {u.workspace_id for u in owners} == {u.workspace_id for u in owners}  # all distinct
    assert len({u.workspace_id for u in owners}) == 2


async def test_returning_identity_reuses_its_workspace_and_refreshes_tokens(session, settings):
    acct1, user1 = await _link_account(
        session, settings, "nina@northwind.in",
        _userinfo("google-sub-A", "nina@northwind.in"), _tokens(access="a1", refresh="r1"),
        linked_workspace_id=None,
    )
    acct2, user2 = await _link_account(
        session, settings, "nina@northwind.in",
        _userinfo("google-sub-A", "nina@northwind.in"), _tokens(access="a2", refresh="r2"),
        linked_workspace_id=None,
    )

    assert acct1.id == acct2.id
    assert user1.id == user2.id
    assert await session.scalar(select(func.count(Workspace.id))) == 1
    assert decrypt_token(acct2.access_token_encrypted) == "a2"


async def test_signed_in_owner_can_link_a_second_mailbox_to_the_same_workspace(
    session, settings
):
    _, user = await _link_account(
        session, settings, "nina@northwind.in",
        _userinfo("google-sub-A", "nina@northwind.in"), _tokens(),
        linked_workspace_id=None,
    )
    account2, user2 = await _link_account(
        session, settings, "billing@northwind.in",
        _userinfo("google-sub-C", "billing@northwind.in"), _tokens(),
        linked_workspace_id=user.workspace_id,
    )

    assert user2.id == user.id
    assert account2.workspace_id == user.workspace_id
    assert await session.scalar(select(func.count(Workspace.id))) == 1
    assert await session.scalar(select(func.count(GmailAccount.id))) == 2
