#!/usr/bin/env python3
"""Collect Snapchat Ads hourly health data as JSON for downstream formatting."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
SNAPCHAT_ADS_ACCOUNT = os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default")
CMD_BASE = ["uv", "run", "--directory", str(CLI_DIR), "snapchat-ads", "--account", SNAPCHAT_ADS_ACCOUNT]
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")


def run(args: list[str], timeout: int = 180) -> dict:
    proc = subprocess.run(CMD_BASE + args, text=True, capture_output=True, timeout=timeout)
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        return {"_error": True, "returncode": proc.returncode, "args": args, "raw_tail": raw[-4000:]}
    text = proc.stdout.strip()
    idx = text.find("{")
    if idx < 0:
        return {"_error": True, "returncode": proc.returncode, "args": args, "raw_tail": raw[-4000:]}
    try:
        return json.loads(text[idx:])
    except Exception as exc:
        return {"_error": True, "returncode": proc.returncode, "args": args, "parse_error": str(exc), "raw_tail": raw[-4000:]}


def dollars(micro) -> float:
    try:
        return round(float(micro or 0) / 1_000_000, 2)
    except Exception:
        return 0.0


def parse_dt(s: str) -> datetime:
    # Snap returns offsets like .000-04:00; datetime handles them.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_hour_et(hours_ago: int = 0) -> str:
    from datetime import timedelta

    now = datetime.now(ET).replace(minute=0, second=0, microsecond=0)
    dt = now - timedelta(hours=hours_ago)
    return dt.isoformat()


def iso_today_start_et() -> str:
    now = datetime.now(ET)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def breakdown_rows(report: dict) -> list[dict]:
    try:
        return report["total_stats"][0]["total_stat"]["breakdown_stats"]["ad"]
    except Exception:
        return []


def summarize_rows(rows: list[dict]) -> dict:
    spend = impressions = swipes = purchases = revenue_micro = 0
    for row in rows:
        stats = row.get("stats", {})
        spend += float(stats.get("spend") or 0)
        impressions += int(stats.get("impressions") or 0)
        swipes += int(stats.get("swipes") or 0)
        purchases += int(stats.get("conversion_purchases") or 0)
        revenue_micro += float(stats.get("conversion_purchases_value") or 0)
    spend_usd = dollars(spend)
    revenue_usd = dollars(revenue_micro)
    return {
        "spend": spend_usd,
        "revenue": revenue_usd,
        "roas": round(revenue_usd / spend_usd, 2) if spend_usd else None,
        "impressions": impressions,
        "swipes": swipes,
        "purchases": purchases,
        "cpa": round(spend_usd / purchases, 2) if purchases else None,
        "ctr_pct": round(swipes / impressions * 100, 2) if impressions else None,
    }


def main() -> int:
    health = run(["account", "health-check"])
    hourly = run(["report", "hourly", "--hours", "24", "--fields", "spend"])
    last24_start = iso_hour_et(24)
    current_hour = iso_hour_et(0)
    today_start = iso_today_start_et()
    last24_by_ad = run([
        "report", "stats", "--entity", "ad_account", "--granularity", "TOTAL",
        "--start-time", last24_start, "--end-time", current_hour,
        "--fields", "spend,impressions,swipes,conversion_purchases,conversion_purchases_value", "--breakdown", "ad", "--omit-empty",
    ])
    # At exactly midnight in the Snap account timezone, the rounded current-hour
    # boundary equals today's start boundary. Snap rejects zero-length stats
    # windows with E1008 ("End time should be after start time"), so represent
    # "today so far" as an empty completed-hour report until the 01:00 ET
    # boundary instead of failing the heartbeat.
    if current_hour <= today_start:
        today_by_ad = {"total_stats": [{"total_stat": {"breakdown_stats": {"ad": []}}}]}
    else:
        today_by_ad = run([
            "report", "stats", "--entity", "ad_account", "--granularity", "TOTAL",
            "--start-time", today_start, "--end-time", current_hour,
            "--fields", "spend,impressions,swipes,conversion_purchases,conversion_purchases_value", "--breakdown", "ad", "--omit-empty",
        ])
    ads = run(["ad", "list", "--limit", "200"])

    errors = [x for x in [health, hourly, last24_by_ad, today_by_ad, ads] if x.get("_error")]
    if errors:
        print(json.dumps({"ok": False, "errors": errors[:4]}, indent=2))
        return 0

    series = []
    try:
        ts = hourly["timeseries_stats"][0]["timeseries_stat"]["timeseries"]
    except Exception:
        ts = []
    for row in ts:
        start = parse_dt(row["start_time"])
        end = parse_dt(row["end_time"])
        spend = dollars(row.get("stats", {}).get("spend"))
        series.append({
            "start_et": start.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
            "end_et": end.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
            "start_pt": start.astimezone(PT).strftime("%Y-%m-%d %H:%M"),
            "end_pt": end.astimezone(PT).strftime("%Y-%m-%d %H:%M"),
            "spend": spend,
        })

    latest = series[-1] if series else None
    previous = series[-2] if len(series) > 1 else None
    last_24_spend = round(sum(x["spend"] for x in series), 2)
    today_et = datetime.now(ET).date().isoformat()
    today_spend = round(sum(x["spend"] for x in series if x["start_et"].startswith(today_et)), 2)
    prior_hours = series[:-1]
    avg_prior = round(sum(x["spend"] for x in prior_hours) / len(prior_hours), 2) if prior_hours else 0.0
    delta_prev_pct = None
    if latest and previous and previous["spend"]:
        delta_prev_pct = round((latest["spend"] - previous["spend"]) / previous["spend"] * 100, 1)
    delta_avg_pct = None
    if latest and avg_prior:
        delta_avg_pct = round((latest["spend"] - avg_prior) / avg_prior * 100, 1)

    ad_name_by_id = {}
    active_ads = 0
    invalid_active = []
    for item in ads.get("ads", []):
        ad = item.get("ad", item) if isinstance(item, dict) else {}
        aid = ad.get("id")
        if aid:
            ad_name_by_id[aid] = ad.get("name") or aid
        if ad.get("status") == "ACTIVE":
            active_ads += 1
            delivery = ad.get("delivery_status") or []
            if any(str(x).startswith("INVALID") for x in delivery):
                invalid_active.append({"id": aid, "name": ad.get("name") or aid, "delivery_status": delivery})

    top_rows = []
    last24_rows = breakdown_rows(last24_by_ad)
    today_rows = breakdown_rows(today_by_ad)
    last24_summary = summarize_rows(last24_rows)
    today_summary = summarize_rows(today_rows)
    for row in last24_rows:
        stats = row.get("stats", {})
        spend = dollars(stats.get("spend"))
        if spend <= 0:
            continue
        purchases = int(stats.get("conversion_purchases") or 0)
        swipes = int(stats.get("swipes") or 0)
        impressions = int(stats.get("impressions") or 0)
        top_rows.append({
            "id": row.get("id"),
            "name": ad_name_by_id.get(row.get("id"), row.get("id")),
            "spend": spend,
            "revenue": dollars(stats.get("conversion_purchases_value")),
            "roas": round(dollars(stats.get("conversion_purchases_value")) / spend, 2) if spend else None,
            "impressions": impressions,
            "swipes": swipes,
            "purchases": purchases,
            "cpa": round(spend / purchases, 2) if purchases else None,
            "ctr_pct": round(swipes / impressions * 100, 2) if impressions else None,
        })
    top_rows.sort(key=lambda x: x["spend"], reverse=True)

    out = {
        "ok": True,
        "generated_at_pt": datetime.now(PT).strftime("%Y-%m-%d %H:%M %Z"),
        "account": health,
        "report_windows": {
            "last24_start_et": last24_start,
            "current_hour_et": current_hour,
            "today_start_et": today_start,
        },
        "latest_hour": latest,
        "previous_hour": previous,
        "last_24": last24_summary,
        "today_et": today_summary,
        "last_24_spend": last24_summary["spend"],
        "today_spend_et": today_summary["spend"],
        "avg_prior_hour_spend": avg_prior,
        "delta_vs_previous_hour_pct": delta_prev_pct,
        "delta_vs_prior_23h_avg_pct": delta_avg_pct,
        "active_ads": active_ads,
        "invalid_active_ads": invalid_active[:10],
        "top_ads_24h": top_rows[:10],
        "hourly_series": series[-12:],
        "notes": [
            "Snap ad-account hourly endpoint supports spend-only at account level; ad-level breakdown supplies impressions, swipes, and purchases for the current 24h window.",
            "Snap account timezone is America/New_York; report displays latest hour in both PT and ET.",
            "ROAS uses Snap conversion_purchases_value divided by spend for the same report window.",
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
