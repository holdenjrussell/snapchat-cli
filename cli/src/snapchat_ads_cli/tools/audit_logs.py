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
    items = client.collect_paginated(
        f"{ENTITY_PATHS[entity_type]}/{entity_id}/external_changelogs",
        "external_changelogs",
        params={"limit": min(max(limit, 1), 1000)},
    )
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "external_changelogs": items,
        "count": len(items),
    }
