"""Brand-lift / conversion-lift studies.

Snap exposes brand-lift study CRUD on the ad-account scope. Endpoint paths
follow the same convention as other ad-account-scoped resources.

Note: study endpoints may be allowlist-only depending on the org. The CLI
ships the surface; live calls will fail closed if the org isn't enrolled.
"""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview


def list_studies(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/brand_lift_studies",
        "brand_lift_studies",
    )
    return {"brand_lift_studies": items, "count": len(items), "ad_account_id": ad_account_id}


def get_study(client: SnapchatApiClient, study_id: str) -> dict[str, Any]:
    body, _ = client.get(f"brand_lift_studies/{study_id}")
    return body


def create_study(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"create brand-lift study '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/brand_lift_studies",
        json_body={"brand_lift_studies": [payload]},
    )
    return body


def study_results(client: SnapchatApiClient, study_id: str) -> dict[str, Any]:
    body, _ = client.get(f"brand_lift_studies/{study_id}/results")
    return body


def list_conversion_lift_studies(
    client: SnapchatApiClient, ad_account_id: str
) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/conversion_lift_studies",
        "conversion_lift_studies",
    )
    return {
        "conversion_lift_studies": items,
        "count": len(items),
        "ad_account_id": ad_account_id,
    }


def get_conversion_lift_study(client: SnapchatApiClient, study_id: str) -> dict[str, Any]:
    body, _ = client.get(f"conversion_lift_studies/{study_id}")
    return body
