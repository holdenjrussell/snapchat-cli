import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from reports import post_snapchat_hourly_heartbeat as heartbeat


@pytest.fixture(autouse=True)
def configured_snap_account(monkeypatch):
    monkeypatch.setenv("SNAPCHAT_AD_ACCOUNT_ID", "snap-account")


def _midnight_sample() -> dict:
    invalid = [{
        "id": f"ad-{index}",
        "name": f"Ad_{index}",
        "delivery_status": ["INVALID_NOT_DELIVERING"],
    } for index in range(10)]
    return {
        "report_windows": {
            "account_timezone": "America/Los_Angeles",
            "last24_start_pt": "2026-07-13T00:00:00-07:00",
            "current_hour_pt": "2026-07-14T00:00:00-07:00",
            "elapsed_hours": 24,
            "completed_hours": True,
        },
        "latest_hour": {
            "start_account_iso": "2026-07-13T23:00:00-07:00",
            "end_account_iso": "2026-07-14T00:00:00-07:00",
            "start_pt_iso": "2026-07-13T23:00:00-07:00",
            "end_pt_iso": "2026-07-14T00:00:00-07:00",
            "spend": 18.0,
        },
        "previous_hour": {
            "start_account_iso": "2026-07-13T22:00:00-07:00",
            "end_account_iso": "2026-07-13T23:00:00-07:00",
            "start_pt_iso": "2026-07-13T22:00:00-07:00",
            "end_pt_iso": "2026-07-13T23:00:00-07:00",
            "spend": 20.0,
        },
        "last_24": {
            "spend": 600.0,
            "revenue": 300.0,
            "roas": 0.5,
            "purchases": 5,
            "cpa": 120.0,
        },
        "today_account": {
            "spend": 0.0,
            "revenue": 0.0,
            "roas": None,
            "purchases": 0,
            "cpa": None,
            "impressions": 0,
            "swipes": 0,
            "ctr_pct": None,
        },
        "top_ads_24h": [
            {
                "id": "ad_top_1",
                "name": "Winner_creative_1",
                "spend": 140.0,
                "revenue": 210.0,
                "roas": 1.5,
                "purchases": 2,
                "cpa": 70.0,
            },
            {
                "id": "ad_top_2",
                "name": "Runner_up_2",
                "spend": 120.0,
                "revenue": 120.0,
                "roas": 1.0,
                "purchases": 1,
                "cpa": 120.0,
            },
        ],
        "invalid_active_ads_total": 129,
        "invalid_active_ads": invalid,
        "hourly_series": [],
        "delta_vs_previous_hour_pct": -10.0,
        "delta_vs_prior_23h_avg_pct": 5.0,
        "avg_prior_hour_spend": 17.14,
    }


def test_headline_uses_one_completed_rolling_window_even_when_today_is_empty():
    main, thread = heartbeat.build_messages(_midnight_sample())

    assert "$120.00 CPA across 24 completed hours ending 12:00 AM PT" in main
    assert "5 purchases" in main
    assert "n/a CPA" not in main
    assert thread.index("*Rolling 24 Completed-Hour Account Summary*") < thread.index(
        "*Today so far (America/Los_Angeles)*"
    )
    assert "• *CPA:* $120.00" in thread
    assert "129 status-ACTIVE ads" in thread
    assert "Sample shown below: 10 of 129" in thread
    assert (
        "<https://ads.snapchat.com/snap-account/ads/ad_top_1|"
        "Winner_creative_1 (ad_top_1)>"
    ) in thread
    assert (
        "<https://ads.snapchat.com/snap-account/ads/ad-0|Ad_0 (ad-0)>"
    ) in thread
    assert "Winner\\_creative" not in thread


def test_block_kit_headline_and_fields_use_rolling_24h_metrics_and_exact_total():
    main, thread = heartbeat.build_blocks(_midnight_sample())
    assert main is not None
    assert thread is not None

    main_payload = main.build(strict=True)
    thread_payload = thread.build(strict=True)
    rendered_main = json.dumps(main_payload)
    rendered_thread = json.dumps(thread_payload)
    assert "rolling 24h CPA" in rendered_main
    assert "24h CPA" in rendered_main
    assert "$120.00" in rendered_main
    assert "24h Purchases" in rendered_main
    assert "5" in rendered_main
    assert "n/a CPA" not in rendered_main
    assert "24 completed hours ending 12:00 AM PT" in rendered_main
    assert "129 status-ACTIVE" in rendered_thread
    assert len(main_payload["blocks"]) <= 50
    assert len(thread_payload["blocks"]) <= 50
    assert "https://ads.snapchat.com/snap-account/ads/ad_top_1" in rendered_thread
    assert "Winner_creative_1 (ad_top_1)" in rendered_thread
    assert "Winner\\\\_creative" not in rendered_thread


def test_account_id_prefers_source_bound_collector_value(monkeypatch):
    monkeypatch.setenv("SNAPCHAT_AD_ACCOUNT_ID", "environment-account")
    sample = _midnight_sample()
    sample["account"] = {"ad_account_id": "collector-account"}

    _, thread = heartbeat.build_messages(sample)

    assert "https://ads.snapchat.com/collector-account/ads/ad_top_1" in thread
    assert "https://ads.snapchat.com/environment-account/" not in thread


def test_account_id_falls_back_to_selected_cli_account(monkeypatch, tmp_path):
    accounts_file = tmp_path / "accounts.toml"
    accounts_file.write_text(
        "[accounts.default]\n"
        'ad_account_id = "default-account"\n'
        "[accounts.acme]\n"
        'ad_account_id = "selected-account"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SNAPCHAT_AD_ACCOUNT_ID")
    monkeypatch.setenv("SNAPCHAT_ACCOUNTS_FILE", str(accounts_file))
    monkeypatch.setenv("SNAPCHAT_ADS_ACCOUNT", "acme")

    assert heartbeat._configured_snap_account_id({}) == "selected-account"


def test_range_label_preserves_repeated_fall_back_hour_offsets():
    label = heartbeat.range_label(
        "2026-11-01T01:00:00-07:00",
        "2026-11-01T01:00:00-08:00",
        heartbeat.PT,
    )

    assert label == "1:00 AM PDT–1:00 AM PST"


def test_range_label_preserves_spring_forward_transition():
    label = heartbeat.range_label(
        "2026-03-08T01:00:00-08:00",
        "2026-03-08T03:00:00-07:00",
        heartbeat.PT,
    )

    assert label == "1:00 AM PST–3:00 AM PDT"


def test_parse_collector_json_redacts_no_json_errors():
    with pytest.raises(RuntimeError) as exc_info:
        heartbeat.parse_collector_json(
            "not json access_token=secret-token password=secret-password"
        )

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "secret-password" not in message


def test_collect_redacts_failed_collector_output(monkeypatch):
    monkeypatch.setattr(
        heartbeat.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="access_token=secret-token",
            stderr="password=secret-password",
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        heartbeat.collect()

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "secret-password" not in message
