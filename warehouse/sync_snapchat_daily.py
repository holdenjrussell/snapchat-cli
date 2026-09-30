#!/usr/bin/env python3
"""Sync Snapchat ad-level daily stats into the warehouse.

Closed mode preserves the original trailing-N-day DAY query. Intraday mode
pulls a current-day TOTAL-by-ad window from account-local midnight through the
latest completed account-local hour. Both paths join ad -> ad squad -> campaign
names, compute derived metrics, and idempotently upsert on (ad_id, recorded_at).

Usage:
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_daily.py --days 7
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_daily.py --mode intraday
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_daily.py --dry-run

Env:
    SNAPCHAT_ADS_ACCOUNT   Account key from ~/.config/snapchat-ads-cli/accounts.toml
                           (falls back to the CLI's own "default" account key).
    DATABASE_URL           Postgres connection string (required unless --dry-run).
    SNAPCHAT_ACCOUNT_TIMEZONE
                           Optional expected IANA timezone. A mismatch with
                           live account health fails closed.
    SNAP_WAREHOUSE_SCHEMA  Validated schema name (default: snapchat_ads).
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from warehouse.schema_config import (
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from warehouse.runtime_paths import (
        RuntimePathError,
        resolve_snapchat_cli,
    )
except ModuleNotFoundError:  # Direct `python warehouse/...` execution.
    from schema_config import (  # type: ignore[no-redef]
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from runtime_paths import (  # type: ignore[no-redef]
        RuntimePathError,
        resolve_snapchat_cli,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
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
    try:
        cli_executable = resolve_snapchat_cli(REPO_ROOT)
    except RuntimePathError as exc:
        raise SyncError(str(exc)) from exc
    argv = [
        str(cli_executable),
        "--account",
        _snapchat_account(),
        *args,
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"Command timed out after {timeout}s: {' '.join(argv)}"
        ) from exc
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


def resolve_account_timezone(
    health: dict[str, Any],
) -> tuple[str, ZoneInfo]:
    """Resolve the live IANA timezone and reject missing or mismatched state."""
    live_name = str(health.get("timezone") or "").strip()
    if not live_name:
        raise SyncError(
            "account health response is missing the Snap account timezone"
        )
    try:
        live_tz = ZoneInfo(live_name)
    except ZoneInfoNotFoundError as exc:
        raise SyncError(
            f"Snap account returned an unknown timezone: {live_name}"
        ) from exc

    configured_name = str(
        os.environ.get("SNAPCHAT_ACCOUNT_TIMEZONE") or ""
    ).strip()
    if configured_name:
        try:
            ZoneInfo(configured_name)
        except ZoneInfoNotFoundError as exc:
            raise SyncError(
                "configured SNAPCHAT_ACCOUNT_TIMEZONE is unknown: "
                f"{configured_name}"
            ) from exc
        if configured_name != live_name:
            raise SyncError(
                "configured SNAPCHAT_ACCOUNT_TIMEZONE does not match live "
                f"account health: {configured_name} != {live_name}"
            )
    return live_name, live_tz


def _window_bounds(
    days: int,
    *,
    account_tz: ZoneInfo,
    now: datetime.datetime | None = None,
) -> tuple[str, str]:
    """Account-timezone midnight-aligned [start, end) window covering the trailing `days` days."""
    end = (
        now or datetime.datetime.now(account_tz)
    ).astimezone(account_tz).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        fold=0,
    )
    start = end - datetime.timedelta(days=days)
    return start.isoformat(), end.isoformat()


# Snap rejects DAY-granularity timeseries over more than 32 days (E1008);
# stay a hair under and let the upsert key absorb any boundary overlap.
MAX_DAYS_PER_QUERY = 30


def _window_chunks(
    days: int,
    *,
    account_tz: ZoneInfo,
    now: datetime.datetime | None = None,
) -> list[tuple[str, str]]:
    """Split the trailing-`days` window into <=MAX_DAYS_PER_QUERY-day [start, end) slices."""
    end = (
        now or datetime.datetime.now(account_tz)
    ).astimezone(account_tz).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        fold=0,
    )
    start = end - datetime.timedelta(days=days)
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor < end:
        upper = min(cursor + datetime.timedelta(days=MAX_DAYS_PER_QUERY), end)
        chunks.append((cursor.isoformat(), upper.isoformat()))
        cursor = upper
    return chunks


def _intraday_window_bounds(
    *,
    account_tz: ZoneInfo,
    now: datetime.datetime | None = None,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return account-local [today midnight, latest completed hour).

    Flooring on the UTC timeline preserves the correct ``fold`` across the
    repeated hour when daylight saving time ends. Midnight is explicitly fold
    zero because it is the start of the account-local calendar day.
    """
    local_now = (
        now or datetime.datetime.now(datetime.timezone.utc)
    ).astimezone(account_tz)
    current_hour_utc = local_now.astimezone(datetime.timezone.utc).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    current_hour = current_hour_utc.astimezone(account_tz)
    today_start = local_now.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        fold=0,
    )
    return today_start, current_hour


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


def extract_daily_ad_rows(
    payload: dict[str, Any],
    *,
    default_date: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize a Snap `report stats` envelope (DAY granularity, ad
    breakdown) into a flat list of per-day, per-ad raw stat dicts.

    Handles both shapes seen from the stats endpoint:
      - `timeseries_stats`: DAY/HOUR granularity. Each top-level block wraps
        a `timeseries_stat` with a `timeseries` list of per-bucket entries
        (`start_time`/`end_time`/`stats`/`breakdown_stats`).
      - `total_stats`: TOTAL granularity fallback. Each block wraps a
        `total_stat` with `breakdown_stats` directly (no per-day buckets).
    """
    rows: list[dict[str, Any]] = []

    def _collect_breakdown(
        entry: dict[str, Any],
        date_str: str | None,
    ) -> None:
        breakdown = entry.get("breakdown_stats") if isinstance(entry, dict) else None
        ad_rows = breakdown.get("ad") if isinstance(breakdown, dict) else None
        if not isinstance(ad_rows, list):
            return
        for ad_row in ad_rows:
            if not isinstance(ad_row, dict):
                continue
            ad_id = ad_row.get("id")
            # DAY-granularity ad-account queries return the inverse nesting:
            # breakdown_stats.ad[i].timeseries[bucket].stats; one bucket per day.
            ad_timeseries = ad_row.get("timeseries")
            if isinstance(ad_timeseries, list):
                if not ad_id:
                    continue
                for bucket in ad_timeseries:
                    if not isinstance(bucket, dict):
                        continue
                    parsed = _parse_iso(bucket.get("start_time"))
                    bucket_date = (
                        parsed.date().isoformat()
                        if parsed
                        else date_str or default_date
                    )
                    stats = bucket.get("stats")
                    if isinstance(stats, dict):
                        rows.append({"date": bucket_date, "ad_id": str(ad_id), "stats": stats})
                continue
            stats = ad_row.get("stats") if isinstance(ad_row.get("stats"), dict) else ad_row
            ad_id = ad_id or stats.get("id")
            if not ad_id:
                continue
            rows.append(
                {
                    "date": date_str or default_date,
                    "ad_id": str(ad_id),
                    "stats": stats,
                }
            )

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
    """Accept CLI flat rows and the provider's nested entity envelopes."""
    items = payload.get(key) or []
    singular = {"ads": "ad", "ad_squads": "adsquad", "adsquads": "adsquad",
                "campaigns": "campaign"}[key]
    if not isinstance(items, list):
        raise SyncError(f"{key} response is not a list")
    entities = []
    for item in items:
        if not isinstance(item, dict):
            raise SyncError(f"{key} response contains a non-object entity")
        status = item.get("sub_request_status")
        if status is not None and status != "SUCCESS":
            raise SyncError(f"{key} response contains a failed entity request")
        entity = item.get(singular, item)
        if not isinstance(entity, dict) or not entity.get("id"):
            raise SyncError(f"{key} response contains an entity without an ID")
        entities.append(entity)
    return entities


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
    *,
    source_window_end: datetime.datetime | str | None = None,
    provisional: bool = False,
    synced_at: datetime.datetime | str | None = None,
) -> list[dict[str, Any]]:
    synced_at = synced_at or datetime.datetime.now(datetime.timezone.utc)
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
                "source_window_end": source_window_end,
                "provisional": provisional,
                "last_synced_at": synced_at,
            }
        )
    return out


ROW_COLUMNS = [
    "recorded_at", "campaign_id", "campaign_name", "ad_squad_id", "ad_squad_name",
    "ad_id", "ad_name", "spend", "impressions", "swipes", "swipe_up_rate", "cpm",
    "cost_per_swipe", "video_views", "video_views_p50", "video_views_p100",
    "conversions", "revenue", "roas", "source_window_end", "provisional",
    "last_synced_at",
]
UPDATE_COLUMNS = [c for c in ROW_COLUMNS if c not in ("recorded_at", "ad_id")]


def upsert_rows(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> int:
    if not rows:
        return 0
    placeholders = "(" + ", ".join(f"%({c})s" for c in ROW_COLUMNS) + ")"
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPDATE_COLUMNS)
    metrics_table = qualified_table(
        warehouse_schema,
        "snapchat_ad_daily_metrics",
    )
    sql = (
        f"INSERT INTO {metrics_table} ({', '.join(ROW_COLUMNS)}) "
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


def apply_schema(
    conn: Any,
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> None:
    sql = render_schema_sql(
        SCHEMA_SQL.read_text(encoding="utf-8"),
        warehouse_schema,
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def collect_sync_rows(
    *,
    mode: str,
    days: int,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Collect and normalize one closed-day or intraday metrics window."""
    if mode not in {"closed", "intraday"}:
        raise SyncError(f"unsupported sync mode: {mode}")
    if days < 1:
        raise SyncError("days must be at least 1")

    health = _run_cli_json("account", "health-check")
    account_timezone, account_tz = resolve_account_timezone(health)
    captured_at = now or datetime.datetime.now(datetime.timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=datetime.timezone.utc)
    captured_at = captured_at.astimezone(datetime.timezone.utc)

    raw_rows: list[dict[str, Any]] = []
    if mode == "closed":
        start_iso, end_iso = _window_bounds(
            days,
            account_tz=account_tz,
            now=captured_at,
        )
        chunks = _window_chunks(
            days,
            account_tz=account_tz,
            now=captured_at,
        )
        for chunk_start, chunk_end in chunks:
            stats_payload = _run_cli_json(
                "report",
                "stats",
                "--entity",
                "ad_account",
                "--granularity",
                "DAY",
                "--start-time",
                chunk_start,
                "--end-time",
                chunk_end,
                "--fields",
                ",".join(STATS_FIELDS),
                "--breakdown",
                "ad",
                "--include-empty",
            )
            raw_rows.extend(extract_daily_ad_rows(stats_payload))
        window_start = _parse_iso(start_iso)
        window_end = _parse_iso(end_iso)
        provisional = False
    else:
        window_start, window_end = _intraday_window_bounds(
            account_tz=account_tz,
            now=captured_at,
        )
        chunks = []
        if (
            window_end.astimezone(datetime.timezone.utc)
            > window_start.astimezone(datetime.timezone.utc)
        ):
            stats_payload = _run_cli_json(
                "report",
                "stats",
                "--entity",
                "ad_account",
                "--granularity",
                "TOTAL",
                "--start-time",
                window_start.isoformat(),
                "--end-time",
                window_end.isoformat(),
                "--fields",
                ",".join(STATS_FIELDS),
                "--breakdown",
                "ad",
                "--omit-empty",
            )
            raw_rows.extend(
                extract_daily_ad_rows(
                    stats_payload,
                    default_date=window_start.date().isoformat(),
                )
            )
        start_iso = window_start.isoformat()
        end_iso = window_end.isoformat()
        provisional = True

    if window_start is None or window_end is None:
        raise SyncError("computed stats window is not valid ISO-8601")

    if raw_rows:
        ads_by_id, squads_by_id, campaigns_by_id = fetch_entity_maps()
        rows = build_rows(
            raw_rows,
            ads_by_id,
            squads_by_id,
            campaigns_by_id,
            source_window_end=window_end,
            provisional=provisional,
            synced_at=captured_at,
        )
    else:
        rows = []

    return {
        "mode": mode,
        "account_timezone": account_timezone,
        "window_start": window_start,
        "window_end": window_end,
        "chunk_count": len(chunks),
        "provisional": provisional,
        "captured_at": captured_at,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync closed or current-day Snapchat ad-level metrics."
    )
    parser.add_argument(
        "--mode",
        choices=("closed", "intraday"),
        default="closed",
        help="closed preserves trailing DAY sync; intraday refreshes today through the latest completed hour.",
    )
    parser.add_argument("--days", type=int, default=7, help="How many trailing days to pull (default 7).")
    parser.add_argument("--dry-run", action="store_true", help="Print sample rows + counts; skip all DB writes.")
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help=(
            "Convenience only: render and commit schema.sql before upserting. "
            "Use sync_snapchat_entities.py --execute --apply-schema for production migration."
        ),
    )
    parser.add_argument("--database-url", default=None, help="Override the DATABASE_URL environment variable.")
    parser.add_argument(
        "--warehouse-schema",
        default=os.environ.get(
            "SNAP_WAREHOUSE_SCHEMA",
            DEFAULT_WAREHOUSE_SCHEMA,
        ),
        help=(
            "Validated PostgreSQL schema for every warehouse relation "
            f"(default {DEFAULT_WAREHOUSE_SCHEMA})."
        ),
    )
    args = parser.parse_args()
    try:
        warehouse_schema = validate_warehouse_schema(args.warehouse_schema)
    except WarehouseSchemaError as exc:
        parser.error(str(exc))

    try:
        result = collect_sync_rows(mode=args.mode, days=args.days)
    except SyncError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)

    rows = result["rows"]
    window = {
        "start": result["window_start"].isoformat(),
        "end": result["window_end"].isoformat(),
    }
    print(
        f"[sync_snapchat_daily] account={_snapchat_account()} mode={args.mode} "
        f"timezone={result['account_timezone']} "
        f"window={window['start']}..{window['end']} "
        f"chunks={result['chunk_count']}",
        file=sys.stderr,
    )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "warehouse_schema": warehouse_schema,
                    "mode": args.mode,
                    "days": args.days,
                    "account_timezone": result["account_timezone"],
                    "window": window,
                    "provisional": result["provisional"],
                    "source_window_end": window["end"],
                    "last_synced_at": result["captured_at"].isoformat(),
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
                apply_schema(conn, warehouse_schema=warehouse_schema)
            upserted = upsert_rows(
                conn,
                rows,
                warehouse_schema=warehouse_schema,
            )
    except (psycopg.Error, WarehouseSchemaError) as exc:
        print(json.dumps({"error": f"Database error: {exc}"}), file=sys.stderr)
        sys.exit(1)

    print(
        json.dumps(
            {
                "rows_upserted": upserted,
                "warehouse_schema": warehouse_schema,
                "mode": args.mode,
                "days": args.days,
                "account_timezone": result["account_timezone"],
                "window": window,
                "provisional": result["provisional"],
                "source_window_end": window["end"],
                "last_synced_at": result["captured_at"].isoformat(),
            }
        )
    )


if __name__ == "__main__":
    main()
