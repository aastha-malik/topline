"""Guard: the SQLAlchemy models and the Supabase SQL migrations must not drift.

The migration file is authoritative for the Supabase project, while the models are what the
application and this test suite actually run against. A column added in one place and not
the other produces a runtime failure in production that no unit test would otherwise catch,
so this test parses the migration and diffs it against the model metadata.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.db import Base
from app.enums import (
    AccountStatus,
    ActorType,
    CustomerMatchMethod,
    EvidenceStrength,
    ExtractionMethod,
    ExtractionStatus,
    LinkType,
    MessageDirection,
    MessageProcessingState,
    PaymentEventType,
    PaymentProvider,
    PaymentState,
    ReminderState,
    SyncMode,
    SyncStatus,
)
from app.models import Invoice, PaymentEvent  # noqa: F401  (registers the metadata)

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
CORE_SCHEMA = MIGRATIONS / "20260827230000_core_schema.sql"

#: The tables this milestone owns. The agent developer's `agent_*` tables share the same
#: SQLAlchemy metadata but are declared in their own migration, so they are out of scope
#: here - each side validates its own schema.
OWNED_TABLES = {
    "workspaces",
    "users",
    "gmail_accounts",
    "oauth_states",
    "sync_runs",
    "source_messages",
    "source_attachments",
    "customers",
    "invoices",
    "payment_events",
    "invoice_source_links",
    "activity_log",
}


def owned_model_tables() -> dict:
    return {name: t for name, t in Base.metadata.tables.items() if name in OWNED_TABLES}


@pytest.fixture(scope="module")
def sql() -> str:
    assert CORE_SCHEMA.exists(), f"missing migration: {CORE_SCHEMA}"
    return CORE_SCHEMA.read_text()


def parse_tables(sql: str) -> dict[str, set[str]]:
    """Extract ``{table: {column, ...}}`` from the CREATE TABLE statements."""
    tables: dict[str, set[str]] = {}
    pattern = re.compile(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
    )
    for name, block in pattern.findall(sql):
        columns: set[str] = set()
        for line in block.splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped:
                continue
            first = stripped.split()[0].upper()
            if first in {
                "PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "CHECK", "EXCLUDE",
            }:
                continue
            columns.add(stripped.split()[0])
        tables[name] = columns
    return tables


class TestTableParity:
    def test_every_model_table_exists_in_the_migration(self, sql):
        missing = set(owned_model_tables()) - set(parse_tables(sql))
        assert not missing, f"tables in models but not in the migration: {sorted(missing)}"

    def test_the_migration_defines_no_unexpected_tables(self, sql):
        extra = set(parse_tables(sql)) - OWNED_TABLES
        assert not extra, f"tables in the migration but not owned here: {sorted(extra)}"

    def test_owned_tables_are_declared_as_models(self):
        missing = OWNED_TABLES - set(Base.metadata.tables)
        assert not missing, f"owned tables with no SQLAlchemy model: {sorted(missing)}"

    def test_columns_match_table_by_table(self, sql):
        migration_tables = parse_tables(sql)
        drift: list[str] = []
        for name, table in owned_model_tables().items():
            model_columns = {c.name for c in table.columns}
            sql_columns = migration_tables.get(name, set())
            if missing := model_columns - sql_columns:
                drift.append(f"{name}: missing from SQL {sorted(missing)}")
            if extra := sql_columns - model_columns:
                drift.append(f"{name}: missing from models {sorted(extra)}")
        assert not drift, "schema drift:\n  " + "\n  ".join(drift)

    def test_the_required_tables_are_all_present(self, sql):
        """The tables this milestone is responsible for."""
        required = {
            "workspaces", "users", "gmail_accounts", "oauth_states", "sync_runs",
            "source_messages", "source_attachments", "customers", "invoices",
            "payment_events", "invoice_source_links", "activity_log",
        }
        assert required <= set(parse_tables(sql))


class TestConstraintParity:
    @pytest.mark.parametrize(
        "enum_cls",
        [
            PaymentState, ReminderState, EvidenceStrength, PaymentProvider,
            PaymentEventType, LinkType, ExtractionStatus, ExtractionMethod,
            MessageProcessingState, MessageDirection, SyncMode, SyncStatus,
            AccountStatus, CustomerMatchMethod, ActorType,
        ],
    )
    def test_every_enum_value_appears_in_a_check_constraint(self, sql, enum_cls):
        """A value added to an enum but not to the migration would fail on insert."""
        for member in enum_cls:
            assert f"'{member.value}'" in sql, (
                f"{enum_cls.__name__}.{member.name} = '{member.value}' "
                "is missing from the SQL CHECK constraints"
            )

    def test_gmail_can_never_assert_confirmed_payment(self, sql):
        """The product's core safety rule must be enforced at the database level too."""
        assert "ck_invoices_confirmed_paid_requires_provider" in sql
        assert "ck_payment_events_gmail_never_confirms" in sql

    def test_idempotency_keys_are_unique_constraints(self, sql):
        for constraint in (
            "uq_source_messages_account_msg",
            "uq_invoices_workspace_dedupe",
            "uq_payment_events_provider_event",
            "uq_invoice_source_links_evidence",
            "uq_activity_log_dedupe",
        ):
            assert constraint in sql, f"missing idempotency constraint: {constraint}"


class TestMigrationHygiene:
    def test_migrations_are_ordered_and_named(self):
        files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        assert files, "no migrations found"
        for name in files:
            assert re.match(r"^\d{14}_[a-z0-9_]+\.sql$", name), f"bad migration name: {name}"
        assert files[0].endswith("_core_schema.sql"), "core schema must apply first"

    def test_core_schema_is_transactional(self, sql):
        assert sql.strip().startswith("-- ")
        assert "BEGIN;" in sql and sql.strip().endswith("COMMIT;")

    def test_row_level_security_is_enabled(self):
        rls = (MIGRATIONS / "20260827230200_row_level_security.sql").read_text()
        assert "ENABLE ROW LEVEL SECURITY" in rls
        assert "FORCE ROW LEVEL SECURITY" in rls
        # PostgREST reaches these tables through the anon/authenticated roles, so both
        # must lose their grants. The revokes are guarded because those roles exist on
        # Supabase but not on a plain Postgres used for local development or CI.
        assert "REVOKE ALL ON TABLE %I FROM anon" in rls
        assert "REVOKE ALL ON TABLE %I FROM authenticated" in rls
