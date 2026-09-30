"""Sync stats + async report jobs.

Sync stats endpoint shape:
  GET /v1/{entity_type}/{id}/stats
  ?granularity={TOTAL,DAY,HOUR,LIFETIME}
  &start_time=...&end_time=...
  &fields=spend,impressions,swipes,...
  &breakdown=...

Async reports (for large pulls):
  POST /v1/adaccounts/{id}/reports     -> {report_run_id}
  GET  /v1/reports/{report_run_id}     -> status
  GET  /v1/reports/{report_run_id}/results / download URL
"""

from __future__ import annotations

import httpx
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from ..api_client import SnapchatApiClient

ENTITY_PATHS = {
    "ad": "ads",
    "ad_squad": "adsquads",
    "campaign": "campaigns",
    "ad_account": "adaccounts",
    "creative": "creatives",
    "pixel": "pixels",
}


def _stats_params(
    *,
    granularity: str = "TOTAL",
    start_time: str | None = None,
    end_time: str | None = None,
    fields: list[str] | None = None,
    breakdown: str | None = None,
    swipe_up_attribution_window: str | None = None,
    view_attribution_window: str | None = None,
    omit_empty: bool | None = None,
    report_dimension: str | None = None,
    position_stats: bool | None = None,
    platform_stats: bool | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"granularity": granularity.upper()}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if fields:
        params["fields"] = ",".join(fields)
    if breakdown:
        params["breakdown"] = breakdown
    if swipe_up_attribution_window:
        params["swipe_up_attribution_window"] = swipe_up_attribution_window
    if view_attribution_window:
        params["view_attribution_window"] = view_attribution_window
    if omit_empty is not None:
        params["omit_empty"] = str(bool(omit_empty)).lower()
    if report_dimension:
        params["report_dimension"] = report_dimension
    if position_stats is not None:
        params["position_stats"] = str(bool(position_stats)).lower()
    if platform_stats is not None:
        params["platform_stats"] = str(bool(platform_stats)).lower()
    if extra_params:
        params.update({k: v for k, v in extra_params.items() if v is not None})
    return params


def _stats_path(entity_type: str, entity_id: str, suffix: str = "stats") -> str:
    if entity_type not in ENTITY_PATHS:
        raise ValueError(f"entity_type must be one of {sorted(ENTITY_PATHS)}")
    return f"{ENTITY_PATHS[entity_type]}/{entity_id}/{suffix}"


def sync_stats(
    client: SnapchatApiClient,
    *,
    entity_type: str,
    entity_id: str,
    granularity: str = "TOTAL",
    start_time: str | None = None,
    end_time: str | None = None,
    fields: list[str] | None = None,
    breakdown: str | None = None,
    swipe_up_attribution_window: str | None = None,
    view_attribution_window: str | None = None,
    omit_empty: bool | None = None,
    report_dimension: str | None = None,
    position_stats: bool | None = None,
    platform_stats: bool | None = None,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if entity_type not in ENTITY_PATHS:
        return {
            "error": {
                "message": f"entity_type must be one of {sorted(ENTITY_PATHS)}",
            }
        }

    params = _stats_params(
        granularity=granularity,
        start_time=start_time,
        end_time=end_time,
        fields=fields,
        breakdown=breakdown,
        swipe_up_attribution_window=swipe_up_attribution_window,
        view_attribution_window=view_attribution_window,
        omit_empty=omit_empty,
        report_dimension=report_dimension,
        position_stats=position_stats,
        platform_stats=platform_stats,
        extra_params=extra_params,
    )

    path = _stats_path(entity_type, entity_id)
    body, _ = client.get(path, params=params)
    return body


# ---------------------------------------------------------------------------
# Account-grain reports
#
# Snap serves only `spend` at AdAccount level; any other field returns E1008
# ("Only field 'spend' should be used when querying AdAccount stats"). The
# account reports below therefore ask for a campaign breakdown and sum it back
# to account grain, keeping the plain timeseries envelope callers and --human
# already understand. Day and hour boundaries are built in the ad account's own
# timezone, which is what Snap buckets on.
# ---------------------------------------------------------------------------

# Fields that cannot be added across campaigns; they are left out of a rollup
# and named in `not_summed_fields`. Deduplicated reach, averages, rates and
# effective costs are recomputed from sums by the caller, never summed.
NON_ADDITIVE_FIELDS = {
    "uniques",
    "frequency",
    "avg_screen_time_millis",
    "avg_view_time_millis",
    "avg_position",
    "swipe_up_percent",
    "view_completion_rate",
    "ecpm",
    "ecpsu",
}
_NON_ADDITIVE_PREFIXES = ("avg_", "ecp")
_NON_ADDITIVE_SUFFIXES = ("_rate", "_percent", "_frequency", "_uniques")


def _is_non_additive(name: str) -> bool:
    return (
        name in NON_ADDITIVE_FIELDS
        or name.startswith(_NON_ADDITIVE_PREFIXES)
        or name.endswith(_NON_ADDITIVE_SUFFIXES)
    )


def _account_timezone(client: SnapchatApiClient, ad_account_id: str) -> tzinfo:
    """The ad account's reporting timezone; UTC when it cannot be read."""
    try:
        body, _ = client.get(f"adaccounts/{ad_account_id}")
        for wrapper in body.get("adaccounts", []):
            name = (wrapper.get("adaccount") or {}).get("timezone")
            if name:
                return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a report should not die on a metadata read
        pass
    return timezone.utc


def _day_start(day: date, tz: tzinfo) -> str:
    return datetime.combine(day, dtime.min, tzinfo=tz).isoformat(timespec="milliseconds")


def _account_stats(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    granularity: str,
    start_time: str,
    end_time: str,
    fields: list[str],
) -> dict[str, Any]:
    """Account-grain stats for any field list (see the section note above)."""
    if set(fields) <= {"spend"}:
        return sync_stats(
            client,
            entity_type="ad_account",
            entity_id=ad_account_id,
            granularity=granularity,
            start_time=start_time,
            end_time=end_time,
            fields=fields,
        )
    body = sync_stats(
        client,
        entity_type="ad_account",
        entity_id=ad_account_id,
        granularity=granularity,
        start_time=start_time,
        end_time=end_time,
        fields=fields,
        breakdown="campaign",
    )
    return _account_rollup(body)


def _account_rollup(body: dict[str, Any]) -> dict[str, Any]:
    """Sum a campaign-breakdown stats body back to one account timeseries."""
    if "timeseries_stats" not in body:
        return body  # error envelope: pass it through untouched
    rolled_stats = []
    for wrapper in body.get("timeseries_stats", []):
        stat = wrapper.get("timeseries_stat") or {}
        buckets: dict[tuple[str, str], dict[str, float]] = {}
        skipped: set[str] = set()
        campaigns = (stat.get("breakdown_stats") or {}).get("campaign", [])
        for campaign in campaigns:
            for point in campaign.get("timeseries", []):
                key = (point.get("start_time", ""), point.get("end_time", ""))
                bucket = buckets.setdefault(key, {})
                for name, value in (point.get("stats") or {}).items():
                    if _is_non_additive(name):
                        skipped.add(name)
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    bucket[name] = bucket.get(name, 0) + value
        rolled = {k: v for k, v in stat.items() if k != "breakdown_stats"}
        rolled["timeseries"] = [
            {"start_time": start, "end_time": end, "stats": buckets[(start, end)]}
            for start, end in sorted(buckets)
        ]
        rolled["rolled_up_from"] = "campaign"
        rolled["campaigns_summed"] = len(campaigns)
        if skipped:
            rolled["not_summed_fields"] = sorted(skipped)
        rolled_stats.append(
            {
                "sub_request_status": wrapper.get("sub_request_status", "SUCCESS"),
                "timeseries_stat": rolled,
            }
        )
    out = {k: v for k, v in body.items() if k != "timeseries_stats"}
    out["timeseries_stats"] = rolled_stats
    return out


def daily_report(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    days: int = 7,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    # `days` complete account-timezone days plus today, which is still running.
    tz = _account_timezone(client, ad_account_id)
    today = datetime.now(tz).date()
    body = _account_stats(
        client,
        ad_account_id,
        granularity="DAY",
        start_time=_day_start(today - timedelta(days=days), tz),
        end_time=_day_start(today + timedelta(days=1), tz),
        fields=fields or ["spend", "impressions", "swipes", "conversion_purchases", "conversion_purchases_value"],
    )
    for wrapper in body.get("timeseries_stats", []):
        for point in (wrapper.get("timeseries_stat") or {}).get("timeseries", []):
            if point.get("start_time", "")[:10] == today.isoformat():
                point["partial"] = True
    return body


def hourly_report(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    hours: int = 24,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    tz = _account_timezone(client, ad_account_id)
    end = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    return _account_stats(
        client,
        ad_account_id,
        granularity="HOUR",
        start_time=start.isoformat(timespec="milliseconds"),
        end_time=end.isoformat(timespec="milliseconds"),
        fields=fields or ["spend", "impressions", "swipes"],
    )


def top_ads(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    days: int = 7,
    sort_by: str = "spend",
    limit: int = 25,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    use_fields = fields or ["spend", "impressions", "swipes", "conversion_purchases"]
    body = sync_stats(
        client,
        entity_type="ad_account",
        entity_id=ad_account_id,
        granularity="TOTAL",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        fields=use_fields,
        breakdown="ad",
    )
    rows = _flatten_stats(body)
    rows.sort(key=lambda r: float(r.get(sort_by, 0) or 0), reverse=True)
    return {"top_ads": rows[:limit], "count": min(limit, len(rows)), "sort_by": sort_by}


def video_report(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    days: int = 7,
) -> dict[str, Any]:
    tz = _account_timezone(client, ad_account_id)
    today = datetime.now(tz).date()
    return _account_stats(
        client,
        ad_account_id,
        granularity="DAY",
        start_time=_day_start(today - timedelta(days=days), tz),
        end_time=_day_start(today, tz),
        fields=[
            "spend",
            "impressions",
            "video_views",
            "video_views_25",
            "video_views_50",
            "video_views_75",
            "video_views_100",
            "view_completion",
        ],
    )


def async_submit(
    client: SnapchatApiClient,
    *,
    entity_type: str = "ad_account",
    entity_id: str,
    granularity: str = "TOTAL",
    start_time: str | None = None,
    end_time: str | None = None,
    fields: list[str] | None = None,
    breakdown: str | None = None,
    swipe_up_attribution_window: str | None = None,
    view_attribution_window: str | None = None,
    omit_empty: bool | None = None,
    report_dimension: str | None = None,
    position_stats: bool | None = None,
    platform_stats: bool | None = None,
    async_format: str = "csv",
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = _stats_params(
        granularity=granularity,
        start_time=start_time,
        end_time=end_time,
        fields=fields,
        breakdown=breakdown,
        swipe_up_attribution_window=swipe_up_attribution_window,
        view_attribution_window=view_attribution_window,
        omit_empty=omit_empty,
        report_dimension=report_dimension,
        position_stats=position_stats,
        platform_stats=platform_stats,
        extra_params=extra_params,
    )
    params["async"] = "true"
    params["async_format"] = async_format
    body, _ = client.get(_stats_path(entity_type, entity_id), params=params)
    return body


def async_status(
    client: SnapchatApiClient,
    *,
    entity_type: str,
    entity_id: str,
    report_run_id: str,
) -> dict[str, Any]:
    body, _ = client.get(
        _stats_path(entity_type, entity_id, suffix="stats_report"),
        params={"report_run_id": report_run_id},
    )
    return body


def async_download(
    client: SnapchatApiClient,
    report_run_id: str,
    *,
    entity_type: str,
    entity_id: str,
    out_path: str | None = None,
) -> dict[str, Any]:
    body = async_status(
        client,
        entity_type=entity_type,
        entity_id=entity_id,
        report_run_id=report_run_id,
    )
    url = _extract_async_result_url(body)
    if not (out_path and url):
        if url:
            return {"report_run_id": report_run_id, "download_url": url, "raw": body}
        return body

    with httpx.Client(timeout=300.0, follow_redirects=True) as c:
        resp = c.get(url)
        resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return {
        "report_run_id": report_run_id,
        "download_url": url,
        "out_path": out_path,
        "bytes": len(resp.content),
    }


def _extract_async_result_url(body: dict[str, Any]) -> str | None:
    reports = body.get("async_stats_reports")
    if isinstance(reports, list):
        for item in reports:
            if not isinstance(item, dict):
                continue
            report = item.get("async_stats_report") or item
            if isinstance(report, dict) and report.get("result"):
                return str(report["result"])
    for key in ("result", "download_url", "url"):
        if body.get(key):
            return str(body[key])
    return None


def lead_gen_submit(
    client: SnapchatApiClient,
    *,
    ad_account_id: str,
    start_time: str,
    end_time: str,
    async_format: str = "csv",
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/leads_report",
        params={
            "async_format": async_format,
            "start_time": start_time,
            "end_time": end_time,
        },
    )
    return body


def lead_gen_status(
    client: SnapchatApiClient,
    *,
    ad_account_id: str,
    report_run_id: str,
) -> dict[str, Any]:
    body, _ = client.get(
        f"adaccounts/{ad_account_id}/leads_report",
        params={"report_run_id": report_run_id},
    )
    return body


def lead_gen_download(
    client: SnapchatApiClient,
    *,
    ad_account_id: str,
    report_run_id: str,
    out_path: str | None = None,
) -> dict[str, Any]:
    body = lead_gen_status(
        client,
        ad_account_id=ad_account_id,
        report_run_id=report_run_id,
    )
    url = _extract_async_result_url(body)
    if not (out_path and url):
        if url:
            return {"report_run_id": report_run_id, "download_url": url, "raw": body}
        return body

    with httpx.Client(timeout=300.0, follow_redirects=True) as c:
        resp = c.get(url)
        resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return {
        "report_run_id": report_run_id,
        "download_url": url,
        "out_path": out_path,
        "bytes": len(resp.content),
    }


def _flatten_stats(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten Snap stats envelope into a list of metric rows."""
    rows: list[dict[str, Any]] = []
    blocks = body.get("total_stats") or body.get("timeseries_stats") or body.get("stats") or []
    if not isinstance(blocks, list):
        return rows
    for block in blocks:
        if not isinstance(block, dict):
            continue
        inner = block
        for key in ("ad_stat", "adsquad_stat", "campaign_stat", "adaccount_stat", "stat"):
            if key in block:
                inner = block[key]
                break
        if isinstance(inner, dict):
            rows.append(inner)
    return rows
