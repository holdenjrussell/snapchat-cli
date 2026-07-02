"""Targeting insights + static reference for documented enumerations.

Snap exposes `POST /adaccounts/{id}/targeting_insights` for a live targeting
preview. Demographic/interest enumerations come from documentation; we ship
small static reference JSON files under data/ rather than fabricate live
endpoints that do not exist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..api_client import SnapchatApiClient

DATA_DIR = Path(__file__).parent.parent / "data"


def targeting_insights(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    spec: dict[str, Any],
    breakdown: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {**spec}
    if breakdown and "breakdown" not in payload:
        payload["breakdown"] = breakdown
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/targeting_insights",
        json_body=payload,
    )
    return body


def geo_search(
    client: SnapchatApiClient,
    *,
    country_code: str,
    location_type: str,
    query: str | None = None,
) -> dict[str, Any]:
    """Hit the documented geo enumeration endpoints.

    Snap exposes per-type geo endpoints (region, metro, dma, postal_code,
    circle). location_type maps to the path segment.
    """
    location_type = location_type.lower()
    valid = {"region", "metro", "dma", "postal_code", "circle", "country"}
    if location_type not in valid:
        return {"error": {"message": f"location_type must be one of {sorted(valid)}"}}

    params: dict[str, Any] = {}
    if query:
        params["query"] = query

    path = f"targeting/geo/{country_code.lower()}/{location_type}"
    body, _ = client.get(path, params=params or None)
    return body


def device_marketing_names(
    client: SnapchatApiClient,
    *,
    os_type: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if os_type:
        params["os_type"] = os_type
    body, _ = client.get("targeting/device/marketing_name", params=params or None)
    return body


def options_by_country(
    client: SnapchatApiClient, country_code: str
) -> dict[str, Any]:
    body, _ = client.get(
        "targeting/geo/options",
        params={"country_code": country_code.lower()},
    )
    return body


def static_reference(name: str) -> dict[str, Any]:
    """Return a packaged static reference JSON.

    name in {"geo", "demo", "interests"}. Marked as static so callers can warn
    users to consult the live docs.
    """
    valid = {"geo", "demo", "interests"}
    if name not in valid:
        return {"error": {"message": f"reference must be one of {sorted(valid)}"}}
    p = DATA_DIR / f"targeting_{name}.json"
    if not p.exists():
        return {"reference": name, "static": True, "data": [], "note": "reference file not packaged"}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"error": {"message": f"failed to read {p}: {e}"}}
    return {
        "reference": name,
        "static": True,
        "note": "Static reference snapshot. See https://developers.snap.com for live values.",
        "data": data,
    }
