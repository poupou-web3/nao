"""Integration tests for the database sync pipeline against a real ClickHouse database.

Connection is configured via environment variables:
    CLICKHOUSE_HOST, CLICKHOUSE_PORT (default 8123), CLICKHOUSE_USER, CLICKHOUSE_PASSWORD,
    CLICKHOUSE_SECURE (optional). Database is created per test module.

The test suite is skipped entirely when CLICKHOUSE_HOST is not set.

With docker-compose.test.yml:
    docker compose -f docker-compose.test.yml up -d
    cd cli && cp tests/nao_core/commands/sync/integration/.env.example \
         tests/nao_core/commands/sync/integration/.env
    uv run pytest tests/nao_core/commands/sync/integration/test_clickhouse.py -v
"""

import os
import uuid
from pathlib import Path

import ibis
import pytest
from rich.progress import Progress

from nao_core.commands.sync.providers.databases.provider import sync_database
from nao_core.config.databases.clickhouse import ClickHouseConfig

from .base import BaseSyncIntegrationTests, SyncTestSpec

CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST")

pytestmark = pytest.mark.skipif(
    CLICKHOUSE_HOST is None,
    reason="CLICKHOUSE_HOST not set — skipping ClickHouse integration tests",
)


def _clickhouse_connect(database: str = "default"):
    port = os.environ.get("CLICKHOUSE_PORT", "8123")
    secure = (os.environ.get("CLICKHOUSE_SECURE", "false") or "false").lower() in ("true", "1", "yes")
    return ibis.clickhouse.connect(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(port),
        database=database,
        user=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=secure,
        connect_timeout=15,
        send_receive_timeout=60,
    )


@pytest.fixture(scope="module")
def temp_database():
    """Create a temporary database, populate it with test data, then clean up."""
    conn = _clickhouse_connect("default")
    try:
        # Drop any leftover nao_unit_tests_* DBs from previous runs so get_schemas returns only ours
        list_db = getattr(conn, "list_databases", None)
        if list_db:
            try:
                for name in list_db():
                    if name.startswith("nao_unit_tests_"):
                        conn.raw_sql(f"DROP DATABASE IF EXISTS `{name}`")
            except Exception:
                pass
        conn.disconnect()
    except Exception:
        conn.disconnect()
        raise

    db_name = f"nao_unit_tests_{uuid.uuid4().hex[:8].lower()}"
    conn = _clickhouse_connect("default")
    try:
        conn.raw_sql(f"CREATE DATABASE IF NOT EXISTS `{db_name}`")
        conn.disconnect()
    except Exception:
        conn.disconnect()
        raise

    conn = _clickhouse_connect(db_name)
    try:
        sql_file = Path(__file__).parent / "dml" / "clickhouse.sql"
        sql_content = sql_file.read_text()

        # Split on ";\n" so INSERT ... VALUES (...); is one statement; then ensure trailing ";"
        for part in sql_content.split(";\n"):
            statement = part.strip()
            if not statement:
                continue
            if not statement.endswith(";"):
                statement += ";"
            try:
                conn.raw_sql(statement)
            except Exception:
                pass

        # So test_sync_all_schemas passes: "default" DB must have spec.another_table so it gets synced
        conn_default = _clickhouse_connect("default")
        try:
            conn_default.raw_sql("CREATE TABLE IF NOT EXISTS nonexistent (id UInt32) ENGINE = MergeTree() ORDER BY id;")
            conn_default.disconnect()
        except Exception:
            conn_default.disconnect()
            raise

        yield db_name

    finally:
        conn.disconnect()
        conn = _clickhouse_connect("default")
        try:
            conn.raw_sql(f"DROP DATABASE IF EXISTS `{db_name}`")
        except Exception:
            pass
        conn.disconnect()


@pytest.fixture(scope="module")
def db_config(temp_database):
    """Build a ClickHouseConfig from environment variables using the temporary database."""
    port = os.environ.get("CLICKHOUSE_PORT", "8123")
    secure = (os.environ.get("CLICKHOUSE_SECURE", "false") or "false").lower() in ("true", "1", "yes")
    return ClickHouseConfig(
        name="test-clickhouse",
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(port),
        database=temp_database,
        user=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        secure=secure,
        connect_timeout=15,
        send_receive_timeout=60,
        schemas_include=[temp_database],
    )


@pytest.fixture(scope="module")
def spec(temp_database):
    return SyncTestSpec(
        db_type="clickhouse",
        primary_schema=temp_database,
        users_column_assertions=(
            "# users",
            f"**Dataset:** `{temp_database}`",
            "## Columns (4)",
            "- id",
            "- name",
            "- email",
            "- active",
        ),
        orders_column_assertions=(
            "# orders",
            f"**Dataset:** `{temp_database}`",
            "## Columns (3)",
            "- id",
            "- user_id",
            "- amount",
        ),
        # description.md: users has a table comment (see COMMENT in dml/clickhouse.sql), orders has none
        users_table_description="User accounts and profile data",
        orders_table_description=None,
        users_preview_rows=[
            {"id": 1, "name": "Alice", "email": "alice@example.com", "active": 1},
            {"id": 2, "name": "Bob", "email": None, "active": 0},
            {"id": 3, "name": "Charlie", "email": "charlie@example.com", "active": 1},
        ],
        orders_preview_rows=[
            {"id": 1, "user_id": 1, "amount": 99.99},
            {"id": 2, "user_id": 1, "amount": 24.5},
        ],
        sort_rows=True,
        row_id_key="id",
        # Enable get_schemas tests: restrict schemas via schemas_include; "default" DB exists for "without" test
        schema_field="schemas_include",
        another_schema="default",
        another_table="nonexistent",
        expects_indexes=True,  # ClickHouse generates indexes.md
    )


@pytest.mark.timeout(120)
class TestClickHouseSyncIntegration(BaseSyncIntegrationTests):
    """Verify the sync pipeline produces correct output against a live ClickHouse database."""

    def test_sync_state_tracks_schemas_and_tables(self, synced, spec):
        """Sync state reflects expected tables for various engine types."""
        state, _, _ = synced
        assert state.schemas_synced == 1
        assert spec.primary_schema in state.synced_schemas

        synced_tables = state.synced_tables[spec.primary_schema]
        # Core tables used by shared tests
        assert spec.users_table in synced_tables
        assert spec.orders_table in synced_tables

        # Extra tables exercising different engine types (users_dict is a dictionary — sync generates description/indexes)
        expected_engine_tables = {
            "orders_summing",  # SummingMergeTree
            "events_replacing",  # ReplacingMergeTree
            "agg_orders",  # AggregatingMergeTree
            "kafka_events",  # Kafka engine (no direct SELECT)
            "orders_by_user_mv_target",  # SummingMergeTree target for materialized view
            "users_dict",  # Dictionary (CREATE DICTIONARY)
        }
        assert expected_engine_tables.issubset(set(synced_tables))
        assert state.tables_synced >= len(expected_engine_tables) + 2  # + users, orders

    def test_exclude_filter(self, tmp_path_factory, db_config, spec):
        """Tables matching exclude patterns should be skipped (excluding orders leaves other tables)."""
        schema = spec.effective_filter_schema
        config = db_config.model_copy(update={"exclude": [f"{schema}.{spec.orders_table}"]})

        output = tmp_path_factory.mktemp(f"{spec.db_type}_exclude")
        with Progress(transient=True) as progress:
            state = sync_database(config, output, progress)

        base = self._base_path(output, config, spec)
        assert (base / f"table={spec.users_table}").is_dir()
        assert not (base / f"table={spec.orders_table}").exists()
        # One schema synced; excluding orders still leaves users + engine tables (e.g. orders_summing, events_replacing)
        assert state.tables_synced >= 2

    def test_dictionary_sync_generates_description_and_metadata(self, synced, spec):
        """Sync must generate description.md and indexes.md for dictionaries (important ClickHouse optimisation)."""
        _, output, config = synced
        base = self._base_path(output, config, spec)
        table_dir = base / "table=users_dict"
        assert table_dir.is_dir(), "users_dict table dir should exist after sync"
        description_md = table_dir / "description.md"
        indexes_md = table_dir / "indexes.md"
        assert description_md.exists(), "description.md must be generated for dictionary"
        assert indexes_md.exists(), "indexes.md must be generated for dictionary"
        idx_content = indexes_md.read_text()
        assert "CREATE DICTIONARY" in idx_content or "create dictionary" in idx_content.lower(), (
            "indexes.md should contain CREATE DICTIONARY DDL"
        )
        assert "SOURCE" in idx_content and "LAYOUT" in idx_content, (
            "indexes.md should contain dictionary SOURCE and LAYOUT"
        )

    def test_sync_all_schemas(self, tmp_path_factory, db_config, spec):
        """Overrides base test to test because clickhouse ddl has multiple tables.
        When schema is not specified, all schemas (temp DB + default with nonexistent) are synced."""
        if spec.schema_field is None:
            pytest.skip("Provider does not support multi-schema test")

        config = db_config.model_copy(update={spec.schema_field: None})

        output = tmp_path_factory.mktemp(f"{spec.db_type}_all_schemas")
        with Progress(transient=True) as progress:
            state = sync_database(config, output, progress)

        db_name = config.get_database_name()

        primary_base = output / f"type={spec.db_type}" / f"database={db_name}" / f"schema={spec.primary_schema}"
        assert primary_base.is_dir()
        assert (primary_base / f"table={spec.users_table}").is_dir()
        assert (primary_base / f"table={spec.orders_table}").is_dir()

        expected_files = ["columns.md", "description.md", "preview.md"]
        if spec.expects_indexes:
            expected_files.append("indexes.md")

        for table in (spec.users_table, spec.orders_table):
            files = sorted(f.name for f in (primary_base / f"table={table}").iterdir())
            assert files == sorted(expected_files)

        another_base = output / f"type={spec.db_type}" / f"database={db_name}" / f"schema={spec.another_schema}"
        assert another_base.is_dir()
        assert (another_base / f"table={spec.another_table}").is_dir()

        files = sorted(f.name for f in (another_base / f"table={spec.another_table}").iterdir())
        assert files == sorted(expected_files)

        assert state.schemas_synced == 2
        assert state.tables_synced >= 3  # primary has many tables + default has nonexistent
        assert spec.primary_schema in state.synced_schemas
        assert spec.another_schema in state.synced_schemas
        assert spec.users_table in state.synced_tables[spec.primary_schema]
        assert spec.orders_table in state.synced_tables[spec.primary_schema]
        assert spec.another_table in state.synced_tables[spec.another_schema]
