"""Dynamic templates are ad-account scoped in the Snap API."""

from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from snapchat_ads_cli import cli as cli_mod
from snapchat_ads_cli.tools import catalog


class _Client:
    def __init__(self):
        self.calls = []

    def collect_paginated(self, path, array_key, params=None):
        self.calls.append(("GET", path, array_key))
        return [{"id": "tmpl1"}]

    def post(self, path, json_body=None, params=None, timeout=None):
        self.calls.append(("POST", path, json_body))
        return {"request_status": "SUCCESS"}, {}


def test_list_reads_the_ad_account_path():
    client = _Client()
    out = catalog.list_dynamic_templates(client, "acct1")
    assert client.calls == [
        ("GET", "adaccounts/acct1/dynamic_templates", "dynamic_templates")
    ]
    assert out == {
        "dynamic_templates": [{"id": "tmpl1"}],
        "count": 1,
        "ad_account_id": "acct1",
    }


def test_create_previews_without_execute_and_fills_renderer_urls():
    client = _Client()
    out = catalog.create_dynamic_template(
        client,
        "acct1",
        "default",
        payload={"name": "Auto", "layout": "AUTOMATIC", "text_fields": ["title"]},
    )
    assert client.calls == []
    assert out["status"] == "preview"
    proposed = out["proposed"]
    assert proposed["ad_account_id"] == "acct1"
    assert proposed["ios_url"] == catalog.DYNAMIC_TEMPLATE_RENDERER_URL
    assert proposed["android_url"] == catalog.DYNAMIC_TEMPLATE_RENDERER_URL


def test_create_execute_posts_one_template_and_keeps_caller_urls():
    client = _Client()
    catalog.create_dynamic_template(
        client,
        "acct1",
        "default",
        payload={"name": "Auto", "ios_url": "https://example.com/ios"},
        execute=True,
    )
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "adaccounts/acct1/dynamic_templates")
    [sent] = body["dynamic_templates"]
    assert sent["ios_url"] == "https://example.com/ios"
    assert sent["android_url"] == catalog.DYNAMIC_TEMPLATE_RENDERER_URL


def test_cli_list_defaults_to_the_configured_ad_account(monkeypatch):
    client = _Client()
    client.close = lambda: None
    account = SimpleNamespace(ad_account_id="configured-acct")
    monkeypatch.setattr(
        cli_mod,
        "_resolve_client",
        lambda _ctx, require_account=True: (object(), account, client),
    )
    result = CliRunner().invoke(cli_mod.cli, ["catalog", "dynamic-templates"])
    assert result.exit_code == 0, result.output
    assert client.calls[0][1] == "adaccounts/configured-acct/dynamic_templates"
    assert json.loads(result.output)["ad_account_id"] == "configured-acct"
