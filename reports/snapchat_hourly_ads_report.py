#!/usr/bin/env python3
"""Collect Snapchat Ads hourly health data as JSON for downstream formatting."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warehouse.delivery_status import entity_is_effectively_active  # noqa: E402

SNAP_SCRIPTS_DIR = REPO_ROOT / "skills" / "snapchat-ads" / "scripts"
if str(SNAP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SNAP_SCRIPTS_DIR))
from secret_scrubber import scrub_sensitive_value, scrub_then_truncate  # noqa: E402

CLI_DIR = REPO_ROOT / "cli"
SNAPCHAT_ADS_ACCOUNT = os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default")
CMD_BASE = ["uv", "run", "--directory", str(CLI_DIR), "snapchat-ads", "--account", SNAPCHAT_ADS_ACCOUNT]
PT = ZoneInfo("America/Los_Angeles")
CONFIGURED_ACCOUNT_TZ_NAME = os.environ.get("SNAPCHAT_ACCOUNT_TIMEZONE", "").strip()


def redact_error_text(value: object) -> str:
    return scrub_then_truncate(value or "", 1000)


def _api_error_summary(payload: dict) -> dict:
    error = payload.get("error")
    if not isinstance(error, dict):
        error = {"message": error}
    return {
        "message": redact_error_text(error.get("message") or "Snap API returned an error"),
        "status_code": error.get("status_code"),
        "error_code": error.get("error_code"),
        "request_id": error.get("request_id"),
    }


def run(args: list[str], timeout: int = 180) -> dict:
    try:
        proc = subprocess.run(CMD_BASE + args, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {
            "_error": True,
            "returncode": 124,
            "args": args,
            "timeout_seconds": timeout,
            "raw_tail": redact_error_text(f"{exc.stdout or ''}\n{exc.stderr or ''}"),
        }
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        return {
            "_error": True,
            "returncode": proc.returncode,
            "args": args,
            "raw_tail": redact_error_text(raw),
        }
    text = proc.stdout.strip()
    idx = text.find("{")
    if idx < 0:
        return {
            "_error": True,
            "returncode": proc.returncode,
            "args": args,
            "raw_tail": redact_error_text(raw),
        }
    try:
        payload = json.loads(text[idx:])
    except Exception as exc:
        return {
            "_error": True,
            "returncode": proc.returncode,
            "args": args,
            "parse_error": str(exc),
            "raw_tail": redact_error_text(raw),
        }
    if not isinstance(payload, dict):
        return {
            "_error": True,
            "returncode": proc.returncode,
            "args": args,
            "parse_error": "Snap CLI JSON root is not an object",
        }
    if "error" in payload or payload.get("ok") is False:
        return {
            "_error": True,
            "returncode": proc.returncode or 1,
            "args": args,
            "api_error": _api_error_summary(payload),
        }
    cleaned = scrub_sensitive_value(payload)
    if not isinstance(cleaned, dict):
        return {
            "_error": True,
            "returncode": proc.returncode,
            "args": args,
            "parse_error": "canonical scrubber returned a non-object payload",
        }
    return cleaned


def micro_value(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def dollars(micro) -> float:
    try:
        return round(micro_value(micro) / 1_000_000, 2)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def parse_dt(s: str) -> datetime:
    # Snap returns offsets like .000-04:00; datetime handles them.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def resolve_account_timezone(health: dict) -> tuple[str, ZoneInfo]:
    live_name = str(health.get("timezone") or "").strip()
    if not live_name:
        raise ValueError("account health response is missing the Snap account timezone")
    try:
        live_tz = ZoneInfo(live_name)
    except Exception as exc:
        raise ValueError(f"Snap account returned an unknown timezone: {live_name}") from exc
    if CONFIGURED_ACCOUNT_TZ_NAME:
        try:
            ZoneInfo(CONFIGURED_ACCOUNT_TZ_NAME)
        except Exception as exc:
            raise ValueError(
                f"configured SNAPCHAT_ACCOUNT_TIMEZONE is unknown: {CONFIGURED_ACCOUNT_TZ_NAME}"
            ) from exc
        if CONFIGURED_ACCOUNT_TZ_NAME != live_name:
            raise ValueError(
                "configured SNAPCHAT_ACCOUNT_TIMEZONE does not match live account health: "
                f"{CONFIGURED_ACCOUNT_TZ_NAME} != {live_name}"
            )
    return live_name, live_tz


def iso_account_hour(
    hours_ago: int = 0,
    *,
    now: datetime | None = None,
    account_tz: ZoneInfo = PT,
) -> str:
    """Return an account-local boundary exactly N elapsed hours ago.

    Subtract on the UTC timeline so a rolling 24-hour report remains exactly
    24 elapsed hours across daylight-saving transitions. The returned offset
    still belongs to the Snap account timezone, which is what its API expects.
    """
    local_now = (now or datetime.now(account_tz)).astimezone(account_tz)
    current_hour_utc = local_now.astimezone(timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    return (current_hour_utc - timedelta(hours=hours_ago)).astimezone(account_tz).isoformat()


def iso_account_today_start(
    *,
    now: datetime | None = None,
    account_tz: ZoneInfo = PT,
) -> str:
    local_now = (now or datetime.now(account_tz)).astimezone(account_tz)
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0, fold=0).isoformat()


def breakdown_rows(report: dict, *, label: str = "stats report") -> list[dict]:
    try:
        total_stats = report["total_stats"]
        total_stat = total_stats[0]["total_stat"]
        rows = total_stat["breakdown_stats"]["ad"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"{label} is missing the required ad breakdown envelope") from exc
    if not isinstance(rows, list):
        raise ValueError(f"{label} ad breakdown is not a list")
    return rows


def hourly_rows(report: dict, *, expected_count: int = 24) -> list[dict]:
    try:
        rows = report["timeseries_stats"][0]["timeseries_stat"]["timeseries"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("hourly spend report is missing the required timeseries envelope") from exc
    if not isinstance(rows, list):
        raise ValueError("hourly spend report timeseries is not a list")
    if len(rows) != expected_count:
        raise ValueError(
            f"hourly spend report returned {len(rows)} rows; expected {expected_count} completed hours"
        )
    return rows


def validate_hourly_window(rows: list[dict], start_time: str, end_time: str) -> None:
    """Require a continuous sequence of exact elapsed-hour buckets."""
    expected_start = parse_dt(start_time).astimezone(timezone.utc)
    expected_end = parse_dt(end_time).astimezone(timezone.utc)
    cursor = expected_start
    for index, row in enumerate(rows):
        try:
            row_start = parse_dt(str(row["start_time"])).astimezone(timezone.utc)
            row_end = parse_dt(str(row["end_time"])).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"hourly spend row {index} has invalid boundaries") from exc
        if row_start != cursor:
            raise ValueError(
                f"hourly spend row {index} starts at {row_start.isoformat()}, "
                f"expected {cursor.isoformat()}"
            )
        if row_end - row_start != timedelta(hours=1):
            raise ValueError(f"hourly spend row {index} is not exactly one elapsed hour")
        cursor = row_end
    if cursor != expected_end:
        raise ValueError(
            f"hourly spend series ends at {cursor.isoformat()}, expected {expected_end.isoformat()}"
        )


def summarize_rows(rows: list[dict]) -> dict:
    spend = impressions = swipes = purchases = revenue_micro = 0
    for row in rows:
        stats = row.get("stats", {})
        spend += micro_value(stats.get("spend"))
        impressions += int(stats.get("impressions") or 0)
        swipes += int(stats.get("swipes") or 0)
        purchases += int(stats.get("conversion_purchases") or 0)
        revenue_micro += micro_value(stats.get("conversion_purchases_value"))
    spend_usd = dollars(spend)
    revenue_usd = dollars(revenue_micro)
    return {
        "spend": spend_usd,
        "revenue": revenue_usd,
        "roas": round(revenue_micro / spend, 2) if spend else None,
        "impressions": impressions,
        "swipes": swipes,
        "purchases": purchases,
        "cpa": round((spend / 1_000_000) / purchases, 2) if purchases else None,
        "ctr_pct": round(swipes / impressions * 100, 2) if impressions else None,
        "spend_micro": spend,
        "revenue_micro": revenue_micro,
    }


def collect_report(*, as_of_utc: datetime | None = None) -> dict:
    captured_at = as_of_utc or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    captured_at = captured_at.astimezone(timezone.utc)

    health = run(["account", "health-check"])
    if health.get("_error"):
        return {"ok": False, "errors": [health]}
    try:
        account_tz_name, account_tz = resolve_account_timezone(health)
    except ValueError as exc:
        return {"ok": False, "errors": [{"validation_error": str(exc)}]}

    last24_start = iso_account_hour(24, now=captured_at, account_tz=account_tz)
    current_hour = iso_account_hour(0, now=captured_at, account_tz=account_tz)
    today_start = iso_account_today_start(now=captured_at, account_tz=account_tz)
    hourly = run([
        "report", "stats", "--entity", "ad_account", "--granularity", "HOUR",
        "--start-time", last24_start, "--end-time", current_hour,
        "--fields", "spend",
    ])
    last24_by_ad = run([
        "report", "stats", "--entity", "ad_account", "--granularity", "TOTAL",
        "--start-time", last24_start, "--end-time", current_hour,
        "--fields", "spend,impressions,swipes,conversion_purchases,conversion_purchases_value", "--breakdown", "ad", "--omit-empty",
    ])
    # At exactly midnight in the Snap account timezone, the rounded current-hour
    # boundary equals today's start boundary. Snap rejects zero-length stats
    # windows with E1008 ("End time should be after start time"), so represent
    # "today so far" as an empty completed-hour report until the 01:00 account
    # boundary instead of failing the heartbeat.
    if parse_dt(current_hour).astimezone(timezone.utc) <= parse_dt(today_start).astimezone(timezone.utc):
        today_by_ad = {"total_stats": [{"total_stat": {"breakdown_stats": {"ad": []}}}]}
    else:
        today_by_ad = run([
            "report", "stats", "--entity", "ad_account", "--granularity", "TOTAL",
            "--start-time", today_start, "--end-time", current_hour,
            "--fields", "spend,impressions,swipes,conversion_purchases,conversion_purchases_value", "--breakdown", "ad", "--omit-empty",
        ])
    ads = run(["ad", "list", "--limit", "200"])

    errors = [x for x in [hourly, last24_by_ad, today_by_ad, ads] if x.get("_error")]
    if errors:
        return {"ok": False, "errors": errors[:4]}

    try:
        ts = hourly_rows(hourly)
        validate_hourly_window(ts, last24_start, current_hour)
        last24_rows = breakdown_rows(last24_by_ad, label="rolling 24h report")
        today_rows = breakdown_rows(today_by_ad, label="today report")
        if not isinstance(ads.get("ads"), list):
            raise ValueError("ad list response is missing the required ads list")
    except ValueError as exc:
        return {"ok": False, "errors": [{"validation_error": str(exc)}]}

    series = []
    for row in ts:
        start = parse_dt(row["start_time"])
        end = parse_dt(row["end_time"])
        spend_micro = micro_value(row.get("stats", {}).get("spend"))
        series.append({
            "start_account_iso": start.astimezone(account_tz).isoformat(),
            "end_account_iso": end.astimezone(account_tz).isoformat(),
            "start_pt_iso": start.astimezone(PT).isoformat(),
            "end_pt_iso": end.astimezone(PT).isoformat(),
            "spend_micro": spend_micro,
            "spend": dollars(spend_micro),
        })

    latest = series[-1] if series else None
    previous = series[-2] if len(series) > 1 else None
    prior_hours = series[:-1]
    avg_prior_micro = (
        sum(x["spend_micro"] for x in prior_hours) / len(prior_hours)
        if prior_hours else 0.0
    )
    avg_prior = dollars(avg_prior_micro)
    delta_prev_pct = None
    if latest and previous and previous["spend_micro"]:
        delta_prev_pct = round(
            (latest["spend_micro"] - previous["spend_micro"])
            / previous["spend_micro"] * 100,
            1,
        )
    delta_avg_pct = None
    if latest and avg_prior:
        delta_avg_pct = round(
            (latest["spend_micro"] - avg_prior_micro) / avg_prior_micro * 100,
            1,
        )

    ad_name_by_id = {}
    active_ads = 0
    invalid_active = []
    for item in ads.get("ads", []):
        ad = item.get("ad", item) if isinstance(item, dict) else {}
        aid = ad.get("id")
        if aid:
            ad_name_by_id[aid] = ad.get("name") or aid
        if str(ad.get("status") or "").upper() == "ACTIVE":
            raw_delivery = ad.get("delivery_status") or []
            delivery = [raw_delivery] if isinstance(raw_delivery, str) else list(raw_delivery)
            if entity_is_effectively_active(
                configured_status=ad.get("status"),
                delivery_status=delivery,
                review_status=ad.get("review_status"),
                native_effective_status=ad.get("effective_status"),
            ):
                active_ads += 1
            else:
                invalid_active.append({"id": aid, "name": ad.get("name") or aid, "delivery_status": delivery})

    top_rows = []
    last24_summary = summarize_rows(last24_rows)
    today_summary = summarize_rows(today_rows)
    hourly_spend_micro = sum(row["spend_micro"] for row in series)
    spend_difference_micro = hourly_spend_micro - last24_summary["spend_micro"]
    if abs(spend_difference_micro) > 10_000:
        return {
            "ok": False,
            "errors": [{
                "validation_error": (
                    "rolling 24h spend does not reconcile between hourly and ad-breakdown reports"
                ),
                "hourly_spend_micro": hourly_spend_micro,
                "ad_breakdown_spend_micro": last24_summary["spend_micro"],
                "difference_micro": spend_difference_micro,
            }],
        }
    for row in last24_rows:
        stats = row.get("stats", {})
        spend_micro = micro_value(stats.get("spend"))
        if spend_micro <= 0:
            continue
        revenue_micro = micro_value(stats.get("conversion_purchases_value"))
        purchases = int(stats.get("conversion_purchases") or 0)
        swipes = int(stats.get("swipes") or 0)
        impressions = int(stats.get("impressions") or 0)
        top_rows.append({
            "id": row.get("id"),
            "name": ad_name_by_id.get(row.get("id"), row.get("id")),
            "spend": dollars(spend_micro),
            "revenue": dollars(revenue_micro),
            "roas": round(revenue_micro / spend_micro, 2),
            "impressions": impressions,
            "swipes": swipes,
            "purchases": purchases,
            "cpa": round((spend_micro / 1_000_000) / purchases, 2) if purchases else None,
            "ctr_pct": round(swipes / impressions * 100, 2) if impressions else None,
        })
    top_rows.sort(key=lambda x: x["spend"], reverse=True)

    return {
        "ok": True,
        "generated_at_utc": captured_at.isoformat(),
        "generated_at_pt": captured_at.astimezone(PT).strftime("%Y-%m-%d %H:%M %Z"),
        "account": health,
        "report_windows": {
            "account_timezone": account_tz_name,
            "last24_start_account": last24_start,
            "current_hour_account": current_hour,
            "today_start_account": today_start,
            "last24_start_pt": parse_dt(last24_start).astimezone(PT).isoformat(),
            "current_hour_pt": parse_dt(current_hour).astimezone(PT).isoformat(),
            "elapsed_hours": 24,
            "completed_hours": True,
        },
        "latest_hour": latest,
        "previous_hour": previous,
        "last_24": last24_summary,
        "today_account": today_summary,
        "last_24_spend": last24_summary["spend"],
        "today_spend_account": today_summary["spend"],
        "avg_prior_hour_spend": avg_prior,
        "delta_vs_previous_hour_pct": delta_prev_pct,
        "delta_vs_prior_23h_avg_pct": delta_avg_pct,
        "active_ads": active_ads,
        "invalid_active_ads_total": len(invalid_active),
        "invalid_active_ads": invalid_active[:10],
        "top_ads_24h": top_rows[:10],
        "hourly_series": series[-12:],
        "reconciliation": {
            "hourly_spend_micro": hourly_spend_micro,
            "ad_breakdown_spend_micro": last24_summary["spend_micro"],
            "difference_micro": spend_difference_micro,
            "within_one_cent": abs(spend_difference_micro) <= 10_000,
        },
        "notes": [
            "The rolling window contains exactly 24 completed elapsed-hour buckets; the partial current hour is excluded.",
            f"Snap account timezone is {account_tz_name}; operational display timestamps also include Pacific Time.",
            "Snap ad-account hourly reporting supports spend-only at account level; the exact matching ad breakdown supplies impressions, swipes, and purchases.",
            "ROAS uses Snap conversion_purchases_value divided by spend for the same report window.",
        ],
    }


def main() -> int:
    out = collect_report()
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
