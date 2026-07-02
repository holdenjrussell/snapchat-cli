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
from datetime import datetime, timedelta, timezone
from typing import Any

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


def daily_report(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    days: int = 7,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    return sync_stats(
        client,
        entity_type="ad_account",
        entity_id=ad_account_id,
        granularity="DAY",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        fields=fields or ["spend", "impressions", "swipes", "conversion_purchases", "conversion_purchases_value"],
    )


def hourly_report(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    hours: int = 24,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    return sync_stats(
        client,
        entity_type="ad_account",
        entity_id=ad_account_id,
        granularity="HOUR",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
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
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    return sync_stats(
        client,
        entity_type="ad_account",
        entity_id=ad_account_id,
        granularity="DAY",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
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
