"""The catalog feed builder never publishes a partial or altered feed."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_builder():
    path = Path(__file__).resolve().parents[2] / "warehouse" / "refresh_catalog_feed.py"
    spec = importlib.util.spec_from_file_location("catalog_feed_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRODUCT = {
    "external_id": "1001",
    "item_group_id": "100",
    "title": "Test item",
    "description": "Test",
    "link": "https://example.myshopify.com/products/test?variant=1001",
    "image_link": "https://cdn.shopify.com/s/files/test.png?v=123",
    "availability": "out of stock",
    "price": {"value": "90.00", "currency": "USD"},
    "brand": "Test",
}


def configure(monkeypatch, module, pages):
    calls = []
    remaining = list(pages)

    def post(path, json_body=None, params=None, timeout=None):
        calls.append((path, params, json_body))
        return remaining.pop(0), {}

    client = SimpleNamespace(post=post, close=lambda: None)
    monkeypatch.setattr(module, "load_config", lambda: None)
    monkeypatch.setattr(module, "get_account", lambda *a: SimpleNamespace(load_token=lambda: "test"))
    monkeypatch.setattr(module, "make_refresh_callback", lambda *a: None)
    monkeypatch.setattr(module, "SnapchatApiClient", lambda *a, **kw: client)
    return calls


def page(products, total, next_cursor=None):
    body = {
        "summary": {"total_products": {"count": str(total)}},
        "products": [{"product": p} for p in products],
        "paging": {},
    }
    if next_cursor:
        body["paging"]["next_cursor"] = next_cursor
    return body


def argv(target):
    return ["--source-catalog", "cat1", "--output", str(target), "--store-domain", "example.com"]


def test_partial_source_read_keeps_previous_feed(monkeypatch, tmp_path):
    module = load_builder()
    target = tmp_path / "feed.csv"
    target.write_text("last known complete feed")
    configure(monkeypatch, module, [page([PRODUCT], total=2)])
    assert module.main(argv(target)) == 1
    assert target.read_text() == "last known complete feed"


def test_duplicate_ids_are_refused(monkeypatch, tmp_path):
    module = load_builder()
    target = tmp_path / "feed.csv"
    configure(monkeypatch, module, [page([PRODUCT, PRODUCT], total=2)])
    assert module.main(argv(target)) == 1
    assert not target.exists()


def test_complete_feed_preserves_variant_stock_price_and_query(monkeypatch, tmp_path):
    module = load_builder()
    target = tmp_path / "feed.csv"
    second = dict(PRODUCT, external_id="1002", image_link="https://img.example.com/a.png")
    calls = configure(
        monkeypatch, module, [page([PRODUCT], total=2, next_cursor="c2"), page([second], total=2)]
    )
    assert module.main(argv(target)) == 0
    assert [c[0] for c in calls] == ["catalogs/cat1/products/search"] * 2
    assert calls[1][1] == {"cursor": "c2"}
    rows = list(csv.DictReader(target.open()))
    assert (rows[0]["id"], rows[0]["item_group_id"], rows[0]["availability"], rows[0]["price"]) == (
        "1001", "100", "out of stock", "90.00 USD",
    )
    assert rows[0]["link"] == "https://example.com/products/test?variant=1001"
    assert rows[0]["image_link"] == "https://cdn.shopify.com/s/files/test.png?v=123&width=1000&format=jpg"
    assert rows[1]["image_link"] == "https://img.example.com/a.png"
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_wrong_currency_is_refused(monkeypatch, tmp_path):
    module = load_builder()
    target = tmp_path / "feed.csv"
    other = dict(PRODUCT, price={"value": "90.00", "currency": "CAD"})
    configure(monkeypatch, module, [page([other], total=1)])
    assert module.main(argv(target)) == 1
    assert not target.exists()


def test_feed_row_rejects_unknown_availability():
    module = load_builder()
    with pytest.raises(module.FeedError, match="availability"):
        module.feed_row(
            dict(PRODUCT, availability="maybe"),
            store_domain="example.com",
            currency="USD",
            image_width=1000,
        )
