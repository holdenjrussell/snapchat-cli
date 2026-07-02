"""Snap Pixel CRUD + domain stats."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


def list_pixels(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/pixels", "pixels"
    )
    return {"pixels": items, "count": len(items), "ad_account_id": ad_account_id}


def get_pixel(client: SnapchatApiClient, pixel_id: str) -> dict[str, Any]:
    body, _ = client.get(f"pixels/{pixel_id}")
    items = body.get("pixels") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "pixel" in first:
            return first["pixel"]
        return first
    return body


def create_pixel(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("ad_account_id", ad_account_id)
    if not execute:
        return format_preview(
            f"create pixel '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/pixels",
        json_body={"pixels": [payload]},
    )
    return body


def update_pixel(
    client: SnapchatApiClient,
    ad_account_id: str,
    pixel_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = pixel_id

    if not execute:
        return format_preview(
            f"update pixel {pixel_id}",
            account_label,
            proposed_state=fields,
        )

    body, _ = client.put(
        f"adaccounts/{ad_account_id}/pixels",
        json_body={"pixels": [payload]},
    )
    return body


def bulk_create_pixels(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    items = [{"ad_account_id": ad_account_id, **i} for i in items]
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-create {len(items)} pixels",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/pixels",
        items=items,
        array_key="pixels",
        chunk_size=chunk_size,
    )


def bulk_update_pixels(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    missing = [i for i in items if not i.get("id")]
    if missing:
        return {"error": {"message": f"{len(missing)} item(s) missing required 'id' field"}}
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-update {len(items)} pixels",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/pixels",
        items=items,
        array_key="pixels",
        chunk_size=chunk_size,
    )


def domain_stats(
    client: SnapchatApiClient,
    pixel_id: str,
    *,
    granularity: str = "DAY",
) -> dict[str, Any]:
    body, _ = client.get(
        f"pixels/{pixel_id}/domains/stats",
        params={"granularity": granularity},
    )
    return body


def pixel_stats(
    client: SnapchatApiClient,
    pixel_id: str,
    *,
    granularity: str = "DAY",
    start_time: str | None = None,
    end_time: str | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"granularity": granularity}
    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time
    if fields:
        params["fields"] = ",".join(fields)
    body, _ = client.get(f"pixels/{pixel_id}/stats", params=params)
    return body
