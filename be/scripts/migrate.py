"""Apply the versioned SQL migrations in `supabase/migrations`.

Use this when the Supabase CLI is not available (`supabase db push` does the same job).
Applied versions are tracked in `schema_migrations`, so re-running is a no-op.

    python scripts/migrate.py            # apply everything pending
    python scripts/migrate.py --status   # show applied / pending without changing anything
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.config import get_settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def discover() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def main(status_only: bool) -> int:
    settings = get_settings()
    if settings.database_url.startswith("sqlite"):
        print("DATABASE_URL points at SQLite; migrations target Postgres.", file=sys.stderr)
        return 2

    engine = create_async_engine(
        settings.database_url, connect_args={"statement_cache_size": 0}
    )
    try:
        try:
            async with engine.connect():
                pass
        except Exception as exc:
            print(f"Cannot reach the database: {exc}", file=sys.stderr)
            print("Check DATABASE_URL in .env (use the Supabase session pooler URI).",
                  file=sys.stderr)
            return 2
        async with engine.begin() as conn:
            await conn.execute(text(TRACKING_TABLE))
            rows = await conn.execute(text("SELECT version, checksum FROM schema_migrations"))
            applied = {version: checksum for version, checksum in rows}

        pending: list[tuple[Path, str, str]] = []
        for path in discover():
            version = path.stem
            body = path.read_text()
            checksum = hashlib.sha256(body.encode()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    print(
                        f"!! {version} was modified after being applied "
                        "(create a new migration instead of editing this one)",
                        file=sys.stderr,
                    )
                continue
            pending.append((path, version, checksum))

        if status_only:
            print(f"applied: {len(applied)}   pending: {len(pending)}")
            for _, version, _ in pending:
                print(f"  pending  {version}")
            return 0

        if not pending:
            print("Database is up to date.")
            return 0

        for path, version, checksum in pending:
            print(f"applying {version} ...", end=" ", flush=True)
            async with engine.connect() as conn:
                # asyncpg cannot put multiple statements through a prepared statement, so
                # the migration body goes to the driver's simple-query protocol. Each file
                # wraps itself in BEGIN/COMMIT, so it is atomic on its own.
                raw = await conn.get_raw_connection()
                await raw.driver_connection.execute(path.read_text())
                await conn.execute(
                    text(
                        "INSERT INTO schema_migrations (version, checksum) "
                        "VALUES (:v, :c) ON CONFLICT (version) DO NOTHING"
                    ),
                    {"v": version, "c": checksum},
                )
                await conn.commit()
            print("ok")
        print(f"Applied {len(pending)} migration(s).")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="show status without applying")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.status)))
