import logging
import re
from typing import Any, Literal

import ibis
from ibis import BaseBackend
from pydantic import Field

from nao_core.config.exceptions import InitError
from nao_core.ui import ask_text

from .base import DatabaseAccessor, DatabaseConfig
from .context import DatabaseContext

logger = logging.getLogger(__name__)

# Stream-like engines (Kafka, RabbitMQ, FileLog) disallow direct SELECT by default (code 620)
_DIRECT_SELECT_DISALLOWED = ("620", "Direct select is not allowed", "stream_like_engine_allow_direct_select")


def _is_direct_select_disallowed(exc: BaseException) -> bool:
    """True if the exception is ClickHouse code 620 / direct select not allowed (e.g. Kafka/RabbitMQ/FileLog)."""
    msg = str(exc)
    return any(s in msg for s in _DIRECT_SELECT_DISALLOWED)


# AggregateFunction(type_str) -> first argument is the function name (uniq, sum, etc.)
_AGGREGATE_FUNCTION_PATTERN = re.compile(
    r"aggregatefunction\s*\(\s*(\w+)",
    re.IGNORECASE,
)


def _aggregate_function_name(dtype: Any) -> str | None:
    """If dtype is AggregateFunction(...), return the function name (e.g. uniq); else None."""
    type_str = str(dtype)
    m = _AGGREGATE_FUNCTION_PATTERN.search(type_str.lower())
    return m.group(1).lower() if m else None


def _normalize_row(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Coerce non-JSON-serializable values to string for preview output."""
    out = dict(row_dict)
    for key, val in out.items():
        if val is not None and not isinstance(val, (str, int, float, bool, list, dict)):
            out[key] = str(val)
    return out


def _show_create_table(conn: BaseBackend, database: str, table_name: str) -> str | None:
    """Return the result of SHOW CREATE TABLE for the given table, or None on error."""
    try:
        sql = f"SHOW CREATE TABLE `{database}`.`{table_name}`"
        cursor = conn.raw_sql(sql)  # type: ignore[union-attr]
        if hasattr(cursor, "fetchone"):
            row = cursor.fetchone()
        elif hasattr(cursor, "result_rows") and hasattr(cursor, "column_names"):
            rows = getattr(cursor, "result_rows", [])
            if not rows:
                return None
            row = rows[0]
        else:
            return None
        if row is not None and len(row) > 0:
            return str(row[0]).strip()
    except Exception:
        return None
    return None


def _build_preview_select_expressions(schema: dict) -> list[str]:
    """Build SELECT expression for each column from table definition (schema).

    Plain columns are selected as-is; AggregateFunction columns use the
    -Merge combinator (e.g. uniqMerge(`col`)) so we can read them. This way
    we know how to query the table from how it is defined.
    """
    parts = []
    for name, dtype in schema.items():
        quoted = f"`{name}`"
        agg_name = _aggregate_function_name(dtype)
        if agg_name:
            # e.g. uniq -> uniqMerge(`name`) AS `name`
            merge_func = f"{agg_name}Merge"
            parts.append(f"{merge_func}({quoted}) AS {quoted}")
        else:
            parts.append(quoted)
    return parts


def _raw_sql_to_rows(cursor: Any) -> list[dict[str, Any]]:
    """Convert raw_sql cursor result to list of dicts (column name -> value)."""
    if hasattr(cursor, "result_rows") and hasattr(cursor, "column_names"):
        columns = list(cursor.column_names)
        raw_rows = cursor.result_rows
        return [dict(zip(columns, row)) for row in raw_rows]
    if hasattr(cursor, "fetchall") and hasattr(cursor, "description"):
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    return []


def _get_table_comment(conn: BaseBackend, database: str, table_name: str) -> str | None:
    """Return the table comment from system.tables, or None if missing or on error."""
    try:
        # Prevent SQL injection by escaping single quotes
        d = database.replace("\\", "\\\\").replace("'", "''")
        t = table_name.replace("\\", "\\\\").replace("'", "''")
        sql = f"SELECT comment FROM system.tables WHERE database = '{d}' AND name = '{t}'"
        cursor = conn.raw_sql(sql)  # type: ignore[union-attr]
        rows = _raw_sql_to_rows(cursor)
        if not rows:
            return None
        comment = rows[0].get("comment")
        if not comment:
            return None
        return str(comment).strip() or None
    except Exception:
        return None


def _columns_from_system(conn: BaseBackend, database: str, table_name: str) -> list[dict[str, Any]]:
    """Return column metadata from system.columns (does not SELECT from the table)."""
    try:
        # Escape single quotes for safe SQL (identifiers from config)
        d = database.replace("\\", "\\\\").replace("'", "''")
        t = table_name.replace("\\", "\\\\").replace("'", "''")
        sql = f"SELECT name, type FROM system.columns WHERE database = '{d}' AND table = '{t}' ORDER BY position"
        cursor = conn.raw_sql(sql)  # type: ignore[union-attr]
        rows = _raw_sql_to_rows(cursor)
        return [
            {
                "name": r["name"],
                "type": str(r.get("type", "")),
                "nullable": "Nullable" in str(r.get("type", "")),
                "description": None,
            }
            for r in rows
        ]
    except Exception:
        return []


class ClickHouseDatabaseContext(DatabaseContext):
    """ClickHouse context that uses SHOW CREATE TABLE and schema to know how to query.

    We use the table definition (from schema, which reflects SHOW CREATE TABLE)
    to build the right SELECT: plain columns as-is, AggregateFunction columns
    via -Merge (e.g. uniqMerge(column)) so preview works for all table types.

    Stream-like engines (Kafka, RabbitMQ, FileLog) disallow direct SELECT (code 620).
    When we detect that error for a table, we set _direct_select_disallowed and automatically
    use the no-SELECT path (SHOW CREATE TABLE + system.columns) for all later operations on that table.
    """

    def __init__(self, conn: BaseBackend, schema: str, table_name: str):
        super().__init__(conn, schema, table_name)
        self._direct_select_disallowed: bool = False

    def description(self) -> str | None:
        return _get_table_comment(self._conn, self._schema, self._table_name)

    def indexes(self) -> str | None:
        """Return table DDL (SHOW CREATE TABLE) so the agent sees ORDER BY, PRIMARY KEY, PARTITION BY, and indexes."""
        return _show_create_table(self._conn, self._schema, self._table_name)

    def row_count(self) -> int:
        """Return row count; for stream-like engines (Kafka/RabbitMQ/FileLog) direct SELECT is disallowed, return 0."""
        if self._direct_select_disallowed:
            return 0
        try:
            return self.table.count().execute()
        except Exception as e:
            if _is_direct_select_disallowed(e):
                self._direct_select_disallowed = True
                logger.debug(
                    "ClickHouse: direct select not allowed for %s.%s; using no-SELECT path for this table",
                    self._schema,
                    self._table_name,
                )
                return 0
            raise

    def column_count(self) -> int:
        """Return column count; for stream-like engines use system.columns if table.schema() is disallowed."""
        if self._direct_select_disallowed:
            return len(_columns_from_system(self._conn, self._schema, self._table_name))
        try:
            return len(self.table.schema())
        except Exception as e:
            if _is_direct_select_disallowed(e):
                self._direct_select_disallowed = True
                return len(_columns_from_system(self._conn, self._schema, self._table_name))
            raise

    def columns(self) -> list[dict[str, Any]]:
        """Return column metadata; for stream-like engines use system.columns (no SELECT from table)."""
        if self._direct_select_disallowed:
            return []
        try:
            schema = self.table.schema()
            return [
                {
                    "name": name,
                    "type": self._format_type(dtype),
                    "nullable": getattr(dtype, "nullable", True),
                    "description": None,
                }
                for name, dtype in schema.items()
            ]
        except Exception as e:
            if _is_direct_select_disallowed(e):
                self._direct_select_disallowed = True
                return _columns_from_system(self._conn, self._schema, self._table_name)
            raise

    def preview(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return preview rows by building SELECT from table definition.

        Uses the table schema (same info as SHOW CREATE TABLE) to figure out
        how to query. For stream-like engines (Kafka/RabbitMQ/FileLog) we
        automatically use the no-SELECT path (DDL only) once 620 is detected.
        """
        if self._direct_select_disallowed:
            ddl = _show_create_table(self._conn, self._schema, self._table_name)
            return [{"create_table": ddl}] if ddl else []

        try:
            schema = self.table.schema()
        except Exception as e:
            if _is_direct_select_disallowed(e):
                self._direct_select_disallowed = True
                ddl = _show_create_table(self._conn, self._schema, self._table_name)
                return [{"create_table": ddl}] if ddl else []
            raise

        if not schema:
            ddl = _show_create_table(self._conn, self._schema, self._table_name)
            return [{"create_table": ddl}] if ddl else []

        select_parts = _build_preview_select_expressions(schema)
        quoted_table = f"`{self._schema}`.`{self._table_name}`"
        sql = f"SELECT {', '.join(select_parts)} FROM {quoted_table} LIMIT {limit}"

        try:
            cursor = self._conn.raw_sql(sql)  # type: ignore[union-attr]
            rows = _raw_sql_to_rows(cursor)
            return [_normalize_row(r) for r in rows]
        except Exception as e:
            if _is_direct_select_disallowed(e):
                self._direct_select_disallowed = True
            else:
                logger.debug(
                    "ClickHouse preview query failed for %s.%s: %s; returning DDL",
                    self._schema,
                    self._table_name,
                    e,
                )
            ddl = _show_create_table(self._conn, self._schema, self._table_name)
            if ddl:
                return [{"create_table": ddl}]
            return []


class ClickHouseConfig(DatabaseConfig):
    """ClickHouse-specific configuration."""

    type: Literal["clickhouse"] = "clickhouse"
    host: str = Field(description="ClickHouse server host")
    port: int | None = Field(default=None, description="HTTP port (8123 plain, 8443 secure)")
    database: str = Field(description="Database name")
    user: str = Field(description="Username")
    password: str = Field(default="", description="Password")
    secure: bool = Field(default=False, description="Use HTTPS")
    connect_timeout: int | None = Field(
        default=None,
        description="Connection timeout in seconds (passed to ibis.clickhouse.connect).",
    )
    send_receive_timeout: int | None = Field(
        default=None,
        description="Send/receive timeout in seconds (passed to ibis.clickhouse.connect).",
    )
    schemas_include: list[str] | None = Field(
        default=None,
        description="If set, only sync these databases (schema names). Empty or None = sync all user databases.",
    )
    accessors: list[DatabaseAccessor] = Field(
        default_factory=lambda: list(DatabaseAccessor),
        description="Which default templates to render per table. Defaults to all (including indexes).",
    )

    @classmethod
    def promptConfig(cls) -> "ClickHouseConfig":
        """Interactively prompt the user for ClickHouse configuration."""
        name = ask_text("Connection name:", default="clickhouse-prod") or "clickhouse-prod"
        host = ask_text("Host:", default="localhost") or "localhost"
        port_str = ask_text(
            "Port (empty = default 8123/8443):",
            default="8123",
        )
        if port_str and not port_str.isdigit():
            raise InitError("Port must be a valid integer or empty.")
        port = int(port_str) if port_str and port_str.isdigit() else None
        database = ask_text("Database name:", default="default") or "default"
        user = ask_text("Username:", default="default") or "default"
        password = ask_text("Password:", password=True) or ""
        secure_str = ask_text("Use HTTPS (y/n):", default="n")
        secure = bool(secure_str and str(secure_str).lower().startswith("y"))
        if port is None:
            port = 8443 if secure else 8123

        return ClickHouseConfig(
            name=name,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            secure=secure,
        )

    def connect(self) -> BaseBackend:
        """Create an Ibis ClickHouse connection."""
        kwargs: dict = {
            "host": self.host,
            "database": self.database,
            "user": self.user,
            "password": self.password,
            "secure": self.secure,
        }
        if self.port is not None:
            kwargs["port"] = self.port
        if self.connect_timeout is not None:
            kwargs["connect_timeout"] = self.connect_timeout
        if self.send_receive_timeout is not None:
            kwargs["send_receive_timeout"] = self.send_receive_timeout
        return ibis.clickhouse.connect(**kwargs)

    def get_database_name(self) -> str:
        """Get the database name for ClickHouse."""
        return self.database

    # Built-in databases to exclude from sync (system tables can hang or require Zookeeper/SSL)
    _SYSTEM_DATABASES = frozenset(("INFORMATION_SCHEMA", "information_schema", "system"))

    def get_schemas(self, conn: BaseBackend) -> list[str]:
        list_databases = getattr(conn, "list_databases", None)
        if list_databases:
            schemas = [s for s in list_databases() if s not in self._SYSTEM_DATABASES]
            if self.schemas_include:
                schemas = [s for s in schemas if s in self.schemas_include]
            return schemas
        return []

    def create_context(self, conn: BaseBackend, schema: str, table_name: str) -> ClickHouseDatabaseContext:
        """Use ClickHouse-specific context for resilient preview."""
        return ClickHouseDatabaseContext(conn, schema, table_name)

    def check_connection(self) -> tuple[bool, str]:
        """Test connectivity to ClickHouse."""
        conn = None
        try:
            conn = self.connect()
            if list_databases := getattr(conn, "list_databases", None):
                schemas = list_databases()
                return True, f"Connected successfully ({len(schemas)} databases found)"
            return True, "Connected successfully"
        except Exception as e:
            return False, str(e)
        finally:
            if conn is not None:
                conn.disconnect()
