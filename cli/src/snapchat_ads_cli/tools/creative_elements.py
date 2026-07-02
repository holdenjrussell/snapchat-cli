"""Creative elements + interaction zones (Collection / DPA primitives)."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


# ---- creative elements ----------------------------------------------------


def list_creative_elements(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/creative_elements", "creative_elements"
    )
    return {"creative_elements": items, "count": len(items), "ad_account_id": ad_account_id}


def get_creative_element(client: SnapchatApiClient, element_id: str) -> dict[str, Any]:
    body, _ = client.get(f"creative_elements/{element_id}")
    items = body.get("creative_elements") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "creative_element" in first:
            return first["creative_element"]
        return first
    return body


def create_creative_element(
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
            f"create creative element '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/creative_elements",
        json_body={"creative_elements": [payload]},
    )
    return body


def update_creative_element(
    client: SnapchatApiClient,
    ad_account_id: str,
    element_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = element_id
    if not execute:
        return format_preview(
            f"update creative element {element_id}",
            account_label,
            proposed_state=fields,
        )
    body, _ = client.put(
        f"adaccounts/{ad_account_id}/creative_elements",
        json_body={"creative_elements": [payload]},
    )
    return body


# ---- interaction zones ----------------------------------------------------


def list_interaction_zones(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/interaction_zones", "interaction_zones"
    )
    return {"interaction_zones": items, "count": len(items), "ad_account_id": ad_account_id}


def get_interaction_zone(client: SnapchatApiClient, zone_id: str) -> dict[str, Any]:
    body, _ = client.get(f"interaction_zones/{zone_id}")
    items = body.get("interaction_zones") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "interaction_zone" in first:
            return first["interaction_zone"]
        return first
    return body


def create_interaction_zone(
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
            f"create interaction zone '{payload.get('headline', payload.get('name', '?'))}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/interaction_zones",
        json_body={"interaction_zones": [payload]},
    )
    return body


def update_interaction_zone(
    client: SnapchatApiClient,
    ad_account_id: str,
    zone_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = zone_id
    if not execute:
        return format_preview(
            f"update interaction zone {zone_id}",
            account_label,
            proposed_state=fields,
        )
    body, _ = client.put(
        f"adaccounts/{ad_account_id}/interaction_zones",
        json_body={"interaction_zones": [payload]},
    )
    return body


def bulk_create_creative_elements(
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
            action=f"bulk-create {len(items)} creative elements",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/creative_elements",
        items=items,
        array_key="creative_elements",
        chunk_size=chunk_size,
    )


def bulk_create_interaction_zones(
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
            action=f"bulk-create {len(items)} interaction zones",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/interaction_zones",
        items=items,
        array_key="interaction_zones",
        chunk_size=chunk_size,
    )


def delete_creative_element(
    client: SnapchatApiClient,
    element_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete creative element {element_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"creative_elements/{element_id}")
    return body


def delete_interaction_zone(
    client: SnapchatApiClient,
    zone_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete interaction zone {zone_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"interaction_zones/{zone_id}")
    return body
