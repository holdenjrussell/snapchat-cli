"""External changelogs (audit trail) for campaigns/adsquads/ads/creatives."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient

ENTITY_PATHS = {
    "campaign": "campaigns",
    "ad_squad": "adsquads",
    "ad": "ads",
    "creative": "creatives",
    "dynamic_template": "dynamic_templates",
}


def changelog(
    client: SnapchatApiClient,
    *,
    entity_type: str,
    entity_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    if entity_type not in ENTITY_PATHS:
        return {"error": {"message": f"entity_type must be one of {sorted(ENTITY_PATHS)}"}}
    # The endpoint path is .../external_changelogs, but Snap returns the rows
    # under "changelogs", each wrapped as {"sub_request_status", "changelog"}.
    # Reading the path name as the key made this command come back empty.
    limit = min(max(limit, 1), 1000)
    items: list[dict[str, Any]] = []
    for body in client.get_paginated(
        f"{ENTITY_PATHS[entity_type]}/{entity_id}/external_changelogs",
        params={"limit": limit},
    ):
        for entry in body.get("changelogs") or body.get("external_changelogs") or []:
            inner = entry.get("changelog") if isinstance(entry, dict) else None
            items.append(inner if isinstance(inner, dict) else entry)
        if len(items) >= limit:
            break
    items = items[:limit]
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "changelogs": items,
        "count": len(items),
    }
