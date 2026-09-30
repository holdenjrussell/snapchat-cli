#!/usr/bin/env python3
"""Build a Snap product-feed CSV from an existing Snap catalog.

Use it when a store integration (for example the Shopify app) keeps one Snap
catalog in sync and you want to serve that catalog as a feed file, e.g. for
a second catalog that ingests by URL. The builder:

- keeps variant IDs, item groups, stock, prices and link query strings;
- points product links at the storefront domain;
- asks the Shopify CDN for smaller JPEG images (other image hosts pass
  through unchanged);
- publishes only a complete read: if Snap reports more products than it
  returned, or IDs repeat, the previous feed stays in place and the run
  exits non-zero.

Usage (the CLI's virtualenv provides snapchat_ads_cli):

    cli/.venv/bin/python warehouse/refresh_catalog_feed.py \
        --source-catalog <CATALOG_ID> --store-domain <STORE_DOMAIN> \
        --output /path/to/feed.csv [--currency USD] [--account default]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from snapchat_ads_cli.api_client import SnapchatApiClient
from snapchat_ads_cli.auth import make_refresh_callback
from snapchat_ads_cli.config import get_account, load_config

FIELDS = [
    "id", "item_group_id", "title", "description", "link", "image_link",
    "availability", "price", "sale_price", "brand", "size", "color", "gtin",
    "mpn", "google_product_category", "product_type", "condition",
]
AVAILABILITY = {
    "in stock", "out of stock", "preorder", "available for order", "discontinued",
}
SHOPIFY_CDN = "cdn.shopify.com"
PAGE_LIMIT = 200
MAX_PAGES = 1000


class FeedError(RuntimeError):
    """The catalog read or a product row is not safe to publish."""


def read_catalog(client: Any, catalog_id: str) -> list[dict[str, Any]]:
    """Every product in the catalog, or FeedError if the read is incomplete."""
    products: list[dict[str, Any]] = []
    expected_total: int | None = None
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        body, _headers = client.post(
            f"catalogs/{catalog_id}/products/search",
            params={"cursor": cursor} if cursor else None,
            json_body={"limit": PAGE_LIMIT},
        )
        count = ((body.get("summary") or {}).get("total_products") or {}).get("count")
        if expected_total is None and count is not None:
            expected_total = int(count)
        for entry in body.get("products") or []:
            product = entry.get("product") if isinstance(entry, dict) else None
            if isinstance(product, dict):
                products.append(product)
        cursor = (body.get("paging") or {}).get("next_cursor")
        if not cursor:
            break
    else:
        raise FeedError(f"catalog read did not finish within {MAX_PAGES} pages")

    ids = [p.get("external_id") for p in products]
    if not products or None in ids or len(set(ids)) != len(ids):
        raise FeedError("missing, blank or duplicate product ids in the catalog read")
    if expected_total is None or len(products) != expected_total:
        raise FeedError(
            f"Incomplete catalog read: {len(products)}/{expected_total}; "
            "keeping the previous feed"
        )
    return products


def _money(value: Any) -> str:
    if not value:
        return ""
    return f"{value['value']} {value['currency']}"


def _image(url: str, width: int) -> str:
    parts = urlsplit(url)
    if parts.netloc != SHOPIFY_CDN:
        return url
    query = dict(parse_qsl(parts.query))
    query.update(width=str(width), format="jpg")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def feed_row(
    product: dict[str, Any], *, store_domain: str, currency: str, image_width: int
) -> dict[str, str]:
    row = {field: product.get(field, "") for field in FIELDS}
    row["id"] = product["external_id"]
    row["image_link"] = _image(product.get("image_link") or "", image_width)
    link = urlsplit(product.get("link") or "")
    row["link"] = urlunsplit(("https", store_domain, link.path, link.query, ""))
    row["price"] = _money(product.get("price"))
    row["sale_price"] = _money(product.get("sale_price"))
    if not row["price"].endswith(f" {currency}"):
        raise FeedError(f"product {row['id']} is not priced in {currency}: {row['price']!r}")
    if row["availability"] not in AVAILABILITY:
        raise FeedError(f"product {row['id']} has unknown availability {row['availability']!r}")
    return row


def write_atomic(rows: list[dict[str, str]], dest: Path) -> None:
    """Write next to the target, then rename, so readers never see half a file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=dest.parent, delete=False, newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        temp = Path(handle.name)
    temp.chmod(0o644)
    temp.replace(dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-catalog", required=True, help="Snap catalog ID to read")
    parser.add_argument("--output", required=True, help="CSV path to publish")
    parser.add_argument("--store-domain", required=True, help="storefront host for product links")
    parser.add_argument("--currency", default="USD", help="currency every price must carry")
    parser.add_argument("--image-width", type=int, default=1000)
    parser.add_argument(
        "--account",
        default=os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default"),
        help="account key in accounts.toml (default: $SNAPCHAT_ADS_ACCOUNT or 'default')",
    )
    args = parser.parse_args(argv)

    config = load_config()
    account = get_account(config, args.account)
    client = SnapchatApiClient(
        account.load_token(), refresh_callback=make_refresh_callback(config, account)
    )
    try:
        products = read_catalog(client, args.source_catalog)
        rows = [
            feed_row(
                p,
                store_domain=args.store_domain,
                currency=args.currency,
                image_width=args.image_width,
            )
            for p in products
        ]
        write_atomic(rows, Path(args.output))
    except FeedError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print(json.dumps({"status": "ok", "products": len(rows), "output": args.output}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
