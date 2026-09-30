"""Attribution windows reach every conversions read; per-ad day parsing holds."""

from __future__ import annotations

import datetime
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))

from warehouse import attribution  # noqa: E402
from warehouse import sync_snapchat_daily as daily  # noqa: E402

LENS_ENV = {
    "SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW": "7_day",
    "SNAPCHAT_VIEW_ATTRIBUTION_WINDOW": "NONE",
    "SNAPCHAT_ENGAGED_VIEW_ATTRIBUTION_WINDOW": "none",
}
NO_LENS_ENV = {key: "" for key in attribution.ENV_KEYS.values()}


class AttributionLensTests(unittest.TestCase):
    def test_unset_windows_send_nothing(self) -> None:
        self.assertEqual(
            attribution.attribution_lens(NO_LENS_ENV),
            {param: None for param in attribution.ENV_KEYS},
        )
        self.assertEqual(attribution.attribution_cli_args(NO_LENS_ENV), [])

    def test_windows_are_normalized_and_become_cli_arguments(self) -> None:
        self.assertEqual(
            attribution.attribution_lens(LENS_ENV),
            {
                "swipe_up_attribution_window": "7_DAY",
                "view_attribution_window": "none",
                "engaged_view_attribution_window": "none",
            },
        )
        args = attribution.attribution_cli_args(LENS_ENV)
        self.assertEqual(
            args[:4],
            [
                "--swipe-up-attribution-window",
                "7_DAY",
                "--view-attribution-window",
                "none",
            ],
        )
        self.assertEqual(args[4], "--params-json")
        self.assertEqual(
            json.loads(args[5]), {"engaged_view_attribution_window": "none"}
        )

    def test_malformed_window_fails_loudly(self) -> None:
        for bad in ("7 days", "SEVEN_DAY", "0_DAY", "7_WEEK"):
            with self.subTest(bad=bad), self.assertRaises(
                attribution.AttributionConfigError
            ):
                attribution.attribution_lens(
                    {"SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW": bad}
                )

    def test_describe_lens_names_snap_defaults(self) -> None:
        self.assertEqual(
            attribution.describe_lens(attribution.attribution_lens(NO_LENS_ENV)),
            "swipe Snap default, view Snap default, engaged view Snap default",
        )


def _fake_cli(calls: list[tuple[str, ...]]):
    def fake(*args: str, timeout: int = 120) -> dict:
        del timeout
        calls.append(args)
        if args == ("account", "health-check"):
            return {"timezone": "America/Los_Angeles"}
        if args[:2] == ("report", "stats"):
            return {"timeseries_stats": []}
        raise AssertionError(f"unexpected CLI call: {args}")

    return fake


class SyncAttributionTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 7, 24, 21, 45, tzinfo=datetime.timezone.utc)

    def _collect(self, mode: str, env: dict[str, str], days: int = 45):
        calls: list[tuple[str, ...]] = []
        with mock.patch.dict(os.environ, env), mock.patch.object(
            daily, "_run_cli_json", side_effect=_fake_cli(calls)
        ):
            result = daily.collect_sync_rows(mode=mode, days=days, now=self.NOW)
        stats_calls = [call for call in calls if call[:2] == ("report", "stats")]
        return result, stats_calls

    def test_closed_mode_sends_the_lens_on_every_chunk(self) -> None:
        result, stats_calls = self._collect("closed", LENS_ENV)
        self.assertEqual(len(stats_calls), 2)  # 45 days -> two <=30-day chunks
        for call in stats_calls:
            self.assertIn("--swipe-up-attribution-window", call)
            self.assertEqual(
                call[call.index("--swipe-up-attribution-window") + 1], "7_DAY"
            )
            self.assertEqual(call[call.index("--view-attribution-window") + 1], "none")
            self.assertIn("--params-json", call)
        self.assertEqual(
            result["attribution"]["swipe_up_attribution_window"], "7_DAY"
        )

    def test_intraday_mode_sends_the_lens(self) -> None:
        _, stats_calls = self._collect("intraday", LENS_ENV, days=1)
        [call] = stats_calls
        self.assertIn("TOTAL", call)
        self.assertIn("--swipe-up-attribution-window", call)

    def test_unset_lens_leaves_the_request_unchanged(self) -> None:
        result, stats_calls = self._collect("closed", NO_LENS_ENV, days=7)
        [call] = stats_calls
        self.assertNotIn("--swipe-up-attribution-window", call)
        self.assertNotIn("--params-json", call)
        self.assertEqual(
            result["attribution"], {param: None for param in attribution.ENV_KEYS}
        )

    def test_bad_lens_fails_before_any_api_call(self) -> None:
        calls: list[tuple[str, ...]] = []
        env = {"SNAPCHAT_VIEW_ATTRIBUTION_WINDOW": "forever"}
        with mock.patch.dict(os.environ, env), mock.patch.object(
            daily, "_run_cli_json", side_effect=_fake_cli(calls)
        ), self.assertRaises(daily.SyncError):
            daily.collect_sync_rows(mode="closed", days=7, now=self.NOW)
        self.assertEqual(calls, [])


class PerAdTimeseriesParsingTests(unittest.TestCase):
    """breakdown=ad nests each day under breakdown_stats.ad[].timeseries[]."""

    def test_each_day_lands_on_its_own_date_with_its_own_spend(self) -> None:
        payload = {
            "timeseries_stats": [
                {
                    "sub_request_status": "SUCCESS",
                    "timeseries_stat": {
                        "id": "acct",
                        "type": "AD_ACCOUNT",
                        "start_time": "2026-09-27T00:00:00.000-04:00",
                        "breakdown_stats": {
                            "ad": [
                                {
                                    "id": "ad-1",
                                    "type": "AD",
                                    "timeseries": [
                                        {
                                            "start_time": "2026-09-27T00:00:00.000-04:00",
                                            "end_time": "2026-09-28T00:00:00.000-04:00",
                                            "stats": {"spend": 1_500_000},
                                        },
                                        {
                                            "start_time": "2026-09-28T00:00:00.000-04:00",
                                            "end_time": "2026-09-29T00:00:00.000-04:00",
                                            "stats": {"spend": 2_250_000},
                                        },
                                    ],
                                },
                                {
                                    "id": "ad-2",
                                    "type": "AD",
                                    "timeseries": [
                                        {
                                            "start_time": "2026-09-28T00:00:00.000-04:00",
                                            "end_time": "2026-09-29T00:00:00.000-04:00",
                                            "stats": {"spend": 750_000},
                                        },
                                    ],
                                },
                            ]
                        },
                    },
                }
            ]
        }
        rows = daily.extract_daily_ad_rows(payload)
        self.assertEqual(
            sorted((r["date"], r["ad_id"], r["stats"]["spend"]) for r in rows),
            [
                ("2026-09-27", "ad-1", 1_500_000),
                ("2026-09-28", "ad-1", 2_250_000),
                ("2026-09-28", "ad-2", 750_000),
            ],
        )
        built = daily.build_rows(rows, {}, {}, {})
        by_day: dict[str, float] = {}
        for row in built:
            by_day[row["recorded_at"]] = by_day.get(row["recorded_at"], 0) + row["spend"]
        self.assertEqual(by_day, {"2026-09-27": 1.5, "2026-09-28": 3.0})


if __name__ == "__main__":
    unittest.main()
