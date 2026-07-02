"""Organizations + member/role discovery."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def list_organizations(client: SnapchatApiClient, with_ad_accounts: bool = False) -> dict[str, Any]:
    params = {"with_ad_accounts": "true"} if with_ad_accounts else None
    items = client.collect_paginated("me/organizations", "organizations", params=params)
    return {"organizations": items, "count": len(items)}


def get_organization(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    body, _ = client.get(f"organizations/{org_id}")
    orgs = body.get("organizations") or []
    if orgs:
        first = orgs[0]
        if isinstance(first, dict) and "organization" in first:
            return first["organization"]
        return first
    return body


def list_ad_accounts(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/adaccounts", "adaccounts"
    )
    return {"ad_accounts": items, "count": len(items), "organization_id": org_id}


def list_members(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/members", "members"
    )
    return {"members": items, "count": len(items), "organization_id": org_id}


def list_roles(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/roles", "roles"
    )
    return {"roles": items, "count": len(items), "organization_id": org_id}


def assign_org_role(
    client: SnapchatApiClient,
    org_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"assign org role under org {org_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"organizations/{org_id}/roles",
        json_body={"roles": [payload]},
    )
    return body


def member_roles(client: SnapchatApiClient, member_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"members/{member_id}/roles", "roles"
    )
    return {"roles": items, "count": len(items), "member_id": member_id}


def invite_member(
    client: SnapchatApiClient,
    org_id: str,
    account_label: str,
    *,
    email: str,
    display_name: str | None = None,
    member_role: str | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    payload: dict[str, Any] = {"email": email}
    if display_name:
        payload["display_name"] = display_name
    if member_role:
        payload["member_role"] = member_role

    if not execute:
        return format_preview(
            f"invite '{email}' to org {org_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"organizations/{org_id}/members",
        json_body={"members": [payload]},
    )
    return body


def revoke_member(
    client: SnapchatApiClient,
    org_id: str,
    member_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"revoke member {member_id} from org {org_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"organizations/{org_id}/members/{member_id}")
    return body
