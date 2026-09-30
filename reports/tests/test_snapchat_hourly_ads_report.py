from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from reports import snapchat_hourly_ads_report as report


ACCOUNT_TZ = ZoneInfo("America/Los_Angeles")


def _elapsed_hours(start: str, end: str) -> float:
    start_dt = datetime.fromisoformat(start).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(end).astimezone(timezone.utc)
    return (end_dt - start_dt).total_seconds() / 3600


def _hour_rows(start: datetime, count: int = 24, spend_micro: int = 1_000_000) -> list[dict]:
    rows = []
    for index in range(count):
        row_start = start + timedelta(hours=index)
        rows.append({
            "start_time": row_start.isoformat(),
            "end_time": (row_start + timedelta(hours=1)).isoformat(),
            "stats": {"spend": spend_micro},
        })
    return rows


def _breakdown(spend_micro: int = 24_000_000) -> dict:
    return {
        "total_stats": [{
            "total_stat": {
                "breakdown_stats": {
                    "ad": [{
                        "id": "ad-1",
                        "stats": {
                            "spend": spend_micro,
                            "impressions": 100,
                            "swipes": 10,
                            "conversion_purchases": 2,
                            "conversion_purchases_value": 30_000_000,
                        },
                    }],
                },
            },
        }],
    }


def test_rolling_window_is_exactly_24_elapsed_hours_on_an_ordinary_day():
    now = datetime(2026, 7, 14, 19, 37, tzinfo=ACCOUNT_TZ)
    start = report.iso_account_hour(24, now=now, account_tz=ACCOUNT_TZ)
    end = report.iso_account_hour(0, now=now, account_tz=ACCOUNT_TZ)

    assert _elapsed_hours(start, end) == 24.0
    assert datetime.fromisoformat(start).hour == 19
    assert datetime.fromisoformat(end).hour == 19


def test_rolling_window_stays_24_elapsed_hours_across_spring_forward():
    now = datetime(2026, 3, 8, 3, 30, tzinfo=ACCOUNT_TZ)
    start = report.iso_account_hour(24, now=now, account_tz=ACCOUNT_TZ)
    end = report.iso_account_hour(0, now=now, account_tz=ACCOUNT_TZ)

    assert _elapsed_hours(start, end) == 24.0
    assert datetime.fromisoformat(start).hour == 2
    assert datetime.fromisoformat(end).hour == 3
    assert datetime.fromisoformat(start).utcoffset() != datetime.fromisoformat(end).utcoffset()


def test_rolling_window_stays_24_elapsed_hours_across_fall_back():
    now = datetime(2026, 11, 1, 2, 30, tzinfo=ACCOUNT_TZ)
    start = report.iso_account_hour(24, now=now, account_tz=ACCOUNT_TZ)
    end = report.iso_account_hour(0, now=now, account_tz=ACCOUNT_TZ)

    assert _elapsed_hours(start, end) == 24.0
    assert datetime.fromisoformat(start).hour == 3
    assert datetime.fromisoformat(end).hour == 2
    assert datetime.fromisoformat(start).utcoffset() != datetime.fromisoformat(end).utcoffset()


def test_today_boundary_is_account_local_midnight():
    now = datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc)
    boundary = datetime.fromisoformat(
        report.iso_account_today_start(now=now, account_tz=ACCOUNT_TZ)
    )

    assert boundary.tzinfo is not None
    assert (boundary.hour, boundary.minute, boundary.second) == (0, 0, 0)
    assert boundary.date().isoformat() == "2026-07-14"


def test_resolve_account_timezone_uses_live_health_without_override(monkeypatch):
    monkeypatch.setattr(report, "CONFIGURED_ACCOUNT_TZ_NAME", "")

    name, zone = report.resolve_account_timezone({"timezone": "America/Los_Angeles"})

    assert name == "America/Los_Angeles"
    assert zone.key == "America/Los_Angeles"


def test_resolve_account_timezone_rejects_missing_or_mismatched_health(monkeypatch):
    monkeypatch.setattr(report, "CONFIGURED_ACCOUNT_TZ_NAME", "America/New_York")

    with pytest.raises(ValueError, match="missing"):
        report.resolve_account_timezone({})
    with pytest.raises(ValueError, match="does not match"):
        report.resolve_account_timezone({"timezone": "America/Los_Angeles"})


def test_run_rejects_api_error_json_even_when_process_exits_zero(monkeypatch):
    payload = {
        "error": {
            "message": "request failed",
            "status_code": 400,
            "error_code": "bad_request",
            "request_id": "request-1",
        }
    }
    monkeypatch.setattr(
        report.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    result = report.run(["report", "stats"])

    assert result["_error"] is True
    assert result["returncode"] == 1
    assert result["api_error"]["error_code"] == "bad_request"


def test_run_redacts_no_json_error_output(monkeypatch):
    monkeypatch.setattr(
        report.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="not json access_token=secret-token",
            stderr="password=secret-password",
        ),
    )

    result = report.run(["report", "stats"])

    assert result["_error"] is True
    assert "secret-token" not in result["raw_tail"]
    assert "secret-password" not in result["raw_tail"]
    assert result["raw_tail"].count("[REDACTED]") == 2


def test_run_fails_closed_on_timeout_and_redacts_output(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            cmd="snapchat-ads",
            timeout=1,
            output="access_token=secret-token",
            stderr="password=secret-password",
        )

    monkeypatch.setattr(report.subprocess, "run", timeout)

    result = report.run(["report", "stats"], timeout=1)

    assert result["_error"] is True
    assert result["returncode"] == 124
    assert "secret-token" not in result["raw_tail"]
    assert "secret-password" not in result["raw_tail"]


def test_breakdown_rows_rejects_missing_or_malformed_envelope():
    for payload in ({}, {"total_stats": []}, {"total_stats": [{"total_stat": {}}]}):
        with pytest.raises(ValueError, match="required ad breakdown envelope"):
            report.breakdown_rows(payload, label="rolling 24h report")


def test_hourly_rows_requires_exactly_24_completed_hours():
    malformed = {"timeseries_stats": [{"timeseries_stat": {"timeseries": []}}]}

    with pytest.raises(ValueError, match="returned 0 rows; expected 24 completed hours"):
        report.hourly_rows(malformed)


def test_validate_hourly_window_rejects_a_gap():
    start = datetime(2026, 7, 13, 23, tzinfo=timezone.utc)
    rows = _hour_rows(start)
    rows[8]["start_time"] = (start + timedelta(hours=9)).isoformat()

    with pytest.raises(ValueError, match="starts at"):
        report.validate_hourly_window(
            rows,
            start.isoformat(),
            (start + timedelta(hours=24)).isoformat(),
        )


def test_error_redaction_removes_tokens_and_passwords():
    text = report.redact_error_text(
        "Authorization: Bearer top-secret access_token=abc123 password=plain"
    )

    assert "top-secret" not in text
    assert "abc123" not in text
    assert "plain" not in text
    assert text.count("[REDACTED]") == 3


def test_collect_report_uses_one_fixed_window_and_counts_full_invalid_total(monkeypatch):
    as_of = datetime(2026, 7, 14, 23, 7, tzinfo=timezone.utc)
    start = datetime(2026, 7, 13, 23, tzinfo=timezone.utc)
    hourly = {
        "timeseries_stats": [{"timeseries_stat": {"timeseries": _hour_rows(start)}}],
    }
    ads = {
        "ads": [{
            "ad": {
                "id": f"ad-{index}",
                "name": f"Ad {index}",
                "status": "ACTIVE",
                "delivery_status": ["INVALID_NOT_DELIVERING"],
            },
        } for index in range(12)] + [
            {"ad": {
                "id": "ad-valid",
                "name": "Valid active",
                "status": "ACTIVE",
                "delivery_status": ["VALID"],
                "review_status": "APPROVED",
            }},
            {"ad": {
                "id": "ad-mixed",
                "name": "Mixed blocker",
                "status": "ACTIVE",
                "delivery_status": [
                    "VALID",
                    "NOT_DELIVERING_AD_CONTAINS_INVALID_AUDIENCE",
                ],
                "review_status": "APPROVED",
            }},
            {"ad": {
                "id": "ad-pending",
                "name": "Pending",
                "status": "ACTIVE",
                "delivery_status": ["VALID", "PENDING"],
                "review_status": "APPROVED",
            }},
        ],
    }
    calls = []

    def fake_run(args, timeout=180):
        calls.append(list(args))
        if args == ["account", "health-check"]:
            return {"healthy": True, "timezone": "America/Los_Angeles"}
        if args[:6] == ["report", "stats", "--entity", "ad_account", "--granularity", "HOUR"]:
            return hourly
        if args[:6] == ["report", "stats", "--entity", "ad_account", "--granularity", "TOTAL"]:
            return _breakdown()
        if args[:2] == ["ad", "list"]:
            return ads
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(report, "CONFIGURED_ACCOUNT_TZ_NAME", "")
    monkeypatch.setattr(report, "run", fake_run)

    result = report.collect_report(as_of_utc=as_of)

    assert result["ok"] is True
    assert result["report_windows"]["account_timezone"] == "America/Los_Angeles"
    assert result["report_windows"]["elapsed_hours"] == 24
    assert result["report_windows"]["completed_hours"] is True
    assert result["active_ads"] == 1
    assert result["invalid_active_ads_total"] == 14
    assert len(result["invalid_active_ads"]) == 10
    assert result["reconciliation"]["difference_micro"] == 0
    assert result["today_account"]["spend"] == 24.0
    assert any("HOUR" in command for command in calls)
    assert not any(command[:2] == ["report", "hourly"] for command in calls)


def test_collect_report_fails_closed_when_hourly_and_breakdown_spend_disagree(monkeypatch):
    as_of = datetime(2026, 7, 14, 23, 7, tzinfo=timezone.utc)
    start = datetime(2026, 7, 13, 23, tzinfo=timezone.utc)

    def fake_run(args, timeout=180):
        if args == ["account", "health-check"]:
            return {"healthy": True, "timezone": "America/Los_Angeles"}
        if "HOUR" in args:
            return {
                "timeseries_stats": [{
                    "timeseries_stat": {"timeseries": _hour_rows(start)},
                }],
            }
        if "TOTAL" in args:
            return _breakdown(spend_micro=23_000_000)
        return {"ads": []}

    monkeypatch.setattr(report, "CONFIGURED_ACCOUNT_TZ_NAME", "")
    monkeypatch.setattr(report, "run", fake_run)

    result = report.collect_report(as_of_utc=as_of)

    assert result["ok"] is False
    assert "does not reconcile" in result["errors"][0]["validation_error"]


def _collect_with_env(monkeypatch, env):
    as_of = datetime(2026, 7, 14, 23, 7, tzinfo=timezone.utc)
    start = datetime(2026, 7, 13, 23, tzinfo=timezone.utc)
    calls = []

    def fake_run(args, timeout=180):
        calls.append(list(args))
        if args == ["account", "health-check"]:
            return {"healthy": True, "timezone": "America/Los_Angeles"}
        if "HOUR" in args:
            return {"timeseries_stats": [{"timeseries_stat": {"timeseries": _hour_rows(start)}}]}
        if "TOTAL" in args:
            return _breakdown()
        return {"ads": []}

    for key in (
        "SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW",
        "SNAPCHAT_VIEW_ATTRIBUTION_WINDOW",
        "SNAPCHAT_ENGAGED_VIEW_ATTRIBUTION_WINDOW",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(report, "CONFIGURED_ACCOUNT_TZ_NAME", "")
    monkeypatch.setattr(report, "run", fake_run)
    return report.collect_report(as_of_utc=as_of), calls


def test_collect_report_sends_the_attribution_lens_on_conversion_reads(monkeypatch):
    result, calls = _collect_with_env(monkeypatch, {
        "SNAPCHAT_SWIPE_UP_ATTRIBUTION_WINDOW": "7_DAY",
        "SNAPCHAT_VIEW_ATTRIBUTION_WINDOW": "none",
        "SNAPCHAT_ENGAGED_VIEW_ATTRIBUTION_WINDOW": "none",
    })

    assert result["ok"] is True
    breakdown_calls = [c for c in calls if "TOTAL" in c]
    assert len(breakdown_calls) == 2
    for command in breakdown_calls:
        assert command[command.index("--swipe-up-attribution-window") + 1] == "7_DAY"
        assert command[command.index("--view-attribution-window") + 1] == "none"
        assert json.loads(command[command.index("--params-json") + 1]) == {
            "engaged_view_attribution_window": "none",
        }
    hourly_call = next(c for c in calls if "HOUR" in c)
    assert "--swipe-up-attribution-window" not in hourly_call
    assert result["attribution"]["swipe_up_attribution_window"] == "7_DAY"
    assert any("swipe 7_DAY" in note for note in result["notes"])


def test_collect_report_without_a_lens_keeps_snap_defaults(monkeypatch):
    result, calls = _collect_with_env(monkeypatch, {})

    assert result["ok"] is True
    assert not any("--swipe-up-attribution-window" in c for c in calls)
    assert set(result["attribution"].values()) == {None}


def test_collect_report_rejects_a_malformed_lens_before_any_call(monkeypatch):
    result, calls = _collect_with_env(monkeypatch, {"SNAPCHAT_VIEW_ATTRIBUTION_WINDOW": "forever"})

    assert result["ok"] is False
    assert calls == []
    assert "SNAPCHAT_VIEW_ATTRIBUTION_WINDOW" in result["errors"][0]["validation_error"]
