import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures" / "optimizer.test.json"
os.environ.setdefault("SNAP_OPTIMIZER_CONFIG", str(FIXTURE_CONFIG))

import snap_optimizer_support as support  # noqa: E402


def test_extract_ad_breakdown_rows_from_snap_stats_envelope():
    payload = {
        "total_stats": [
            {
                "total_stat": {
                    "breakdown_stats": {
                        "ad": [
                            {
                                "id": "ad-1",
                                "stats": {
                                    "spend": 12300000,
                                    "impressions": 1000,
                                    "swipes": 24,
                                    "conversion_purchases": 0,
                                },
                            }
                        ]
                    }
                }
            }
        ]
    }

    rows = support.extract_ad_breakdown_rows(payload)

    assert rows == [
        {
            "ad_id": "ad-1",
            "spend": 12.3,
            "impressions": 1000,
            "swipes": 24,
            "purchases": 0,
        }
    ]


def test_product_routing_keeps_products_separate():
    prod_a = support.resolve_product_route(
        {
            "product_type": "product_a",
            "ad_name": "Product A tier 4",
            "link_url": "https://example.com/products/product-a-standard",
        },
        placement="nondpa",
    )
    prod_b = support.resolve_product_route(
        {
            "product_type": "product_b",
            "ad_name": "Product B winner",
            "link_url": "https://example.com/products/product-b-standard",
        },
        placement="dpa",
    )
    ambiguous = support.resolve_product_route(
        {
            "product_type": "",
            "ad_name": "Generic winner",
            "link_url": "https://example.com/products/generic",
        },
        placement="dpa",
    )

    assert prod_a["product"] == "product_a"
    assert prod_a["ad_squad_id"] == support.ROUTES["product_a"]["nondpa_ad_squad_id"]
    assert "product-a" in prod_a["landing_page"]
    assert prod_b["product"] == "product_b"
    assert prod_b["ad_squad_id"] == support.ROUTES["product_b"]["dpa_ad_squad_id"]
    assert prod_b["product_set_id"] == support.ROUTES["product_b"]["product_set_id"]
    assert ambiguous is None


def test_pause_plan_guardrails_block_fresh_and_last_active_ads():
    now = dt.datetime(2026, 6, 24, 16, 0, tzinfo=dt.timezone.utc)
    context = {
        "generated_at": now.isoformat(),
        "config": support.DEFAULT_CONFIG,
        "ads": [
            {
                "id": "fresh",
                "name": "Fresh weak ad",
                "ad_squad_id": "squad-a",
                "status": "ACTIVE",
                "created_at": "2026-06-24T14:30:00Z",
                "updated_at": "2026-06-24T14:30:00Z",
                "windows": {"7": {"spend": 100.0, "purchases": 0}},
            },
            {
                "id": "last-active",
                "name": "Only live ad",
                "ad_squad_id": "squad-b",
                "status": "ACTIVE",
                "created_at": "2026-06-10T00:00:00Z",
                "updated_at": "2026-06-10T00:00:00Z",
                "windows": {"7": {"spend": 120.0, "purchases": 0}},
            },
        ],
        "active_counts_by_ad_squad": {"squad-a": 2, "squad-b": 1},
        "pause_candidates": ["fresh", "last-active"],
    }

    result = support.validate_pause_plan(
        context,
        [{"ad_id": "fresh", "reason": "zero purchases"}, {"ad_id": "last-active", "reason": "zero purchases"}],
    )

    assert result["approved"] == []
    assert {row["ad_id"]: row["decision"] for row in result["blocked"]} == {
        "fresh": "blocked_fresh",
        "last-active": "blocked_last_active_in_squad",
    }


def test_pause_plan_guardrails_cap_total_paused_spend():
    context = {
        "generated_at": "2026-06-24T16:00:00Z",
        "config": {
            **support.DEFAULT_CONFIG,
            "max_pause_ad_share": 1.0,
            "max_pause_spend_share": 0.25,
            "min_active_ads_per_squad_after_pause": 1,
        },
        "ads": [
            {
                "id": "ad-1",
                "name": "Weak one",
                "ad_squad_id": "squad-a",
                "status": "ACTIVE",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
                "windows": {"7": {"spend": 100.0, "purchases": 0}},
            },
            {
                "id": "ad-2",
                "name": "Weak two",
                "ad_squad_id": "squad-a",
                "status": "ACTIVE",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
                "windows": {"7": {"spend": 100.0, "purchases": 0}},
            },
            {
                "id": "ad-3",
                "name": "Healthy volume",
                "ad_squad_id": "squad-a",
                "status": "ACTIVE",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": "2026-06-01T00:00:00Z",
                "windows": {"7": {"spend": 600.0, "purchases": 9}},
            },
        ],
        "active_counts_by_ad_squad": {"squad-a": 3},
        "pause_candidates": ["ad-1", "ad-2"],
    }

    result = support.validate_pause_plan(
        context,
        [{"ad_id": "ad-1", "reason": "zero purchases"}, {"ad_id": "ad-2", "reason": "zero purchases"}],
    )

    assert [row["ad_id"] for row in result["approved"]] == ["ad-1", "ad-2"]

    context["ads"][2]["windows"]["7"]["spend"] = 100.0
    result = support.validate_pause_plan(
        context,
        [{"ad_id": "ad-1", "reason": "zero purchases"}, {"ad_id": "ad-2", "reason": "zero purchases"}],
    )

    assert [row["ad_id"] for row in result["approved"]] == ["ad-1"]
    assert result["blocked"][0]["decision"] == "blocked_spend_cap"


def test_cron_job_prompts_are_agent_driven_and_threaded():
    jobs = support.build_cron_jobs()

    assert {job["name"] for job in jobs} == {
        "snapchat-optimizer-daily",
        "snapchat-meta-winners-weekly",
    }
    assert all(job["deliver"] == f"slack:{support.SLACK_CHANNEL_ID}" for job in jobs)
    assert all(job["threaded_slack"] is True for job in jobs)
    assert all(job["agent_id"] is None for job in jobs)
    assert all(job["no_agent"] is False for job in jobs)
    combined_prompt = "\n".join(job["prompt"] for job in jobs)
    assert "Final response must be valid JSON" in combined_prompt
    assert "Product A" in combined_prompt and "Product B" in combined_prompt


class SquadOptimizerTest(unittest.TestCase):
    CFG = dict(support.DEFAULT_CONFIG)

    def test_classify_scale_budget_when_roas_high_and_utilized(self):
        act = support.classify_squad(
            budget=1000.0,
            cpa_target=80.0,
            window={"spend": 6500.0, "revenue": 9100.0, "purchases": 90},
            cfg=self.CFG,
        )
        self.assertEqual(act["action"], "budget_increase")
        self.assertEqual(act["to_value"], 1300.0)

    def test_classify_loosen_cpa_when_roas_high_but_underdelivering(self):
        act = support.classify_squad(
            budget=1500.0,
            cpa_target=67.5,
            window={"spend": 2600.0, "revenue": 3800.0, "purchases": 36},
            cfg=self.CFG,
        )
        self.assertEqual(act["action"], "cpa_increase")
        self.assertEqual(act["to_value"], round(67.5 * 1.10, 2))

    def test_classify_walk_cpa_down_when_below_target(self):
        act = support.classify_squad(
            budget=2500.0,
            cpa_target=87.3,
            window={"spend": 15500.0, "revenue": 12245.0, "purchases": 178},
            cfg=self.CFG,
        )
        self.assertEqual(act["action"], "cpa_decrease")
        # 0.79 roas < 0.8*1.2=0.96 -> severe 15% step
        self.assertEqual(act["to_value"], round(87.3 * 0.85, 2))

    def test_classify_budget_cut_when_cpa_at_floor(self):
        cfg = dict(self.CFG)
        act = support.classify_squad(
            budget=500.0,
            cpa_target=cfg["cpa_floor"],
            window={"spend": 900.0, "revenue": 400.0, "purchases": 8},
            cfg=cfg,
        )
        self.assertEqual(act["action"], "budget_decrease")

    def test_classify_holds_small_spend(self):
        act = support.classify_squad(
            budget=750.0,
            cpa_target=75.0,
            window={"spend": 60.0, "revenue": 20.0, "purchases": 1},
            cfg=self.CFG,
        )
        self.assertIsNone(act)

    def test_governor_modes(self):
        g = support._squad_governor(10000, 8000, self.CFG)   # 0.8
        self.assertEqual(g["mode"], "cut_only")
        g = support._squad_governor(10000, 12500, self.CFG)  # 1.25
        self.assertEqual(g["mode"], "rebalance")
        g = support._squad_governor(10000, 14000, self.CFG)  # 1.4
        self.assertEqual(g["mode"], "scale")

    def test_delayed_window_excludes_yesterday_and_today(self):
        import datetime as dt
        now = dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.timezone.utc)
        start, end = support._delayed_window_bounds(7, 2, now)
        self.assertTrue(end.startswith("2026-06-30T00:00:00"))
        self.assertTrue(start.startswith("2026-06-23T00:00:00"))


class SquadPlanValidationTest(unittest.TestCase):
    def _ctx(self, mode="rebalance", on_cooldown=False):
        return {
            "config": dict(support.DEFAULT_CONFIG),
            "governor": {"mode": mode},
            "squads": [
                {
                    "ad_squad_id": "sq1",
                    "name": "ProductA",
                    "status": "ACTIVE",
                    "daily_budget": 2500.0,
                    "cpa_target": 87.3,
                    "window": {"spend": 15500, "roas": 0.79},
                    "on_cooldown": on_cooldown,
                }
            ],
        }

    def test_governor_cut_only_blocks_increases(self):
        out = support.validate_squad_plan(self._ctx(mode="cut_only"), [
            {"ad_squad_id": "sq1", "action": "budget_increase", "to_value": 3000.0},
            {"ad_squad_id": "sq1", "action": "cpa_decrease", "to_value": 74.21},
        ])
        decisions = {(b["action"], b["decision"]) for b in out["blocked"]}
        self.assertIn(("budget_increase", "blocked_governor_cut_only"), decisions)
        self.assertEqual(len(out["approved"]), 1)
        self.assertEqual(out["approved"][0]["action"], "cpa_decrease")

    def test_change_caps_and_bounds(self):
        out = support.validate_squad_plan(self._ctx(), [
            {"ad_squad_id": "sq1", "action": "cpa_decrease", "to_value": 20.0},   # below floor
            {"ad_squad_id": "sq1", "action": "budget_increase", "to_value": 5000.0},  # +100% > 35% cap
            {"ad_squad_id": "sq1", "action": "cpa_increase", "to_value": 80.0},   # wrong direction
        ])
        decisions = {b["decision"] for b in out["blocked"]}
        self.assertIn("blocked_cpa_bounds", decisions)
        self.assertIn("blocked_change_cap", decisions)
        self.assertIn("blocked_wrong_direction", decisions)
        self.assertEqual(out["approved"], [])

    def test_cooldown_blocks_via_ledger(self):
        import tempfile, pathlib, datetime as dt, json as js
        with tempfile.TemporaryDirectory() as tmp:
            ledger = pathlib.Path(tmp) / "squad_actions.jsonl"
            ledger.write_text(js.dumps({
                "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                "ad_squad_id": "sq1", "action": "cpa_decrease",
            }) + "\n", encoding="utf-8")
            old = support.SQUAD_LEDGER
            try:
                support.SQUAD_LEDGER = ledger
                out = support.validate_squad_plan(self._ctx(), [
                    {"ad_squad_id": "sq1", "action": "cpa_decrease", "to_value": 74.21},
                ])
            finally:
                support.SQUAD_LEDGER = old
            self.assertEqual(out["approved"], [])
            self.assertEqual(out["blocked"][0]["decision"], "blocked_cooldown")


class InventoryLpTest(unittest.TestCase):
    def test_lp_choices_cover_both_products(self):
        self.assertIn("product_a", support.LP_CHOICES)
        self.assertIn("product_b", support.LP_CHOICES)
        for choices in support.LP_CHOICES.values():
            self.assertEqual(len(choices), 2)


# --- weekly Meta -> Snap bridge: winner and landing-page sources -------------

def test_default_meta_winners_sql_reads_the_reference_layout():
    sql = support._build_meta_winners_sql(support._PRODUCTS, {"lookback_days": 30})
    assert "from meta_daily_metrics" in sql
    assert "left join meta_creatives c" in sql
    assert "then 'product_a'" in sql


def test_meta_winners_template_fills_tokens_from_config(tmp_path):
    template = tmp_path / "winners.sql"
    template.write_text(
        "select __PRODUCT_CASE__, ad_id from t\n"
        "where d >= current_date - interval '__LOOKBACK_DAYS__ days'\n"
        "  and attribution_windows = '__ATTRIBUTION_WINDOWS__'::text[]\n"
        "  and (__SCOPED_PREDICATE__)\n"
        "  and spend >= __MIN_SPEND__ and roas >= __MIN_ROAS__",
        encoding="utf-8",
    )
    sql = support._build_meta_winners_sql(
        support._PRODUCTS,
        {"sql_template_file": str(template), "lookback_days": 14, "min_spend": 100, "min_roas": 2},
    )
    assert "interval '14 days'" in sql
    assert "'{7d_click,1d_view}'::text[]" in sql
    assert "spend >= 100.0 and roas >= 2.0" in sql
    assert "like '%product a%'" in sql and "then 'product_b'" in sql
    assert "__" not in sql


def test_meta_winners_template_rejects_bad_attribution_and_unknown_tokens(tmp_path):
    import pytest

    template = tmp_path / "winners.sql"
    template.write_text("select 1 where x = '__ATTRIBUTION_WINDOWS__'", encoding="utf-8")
    with pytest.raises(ValueError, match="attribution_windows"):
        support._build_meta_winners_sql(
            support._PRODUCTS,
            {"sql_template_file": str(template), "attribution_windows": "{7d_click}'; drop table x; --"},
        )
    template.write_text("select __TYPO_TOKEN__", encoding="utf-8")
    with pytest.raises(ValueError, match="__TYPO_TOKEN__"):
        support._build_meta_winners_sql(support._PRODUCTS, {"sql_template_file": str(template)})


def test_bridge_query_defaults_to_public_search_schema_and_ambient_dsn():
    argv, env = support._bridge_query("select 1", "why", {})
    assert argv[-2:] == ["--warehouse-schema", "public"]
    assert env is None


def test_bridge_query_can_target_a_separate_meta_warehouse(monkeypatch):
    import pytest

    monkeypatch.setenv("DATABASE_URL", "postgresql:///snap_warehouse")
    monkeypatch.setenv("META_WAREHOUSE_URL", "postgresql:///meta_warehouse")
    argv, env = support._bridge_query(
        "select 1", "why", {"database_url_env": "META_WAREHOUSE_URL", "search_schema": "meta_ads"}
    )
    assert argv[-2:] == ["--warehouse-schema", "meta_ads"]
    assert env["DATABASE_URL"] == "postgresql:///meta_warehouse"
    assert os.environ["DATABASE_URL"] == "postgresql:///snap_warehouse"
    monkeypatch.delenv("META_WAREHOUSE_URL")
    with pytest.raises(RuntimeError, match="META_WAREHOUSE_URL is not set"):
        support._bridge_query("select 1", "why", {"database_url_env": "META_WAREHOUSE_URL"})


def test_inventory_template_fills_prefix_and_rejects_unsafe_input():
    import pytest

    sql = support._render_inventory_template(
        "select handle, available from inv where handle like '__HANDLE_PREFIX__%'", "product-a"
    )
    assert "like 'product-a%'" in sql and "__" not in sql
    for bad in ("", "Product-A", "a'; drop table x; --", "a b"):
        with pytest.raises(ValueError, match="handle_prefix"):
            support._render_inventory_template("select '__HANDLE_PREFIX__'", bad)
    with pytest.raises(ValueError, match="__OTHER__"):
        support._render_inventory_template("select '__HANDLE_PREFIX__', __OTHER__", "product-a")


def test_inventory_template_picks_the_landing_page_with_most_stock(tmp_path, monkeypatch):
    template = tmp_path / "inventory.sql"
    template.write_text(
        "select handle, available from mcp_reporting.stock where handle like '__HANDLE_PREFIX__%'",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        support, "_LP_INVENTORY_CFG",
        {"sql_template_file": str(template), "search_schema": "mcp_reporting"},
    )
    stock = {
        "product-a": [
            {"handle": "product-a-standard", "available": 5},
            {"handle": "product-a-deluxe", "available": 40},
            {"handle": "product-a-deluxe", "available": -3},  # oversold location counts as zero
        ],
        "product-b": [{"handle": "product-b-standard", "available": 9}],
    }
    calls = []

    def fake_run_json(argv, *, timeout=120, env=None):
        sql = argv[argv.index("--sql") + 1]
        calls.append(argv)
        prefix = next(p for p in stock if f"like '{p}%'" in sql)
        return {"rows": stock[prefix]}

    monkeypatch.setattr(support, "_run_json", fake_run_json)

    result = support.fetch_inventory_lp_choice()

    assert result["chosen"]["product_a"]["handle"] == "product-a-deluxe"
    assert result["chosen"]["product_a"]["available"] == 40.0
    assert result["chosen"]["product_b"]["handle"] == "product-b-standard"
    assert len(calls) == 2  # one per product, no "newest snapshot" query
    assert all(argv[-2:] == ["--warehouse-schema", "mcp_reporting"] for argv in calls)
    assert not any(support.INVENTORY_NEWEST_SQL in argv for argv in calls)
