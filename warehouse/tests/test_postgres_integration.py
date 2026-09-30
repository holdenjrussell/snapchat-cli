from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from pathlib import Path


CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))

from warehouse import run_snapchat_warehouse_cycle as cycle  # noqa: E402
from warehouse import schema_config  # noqa: E402
from warehouse import sync_snapchat_daily as daily  # noqa: E402


TEST_DATABASE_URL = os.environ.get("SNAPCHAT_TEST_DATABASE_URL")


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "SNAPCHAT_TEST_DATABASE_URL is required for disposable Postgres coverage",
)
class DisposablePostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg

        assert TEST_DATABASE_URL is not None
        cls.database_url = TEST_DATABASE_URL
        with psycopg.connect(cls.database_url) as conn:
            if not str(conn.info.dbname).startswith("snapchat_candidate_"):
                raise RuntimeError(
                    "integration tests refuse a database whose name does not "
                    "start with snapchat_candidate_"
                )
        cls.schema = f"snap_candidate_{uuid.uuid4().hex[:12]}"

    @classmethod
    def tearDownClass(cls) -> None:
        import psycopg

        if not hasattr(cls, "database_url"):
            return
        with psycopg.connect(cls.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"DROP SCHEMA IF EXISTS "
                    f"{schema_config.quoted_identifier(cls.schema)} CASCADE"
                )
            conn.commit()

    @staticmethod
    def _metric_row(
        *,
        provisional: bool,
        source_window_end: datetime.datetime,
        spend: float,
    ) -> dict:
        row = {column: None for column in daily.ROW_COLUMNS}
        row.update(
            {
                "recorded_at": "2026-07-24",
                "campaign_id": "campaign-1",
                "campaign_name": "Campaign One",
                "ad_squad_id": "squad-1",
                "ad_squad_name": "Squad One",
                "ad_id": "ad-1",
                "ad_name": "Ad One",
                "spend": spend,
                "impressions": 1000,
                "swipes": 25,
                "conversions": 2,
                "revenue": 30.0,
                "roas": round(30.0 / spend, 4),
                "source_window_end": source_window_end,
                "provisional": provisional,
                "last_synced_at": datetime.datetime.now(
                    datetime.timezone.utc
                ),
            }
        )
        return row

    def test_schema_upserts_and_terminal_receipts(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url) as conn:
            daily.apply_schema(conn, warehouse_schema=self.schema)
            daily.apply_schema(conn, warehouse_schema=self.schema)

            intraday_end = datetime.datetime.fromisoformat(
                "2026-07-24T14:00:00-07:00"
            )
            provisional = self._metric_row(
                provisional=True,
                source_window_end=intraday_end,
                spend=12.5,
            )
            self.assertEqual(
                daily.upsert_rows(
                    conn,
                    [provisional],
                    warehouse_schema=self.schema,
                ),
                1,
            )
            self.assertEqual(
                daily.upsert_rows(
                    conn,
                    [provisional],
                    warehouse_schema=self.schema,
                ),
                1,
            )

            closed_end = datetime.datetime.fromisoformat(
                "2026-07-25T00:00:00-07:00"
            )
            closed = self._metric_row(
                provisional=False,
                source_window_end=closed_end,
                spend=13.0,
            )
            self.assertEqual(
                daily.upsert_rows(
                    conn,
                    [closed],
                    warehouse_schema=self.schema,
                ),
                1,
            )
            self.assertEqual(
                daily.upsert_rows(
                    conn,
                    [closed],
                    warehouse_schema=self.schema,
                ),
                1,
            )

            table = schema_config.qualified_table(
                self.schema,
                "snapchat_ad_daily_metrics",
            )
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT pg_catalog.count(*),
                           pg_catalog.bool_and(NOT provisional),
                           pg_catalog.max(source_window_end),
                           pg_catalog.max(spend)
                      FROM {table}
                     WHERE ad_id = %s
                       AND recorded_at = %s
                    """,
                    ("ad-1", datetime.date(2026, 7, 24)),
                )
                count, all_closed, stored_end, stored_spend = cur.fetchone()

        self.assertEqual(count, 1)
        self.assertTrue(all_closed)
        self.assertEqual(
            stored_end,
            closed_end.astimezone(datetime.timezone.utc),
        )
        self.assertEqual(float(stored_spend), 13.0)

        started_at = datetime.datetime.now(datetime.timezone.utc)
        success_id = uuid.uuid4()
        cycle.start_receipt(
            self.database_url,
            run_id=success_id,
            mode="recent",
            account_key="candidate",
            warehouse_schema=self.schema,
            started_at=started_at,
            stale_after_seconds=7200,
        )
        cycle.finish_receipt(
            self.database_url,
            run_id=success_id,
            warehouse_schema=self.schema,
            status="succeeded",
            window_start="2026-07-24T00:00:00-07:00",
            window_end="2026-07-24T14:00:00-07:00",
            metrics_rows=1,
            entity_rows=240,
            error=None,
            metadata={"integration_test": True},
        )

        failure_id = uuid.uuid4()
        cycle.start_receipt(
            self.database_url,
            run_id=failure_id,
            mode="recent",
            account_key="candidate",
            warehouse_schema=self.schema,
            started_at=started_at,
            stale_after_seconds=7200,
        )
        cycle.finish_receipt(
            self.database_url,
            run_id=failure_id,
            warehouse_schema=self.schema,
            status="failed",
            window_start="2026-07-24T00:00:00-07:00",
            window_end="2026-07-24T14:00:00-07:00",
            metrics_rows=1,
            entity_rows=0,
            error="candidate failure",
            metadata={"integration_test": True},
        )

        receipts = schema_config.qualified_table(
            self.schema,
            "snapchat_sync_runs",
        )
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT run_id, status, completed_at IS NOT NULL, error
                      FROM {receipts}
                     WHERE run_id = ANY(%s)
                     ORDER BY status
                    """,
                    ([success_id, failure_id],),
                )
                rows = cur.fetchall()

        self.assertEqual(len(rows), 2)
        by_id = {row[0]: row[1:] for row in rows}
        self.assertEqual(by_id[success_id], ("succeeded", True, None))
        self.assertEqual(
            by_id[failure_id],
            ("failed", True, "candidate failure"),
        )


if __name__ == "__main__":
    unittest.main()
