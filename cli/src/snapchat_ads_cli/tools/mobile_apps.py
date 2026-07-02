"""Snap App IDs for app-install ads."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


def list_mobile_apps(
    client: SnapchatApiClient, *, organization_id: str | None = None
) -> dict[str, Any]:
    if organization_id:
        path = f"organizations/{organization_id}/mobile_apps"
    else:
        path = "mobile_apps"
    items = client.collect_paginated(path, "mobile_apps")
    return {"mobile_apps": items, "count": len(items)}


def get_mobile_app(client: SnapchatApiClient, mobile_app_id: str) -> dict[str, Any]:
    body, _ = client.get(f"mobile_apps/{mobile_app_id}")
    items = body.get("mobile_apps") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "mobile_app" in first:
            return first["mobile_app"]
        return first
    return body


def create_mobile_app(
    client: SnapchatApiClient,
    organization_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"register mobile app '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"organizations/{organization_id}/mobile_apps",
        json_body={"mobile_apps": [payload]},
    )
    return body


def ecid_status(client: SnapchatApiClient, snap_app_id: str) -> dict[str, Any]:
    body, _ = client.get(f"mobile_apps/{snap_app_id}/ecid_status")
    return body
