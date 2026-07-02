import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snapchat_ads_cli import config as config_mod
from snapchat_ads_cli.tools import ads, adsquads, creatives, media, reports


class FakeClient:
    def __init__(self):
        self.calls = []
        self.get_responses = {}
        self.post_responses = []
        self.put_responses = []

    def get(self, path, params=None, timeout=None):
        self.calls.append(("GET", path, params))
        return self.get_responses.get(path, {}), {}

    def collect_paginated(self, path, array_key, params=None):
        self.calls.append(("GET", path, params))
        return []

    def post(self, path, json_body=None, params=None, timeout=None):
        self.calls.append(("POST", path, json_body, params))
        if self.post_responses:
            return self.post_responses.pop(0), {}
        return {}, {}

    def put(self, path, json_body=None, params=None, timeout=None):
        self.calls.append(("PUT", path, json_body, params))
        if self.put_responses:
            return self.put_responses.pop(0), {}
        return {"request_status": "SUCCESS"}, {}

    def post_multipart(self, path, data, files, params=None, timeout=None):
        self.calls.append(("POST_MULTIPART", path, data, params, sorted(files)))
        if self.post_responses:
            return self.post_responses.pop(0), {}
        return {}, {}


class ReportingTests(unittest.TestCase):
    def test_async_submit_uses_stats_endpoint_with_async_params(self):
        client = FakeClient()

        reports.async_submit(
            client,
            entity_type="ad_account",
            entity_id="acct1",
            granularity="DAY",
            start_time="2026-04-01T00:00:00Z",
            end_time="2026-04-02T00:00:00Z",
            fields=["spend", "impressions"],
            breakdown="ad",
            async_format="csv",
            extra_params={"omit_empty": "true"},
        )

        self.assertEqual(client.calls[0][0], "GET")
        self.assertEqual(client.calls[0][1], "adaccounts/acct1/stats")
        self.assertEqual(
            client.calls[0][2],
            {
                "granularity": "DAY",
                "start_time": "2026-04-01T00:00:00Z",
                "end_time": "2026-04-02T00:00:00Z",
                "fields": "spend,impressions",
                "breakdown": "ad",
                "async": "true",
                "async_format": "csv",
                "omit_empty": "true",
            },
        )

    def test_async_status_uses_entity_stats_report_endpoint(self):
        client = FakeClient()

        reports.async_status(
            client,
            entity_type="campaign",
            entity_id="camp1",
            report_run_id="ASYNC_STATS:abc",
        )

        self.assertEqual(
            client.calls[0],
            ("GET", "campaigns/camp1/stats_report", {"report_run_id": "ASYNC_STATS:abc"}),
        )

    def test_lead_gen_report_submit_and_status_use_leads_report_endpoint(self):
        client = FakeClient()

        reports.lead_gen_submit(
            client,
            ad_account_id="acct1",
            start_time="2026-04-01T00:00:00Z",
            end_time="2026-04-02T00:00:00Z",
        )
        reports.lead_gen_status(
            client,
            ad_account_id="acct1",
            report_run_id="ASYNC_STATS:lead",
        )

        self.assertEqual(
            client.calls[0],
            (
                "POST",
                "adaccounts/acct1/leads_report",
                None,
                {
                    "async_format": "csv",
                    "start_time": "2026-04-01T00:00:00Z",
                    "end_time": "2026-04-02T00:00:00Z",
                },
            ),
        )
        self.assertEqual(
            client.calls[1],
            (
                "GET",
                "adaccounts/acct1/leads_report",
                {"report_run_id": "ASYNC_STATS:lead"},
            ),
        )


class MediaUploadTests(unittest.TestCase):
    def test_chunked_upload_uses_snap_returned_paths_and_upload_id(self):
        client = FakeClient()
        client.post_responses = [
            {
                "upload_id": "upload1",
                "add_path": "/us/v1/media/media1/multipart-upload-v2?action=ADD",
                "finalize_path": "/v1/media/media1/multipart-upload-v2?action=FINALIZE",
            },
            {"part": "ok"},
            {"final": "ok"},
        ]

        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"x" * 10)
            path = fh.name
        try:
            media._chunked_upload(client, "media1", Path(path), chunk_size=20)
        finally:
            os.unlink(path)

        self.assertEqual(
            client.calls[0],
            (
                "POST_MULTIPART",
                "media/media1/multipart-upload-v2",
                {"file_name": Path(path).name, "file_size": "10", "number_of_parts": "1"},
                {"action": "INIT"},
                [],
            ),
        )
        self.assertEqual(client.calls[1][1], "/us/v1/media/media1/multipart-upload-v2?action=ADD")
        self.assertEqual(client.calls[1][2]["upload_id"], "upload1")
        self.assertEqual(client.calls[2][1], "/v1/media/media1/multipart-upload-v2?action=FINALIZE")
        self.assertEqual(client.calls[2][2], {"upload_id": "upload1"})


class CreativeBuilderTests(unittest.TestCase):
    def test_web_view_builder_accepts_profile_cta_and_url(self):
        client = FakeClient()

        result = creatives.create_web_view(
            client,
            "acct1",
            "default",
            name="Launch",
            headline="Hotel sheets",
            brand_name="My Brand",
            top_snap_media_id="media1",
            url="https://example.com/products/x",
            call_to_action="SHOP_NOW",
            profile_id="profile1",
            execute=False,
        )

        proposed = result["proposed"]
        self.assertEqual(proposed["web_view_properties"]["url"], "https://example.com/products/x")
        self.assertEqual(proposed["call_to_action"], "SHOP_NOW")
        self.assertEqual(proposed["profile_properties"], {"profile_id": "profile1"})

    def test_lead_generation_builder_uses_documented_form_id_field(self):
        client = FakeClient()

        result = creatives.create_lead_generation(
            client,
            "acct1",
            "default",
            name="Lead",
            headline="Get a quote",
            brand_name="My Brand",
            top_snap_media_id="media1",
            lead_form_id="form1",
            profile_id="profile1",
            call_to_action="REQUEST_QUOTE",
            execute=False,
        )

        proposed = result["proposed"]
        self.assertEqual(proposed["lead_generation_form_id"], "form1")
        self.assertNotIn("lead_generation_properties", proposed)
        self.assertEqual(proposed["profile_properties"], {"profile_id": "profile1"})

    def test_lens_web_view_builder_marks_lens_product_and_url(self):
        client = FakeClient()

        result = creatives.create_lens_web_view(
            client,
            "acct1",
            "default",
            name="Lens",
            headline="Try it",
            brand_name="My Brand",
            top_snap_media_id="lens_media",
            url="https://example.com/lens",
            profile_id="profile1",
            call_to_action="SHOP_NOW",
            execute=False,
        )

        proposed = result["proposed"]
        self.assertEqual(proposed["type"], "LENS_WEB_VIEW")
        self.assertEqual(proposed["ad_product"], "LENS")
        self.assertEqual(proposed["web_view_properties"]["url"], "https://example.com/lens")
        self.assertEqual(proposed["profile_properties"], {"profile_id": "profile1"})


class FullObjectUpdateTests(unittest.TestCase):
    def test_adsquad_reads_request_placement_v2(self):
        client = FakeClient()
        client.get_responses["adsquads/squad1"] = {
            "adsquads": [
                {
                    "adsquad": {
                        "id": "squad1",
                        "campaign_id": "camp1",
                        "placement_v2": {"config": "AUTOMATIC"},
                    }
                }
            ]
        }

        result = adsquads.get_ad_squad(client, "squad1")

        self.assertEqual(result["placement_v2"], {"config": "AUTOMATIC"})
        self.assertEqual(
            client.calls[0],
            ("GET", "adsquads/squad1", {"return_placement_v2": "true"}),
        )

    def test_adsquad_list_requests_placement_v2(self):
        client = FakeClient()

        adsquads.list_ad_squads(client, campaign_id="camp1")

        self.assertEqual(
            client.calls[0],
            (
                "GET",
                "campaigns/camp1/adsquads",
                {"limit": 100, "return_placement_v2": "true"},
            ),
        )

    def test_adsquad_update_merges_current_object_and_uses_campaign_endpoint(self):
        client = FakeClient()
        client.get_responses["adsquads/squad1"] = {
            "adsquads": [
                {
                    "adsquad": {
                        "id": "squad1",
                        "campaign_id": "camp1",
                        "name": "Old",
                        "type": "SNAP_ADS",
                        "status": "PAUSED",
                        "targeting": {"geos": [{"country_code": "us"}]},
                        "daily_budget_micro": 1000000,
                    }
                }
            ]
        }

        adsquads.update_ad_squad(
            client,
            "acct1",
            "squad1",
            "default",
            fields={"daily_budget_micro": 2000000},
            execute=True,
        )

        self.assertEqual(client.calls[-1][0], "PUT")
        self.assertEqual(client.calls[-1][1], "campaigns/camp1/adsquads")
        payload = client.calls[-1][2]["adsquads"][0]
        self.assertEqual(payload["name"], "Old")
        self.assertEqual(payload["targeting"], {"geos": [{"country_code": "us"}]})
        self.assertEqual(payload["daily_budget_micro"], 2000000)

    def test_ad_update_merges_current_object_before_put(self):
        client = FakeClient()
        client.get_responses["ads/ad1"] = {
            "ads": [
                {
                    "ad": {
                        "id": "ad1",
                        "ad_squad_id": "squad1",
                        "creative_id": "creative1",
                        "name": "Old",
                        "type": "SNAP_AD",
                        "status": "PAUSED",
                    }
                }
            ]
        }

        ads.update_ad(
            client,
            "squad1",
            "ad1",
            "default",
            fields={"status": "ACTIVE"},
            execute=True,
        )

        payload = client.calls[-1][2]["ads"][0]
        self.assertEqual(payload["creative_id"], "creative1")
        self.assertEqual(payload["type"], "SNAP_AD")
        self.assertEqual(payload["status"], "ACTIVE")


class ConfigEnvTests(unittest.TestCase):
    def test_load_env_files_prefers_override_then_config_dir_without_overriding_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir_env = home / ".config" / "snapchat-ads-cli" / ".env"
            config_dir_env.parent.mkdir(parents=True)
            config_dir_env.write_text(
                'SNAPCHAT_CLIENT_ID="config-dir-client"\n'
                'SNAPCHAT_CLIENT_SECRET="config-dir-secret"\n'
            )

            override_env = Path(tmp) / "override.env"
            override_env.write_text(
                'SNAPCHAT_CLIENT_ID="override-client"\n'
                'SNAPCHAT_REDIRECT_URI="https://localhost:8080/callback"\n'
            )

            with patch.object(config_mod.Path, "home", return_value=home), patch.dict(
                os.environ,
                {
                    "SNAPCHAT_CLIENT_ID": "shell-client",
                    "SNAPCHAT_ADS_ENV_FILE": str(override_env),
                },
                clear=True,
            ):
                config_mod.load_env_files()

                # Shell env always wins, even over the explicit override file.
                self.assertEqual(os.environ["SNAPCHAT_CLIENT_ID"], "shell-client")
                # SNAPCHAT_ADS_ENV_FILE is searched before the config dir .env.
                self.assertEqual(
                    os.environ["SNAPCHAT_REDIRECT_URI"],
                    "https://localhost:8080/callback",
                )
                # Vars only present in the config dir .env still get picked up.
                self.assertEqual(os.environ["SNAPCHAT_CLIENT_SECRET"], "config-dir-secret")

    def test_account_config_load_token_falls_back_to_env_next_to_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_dir = Path(tmp) / "profile"
            token_dir.mkdir(parents=True)
            (token_dir / ".env").write_text('SNAPCHAT_ACCESS_TOKEN="token-from-profile-env"\n')

            account = config_mod.AccountConfig(
                name="default",
                token_source=str(token_dir / "token.json"),
            )

            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(account.load_token(), "token-from-profile-env")


if __name__ == "__main__":
    unittest.main()
