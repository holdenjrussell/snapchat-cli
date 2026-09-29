#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]"]
# ///
"""Sync the last N days of Snapchat ad-level daily stats into the warehouse.

Pulls DAY-granularity, ad-level breakdown stats from the Snap Marketing API
via the `snapchat-ads` CLI (`cli/`, sibling of this directory), joins in
ad -> ad squad -> campaign names, computes the derived metric columns, and
upserts into `snapchat_ad_daily_metrics` keyed on (ad_id, recorded_at).

Usage:
    uv run warehouse/sync_snapchat_daily.py --days 7
    uv run warehouse/sync_snapchat_daily.py --days 30 --apply-schema
    uv run warehouse/sync_snapchat_daily.py --dry-run

Env:
    SNAPCHAT_ADS_ACCOUNT   Account key from ~/.config/snapchat-ads-cli/accounts.toml
                           (falls back to the CLI's own "default" account key).
    DATABASE_URL           Postgres connection string (required unless --dry-run).
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = REPO_ROOT / "cli"
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"

MICRO = 1_000_000
UPSERT_CHUNK_SIZE = 500

STATS_FIELDS = [
    "spend",
    "impressions",
    "swipes",
    "conversion_purchases",
    "conversion_purchases_value",
    "video_views",
    "video_views_time_based",
]


class SyncError(RuntimeError):
    """Raised for CLI/subprocess/parsing failures with a clean message."""


def _snapchat_account() -> str:
    return os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default")


def _run_cli_json(*args: str, timeout: int = 120) -> dict[str, Any]:
    argv = [
        "uv", "run", "--directory", str(CLI_DIR),
        "snapchat-ads", "--account", _snapchat_account(),
        *args,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise SyncError(
            f"Command failed (exit {result.returncode}): {' '.join(argv)}\n"
            f"{(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Command did not return JSON: {' '.join(argv)}\n{result.stdout[:1000]}") from exc
    if isinstance(payload, dict) and "error" in payload:
        raise SyncError(f"API error from {' '.join(argv)}: {payload['error']}")
    return payload


def _account_tz() -> datetime.tzinfo:
    """Timezone of the Snap ad account (SNAPCHAT_ACCOUNT_TIMEZONE, default UTC).

    Snap rejects DAY-granularity stats unless start/end sit on the *ad account's*
    local midnight (error E1008), so the window must be built in that zone,
    not in UTC. Set SNAPCHAT_ACCOUNT_TIMEZONE in the env file to the value of
    `adaccount.timezone` (e.g. America/Toronto).
    """
    name = os.environ.get("SNAPCHAT_ACCOUNT_TIMEZONE", "UTC")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - bad tz name falls back to UTC
        print(f"[sync_snapchat_daily] unknown SNAPCHAT_ACCOUNT_TIMEZONE={name!r}; using UTC", file=sys.stderr)
        return datetime.timezone.utc


def _window_bounds(
    days: int, now: datetime.datetime | None = None, include_today: bool = True
) -> tuple[str, str]:
    """Account-local midnight-aligned [start, end) window: the trailing `days`
    closed days, plus today so far unless `include_today` is False.

    Today is included because the 437 dashboard P&L reads this table: without
    it the current day's column carried no Snapchat spend and overstated Net
    Profit by whatever Snap had spent since midnight. Today's row is partial
    and is overwritten by every run until the day closes."""
    tz = _account_tz()
    today = (now or datetime.datetime.now(tz)).astimezone(tz).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = (today - datetime.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today
    if include_today:
        end = (today + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), end.isoformat()


def _parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _micro_to_usd(value: Any) -> float:
    return round(_num(value) / MICRO, 6)


def _extract_video_quartiles(value: Any) -> tuple[int | None, int | None]:
    """Best-effort extraction of p50/p100 video-view counts from Snap's
    `video_views_time_based` stats field.

    Snap does not document a single canonical key shape for this field
    across API versions, so this checks a handful of plausible key aliases
    and falls back to None (column stays NULL) when none match. If the
    live payload uses a different key, extend `_P50_KEYS` / `_P100_KEYS`.
    """
    if not isinstance(value, dict):
        return None, None
    p50 = None
    p100 = None
    for key in _P50_KEYS:
        if key in value:
            p50 = int(_num(value[key]))
            break
    for key in _P100_KEYS:
        if key in value:
            p100 = int(_num(value[key]))
            break
    return p50, p100


_P50_KEYS = ("50_percent_views", "video_views_p50", "p50", "50")
_P100_KEYS = ("100_percent_views", "video_views_p100", "p100", "100")


def extract_daily_ad_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a Snap `report stats` envelope (DAY granularity, ad
    breakdown) into a flat list of per-day, per-ad raw stat dicts.

    Handles the shapes seen from the stats endpoint:
      - `timeseries_stats` with `breakdown=ad` (what this sync requests):
        the block's `timeseries_stat` carries `breakdown_stats.ad[]`, and each
        ad row holds its OWN `timeseries` list of per-day entries
        (`start_time`/`end_time`/`stats`). The day comes from each entry.
      - `timeseries_stats` with per-bucket breakdowns: `timeseries_stat` has a
        `timeseries` list whose entries each carry `breakdown_stats`.
      - `total_stats`: TOTAL granularity fallback. Each block wraps a
        `total_stat` with `breakdown_stats` directly (no per-day buckets).

    Until 2026-09-29 only the second shape was read, so every live payload
    (the first shape) stamped each ad with the window's start date and read
    spend from the ad row itself, which has none: the table held one
    zero-spend row per ad per run while 437 spent ~USD 2,200/day on Snap.
    """
    rows: list[dict[str, Any]] = []

    def _collect_breakdown(entry: dict[str, Any], date_str: str | None) -> None:
        breakdown = entry.get("breakdown_stats") if isinstance(entry, dict) else None
        ad_rows = breakdown.get("ad") if isinstance(breakdown, dict) else None
        if not isinstance(ad_rows, list):
            return
        for ad_row in ad_rows:
            if not isinstance(ad_row, dict):
                continue
            ad_id = ad_row.get("id")
            per_day = ad_row.get("timeseries")
            if isinstance(per_day, list):
                if not ad_id:
                    continue
                for day_entry in per_day:
                    if not isinstance(day_entry, dict):
                        continue
                    parsed = _parse_iso(day_entry.get("start_time"))
                    day_stats = day_entry.get("stats")
                    if parsed is None or not isinstance(day_stats, dict):
                        continue
                    rows.append({"date": parsed.date().isoformat(), "ad_id": str(ad_id), "stats": day_stats})
                continue
            stats = ad_row.get("stats") if isinstance(ad_row.get("stats"), dict) else ad_row
            ad_id = ad_id or stats.get("id")
            if not ad_id:
                continue
            rows.append({"date": date_str, "ad_id": str(ad_id), "stats": stats})

    timeseries_blocks = payload.get("timeseries_stats")
    if isinstance(timeseries_blocks, list):
        for block in timeseries_blocks:
            if not isinstance(block, dict):
                continue
            inner = block.get("timeseries_stat") or block.get("stat") or block
            timeseries = inner.get("timeseries") if isinstance(inner, dict) else None
            if isinstance(timeseries, list):
                for entry in timeseries:
                    if not isinstance(entry, dict):
                        continue
                    parsed = _parse_iso(entry.get("start_time"))
                    date_str = parsed.date().isoformat() if parsed else None
                    _collect_breakdown(entry, date_str)
            elif isinstance(inner, dict):
                parsed = _parse_iso(inner.get("start_time"))
                date_str = parsed.date().isoformat() if parsed else None
                _collect_breakdown(inner, date_str)

    total_blocks = payload.get("total_stats")
    if isinstance(total_blocks, list):
        for block in total_blocks:
            if not isinstance(block, dict):
                continue
            inner = block.get("total_stat") or block.get("stat") or block
            parsed = _parse_iso(inner.get("start_time")) if isinstance(inner, dict) else None
            date_str = parsed.date().isoformat() if parsed else None
            _collect_breakdown(inner, date_str)

    return rows


def _unwrap_entity_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Entities from a list envelope.

    Snap wraps each one as {"sub_request_status": ..., "<entity>": {...}}
    (e.g. "ad", "adsquad", "campaign"); unwrap it so lookups by "id" work.
    """
    items = payload.get(key) or []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "id" not in item:
            inner = [v for k, v in item.items() if k != "sub_request_status" and isinstance(v, dict)]
            if len(inner) == 1:
                item = inner[0]
        out.append(item)
    return out


def fetch_entity_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (ads_by_id, squads_by_id, campaigns_by_id) lookup maps."""
    ads_payload = _run_cli_json("ad", "list", "--limit", "500")
    ads = _unwrap_entity_list(ads_payload, "ads")
    ads_by_id = {str(ad["id"]): ad for ad in ads if ad.get("id")}

    squads_payload = _run_cli_json("adsquad", "list", "--limit", "500")
    squads = _unwrap_entity_list(squads_payload, "ad_squads") or _unwrap_entity_list(squads_payload, "adsquads")
    squads_by_id = {str(sq["id"]): sq for sq in squads if sq.get("id")}

    campaigns_payload = _run_cli_json("campaign", "list", "--limit", "500")
    campaigns = _unwrap_entity_list(campaigns_payload, "campaigns")
    campaigns_by_id = {str(c["id"]): c for c in campaigns if c.get("id")}

    return ads_by_id, squads_by_id, campaigns_by_id


def build_rows(
    daily_rows: list[dict[str, Any]],
    ads_by_id: dict[str, dict[str, Any]],
    squads_by_id: dict[str, dict[str, Any]],
    campaigns_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in daily_rows:
        date_str = raw.get("date")
        ad_id = raw.get("ad_id")
        stats = raw.get("stats") or {}
        if not date_str or not ad_id:
            continue

        ad = ads_by_id.get(ad_id, {})
        squad_id = str(ad.get("ad_squad_id") or "") or None
        squad = squads_by_id.get(squad_id, {}) if squad_id else {}
        campaign_id = str(squad.get("campaign_id") or "") or None
        campaign = campaigns_by_id.get(campaign_id, {}) if campaign_id else {}

        spend = _micro_to_usd(stats.get("spend"))
        impressions = int(_num(stats.get("impressions")))
        swipes = int(_num(stats.get("swipes")))
        conversions = int(_num(stats.get("conversion_purchases")))
        revenue = _micro_to_usd(stats.get("conversion_purchases_value"))
        video_views = int(_num(stats.get("video_views"))) if stats.get("video_views") is not None else None
        video_views_p50, video_views_p100 = _extract_video_quartiles(stats.get("video_views_time_based"))

        swipe_up_rate = round(swipes / impressions, 4) if impressions > 0 else None
        cpm = round(spend / impressions * 1000, 4) if impressions > 0 else None
        cost_per_swipe = round(spend / swipes, 4) if swipes > 0 else None
        roas = round(revenue / spend, 4) if spend > 0 else None

        out.append(
            {
                "recorded_at": date_str,
                "campaign_id": campaign_id,
                "campaign_name": campaign.get("name"),
                "ad_squad_id": squad_id,
                "ad_squad_name": squad.get("name"),
                "ad_id": ad_id,
                "ad_name": ad.get("name"),
                "spend": spend,
                "impressions": impressions,
                "swipes": swipes,
                "swipe_up_rate": swipe_up_rate,
                "cpm": cpm,
                "cost_per_swipe": cost_per_swipe,
                "video_views": video_views,
                "video_views_p50": video_views_p50,
                "video_views_p100": video_views_p100,
                "conversions": conversions,
                "revenue": revenue,
                "roas": roas,
            }
        )
    return out


ROW_COLUMNS = [
    "recorded_at", "campaign_id", "campaign_name", "ad_squad_id", "ad_squad_name",
    "ad_id", "ad_name", "spend", "impressions", "swipes", "swipe_up_rate", "cpm",
    "cost_per_swipe", "video_views", "video_views_p50", "video_views_p100",
    "conversions", "revenue", "roas",
]
UPDATE_COLUMNS = [c for c in ROW_COLUMNS if c not in ("recorded_at", "ad_id")]


def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    placeholders = "(" + ", ".join(f"%({c})s" for c in ROW_COLUMNS) + ")"
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPDATE_COLUMNS)
    sql = (
        f"INSERT INTO snapchat_ad_daily_metrics ({', '.join(ROW_COLUMNS)}) "
        f"VALUES {placeholders} "
        f"ON CONFLICT (ad_id, recorded_at) DO UPDATE SET {set_clause}"
    )
    upserted = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), UPSERT_CHUNK_SIZE):
            chunk = rows[i : i + UPSERT_CHUNK_SIZE]
            cur.executemany(sql, chunk)
            upserted += len(chunk)
    conn.commit()
    return upserted


def apply_schema(conn: Any) -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync trailing N days of Snapchat ad-level daily stats.")
    parser.add_argument("--days", type=int, default=7, help="How many trailing days to pull (default 7).")
    parser.add_argument("--dry-run", action="store_true", help="Print sample rows + counts; skip all DB writes.")
    parser.add_argument("--apply-schema", action="store_true", help="Execute schema.sql before upserting.")
    parser.add_argument("--closed-days-only", action="store_true",
                        help="End the window at today's midnight (omit today's partial day).")
    parser.add_argument("--database-url", default=None, help="Override the DATABASE_URL environment variable.")
    args = parser.parse_args()

    start_iso, end_iso = _window_bounds(args.days, include_today=not args.closed_days_only)

    print(f"[sync_snapchat_daily] account={_snapchat_account()} window={start_iso}..{end_iso}", file=sys.stderr)

    try:
        stats_payload = _run_cli_json(
            "report", "stats",
            "--entity", "ad_account",
            "--granularity", "DAY",
            "--start-time", start_iso,
            "--end-time", end_iso,
            "--fields", ",".join(STATS_FIELDS),
            "--breakdown", "ad",
            "--swipe-up-attribution-window", "7_DAY",
            "--view-attribution-window", "none",
            "--params-json", '{"engaged_view_attribution_window":"none"}',
            "--include-empty",
        )
        daily_rows = extract_daily_ad_rows(stats_payload)
        ads_by_id, squads_by_id, campaigns_by_id = fetch_entity_maps()
        rows = build_rows(daily_rows, ads_by_id, squads_by_id, campaigns_by_id)
    except SyncError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "days": args.days,
                    "window": {"start": start_iso, "end": end_iso},
                    "row_count": len(rows),
                    "sample_rows": rows[:5],
                },
                indent=2,
                default=str,
            )
        )
        return

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print(json.dumps({"error": "DATABASE_URL environment variable is not set."}), file=sys.stderr)
        sys.exit(2)

    import psycopg

    try:
        with psycopg.connect(database_url) as conn:
            if args.apply_schema:
                apply_schema(conn)
            upserted = upsert_rows(conn, rows)
    except psycopg.Error as exc:
        print(json.dumps({"error": f"Database error: {exc}"}), file=sys.stderr)
        sys.exit(1)

    print(
        json.dumps(
            {
                "rows_upserted": upserted,
                "days": args.days,
                "window": {"start": start_iso, "end": end_iso},
            }
        )
    )


if __name__ == "__main__":
    main()
