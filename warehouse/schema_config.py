"""Validated, explicit PostgreSQL schema routing for the Snap warehouse."""

from __future__ import annotations

import re


DEFAULT_WAREHOUSE_SCHEMA = "snapchat_ads"
SCHEMA_TEMPLATE_TOKEN = "__SNAP_WAREHOUSE_SCHEMA__"
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class WarehouseSchemaError(ValueError):
    """Raised when a configured PostgreSQL schema identifier is unsafe."""


def validate_identifier(value: str, *, label: str = "identifier") -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(text) or text.startswith("pg_"):
        raise WarehouseSchemaError(
            f"{label} must be 1-63 lowercase letters, digits, or underscores, "
            "start with a letter or underscore, and must not start with pg_"
        )
    return text


def validate_warehouse_schema(value: str | None) -> str:
    return validate_identifier(
        DEFAULT_WAREHOUSE_SCHEMA if value is None else value,
        label="warehouse schema",
    )


def quoted_identifier(value: str, *, label: str = "identifier") -> str:
    return f'"{validate_identifier(value, label=label)}"'


def qualified_table(warehouse_schema: str, table_name: str) -> str:
    return (
        f"{quoted_identifier(warehouse_schema, label='warehouse schema')}."
        f"{quoted_identifier(table_name, label='table name')}"
    )


def search_path_sql(warehouse_schema: str) -> str:
    """Return a safe explicit local search path for defensive compatibility."""
    return (
        "SET LOCAL search_path TO pg_catalog, "
        f"{quoted_identifier(warehouse_schema, label='warehouse schema')}"
    )


def render_schema_sql(template: str, warehouse_schema: str) -> str:
    """Render the checked-in DDL template with a validated quoted schema."""
    schema = quoted_identifier(warehouse_schema, label="warehouse schema")
    if SCHEMA_TEMPLATE_TOKEN not in template:
        raise WarehouseSchemaError(
            f"schema template is missing {SCHEMA_TEMPLATE_TOKEN}"
        )
    rendered = template.replace(SCHEMA_TEMPLATE_TOKEN, schema)
    if SCHEMA_TEMPLATE_TOKEN in rendered:
        raise WarehouseSchemaError("schema template rendering was incomplete")
    return rendered
