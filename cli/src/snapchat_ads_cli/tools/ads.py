"""Ad CRUD + bulk fetch + bulk pause/launch."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview

READ_ONLY_AD_FIELDS = {
    "created_at",
    "updated_at",
    "delivery_status",
    "deleted",
}


def _merge_update_payload(
    current: dict[str, Any],
    fields: dict[str, Any],
    ad_id: str,
    ad_squad_id: str,
) -> dict[str, Any]:
    payload = {
        k: v for k, v in current.items()
        if k not in READ_ONLY_AD_FIELDS and v is not None
    }
    payload.update(fields)
    payload["id"] = ad_id
    payload.setdefault("ad_squad_id", ad_squad_id)
    return payload


def list_ads(
    client: SnapchatApiClient,
    *,
    ad_account_id: str | None = None,
    campaign_id: str | None = None,
    ad_squad_id: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    params = {"limit": min(max(limit, 1), 1000)}
    if ad_squad_id:
        path = f"adsquads/{ad_squad_id}/ads"
    elif campaign_id:
        path = f"campaigns/{campaign_id}/ads"
    elif ad_account_id:
        path = f"adaccounts/{ad_account_id}/ads"
    else:
        return {"error": {"message": "Provide --ad-account-id, --campaign-id, or --ad-squad-id"}}
    items = client.collect_paginated(path, "ads", params=params)
    return {"ads": items, "count": len(items)}


def get_ad(client: SnapchatApiClient, ad_id: str) -> dict[str, Any]:
    body, _ = client.get(f"ads/{ad_id}")
    items = body.get("ads") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "ad" in first:
            return first["ad"]
        return first
    return body


def get_ads_by_ids(
    client: SnapchatApiClient,
    ad_account_id: str,
    ad_ids: list[str],
) -> dict[str, Any]:
    from . import _bulk

    return _bulk.bulk_get_by_ids(
        client,
        path=f"adaccounts/{ad_account_id}/get_ads_by_ids",
        ids=ad_ids,
        id_array_key="entity_ids",
        id_item_key="id",
        response_array_key="ads",
        inner_singular="ad",
    )


def create_ad(
    client: SnapchatApiClient,
    ad_squad_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("ad_squad_id", ad_squad_id)
    if not execute:
        return format_preview(
            f"create ad in squad {ad_squad_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adsquads/{ad_squad_id}/ads",
        json_body={"ads": [payload]},
    )
    return body


def update_ad(
    client: SnapchatApiClient,
    ad_squad_id: str,
    ad_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}

    current: dict[str, Any] = {}
    try:
        current = get_ad(client, ad_id)
    except Exception:
        pass

    payload = _merge_update_payload(current, fields, ad_id, ad_squad_id)

    if not execute:
        return format_preview(
            f"update ad {ad_id}",
            account_label,
            current_state={k: current.get(k) for k in fields if k in current},
            proposed_state=payload,
        )

    body, _ = client.put(
        f"adsquads/{ad_squad_id}/ads",
        json_body={"ads": [payload]},
    )
    return body


def delete_ad(
    client: SnapchatApiClient,
    ad_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete ad {ad_id}", account_label, details="DELETE is irreversible."
        )
    body, _ = client.delete(f"ads/{ad_id}")
    return body


def bulk_create_ads(
    client: SnapchatApiClient,
    ad_squad_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    items = [{"ad_squad_id": ad_squad_id, **i} for i in items]
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-create {len(items)} ads in squad {ad_squad_id}",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adsquads/{ad_squad_id}/ads",
        items=items,
        array_key="ads",
        chunk_size=chunk_size,
    )


def bulk_update_ads(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 100,
    execute: bool = False,
) -> dict[str, Any]:
    """Update arbitrary fields on many ads in one PUT (chunked at 100)."""
    from . import _bulk
    missing = [i for i in items if not i.get("id")]
    if missing:
        return {"error": {"message": f"{len(missing)} item(s) missing required 'id' field"}}
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-update {len(items)} ads",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/ads",
        items=items,
        array_key="ads",
        chunk_size=chunk_size,
    )


def bulk_set_status(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    ad_ids: list[str],
    status: str,
    execute: bool = False,
) -> dict[str, Any]:
    """Pause / launch / archive many ads in one PUT.

    Status is one of ACTIVE, PAUSED, ARCHIVED.
    """
    status = status.upper()
    if status not in {"ACTIVE", "PAUSED", "ARCHIVED"}:
        return {"error": {"message": f"status must be ACTIVE/PAUSED/ARCHIVED, got {status}"}}

    payload = [{"id": aid, "status": status} for aid in ad_ids]

    if not execute:
        return format_preview(
            f"set status={status} on {len(ad_ids)} ads",
            account_label,
            proposed_state={"ad_ids": ad_ids, "status": status},
        )

    from . import _bulk
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/ads",
        items=payload,
        array_key="ads",
        chunk_size=100,
    )


def duplicate_ad(
    client: SnapchatApiClient,
    source_ad_id: str,
    account_label: str,
    *,
    target_ad_squad_id: str | None = None,
    new_name: str | None = None,
    status: str = "PAUSED",
    overrides: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    src = get_ad(client, source_ad_id)
    if not src:
        return {"error": {"message": f"ad {source_ad_id} not found"}}

    skip_keys = {"id", "created_at", "updated_at", "delivery_status"}
    payload = {k: v for k, v in src.items() if k not in skip_keys}
    payload["name"] = new_name or f"{src.get('name', 'ad')} (copy)"
    payload["status"] = status
    sqid = target_ad_squad_id or src.get("ad_squad_id")
    payload["ad_squad_id"] = sqid
    if overrides:
        payload.update(overrides)

    if not execute:
        return format_preview(
            f"duplicate ad {source_ad_id} -> squad {sqid}",
            account_label,
            current_state={"id": src.get("id"), "name": src.get("name")},
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adsquads/{sqid}/ads",
        json_body={"ads": [payload]},
    )
    return body
