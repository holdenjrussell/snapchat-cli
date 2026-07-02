"""Ad Squad CRUD + spend guidance."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview

READ_ONLY_AD_SQUAD_FIELDS = {
    "created_at",
    "updated_at",
    "delivery_status",
    "deleted",
}


def _merge_update_payload(
    current: dict[str, Any],
    fields: dict[str, Any],
    ad_squad_id: str,
) -> dict[str, Any]:
    payload = {
        k: v for k, v in current.items()
        if k not in READ_ONLY_AD_SQUAD_FIELDS and v is not None
    }
    payload.update(fields)
    payload["id"] = ad_squad_id
    return payload


def list_ad_squads(
    client: SnapchatApiClient,
    *,
    ad_account_id: str | None = None,
    campaign_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if not (ad_account_id or campaign_id):
        return {"error": {"message": "Provide --ad-account-id or --campaign-id"}}

    params = {
        "limit": min(max(limit, 1), 1000),
        "return_placement_v2": "true",
    }
    if campaign_id:
        path = f"campaigns/{campaign_id}/adsquads"
    else:
        path = f"adaccounts/{ad_account_id}/adsquads"
    items = client.collect_paginated(path, "adsquads", params=params)
    return {
        "ad_squads": items,
        "count": len(items),
        "ad_account_id": ad_account_id,
        "campaign_id": campaign_id,
    }


def get_ad_squad(client: SnapchatApiClient, ad_squad_id: str) -> dict[str, Any]:
    body, _ = client.get(
        f"adsquads/{ad_squad_id}",
        params={"return_placement_v2": "true"},
    )
    items = body.get("adsquads") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "adsquad" in first:
            return first["adsquad"]
        return first
    return body


def create_ad_squad(
    client: SnapchatApiClient,
    campaign_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("campaign_id", campaign_id)

    if not execute:
        return format_preview(
            f"create ad squad in campaign {campaign_id}",
            account_label,
            proposed_state=payload,
        )

    body, _ = client.post(
        f"campaigns/{campaign_id}/adsquads",
        json_body={"adsquads": [payload]},
    )
    return body


def build_ad_squad_payload(
    *,
    name: str,
    campaign_id: str,
    type: str = "SNAP_ADS",
    placement_v2: dict[str, Any] | None = None,
    optimization_goal: str | None = None,
    bid_strategy: str | None = None,
    bid_micro: int | None = None,
    target_cost_micro: int | None = None,
    min_roas: float | None = None,
    daily_budget_micro: int | None = None,
    lifetime_budget_micro: int | None = None,
    billing_event: str | None = None,
    conversion_window: str | None = None,
    pixel_id: str | None = None,
    snap_pixel_id: str | None = None,
    targeting: dict[str, Any] | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    skadnetwork_status: str | None = None,
    attribution_settings: dict[str, Any] | None = None,
    status: str = "PAUSED",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a documented ad-squad payload from first-class parameters."""
    payload: dict[str, Any] = {
        "name": name,
        "campaign_id": campaign_id,
        "type": type,
        "status": status,
    }
    if placement_v2:
        payload["placement_v2"] = placement_v2
    if optimization_goal:
        payload["optimization_goal"] = optimization_goal
    if bid_strategy:
        payload["bid_strategy"] = bid_strategy
    if bid_micro is not None:
        payload["bid_micro"] = int(bid_micro)
    if target_cost_micro is not None:
        payload["target_cost_micro"] = int(target_cost_micro)
    if min_roas is not None:
        payload["min_roas"] = float(min_roas)
    if daily_budget_micro is not None:
        payload["daily_budget_micro"] = int(daily_budget_micro)
    if lifetime_budget_micro is not None:
        payload["lifetime_budget_micro"] = int(lifetime_budget_micro)
    if billing_event:
        payload["billing_event"] = billing_event
    if conversion_window:
        payload["conversion_window"] = conversion_window
    if pixel_id:
        payload["pixel_id"] = pixel_id
    if snap_pixel_id:
        payload["snap_pixel_id"] = snap_pixel_id
    if targeting:
        payload["targeting"] = targeting
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    settings = dict(attribution_settings) if attribution_settings else {}
    if skadnetwork_status:
        settings["skadnetwork_attribution_status"] = skadnetwork_status
    if settings:
        payload["attribution_settings"] = settings
    if extra:
        payload.update(extra)
    return payload


def update_ad_squad(
    client: SnapchatApiClient,
    ad_account_id: str,
    ad_squad_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}

    current: dict[str, Any] = {}
    try:
        current = get_ad_squad(client, ad_squad_id)
    except Exception:
        pass

    payload = _merge_update_payload(current, fields, ad_squad_id)
    campaign_id = payload.get("campaign_id")

    if not execute:
        return format_preview(
            f"update ad squad {ad_squad_id}",
            account_label,
            current_state={k: current.get(k) for k in fields if k in current},
            proposed_state=payload,
        )

    if not campaign_id:
        return {"error": {"message": "campaign_id required to update ad squad; fetch current object failed"}}

    body, _ = client.put(
        f"campaigns/{campaign_id}/adsquads",
        json_body={"adsquads": [payload]},
    )
    return body


def bulk_create_ad_squads(
    client: SnapchatApiClient,
    campaign_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    items = [{"campaign_id": campaign_id, **i} for i in items]
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-create {len(items)} ad squads in campaign {campaign_id}",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"campaigns/{campaign_id}/adsquads",
        items=items,
        array_key="adsquads",
        chunk_size=chunk_size,
    )


def bulk_update_ad_squads(
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
            action=f"bulk-update {len(items)} ad squads",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/adsquads",
        items=items,
        array_key="adsquads",
        chunk_size=chunk_size,
    )


def delete_ad_squad(
    client: SnapchatApiClient,
    ad_squad_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete ad squad {ad_squad_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"adsquads/{ad_squad_id}")
    return body


def spend_guidance(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    signal_type: str = "PIXEL",
    signal_id: str | None = None,
    optimization_goal: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"signal_type": signal_type}
    if signal_id:
        params["signal_id"] = signal_id
    if optimization_goal:
        params["optimization_goal"] = optimization_goal
    body, _ = client.get(
        f"adaccounts/{ad_account_id}/spend_guidance", params=params
    )
    return body


def ad_squad_ad_restrictions(
    client: SnapchatApiClient, ad_squad_id: str
) -> dict[str, Any]:
    body, _ = client.get(f"adsquads/{ad_squad_id}/ad_squad_ad_restrictions")
    return body


def duplicate_ad_squad(
    client: SnapchatApiClient,
    source_ad_squad_id: str,
    account_label: str,
    *,
    target_campaign_id: str | None = None,
    new_name: str | None = None,
    status: str = "PAUSED",
    overrides: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    src = get_ad_squad(client, source_ad_squad_id)
    if not src:
        return {"error": {"message": f"ad squad {source_ad_squad_id} not found"}}

    skip_keys = {"id", "created_at", "updated_at", "delivery_status"}
    payload = {k: v for k, v in src.items() if k not in skip_keys}
    payload["name"] = new_name or f"{src.get('name', 'adsquad')} (copy)"
    payload["status"] = status
    cid = target_campaign_id or src.get("campaign_id")
    payload["campaign_id"] = cid
    if overrides:
        payload.update(overrides)

    if not execute:
        return format_preview(
            f"duplicate ad squad {source_ad_squad_id} -> campaign {cid}",
            account_label,
            current_state={"id": src.get("id"), "name": src.get("name")},
            proposed_state=payload,
        )
    body, _ = client.post(
        f"campaigns/{cid}/adsquads",
        json_body={"adsquads": [payload]},
    )
    return body
