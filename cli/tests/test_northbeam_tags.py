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
    r = c.create_web_view(_FakeClient(), "acct", "cgk", name="n", headline="h", brand_name="b",
                          top_snap_media_id="m", url="https://example.com/p")
    p = _preview_payload(r)
    assert "nbt=nb:snapchat:" in p["web_view_properties"]["url"]
    assert p["url_macro_parameters"] == c.NORTHBEAM_SNAP_URL_PARAMS
    r2 = c.create_web_view(_FakeClient(), "acct", "cgk", name="n", headline="h", brand_name="b",
                           top_snap_media_id="m", url="https://example.com/p", northbeam_tags=False)
    p2 = _preview_payload(r2)
    assert p2["web_view_properties"]["url"] == "https://example.com/p"
    assert "url_macro_parameters" not in p2


def test_create_collection_tags_fallback_url():
    r = c.create_collection(_FakeClient(), "acct", "cgk", name="n", headline="h", brand_name="b",
                            top_snap_media_id="m", interaction_zone_id="iz", fallback_url="https://example.com/p")
    p = _preview_payload(r)
    assert "nbt=nb:snapchat:" in p["collection_properties"]["web_view_properties"]["url"]
    assert p["url_macro_parameters"] == c.NORTHBEAM_SNAP_URL_PARAMS
