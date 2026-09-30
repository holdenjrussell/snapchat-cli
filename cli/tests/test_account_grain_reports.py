"""Account-grain reports sum a campaign breakdown (Snap E1008 workaround)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from snapchat_ads_cli.tools import reports


class FakeClient:
    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = []

    def get(self, path, params=None, timeout=None):
        self.calls.append((path, dict(params or {})))
        # Longest prefix wins so "adaccounts/acct/stats" beats "adaccounts/acct".
        for prefix in sorted(self.bodies, key=len, reverse=True):
            if path.startswith(prefix):
                return self.bodies[prefix], {}
        raise AssertionError(f"unexpected GET {path}")

    def stats_params(self):
        return [params for path, params in self.calls if path.endswith("/stats")][0]


def _point(day, **stats):
    return {
        "start_time": f"2026-09-{day:02d}T00:00:00.000-04:00",
        "end_time": f"2026-09-{day + 1:02d}T00:00:00.000-04:00",
        "stats": stats,
    }


ACCOUNT = {"adaccounts": [{"adaccount": {"timezone": "America/New_York"}}]}

BREAKDOWN = {
    "request_status": "SUCCESS",
    "timeseries_stats": [
        {
            "sub_request_status": "SUCCESS",
            "timeseries_stat": {
                "id": "acct",
                "type": "AD_ACCOUNT",
                "finalized_data_end_time": "2026-09-17T18:00:00.000Z",
                "breakdown_stats": {
                    "campaign": [
                        {
                            "id": "c1",
                            "timeseries": [
                                _point(15, spend=100, conversion_purchases=1, uniques=9,
                                       avg_view_time_millis=900.0),
                                _point(16, spend=50),
                            ],
                        },
                        {
                            "id": "c2",
                            "timeseries": [
                                _point(15, spend=25, conversion_purchases=2, uniques=4,
                                       swipe_up_percent=0.5),
                            ],
                        },
                    ]
                },
            },
        }
    ],
}


def test_rollup_sums_campaigns_per_bucket_and_keeps_watermarks():
    out = reports._account_rollup(BREAKDOWN)
    stat = out["timeseries_stats"][0]["timeseries_stat"]
    assert "breakdown_stats" not in stat
    assert stat["finalized_data_end_time"] == "2026-09-17T18:00:00.000Z"
    assert stat["rolled_up_from"] == "campaign"
    assert stat["campaigns_summed"] == 2
    assert [p["stats"] for p in stat["timeseries"]] == [
        {"spend": 125, "conversion_purchases": 3},
        {"spend": 50},
    ]
    assert stat["not_summed_fields"] == [
        "avg_view_time_millis",
        "swipe_up_percent",
        "uniques",
    ]


def test_non_additive_rule_covers_averages_rates_and_effective_costs():
    for name in ("uniques", "frequency", "avg_screen_time_millis", "ecpm",
                 "ecpsu", "view_completion_rate", "swipe_up_percent"):
        assert reports._is_non_additive(name), name
    for name in ("spend", "impressions", "swipes", "conversion_purchases",
                 "conversion_purchases_value", "video_views"):
        assert not reports._is_non_additive(name), name


def test_rollup_passes_error_envelopes_through():
    err = {"error": {"message": "nope", "error_code": "E1008"}}
    assert reports._account_rollup(err) is err


def test_daily_report_uses_campaign_breakdown_in_account_timezone():
    client = FakeClient({"adaccounts/acct/stats": BREAKDOWN, "adaccounts/acct": ACCOUNT})
    out = reports.daily_report(client, "acct", days=2)
    params = client.stats_params()
    assert params["breakdown"] == "campaign"
    assert params["granularity"] == "DAY"
    assert params["start_time"].endswith(("-04:00", "-05:00"))
    assert "T00:00:00.000" in params["start_time"]
    # `days` closed days plus today, so the window ends at tomorrow's midnight.
    today = datetime.now(ZoneInfo("America/New_York")).date()
    assert params["end_time"].startswith((today + timedelta(days=1)).isoformat())
    assert out["timeseries_stats"][0]["timeseries_stat"]["rolled_up_from"] == "campaign"


def test_daily_report_marks_todays_bucket_partial():
    today = datetime.now(ZoneInfo("America/New_York")).date()
    body = {
        "timeseries_stats": [
            {
                "timeseries_stat": {
                    "timeseries": [
                        {"start_time": f"{today.isoformat()}T00:00:00.000-04:00",
                         "stats": {"spend": 1}},
                    ]
                }
            }
        ]
    }
    client = FakeClient({"adaccounts/acct/stats": body, "adaccounts/acct": ACCOUNT})
    out = reports.daily_report(client, "acct", days=1, fields=["spend"])
    [point] = out["timeseries_stats"][0]["timeseries_stat"]["timeseries"]
    assert point["partial"] is True


def test_spend_only_keeps_the_direct_account_call():
    client = FakeClient(
        {"adaccounts/acct/stats": {"timeseries_stats": []}, "adaccounts/acct": ACCOUNT}
    )
    reports.daily_report(client, "acct", days=1, fields=["spend"])
    assert "breakdown" not in client.stats_params()


def test_hourly_report_rolls_up_non_spend_fields_on_account_hours():
    client = FakeClient({"adaccounts/acct/stats": BREAKDOWN, "adaccounts/acct": ACCOUNT})
    reports.hourly_report(client, "acct", hours=24)
    params = client.stats_params()
    assert params["granularity"] == "HOUR"
    assert params["breakdown"] == "campaign"
    assert ":00:00.000" in params["end_time"]


def test_video_report_ends_at_todays_midnight():
    client = FakeClient({"adaccounts/acct/stats": BREAKDOWN, "adaccounts/acct": ACCOUNT})
    reports.video_report(client, "acct", days=7)
    params = client.stats_params()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    assert params["end_time"].startswith(f"{today.isoformat()}T00:00:00.000")
    assert params["breakdown"] == "campaign"


def test_unreadable_account_timezone_falls_back_to_utc():
    client = FakeClient({"adaccounts/acct/stats": {"timeseries_stats": []},
                         "adaccounts/acct": {"adaccounts": []}})
    reports.daily_report(client, "acct", days=1, fields=["spend"])
    assert client.stats_params()["start_time"].endswith("+00:00")
