"""One-time repair: give every owner their own workspace.

Before session auth landed, `_link_account` dropped every connected Google account into a
single shared workspace, so owners could see each other's ledgers. New sign-ins are now
isolated, but any workspace that already accumulated more than one `owner`-role user stays
mixed until this script splits it.

For each such workspace it keeps the earliest owner in place and moves every other owner -
their user row, Gmail accounts, customers, invoices, evidence, payments, activity and agent
workflow rows - into a fresh workspace of their own. A workspace with a single owner (plus
any `member` users, who legitimately share it) is left untouched, so this is safe to run
against already-clean data.

    python scripts/split_shared_workspaces.py            # dry run - report only
    python scripts/split_shared_workspaces.py --apply    # perform the split
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402


async def _scalars(conn, sql: str, **params) -> list:
    return [row[0] for row in (await conn.execute(text(sql), params)).all()]


async def _find_shared_workspaces(conn) -> list[tuple]:
    return (
        await conn.execute(
            text(
                """
                SELECT u.workspace_id,
                       array_agg(u.id ORDER BY u.created_at, u.id) AS owner_ids
                FROM users u
                WHERE u.role = 'owner'
                GROUP BY u.workspace_id
                HAVING count(*) > 1
                """
            )
        )
    ).all()


async def _plan_owner(conn, owner_id) -> dict:
    account_ids = await _scalars(
        conn, "SELECT id FROM gmail_accounts WHERE user_id = :o", o=owner_id
    )
    invoice_ids = await _scalars(
        conn, "SELECT id FROM invoices WHERE owner_id = :o", o=owner_id
    )
    message_ids = (
        await _scalars(
            conn,
            "SELECT id FROM source_messages WHERE gmail_account_id = ANY(:a)",
            a=account_ids,
        )
        if account_ids
        else []
    )
    digest_ids = await _scalars(
        conn, "SELECT id FROM agent_digests WHERE owner_id = :o", o=owner_id
    )
    return {
        "account_ids": account_ids,
        "invoice_ids": invoice_ids,
        "message_ids": message_ids,
        "digest_ids": digest_ids,
    }


async def _move_owner(conn, *, source_ws, owner_id, plan: dict) -> str:
    new_ws = (
        await conn.execute(
            text(
                """
                INSERT INTO workspaces (name, business_name, sender_name, primary_color,
                                        logo_url, reply_to, default_currency, timezone)
                SELECT COALESCE(u.name, u.email), w.business_name, w.sender_name,
                       w.primary_color, w.logo_url, w.reply_to, w.default_currency, w.timezone
                FROM workspaces w, users u
                WHERE w.id = :ws AND u.id = :o
                RETURNING id
                """
            ),
            {"ws": source_ws, "o": owner_id},
        )
    ).scalar_one()

    base = {"new": new_ws, "o": owner_id}

    async def run(sql: str, **extra):
        await conn.execute(text(sql), {**base, **extra})

    await run("UPDATE users SET workspace_id = :new WHERE id = :o")
    await run("UPDATE gmail_accounts SET workspace_id = :new WHERE user_id = :o")
    await run("UPDATE customers SET workspace_id = :new WHERE owner_id = :o")
    await run("UPDATE invoices SET workspace_id = :new WHERE owner_id = :o")
    await run("UPDATE activity_log SET workspace_id = :new WHERE owner_id = :o")
    await run("UPDATE agent_digests SET workspace_id = :new WHERE owner_id = :o")
    await run("UPDATE agent_drafts SET workspace_id = :new WHERE owner_id = :o")
    await run("UPDATE agent_review_tasks SET workspace_id = :new WHERE owner_id = :o")
    await run(
        "UPDATE payment_events SET workspace_id = :new "
        "WHERE customer_id IN (SELECT id FROM customers WHERE owner_id = :o)"
    )
    if plan["account_ids"]:
        await run(
            "UPDATE sync_runs SET workspace_id = :new WHERE gmail_account_id = ANY(:a)",
            a=plan["account_ids"],
        )
        await run(
            "UPDATE source_messages SET workspace_id = :new "
            "WHERE gmail_account_id = ANY(:a)",
            a=plan["account_ids"],
        )
    if plan["message_ids"]:
        await run(
            "UPDATE source_attachments SET workspace_id = :new "
            "WHERE source_message_id = ANY(:m)",
            m=plan["message_ids"],
        )
    if plan["invoice_ids"]:
        await run(
            "UPDATE invoice_source_links SET workspace_id = :new "
            "WHERE invoice_id = ANY(:i)",
            i=plan["invoice_ids"],
        )
        await run(
            "UPDATE payment_events SET workspace_id = :new WHERE invoice_id = ANY(:i)",
            i=plan["invoice_ids"],
        )
    if plan["digest_ids"]:
        await run(
            "UPDATE agent_digest_items SET workspace_id = :new "
            "WHERE digest_id = ANY(:d)",
            d=plan["digest_ids"],
        )
    return str(new_ws)


async def main(apply: bool) -> int:
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        print("DATABASE_URL points at SQLite; this repair targets the Postgres project.")
        return 1

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            shared = await _find_shared_workspaces(conn)
            if not shared:
                print("No shared workspaces found. Nothing to do.")
                return 0

            txn = await conn.begin()
            for workspace_id, owner_ids in shared:
                keep, *move = owner_ids
                print(f"\nworkspace {workspace_id}")
                print(f"  keep  {keep}")
                for owner_id in move:
                    plan = await _plan_owner(conn, owner_id)
                    summary = {k: len(v) for k, v in plan.items()}
                    if apply:
                        dest = await _move_owner(
                            conn, source_ws=workspace_id, owner_id=owner_id, plan=plan
                        )
                        print(f"  split {owner_id} -> {dest}  moved {summary}")
                    else:
                        print(f"  split {owner_id} -> (new)  would move {summary}")

            if apply:
                await txn.commit()
                print("\nDone.")
            else:
                await txn.rollback()
                print("\nDry run. Re-run with --apply to perform the split.")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the split")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
