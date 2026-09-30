"""Northbeam Snapchat tagging defaults on creative builders."""
from snapchat_ads_cli.tools import creatives as c


class _FakeClient:
    def __init__(self):
        self.posted = []

    def post(self, path, json_body=None, **kw):
        self.posted.append((path, json_body))
        return {"creatives": [{"sub_request_status": "SUCCESS", "creative": {"id": "new"}}]}, {}


def test_with_northbeam_params_appends_and_is_idempotent():
    tagged = c.with_northbeam_params("https://example.com/p")
    assert tagged.startswith("https://example.com/p?nbt=nb:snapchat:{{site_source_name}}:{{campaign.id}}:{{adSet.id}}:{{ad.id}}")
    assert c.with_northbeam_params(tagged) == tagged
    assert c.with_northbeam_params("https://example.com/p?x=1").startswith("https://example.com/p?x=1&nbt=")
    assert c.with_northbeam_params(None) is None


def _preview_payload(result):
    return result.get("proposed") or result.get("proposed_state") or result


def test_create_web_view_tags_by_default_and_can_opt_out():
    r = c.create_web_view(_FakeClient(), "acct", "default", name="n", headline="h", brand_name="b",
                          top_snap_media_id="m", url="https://example.com/p")
    p = _preview_payload(r)
    assert "nbt=nb:snapchat:" in p["web_view_properties"]["url"]
    assert p["url_macro_parameters"] == c.NORTHBEAM_SNAP_URL_PARAMS
    r2 = c.create_web_view(_FakeClient(), "acct", "default", name="n", headline="h", brand_name="b",
                           top_snap_media_id="m", url="https://example.com/p", northbeam_tags=False)
    p2 = _preview_payload(r2)
    assert p2["web_view_properties"]["url"] == "https://example.com/p"
    assert "url_macro_parameters" not in p2


def test_create_collection_tags_fallback_url():
    r = c.create_collection(_FakeClient(), "acct", "default", name="n", headline="h", brand_name="b",
                            top_snap_media_id="m", interaction_zone_id="iz", fallback_url="https://example.com/p")
    p = _preview_payload(r)
    assert "nbt=nb:snapchat:" in p["collection_properties"]["web_view_properties"]["url"]
    assert p["url_macro_parameters"] == c.NORTHBEAM_SNAP_URL_PARAMS


def test_env_turns_the_default_off_and_an_explicit_flag_still_wins(monkeypatch):
    kwargs = dict(name="n", headline="h", brand_name="b", top_snap_media_id="m",
                  url="https://example.com/p?utm_source=own")
    for value in ("0", "false", "No", "OFF"):
        monkeypatch.setenv("SNAPCHAT_NORTHBEAM_TAGS", value)
        assert c.northbeam_tags_default() is False
        p = _preview_payload(c.create_web_view(_FakeClient(), "acct", "default", **kwargs))
        assert p["web_view_properties"]["url"] == "https://example.com/p?utm_source=own"
        assert "url_macro_parameters" not in p
    forced = _preview_payload(
        c.create_web_view(_FakeClient(), "acct", "default", northbeam_tags=True, **kwargs)
    )
    assert "nbt=nb:snapchat:" in forced["web_view_properties"]["url"]
    collection = _preview_payload(
        c.create_collection(_FakeClient(), "acct", "default", name="n", headline="h",
                            brand_name="b", top_snap_media_id="m", interaction_zone_id="iz",
                            fallback_url="https://example.com/p")
    )
    assert collection["collection_properties"]["web_view_properties"]["url"] == "https://example.com/p"


def test_default_stays_on_when_the_variable_is_unset_or_truthy(monkeypatch):
    monkeypatch.delenv("SNAPCHAT_NORTHBEAM_TAGS", raising=False)
    assert c.northbeam_tags_default() is True
    monkeypatch.setenv("SNAPCHAT_NORTHBEAM_TAGS", "1")
    assert c.northbeam_tags_default() is True


def test_cli_leaves_the_choice_to_the_env_unless_a_flag_is_given(monkeypatch):
    import json
    from types import SimpleNamespace

    from click.testing import CliRunner

    from snapchat_ads_cli import cli as cli_mod

    client = _FakeClient()
    client.close = lambda: None
    account = SimpleNamespace(ad_account_id="acct")
    monkeypatch.setattr(
        cli_mod, "_resolve_client", lambda _ctx, require_account=True: (object(), account, client)
    )
    base = ["creative", "create-web-view", "--name", "n", "--headline", "h", "--brand-name", "b",
            "--top-snap-media-id", "m", "--url", "https://example.com/p"]

    def proposed_url(extra):
        result = CliRunner().invoke(cli_mod.cli, base + extra)
        assert result.exit_code == 0, result.output
        return _preview_payload(json.loads(result.output))["web_view_properties"]["url"]

    monkeypatch.setenv("SNAPCHAT_NORTHBEAM_TAGS", "0")
    assert proposed_url([]) == "https://example.com/p"
    assert "nbt=nb:snapchat:" in proposed_url(["--northbeam-tags"])
    monkeypatch.delenv("SNAPCHAT_NORTHBEAM_TAGS")
    assert "nbt=nb:snapchat:" in proposed_url([])
    assert proposed_url(["--no-northbeam-tags"]) == "https://example.com/p"
