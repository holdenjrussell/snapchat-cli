from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse import query as warehouse_query  # noqa: E402
from warehouse import schema_config  # noqa: E402
from warehouse import sync_snapchat_daily as daily_sync  # noqa: E402
from warehouse import sync_snapchat_entities as entity_sync  # noqa: E402


class WarehouseSchemaTests(unittest.TestCase):
    def test_schema_validation_accepts_only_safe_lowercase_identifiers(self) -> None:
        self.assertEqual(
            schema_config.validate_warehouse_schema("snapchat_ads"),
            "snapchat_ads",
        )
        self.assertEqual(
            schema_config.validate_warehouse_schema("snap_stage2"),
            "snap_stage2",
        )
        for invalid in (
            "public; DROP TABLE x",
            "Snapchat_Ads",
            "pg_temp",
            "contains-hyphen",
            "",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(schema_config.WarehouseSchemaError):
                    schema_config.validate_warehouse_schema(invalid)

    def test_schema_template_renders_every_relation_in_configured_schema(self) -> None:
        template = (REPO_ROOT / "warehouse" / "schema.sql").read_text(
            encoding="utf-8"
        )
        rendered = schema_config.render_schema_sql(template, "snap_stage")

        self.assertNotIn(schema_config.SCHEMA_TEMPLATE_TOKEN, rendered)
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "snap_stage"', rendered)
        for table_name in (
            "snapchat_ad_daily_metrics",
            "snapchat_entity_state",
            "snapchat_change_history",
        ):
            self.assertIn(f'"snap_stage".{table_name}', rendered)
        self.assertIn("pg_catalog.gen_random_uuid()", rendered)
        self.assertIn("pg_catalog.now()", rendered)
        self.assertIn("pg_catalog.length(event_key)", rendered)

    def test_table_existence_lookup_is_schema_qualified(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.params = None

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def execute(self, _sql, params) -> None:
                self.params = params

            def fetchone(self):
                return ("snap_stage.snapchat_entity_state",)

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()

            def cursor(self):
                return self.cursor_instance

        conn = Connection()
        self.assertTrue(
            entity_sync.table_exists(
                conn,
                "snapchat_entity_state",
                warehouse_schema="snap_stage",
            )
        )
        self.assertEqual(
            conn.cursor_instance.params,
            ('"snap_stage"."snapchat_entity_state"',),
        )

    def test_daily_upsert_uses_fully_qualified_target(self) -> None:
        class Cursor:
            def __init__(self) -> None:
                self.sql = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def executemany(self, sql, _rows) -> None:
                self.sql = sql

        class Connection:
            def __init__(self) -> None:
                self.cursor_instance = Cursor()
                self.committed = False

            def cursor(self):
                return self.cursor_instance

            def commit(self) -> None:
                self.committed = True

        row = {column: None for column in daily_sync.ROW_COLUMNS}
        row.update({"recorded_at": "2026-07-13", "ad_id": "ad-1"})
        conn = Connection()
        daily_sync.upsert_rows(
            conn,
            [row],
            warehouse_schema="snap_stage",
        )

        self.assertIn(
            'INSERT INTO "snap_stage"."snapchat_ad_daily_metrics"',
            conn.cursor_instance.sql,
        )
        self.assertTrue(conn.committed)

    def test_query_uses_configured_non_public_path_and_read_only_transaction(
        self,
    ) -> None:
        executed: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            def execute(self, sql) -> None:
                executed.append(sql)

            def fetchall(self):
                return [{"ok": 1}]

        class Context:
            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

        class Connection(Context):
            def transaction(self):
                return Context()

            def cursor(self, **_kwargs):
                return Cursor()

        fake_psycopg = types.ModuleType("psycopg")
        fake_psycopg.Error = Exception
        fake_psycopg.connect = lambda _url: Connection()
        fake_rows = types.ModuleType("psycopg.rows")
        fake_rows.dict_row = object()

        argv = [
            "query.py",
            "--warehouse-schema",
            "snap_stage",
            "--sql",
            "SELECT 1 AS ok",
            "--reason",
            "schema routing test",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql:///test"}),
            mock.patch.dict(
                sys.modules,
                {"psycopg": fake_psycopg, "psycopg.rows": fake_rows},
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            warehouse_query.main()

        self.assertEqual(executed[0], "SET TRANSACTION READ ONLY")
        self.assertEqual(
            executed[1],
            'SET LOCAL search_path TO pg_catalog, "snap_stage"',
        )
        self.assertEqual(executed[2], "SELECT 1 AS ok\nLIMIT 5000")
        self.assertEqual(json.loads(stdout.getvalue())["rows"], [{"ok": 1}])


if __name__ == "__main__":
    unittest.main()
