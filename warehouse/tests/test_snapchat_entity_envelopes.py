from unittest import TestCase, mock

from warehouse import sync_snapchat_daily as daily


class EntityEnvelopeTests(TestCase):
    def test_provider_envelopes_preserve_metric_lineage(self):
        payloads = [
            {"ads": [{"sub_request_status": "SUCCESS", "ad":
                       {"id": "ad-1", "ad_squad_id": "squad-1", "name": "Ad"}}]},
            {"ad_squads": [{"sub_request_status": "SUCCESS", "adsquad":
                              {"id": "squad-1", "campaign_id": "campaign-1", "name": "Squad"}}]},
            {"campaigns": [{"sub_request_status": "SUCCESS", "campaign":
                              {"id": "campaign-1", "name": "Campaign"}}]},
        ]
        with mock.patch.object(daily, "_run_cli_json", side_effect=payloads):
            maps = daily.fetch_entity_maps()
        rows = daily.build_rows([{"date": "2026-09-14", "ad_id": "ad-1",
                                  "stats": {"spend": 1000000}}], *maps)
        self.assertEqual(rows[0]["campaign_id"], "campaign-1")
        self.assertEqual(rows[0]["ad_squad_id"], "squad-1")
        self.assertEqual(rows[0]["ad_name"], "Ad")

    def test_flat_cli_shape_remains_supported(self):
        row = {"id": "ad-1", "ad_squad_id": "squad-1"}
        self.assertEqual(daily._unwrap_entity_list({"ads": [row]}, "ads"), [row])

    def test_failed_or_malformed_entities_do_not_silently_drop(self):
        for row in [{"sub_request_status": "ERROR", "ad": {"id": "a"}},
                    {"ad": {}}, None]:
            with self.subTest(row=row), self.assertRaises(daily.SyncError):
                daily._unwrap_entity_list({"ads": [row]}, "ads")
