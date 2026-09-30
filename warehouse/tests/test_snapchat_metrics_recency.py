from __future__ import annotations

import datetime
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo


CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))

from warehouse import schema_config  # noqa: E402
from warehouse import sync_snapchat_daily as daily  # noqa: E402


class AccountTimezoneTests(unittest.TestCase):
    def test_missing_live_timezone_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": ""},
        ):
            with self.assertRaisesRegex(
                daily.SyncError,
                "missing the Snap account timezone",
            ):
                daily.resolve_account_timezone({})

    def test_configured_timezone_mismatch_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": "UTC"},
        ):
            with self.assertRaisesRegex(
                daily.SyncError,
                "does not match live account health",
            ):
                daily.resolve_account_timezone(
                    {"timezone": "America/Los_Angeles"}
                )

    def test_unknown_configured_timezone_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": "America/Not_A_Zone"},
        ):
            with self.assertRaisesRegex(
                daily.SyncError,
                "configured SNAPCHAT_ACCOUNT_TIMEZONE is unknown",
            ):
                daily.resolve_account_timezone(
                    {"timezone": "America/Los_Angeles"}
                )


class IntradayWindowTests(unittest.TestCase):
    def test_fall_back_uses_second_repeated_hour(self) -> None:
        account_tz = ZoneInfo("America/Los_Angeles")
        now = datetime.datetime(
            2026,
            11,
            1,
            9,
            30,
            tzinfo=datetime.timezone.utc,
        )
        start, end = daily._intraday_window_bounds(
            account_tz=account_tz,
            now=now,
        )

        self.assertEqual(start.isoformat(), "2026-11-01T00:00:00-07:00")
        self.assertEqual(end.isoformat(), "2026-11-01T01:00:00-08:00")
        self.assertEqual(end.fold, 1)
        self.assertEqual(
            end.astimezone(datetime.timezone.utc)
            - start.astimezone(datetime.timezone.utc),
            datetime.timedelta(hours=2),
        )

    def test_spring_forward_skips_nonexistent_hour(self) -> None:
        account_tz = ZoneInfo("America/Los_Angeles")
        now = datetime.datetime(
            2026,
            3,
            8,
            10,
            30,
            tzinfo=datetime.timezone.utc,
        )
        start, end = daily._intraday_window_bounds(
            account_tz=account_tz,
            now=now,
        )

        self.assertEqual(start.isoformat(), "2026-03-08T00:00:00-08:00")
        self.assertEqual(end.isoformat(), "2026-03-08T03:00:00-07:00")
        self.assertEqual(
            end.astimezone(datetime.timezone.utc)
            - start.astimezone(datetime.timezone.utc),
            datetime.timedelta(hours=2),
        )

    def test_midnight_is_successful_noop_without_stats_or_entity_calls(
        self,
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: int = 120) -> dict:
            del timeout
            calls.append(args)
            if args == ("account", "health-check"):
                return {"timezone": "America/Los_Angeles"}
            raise AssertionError(f"unexpected CLI call: {args}")

        now = datetime.datetime(
            2026,
            7,
            24,
            7,
            12,
            tzinfo=datetime.timezone.utc,
        )
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": "America/Los_Angeles"},
        ), mock.patch.object(daily, "_run_cli_json", side_effect=fake_cli):
            result = daily.collect_sync_rows(
                mode="intraday",
                days=14,
                now=now,
            )

        self.assertEqual(calls, [("account", "health-check")])
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["provisional"])
        self.assertEqual(
            result["window_start"].isoformat(),
            "2026-07-24T00:00:00-07:00",
        )
        self.assertEqual(
            result["window_end"].isoformat(),
            "2026-07-24T00:00:00-07:00",
        )


class MetricsCollectionTests(unittest.TestCase):
    def test_intraday_uses_total_by_ad_and_marks_current_day_provisional(
        self,
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: int = 120) -> dict:
            del timeout
            calls.append(args)
            if args == ("account", "health-check"):
                return {"timezone": "America/Los_Angeles"}
            if args[:2] == ("report", "stats"):
                return {
                    "total_stats": [
                        {
                            "total_stat": {
                                "breakdown_stats": {
                                    "ad": [
                                        {
                                            "id": "ad-1",
                                            "stats": {
                                                "spend": 12_500_000,
                                                "impressions": 1000,
                                                "swipes": 25,
                                                "conversion_purchases": 2,
                                                "conversion_purchases_value": 30_000_000,
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    ]
                }
            if args[:2] == ("ad", "list"):
                return {
                    "ads": [
                        {
                            "id": "ad-1",
                            "name": "Ad One",
                            "ad_squad_id": "squad-1",
                        }
                    ]
                }
            if args[:2] == ("adsquad", "list"):
                return {
                    "ad_squads": [
                        {
                            "id": "squad-1",
                            "name": "Squad One",
                            "campaign_id": "campaign-1",
                        }
                    ]
                }
            if args[:2] == ("campaign", "list"):
                return {
                    "campaigns": [
                        {"id": "campaign-1", "name": "Campaign One"}
                    ]
                }
            raise AssertionError(f"unexpected CLI call: {args}")

        now = datetime.datetime(
            2026,
            7,
            24,
            21,
            45,
            tzinfo=datetime.timezone.utc,
        )
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": "America/Los_Angeles"},
        ), mock.patch.object(daily, "_run_cli_json", side_effect=fake_cli):
            result = daily.collect_sync_rows(
                mode="intraday",
                days=14,
                now=now,
            )

        report_call = next(call for call in calls if call[:2] == ("report", "stats"))
        self.assertIn("TOTAL", report_call)
        self.assertIn("--breakdown", report_call)
        self.assertIn("ad", report_call)
        self.assertIn("--omit-empty", report_call)
        self.assertNotIn("--include-empty", report_call)
        self.assertEqual(
            report_call[report_call.index("--start-time") + 1],
            "2026-07-24T00:00:00-07:00",
        )
        self.assertEqual(
            report_call[report_call.index("--end-time") + 1],
            "2026-07-24T14:00:00-07:00",
        )

        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["recorded_at"], "2026-07-24")
        self.assertEqual(row["campaign_name"], "Campaign One")
        self.assertEqual(row["spend"], 12.5)
        self.assertEqual(row["conversions"], 2)
        self.assertEqual(row["roas"], 2.4)
        self.assertTrue(row["provisional"])
        self.assertEqual(
            row["source_window_end"].isoformat(),
            "2026-07-24T14:00:00-07:00",
        )
        self.assertEqual(row["last_synced_at"], now)

    def test_closed_mode_preserves_day_query_and_closed_midnight_end(
        self,
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_cli(*args: str, timeout: int = 120) -> dict:
            del timeout
            calls.append(args)
            if args == ("account", "health-check"):
                return {"timezone": "America/Los_Angeles"}
            if args[:2] == ("report", "stats"):
                return {"timeseries_stats": []}
            raise AssertionError(f"unexpected CLI call: {args}")

        now = datetime.datetime(
            2026,
            7,
            24,
            21,
            45,
            tzinfo=datetime.timezone.utc,
        )
        with mock.patch.dict(
            os.environ,
            {"SNAPCHAT_ACCOUNT_TIMEZONE": "America/Los_Angeles"},
        ), mock.patch.object(daily, "_run_cli_json", side_effect=fake_cli):
            result = daily.collect_sync_rows(
                mode="closed",
                days=14,
                now=now,
            )

        report_call = next(call for call in calls if call[:2] == ("report", "stats"))
        self.assertIn("DAY", report_call)
        self.assertIn("--include-empty", report_call)
        self.assertNotIn("--omit-empty", report_call)
        self.assertEqual(
            result["window_start"].isoformat(),
            "2026-07-10T00:00:00-07:00",
        )
        self.assertEqual(
            result["window_end"].isoformat(),
            "2026-07-24T00:00:00-07:00",
        )
        self.assertFalse(result["provisional"])
        self.assertEqual(result["rows"], [])


class _UpsertCursor:
    def __init__(self, store: dict[tuple[str, str], dict]) -> None:
        self.store = store
        self.sql = ""

    def __enter__(self) -> "_UpsertCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def executemany(self, sql: str, rows: list[dict]) -> None:
        self.sql = sql
        for row in rows:
            self.store[(row["ad_id"], row["recorded_at"])] = dict(row)


class _UpsertConnection:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict] = {}
        self.cursor_instance = _UpsertCursor(self.store)
        self.commits = 0

    def cursor(self) -> _UpsertCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


class UpsertAndSchemaCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _row(spend: float) -> dict:
        return {
            column: None
            for column in daily.ROW_COLUMNS
        } | {
            "recorded_at": "2026-07-24",
            "ad_id": "ad-1",
            "spend": spend,
            "provisional": True,
            "source_window_end": "2026-07-24T14:00:00-07:00",
            "last_synced_at": "2026-07-24T21:45:00+00:00",
        }

    def test_repeated_upsert_is_one_key_with_latest_values(self) -> None:
        conn = _UpsertConnection()
        first = self._row(10.0)
        retry = self._row(12.5)

        self.assertEqual(daily.upsert_rows(conn, [first]), 1)
        self.assertEqual(daily.upsert_rows(conn, [retry]), 1)

        self.assertEqual(len(conn.store), 1)
        self.assertEqual(conn.store[("ad-1", "2026-07-24")]["spend"], 12.5)
        self.assertEqual(conn.commits, 2)
        self.assertIn(
            "ON CONFLICT (ad_id, recorded_at) DO UPDATE",
            conn.cursor_instance.sql,
        )
        self.assertIn("provisional = EXCLUDED.provisional", conn.cursor_instance.sql)
        self.assertIn(
            "source_window_end = EXCLUDED.source_window_end",
            conn.cursor_instance.sql,
        )
        self.assertIn(
            "last_synced_at = EXCLUDED.last_synced_at",
            conn.cursor_instance.sql,
        )

    def test_schema_is_idempotent_and_compatible_with_existing_table(
        self,
    ) -> None:
        template = daily.SCHEMA_SQL.read_text(encoding="utf-8")
        rendered = schema_config.render_schema_sql(template, "custom_snap")

        self.assertNotIn(schema_config.SCHEMA_TEMPLATE_TOKEN, rendered)
        self.assertIn(
            'ALTER TABLE "custom_snap".snapchat_ad_daily_metrics',
            rendered,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS provisional boolean NOT NULL DEFAULT false",
            rendered,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS source_window_end timestamptz",
            rendered,
        )
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS last_synced_at timestamptz",
            rendered,
        )
        self.assertIn(
            'CREATE TABLE IF NOT EXISTS "custom_snap".snapchat_sync_runs',
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
