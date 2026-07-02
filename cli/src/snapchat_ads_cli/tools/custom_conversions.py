"""Custom conversion events for pixels and mobile apps."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


# Pixel custom conversions ---------------------------------------------------


def list_pixel_custom_conversions(client: SnapchatApiClient, pixel_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"pixels/{pixel_id}/custom_conversions", "custom_conversions"
    )
    return {"custom_conversions": items, "count": len(items), "pixel_id": pixel_id}


def create_pixel_custom_conversion(
    client: SnapchatApiClient,
    pixel_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"create custom conversion '{payload.get('name', '?')}' on pixel {pixel_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"pixels/{pixel_id}/custom_conversions",
        json_body={"custom_conversions": [payload]},
    )
    return body


def get_custom_conversion(client: SnapchatApiClient, conversion_id: str) -> dict[str, Any]:
    body, _ = client.get(f"custom_conversions/{conversion_id}")
    items = body.get("custom_conversions") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "custom_conversion" in first:
            return first["custom_conversion"]
        return first
    return body


def delete_custom_conversion(
    client: SnapchatApiClient,
    conversion_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete custom conversion {conversion_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"custom_conversions/{conversion_id}")
    return body


# Mobile app custom conversions ----------------------------------------------


def list_mobile_app_custom_conversions(
    client: SnapchatApiClient, mobile_app_id: str
) -> dict[str, Any]:
    items = client.collect_paginated(
        f"mobile_apps/{mobile_app_id}/custom_conversions", "custom_conversions"
    )
    return {
        "custom_conversions": items,
        "count": len(items),
        "mobile_app_id": mobile_app_id,
    }


def create_mobile_app_custom_conversion(
    client: SnapchatApiClient,
    mobile_app_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"create app custom conversion '{payload.get('name', '?')}' on app {mobile_app_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"mobile_apps/{mobile_app_id}/custom_conversions",
        json_body={"custom_conversions": [payload]},
    )
    return body


def delete_mobile_app_custom_conversion(
    client: SnapchatApiClient,
    conversion_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete app custom conversion {conversion_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"custom_conversions/{conversion_id}")
    return body
