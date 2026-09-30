"""`audit changelog` reads the provider's "changelogs" key."""

from __future__ import annotations

from snapchat_ads_cli.tools import audit_logs


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_paginated(self, path, params=None, max_pages=100):
        self.calls.append((path, dict(params or {})))
        yield from self.pages


def _entry(entry_id, action):
    return {
        "sub_request_status": "SUCCESS",
        "changelog": {"id": entry_id, "action": action},
    }


def test_changelog_reads_and_unwraps_the_changelogs_key_across_pages():
    client = FakeClient(
        [
            {"changelogs": [_entry("1", "UPDATED")]},
            {"changelogs": [_entry("2", "CREATED")]},
        ]
    )
    out = audit_logs.changelog(client, entity_type="ad_squad", entity_id="s1", limit=50)
    assert client.calls == [("adsquads/s1/external_changelogs", {"limit": 50})]
    assert out["count"] == 2
    assert [c["id"] for c in out["changelogs"]] == ["1", "2"]


def test_changelog_still_accepts_the_path_named_key():
    client = FakeClient([{"external_changelogs": [_entry("9", "UPDATED")]}])
    out = audit_logs.changelog(client, entity_type="ad", entity_id="a1")
    assert [c["id"] for c in out["changelogs"]] == ["9"]


def test_changelog_stops_at_the_limit():
    client = FakeClient(
        [
            {"changelogs": [_entry("1", "UPDATED"), _entry("2", "UPDATED")]},
            {"changelogs": [_entry("3", "UPDATED")]},
        ]
    )
    out = audit_logs.changelog(client, entity_type="campaign", entity_id="c1", limit=1)
    assert out["count"] == 1
    assert out["changelogs"] == [{"id": "1", "action": "UPDATED"}]


def test_changelog_rejects_unknown_entity_types():
    out = audit_logs.changelog(FakeClient([]), entity_type="pixel", entity_id="p1")
    assert "error" in out
