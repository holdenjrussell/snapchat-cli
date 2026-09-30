#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]"]
# ///
"""Run one read-only SELECT against the Snapchat Ads warehouse.

Usage:
    uv run warehouse/query.py \\
        --warehouse-schema "${SNAP_WAREHOUSE_SCHEMA:-snapchat_ads}" \\
        --sql 'SELECT COUNT(*) FROM snapchat_ad_daily_metrics' \\
        --reason "how many rows in the warehouse"

Guardrails: single SELECT/WITH...SELECT statement only, no writes, no DDL,
no multi-statement input. A LIMIT is auto-injected when the query doesn't
already have one, capped by --limit-guard (default 5000).

Output contract (stdout, JSON):
    {"rows": [...], "durationMs": <int>, "rowCount": <int>}

This exact shape is consumed by downstream automation, so it must not
change without updating every caller.

Reads the connection string from the DATABASE_URL environment variable.
Exits 2 if DATABASE_URL is unset or the schema is invalid. The configured
schema defaults to SNAP_WAREHOUSE_SCHEMA or snapchat_ads.
"""
from __future__ import annotations

import argparse
import datetime
import decimal
import json
import os
import re
import sys
import time
import uuid
from typing import Any

try:
    from warehouse.schema_config import (
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        search_path_sql,
        validate_warehouse_schema,
    )
except ModuleNotFoundError:  # Direct script execution from warehouse/.
    from schema_config import (
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        search_path_sql,
        validate_warehouse_schema,
    )

# Statement keywords that must never appear in an agent-issued read query.
# Matched as whole SQL keywords (word boundaries) so they don't false-positive
# on identifiers like "updated_at" or "created_at".
_FORBIDDEN_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT",
    "REVOKE", "CREATE", "COPY", "CALL", "MERGE", "VACUUM", "REINDEX",
    "ATTACH", "DETACH", "EXECUTE", "EXEC", "LOCK", "LISTEN", "NOTIFY",
    "UNLISTEN", "SET", "RESET", "BEGIN", "COMMIT", "ROLLBACK",
    "SAVEPOINT", "DO", "REFRESH", "SECURITY", "PREPARE", "DEALLOCATE",
)
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b", re.IGNORECASE
)
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+\b", re.IGNORECASE)
_LEADING_KEYWORD_RE = re.compile(r"^\s*\(?\s*(WITH|SELECT)\b", re.IGNORECASE)


class QueryValidationError(ValueError):
    """Raised when the SQL fails the SELECT-only guard."""


def _strip_sql_comments(sql: str) -> str:
    """Strip -- line comments and /* */ block comments before validation."""
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", " ", no_block)
    return no_line


def validate_select_only(sql: str) -> str:
    """Validate that `sql` is a single, read-only SELECT/WITH statement.

    Returns the query with any trailing semicolon and surrounding whitespace
    stripped. Raises QueryValidationError on anything that looks like a
    write, DDL, multi-statement payload, or session/transaction control.
    """
    raw = sql.strip()
    if not raw:
        raise QueryValidationError("Empty SQL is not allowed.")

    stripped = _strip_sql_comments(raw).strip()
    # Allow exactly one optional trailing semicolon; reject anything after it
    # and reject any semicolon embedded earlier (stacked statements).
    body = stripped
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise QueryValidationError("Only a single SQL statement is allowed.")

    if not _LEADING_KEYWORD_RE.match(body):
        raise QueryValidationError("Query must start with SELECT or WITH.")

    match = _FORBIDDEN_RE.search(body)
    if match:
        raise QueryValidationError(f"Query contains a disallowed keyword: {match.group(1).upper()}")

    return raw[:-1].rstrip() if raw.rstrip().endswith(";") else raw


def apply_limit_guard(sql: str, limit_guard: int) -> str:
    """Append a LIMIT clause when the query doesn't already have one."""
    if _LIMIT_RE.search(sql):
        return sql
    return f"{sql}\nLIMIT {int(limit_guard)}"


def _json_default(value: Any) -> Any:
    """Coerce non-JSON-native Postgres/Python types to JSON-safe values."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def rows_to_json_safe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round-trip each row through the JSON encoder's default= hook so every
    value in the output is guaranteed JSON-serializable (dates/decimals/UUIDs
    become str/float, matching the documented output contract)."""
    return json.loads(json.dumps(rows, default=_json_default))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a single read-only SELECT against the warehouse.")
    parser.add_argument("--sql", required=True, help="A single SELECT or WITH...SELECT statement.")
    parser.add_argument(
        "--reason",
        required=True,
        help="One sentence describing why this query is running (written to stderr as an audit line).",
    )
    parser.add_argument(
        "--limit-guard",
        type=int,
        default=5000,
        help="Max rows returned when the query doesn't already specify a LIMIT (default 5000).",
    )
    parser.add_argument(
        "--warehouse-schema",
        default=os.environ.get("SNAP_WAREHOUSE_SCHEMA", DEFAULT_WAREHOUSE_SCHEMA),
        help=(
            "Validated warehouse schema used for this transaction's local search path "
            f"(default: SNAP_WAREHOUSE_SCHEMA or {DEFAULT_WAREHOUSE_SCHEMA})."
        ),
    )
    args = parser.parse_args()

    try:
        warehouse_schema = validate_warehouse_schema(args.warehouse_schema)
    except WarehouseSchemaError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(2)

    print(
        f"[query.py] reason={args.reason!r} limit_guard={args.limit_guard} "
        f"warehouse_schema={warehouse_schema!r}",
        file=sys.stderr,
    )

    try:
        validated = validate_select_only(args.sql)
    except QueryValidationError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)

    executed_sql = apply_limit_guard(validated, args.limit_guard)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(json.dumps({"error": "DATABASE_URL environment variable is not set."}), file=sys.stderr)
        sys.exit(2)

    import psycopg
    from psycopg.rows import dict_row

    start = time.monotonic()
    try:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute(search_path_sql(warehouse_schema))
                    cur.execute(executed_sql)
                    rows = cur.fetchall()
    except psycopg.Error as exc:
        print(json.dumps({"error": f"Query failed: {exc}"}), file=sys.stderr)
        sys.exit(1)

    duration_ms = int((time.monotonic() - start) * 1000)
    safe_rows = rows_to_json_safe(rows)
    print(json.dumps({"rows": safe_rows, "durationMs": duration_ms, "rowCount": len(safe_rows)}))


if __name__ == "__main__":
    main()
