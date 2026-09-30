"""Product catalog endpoints (Collection Ads / dynamic catalog).

Light surface for now -- listing/getting catalogs and product sets. Catalog
ingest goes through Snap Business Manager; we expose what the API documents.
"""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def list_catalogs(client: SnapchatApiClient, org_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{org_id}/catalogs", "catalogs"
    )
    return {"catalogs": items, "count": len(items), "organization_id": org_id}


def get_catalog(client: SnapchatApiClient, catalog_id: str) -> dict[str, Any]:
    body, _ = client.get(f"catalogs/{catalog_id}")
    return body


def list_product_sets(client: SnapchatApiClient, catalog_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"catalogs/{catalog_id}/product_sets", "product_sets"
    )
    return {"product_sets": items, "count": len(items), "catalog_id": catalog_id}


def get_product_set(client: SnapchatApiClient, product_set_id: str) -> dict[str, Any]:
    body, _ = client.get(f"product_sets/{product_set_id}")
    return body


def create_catalog(
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
            f"create catalog '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"organizations/{organization_id}/catalogs",
        json_body={"catalogs": [payload]},
    )
    return body


def create_product_set(
    client: SnapchatApiClient,
    catalog_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"create product set '{payload.get('name', '?')}' in catalog {catalog_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"catalogs/{catalog_id}/product_sets",
        json_body={"product_sets": [payload]},
    )
    return body


def update_product_set(
    client: SnapchatApiClient,
    catalog_id: str,
    product_set_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = product_set_id
    if not execute:
        return format_preview(
            f"update product set {product_set_id}",
            account_label,
            proposed_state=fields,
        )
    body, _ = client.put(
        f"catalogs/{catalog_id}/product_sets",
        json_body={"product_sets": [payload]},
    )
    return body


# Snap's V2 renderer; the API requires both URLs on a new dynamic template.
DYNAMIC_TEMPLATE_RENDERER_URL = (
    "https://ads-interfaces.sc-cdn.net/adformats/templates/V2/index.html"
)


def list_dynamic_templates(
    client: SnapchatApiClient, ad_account_id: str
) -> dict[str, Any]:
    """GET /v1/adaccounts/{id}/dynamic_templates.

    Dynamic templates belong to the ad account, not the catalog. The
    catalog-scoped path this command used before answers E3003 ("Resource
    can not be found") for every catalog.
    """
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/dynamic_templates", "dynamic_templates"
    )
    return {
        "dynamic_templates": items,
        "count": len(items),
        "ad_account_id": ad_account_id,
    }


def create_dynamic_template(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    """POST /v1/adaccounts/{id}/dynamic_templates.

    Snap requires name, layout (AUTOMATIC, FILL_WIDTH, FILL_HEIGHT, FIT,
    HEADER or TILT; CAROUSEL or SLIDESHOW for Collection ads), text_fields
    (at most two of title, price, descriptionTruncated, salePrice,
    availability, brand, ...) and the two renderer URLs, which default to
    Snap's V2 template.
    """
    from ..safety import format_preview

    payload = dict(payload)
    payload.setdefault("ad_account_id", ad_account_id)
    payload.setdefault("ios_url", DYNAMIC_TEMPLATE_RENDERER_URL)
    payload.setdefault("android_url", DYNAMIC_TEMPLATE_RENDERER_URL)
    if not execute:
        return format_preview(
            f"create dynamic template '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/dynamic_templates",
        json_body={"dynamic_templates": [payload]},
    )
    return body


def get_dynamic_template(
    client: SnapchatApiClient, dynamic_template_id: str
) -> dict[str, Any]:
    body, _ = client.get(f"dynamic_templates/{dynamic_template_id}")
    return body


def list_product_feeds(client: SnapchatApiClient, catalog_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"catalogs/{catalog_id}/product_feeds", "product_feeds"
    )
    return {"product_feeds": items, "count": len(items), "catalog_id": catalog_id}


def get_product_feed(client: SnapchatApiClient, feed_id: str) -> dict[str, Any]:
    body, _ = client.get(f"product_feeds/{feed_id}")
    return body


def create_product_feed(
    client: SnapchatApiClient,
    catalog_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"create product feed '{payload.get('name', '?')}' in catalog {catalog_id}",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"catalogs/{catalog_id}/product_feeds",
        json_body={"product_feeds": [payload]},
    )
    return body


def delete_product_feed(
    client: SnapchatApiClient,
    feed_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    from ..safety import format_preview

    if not execute:
        return format_preview(
            f"delete product feed {feed_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"product_feeds/{feed_id}")
    return body
