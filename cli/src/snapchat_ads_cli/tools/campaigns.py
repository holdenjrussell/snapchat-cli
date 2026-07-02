"""Campaign CRUD."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview

READ_ONLY_CAMPAIGN_FIELDS = {
    "created_at",
    "updated_at",
    "delivery_status",
    "deleted",
}


def _merge_update_payload(
    current: dict[str, Any],
    fields: dict[str, Any],
    campaign_id: str,
) -> dict[str, Any]:
    payload = {
        k: v for k, v in current.items()
        if k not in READ_ONLY_CAMPAIGN_FIELDS and v is not None
    }
    payload.update(fields)
    payload["id"] = campaign_id
    return payload


def list_campaigns(
    client: SnapchatApiClient,
    ad_account_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    params = {"limit": min(max(limit, 1), 1000)}
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/campaigns",
        "campaigns",
        params=params,
    )
    return {"campaigns": items, "count": len(items), "ad_account_id": ad_account_id}


def get_campaign(client: SnapchatApiClient, campaign_id: str) -> dict[str, Any]:
    body, _ = client.get(f"campaigns/{campaign_id}")
    items = body.get("campaigns") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "campaign" in first:
            return first["campaign"]
        return first
    return body


def get_campaigns_by_ids(
    client: SnapchatApiClient,
    ad_account_id: str,
    campaign_ids: list[str],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/get_campaigns_by_ids",
        json_body={"campaign_ids": campaign_ids},
    )
    items = body.get("campaigns") or []
    flat = []
    for e in items:
        if isinstance(e, dict) and "campaign" in e:
            flat.append(e["campaign"])
        elif isinstance(e, dict):
            flat.append(e)
    return {"campaigns": flat, "count": len(flat)}


def create_campaign(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    objective: str | None = None,
    objective_v2_type: str | None = None,
    promotion_type: str | None = None,
    buy_model: str | None = None,
    reserved_type: str | None = None,
    status: str = "PAUSED",
    daily_budget_micro: int | None = None,
    lifetime_spend_cap_micro: int | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    reach_frequency_spec: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "ad_account_id": ad_account_id,
        "status": status,
    }
    if objective:
        payload["objective"] = objective
    if objective_v2_type or promotion_type:
        ov2: dict[str, Any] = {}
        if objective_v2_type:
            ov2["objective_v2_type"] = objective_v2_type
        if promotion_type:
            ov2["promotion_type"] = promotion_type
        payload["objective_v2_properties"] = ov2
    if buy_model:
        payload["buy_model"] = buy_model
    if reserved_type:
        payload["reserved_type"] = reserved_type
    if daily_budget_micro is not None:
        payload["daily_budget_micro"] = int(daily_budget_micro)
    if lifetime_spend_cap_micro is not None:
        payload["lifetime_spend_cap_micro"] = int(lifetime_spend_cap_micro)
    if start_time:
        payload["start_time"] = start_time
    if end_time:
        payload["end_time"] = end_time
    if reach_frequency_spec:
        payload["reach_frequency_spec"] = reach_frequency_spec
    if extra:
        payload.update(extra)

    if not execute:
        return format_preview(
            f"create campaign '{name}' (objective={objective})",
            account_label,
            current_state=None,
            proposed_state=payload,
        )

    body, _ = client.post(
        f"adaccounts/{ad_account_id}/campaigns",
        json_body={"campaigns": [payload]},
    )
    return body


def update_campaign(
    client: SnapchatApiClient,
    ad_account_id: str,
    campaign_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}

    current: dict[str, Any] = {}
    try:
        current = get_campaign(client, campaign_id)
    except Exception:
        pass

    payload = _merge_update_payload(current, fields, campaign_id)

    if not execute:
        return format_preview(
            f"update campaign {campaign_id}",
            account_label,
            current_state={k: current.get(k) for k in fields if k in current},
            proposed_state=payload,
        )

    body, _ = client.put(
        f"adaccounts/{ad_account_id}/campaigns",
        json_body={"campaigns": [payload]},
    )
    return body


def bulk_create_campaigns(
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
            action=f"bulk-create {len(items)} campaigns",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/campaigns",
        items=items,
        array_key="campaigns",
        chunk_size=chunk_size,
    )


def bulk_update_campaigns(
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
            action=f"bulk-update {len(items)} campaigns",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/campaigns",
        items=items,
        array_key="campaigns",
        chunk_size=chunk_size,
    )


def delete_campaign(
    client: SnapchatApiClient,
    campaign_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete campaign {campaign_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"campaigns/{campaign_id}")
    return body


def duplicate_campaign(
    client: SnapchatApiClient,
    ad_account_id: str,
    source_campaign_id: str,
    account_label: str,
    *,
    new_name: str | None = None,
    status: str = "PAUSED",
    overrides: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Read-then-create duplicate (Snap has no native duplicate endpoint)."""
    src = get_campaign(client, source_campaign_id)
    if not src:
        return {"error": {"message": f"campaign {source_campaign_id} not found"}}

    skip_keys = {"id", "created_at", "updated_at", "delivery_status"}
    payload = {k: v for k, v in src.items() if k not in skip_keys}
    payload["name"] = new_name or f"{src.get('name', 'campaign')} (copy)"
    payload["ad_account_id"] = ad_account_id
    payload["status"] = status
    if overrides:
        payload.update(overrides)

    if not execute:
        return format_preview(
            f"duplicate campaign {source_campaign_id} as '{payload['name']}'",
            account_label,
            current_state={"id": src.get("id"), "name": src.get("name")},
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/campaigns",
        json_body={"campaigns": [payload]},
    )
    return body
