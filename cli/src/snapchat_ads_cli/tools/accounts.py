"""Ad account info, funding sources, billing centers, ad-account roles."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def get_ad_account(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    body, _ = client.get(f"adaccounts/{ad_account_id}")
    items = body.get("adaccounts") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "adaccount" in first:
            return first["adaccount"]
        return first
    return body


def list_funding_sources(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/fundingsources", "fundingsources"
    )
    return {"funding_sources": items, "count": len(items), "organization_id": org_id}


def get_funding_source(client: SnapchatApiClient, funding_id: str) -> dict[str, Any]:
    body, _ = client.get(f"fundingsources/{funding_id}")
    return body


def list_billing_centers(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/billingcenters", "billingcenters"
    )
    return {"billing_centers": items, "count": len(items), "organization_id": org_id}


def get_billing_center(client: SnapchatApiClient, billing_id: str) -> dict[str, Any]:
    body, _ = client.get(f"billingcenters/{billing_id}")
    return body


def list_ad_account_roles(client: SnapchatApiClient, ad_account_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/roles", "roles"
    )
    return {"roles": items, "count": len(items), "ad_account_id": ad_account_id}


def create_ad_account(
    client: SnapchatApiClient,
    organization_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"create ad account '{payload.get('name', '?')}' under org {organization_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"organizations/{organization_id}/adaccounts",
        json_body={"adaccounts": [payload]},
    )
    return body


def update_ad_account(
    client: SnapchatApiClient,
    organization_id: str,
    ad_account_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = ad_account_id
    if not execute:
        return format_preview(
            f"update ad account {ad_account_id}",
            account_label,
            proposed_state=fields,
        )
    body, _ = client.put(
        f"organizations/{organization_id}/adaccounts",
        json_body={"adaccounts": [payload]},
    )
    return body


def list_phone_numbers(
    client: SnapchatApiClient, ad_account_id: str
) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/phone_numbers", "phone_numbers"
    )
    return {
        "phone_numbers": items,
        "count": len(items),
        "ad_account_id": ad_account_id,
    }


def assign_ad_account_role(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"assign role on ad account {ad_account_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/roles",
        json_body={"roles": [payload]},
    )
    return body


def remove_role(
    client: SnapchatApiClient,
    role_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"remove role {role_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"roles/{role_id}")
    return body


def health_check(
    client: SnapchatApiClient, ad_account_id: str
) -> dict[str, Any]:
    """Confirm we can reach the API and the ad account is accessible."""
    try:
        acct = get_ad_account(client, ad_account_id)
        return {
            "healthy": True,
            "ad_account_id": ad_account_id,
            "name": acct.get("name"),
            "status": acct.get("status"),
            "currency": acct.get("currency"),
            "timezone": acct.get("timezone"),
        }
    except Exception as e:
        return {
            "healthy": False,
            "ad_account_id": ad_account_id,
            "error": str(e),
        }
