#!/usr/bin/env python3
"""Agent-driven Snapchat optimizer support.

This module intentionally does not replace the scheduled agent. It prepares
clean account context and enforces mutation guardrails so the agent can make
judgment calls without risking runaway pauses or product-route mistakes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ENV_VAR = "SNAP_OPTIMIZER_CONFIG"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "optimizer.json"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "optimizer.example.json"
CACHE_DIR = Path.home() / ".cache" / "snapchat-optimizer"
BRIDGE_REGISTRY = Path.home() / ".cache" / "snapchat-bridge" / "registry.jsonl"
SNAP_SAFE = SKILL_DIR / "scripts" / "snapchat_ads_safe.sh"
WAREHOUSE_QUERY_ENV_VAR = "SNAP_WAREHOUSE_QUERY"
DASHBOARD_SQL = Path(os.environ.get(WAREHOUSE_QUERY_ENV_VAR) or (REPO_ROOT / "warehouse" / "query.py"))
CRON_JOBS_ENV_VAR = "SNAP_CRON_JOBS_PATH"
CRON_JOBS = Path(os.environ[CRON_JOBS_ENV_VAR]).expanduser() if os.environ.get(CRON_JOBS_ENV_VAR) else None


def _load_optimizer_config() -> dict[str, Any]:
    override = os.environ.get(CONFIG_ENV_VAR)
    config_path = Path(override).expanduser() if override else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise RuntimeError(
            f"Snap optimizer config not found at {config_path}. "
            f"Copy the example to get started: cp {EXAMPLE_CONFIG_PATH} {DEFAULT_CONFIG_PATH}"
            f" (or set {CONFIG_ENV_VAR} to point at a config file)."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


_CONFIG = _load_optimizer_config()
_PRODUCTS: dict[str, dict[str, Any]] = _CONFIG.get("products") or {}
_META_WINNERS_CFG: dict[str, Any] = _CONFIG.get("meta_winners") or {}
_CRON_CFG: dict[str, Any] = _CONFIG.get("cron") or {}

BRAND_NAME = str(_CONFIG.get("brand_name") or "")
HEADLINE = str(_CONFIG.get("headline") or "")
SLACK_CHANNEL_ID = str(_CONFIG.get("slack_channel_id") or "")
CLI_ACCOUNT = str(_CONFIG.get("cli_account") or "default")
CRON_TZ = str(_CONFIG.get("timezone") or "America/Los_Angeles")

# Per-product keyword lists used for both product detection (ad/adset/
# campaign name matching) and landing-page validation.
_PRODUCT_KEYWORDS: dict[str, list[str]] = {
    key: [str(kw).lower() for kw in (product.get("keywords") or [])] for key, product in _PRODUCTS.items()
}

ROUTES: dict[str, dict[str, str]] = {
    key: {
        "label": product.get("label") or key,
        "dpa_campaign_id": product.get("dpa_campaign_id") or "",
        "dpa_ad_squad_id": product.get("dpa_ad_squad_id") or "",
        "nondpa_campaign_id": product.get("nondpa_campaign_id") or "",
        "nondpa_ad_squad_id": product.get("nondpa_ad_squad_id") or "",
        "product_set_id": product.get("product_set_id") or "",
        "landing_page": ((product.get("landing_pages") or [{}])[0] or {}).get("landing_page", ""),
    }
    for key, product in _PRODUCTS.items()
}

SNAP_SHARED = {
    **{k: "" for k in ("dynamic_template_id", "interaction_zone_id", "profile_id", "pixel_id")},
    **(_CONFIG.get("snap_shared") or {}),
    "brand_name": BRAND_NAME,
    "headline": HEADLINE,
}

DEFAULT_CONFIG = dict(_CONFIG.get("optimizer") or {})

SQUAD_LEDGER = CACHE_DIR / "squad_actions.jsonl"

# Landing-page choices per product; the weekly bridge picks whichever has the
# most available Shopify inventory at run time.
LP_CHOICES: dict[str, list[dict[str, str]]] = {
    key: list(product.get("landing_pages") or []) for key, product in _PRODUCTS.items()
}

# The dashboard SQL guardrail rejects CTEs/subqueries, so the LP inventory
# lookup runs as two flat queries: newest snapshot date, then per-item rows
# for that date (deduped by max per item+location, summed in Python).
INVENTORY_NEWEST_SQL = "select max(recorded_at) as newest from shopify_inventory_levels"

# NOTE: the guardrail keyword-scans the whole SQL string — some product
# handles contain rejected substrings (e.g. "drop"), so each product queries
# via a LIKE prefix instead of quoting handles verbatim.
INVENTORY_ROWS_SQL_TEMPLATE = (
    "select p.handle, v.inventory_item_id, l.location_id, max(l.available) as available "
    "from shopify_products p "
    "join shopify_product_variants v on v.product_id = p.id "
    "join shopify_inventory_levels l on l.inventory_item_id = v.inventory_item_id "
    "where p.handle like '{prefix}%' and l.recorded_at >= '{newest}' "
    "group by 1, 2, 3 limit 2000"
)

# Shared safe prefix per product (must not contain guardrail keywords).
LP_HANDLE_PREFIX = {key: product.get("handle_prefix") or "" for key, product in _PRODUCTS.items()}


def _sql_escape(value: str) -> str:
    return value.replace("'", "''")


def _keyword_predicate(keywords: list[str]) -> str:
    conditions = [
        (
            f"lower(coalesce(ad_name,'')) like '%{_sql_escape(kw)}%'"
            f" or lower(coalesce(adset_name,'')) like '%{_sql_escape(kw)}%'"
            f" or lower(coalesce(campaign_name,'')) like '%{_sql_escape(kw)}%'"
        )
        for kw in keywords
    ]
    return "(\n        " + "\n        or ".join(conditions) + "\n      )"


def _product_filters(products: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """(scoped keyword predicate, product_type CASE) from configured products."""
    all_keywords = {key: [str(kw).lower() for kw in (product.get("keywords") or [])] for key, product in products.items()}
    product_keywords = [(key, kws) for key, kws in all_keywords.items() if kws]
    scoped_predicate = "\n      or ".join(_keyword_predicate(kws) for _, kws in product_keywords) or "false"
    case_lines = [f"    when {_keyword_predicate(kws)}\n      then '{_sql_escape(key)}'" for key, kws in product_keywords]
    case_sql = "case\n" + "\n".join(case_lines) + "\n    else null\n  end as product_type"
    return scoped_predicate, case_sql


# Brand-authored winners SQL (meta_winners.sql_template_file) for a Meta
# warehouse whose layout differs from meta_daily_metrics/meta_ads/
# meta_creatives. It must return the generated query's columns: product_type,
# ad_id, ad_name, spend, revenue, roas, purchases, last_date,
# entity_creative_id, link_url. Tokens are filled from config; the keyword
# predicate and product CASE reference unqualified ad_name/adset_name/
# campaign_name columns.
META_WINNERS_TEMPLATE_TOKENS = (
    "__LOOKBACK_DAYS__", "__MIN_SPEND__", "__MIN_ROAS__",
    "__SCOPED_PREDICATE__", "__PRODUCT_CASE__", "__ATTRIBUTION_WINDOWS__",
)
_ATTRIBUTION_WINDOWS_RE = re.compile(r"^\{[a-z0-9_]+(,[a-z0-9_]+)*\}$")


def _read_sql_template(path_value: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.read_text(encoding="utf-8")


def _render_meta_winners_template(
    template: str,
    *,
    lookback_days: int,
    min_spend: float,
    min_roas: float,
    scoped_predicate: str,
    product_case: str,
    attribution_windows: str,
) -> str:
    if not _ATTRIBUTION_WINDOWS_RE.fullmatch(attribution_windows):
        raise ValueError(
            "meta_winners.attribution_windows must look like {7d_click,1d_view}"
        )
    values = {
        "__LOOKBACK_DAYS__": str(int(lookback_days)),
        "__MIN_SPEND__": repr(float(min_spend)),
        "__MIN_ROAS__": repr(float(min_roas)),
        "__SCOPED_PREDICATE__": scoped_predicate,
        "__PRODUCT_CASE__": product_case,
        "__ATTRIBUTION_WINDOWS__": attribution_windows,
    }
    rendered = template
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    unfilled = sorted(set(re.findall(r"__[A-Z][A-Z_]*__", rendered)))
    if unfilled:
        raise ValueError(f"meta winners SQL template has unknown tokens: {unfilled}")
    return rendered.strip()


def _build_meta_winners_sql(products: dict[str, dict[str, Any]], meta_winners_cfg: dict[str, Any]) -> str:
    """Generate the ad-level Meta winners SQL from configured products.

    Same shape as a hand-written query would take: a scoped CTE over
    meta_daily_metrics filtered by per-product keyword LIKEs over the
    lookback window, a ranked CTE that assigns product_type via a CASE over
    those same keyword predicates and filters by spend/ROAS thresholds, and a
    final select joined against meta_ads + meta_creatives.
    """
    lookback_days = int(meta_winners_cfg.get("lookback_days", 30))
    min_spend = float(meta_winners_cfg.get("min_spend", 250.0))
    min_roas = float(meta_winners_cfg.get("min_roas", 1.5))

    scoped_predicate, case_sql = _product_filters(products)
    template_file = str(meta_winners_cfg.get("sql_template_file") or "").strip()
    if template_file:
        return _render_meta_winners_template(
            _read_sql_template(template_file),
            lookback_days=lookback_days,
            min_spend=min_spend,
            min_roas=min_roas,
            scoped_predicate=scoped_predicate,
            product_case=case_sql,
            attribution_windows=str(meta_winners_cfg.get("attribution_windows") or "{7d_click,1d_view}"),
        )

    return f"""
with scoped as (
  select
    ad_id,
    max(ad_name) as ad_name,
    max(adset_name) as adset_name,
    max(campaign_name) as campaign_name,
    sum(spend)::numeric as spend,
    sum(revenue)::numeric as revenue,
    sum(purchases)::int as purchases,
    max(recorded_at) as last_date
  from meta_daily_metrics
  where recorded_at >= current_date - interval '{lookback_days} days'
    and level = 'ad'
    and (
      {scoped_predicate}
    )
  group by ad_id
),
ranked as (
  select
    *,
    {case_sql}
  from scoped
  where spend >= {min_spend}
    and revenue / nullif(spend, 0) >= {min_roas}
)
select
  r.product_type,
  r.ad_id,
  r.ad_name,
  round(r.spend, 2)::text as spend,
  round(r.revenue, 2)::text as revenue,
  round((r.revenue / nullif(r.spend, 0))::numeric, 2)::text as roas,
  r.purchases,
  r.last_date::text as last_date,
  a.creative_id as entity_creative_id,
  c.link_url
from ranked r
left join meta_ads a on a.ad_id = r.ad_id
left join meta_creatives c on c.creative_id = a.creative_id
order by r.product_type, r.spend desc
limit 200
""".strip()


META_WINNERS_SQL = _build_meta_winners_sql(_PRODUCTS, _META_WINNERS_CFG)


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _window_bounds(days: int, now: dt.datetime | None = None) -> tuple[str, str]:
    end = (now or _now_utc()).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _money_from_snap(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if abs(number) >= 10000:
        return round(number / 1_000_000.0, 6)
    return round(number, 6)


def _num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def extract_ad_breakdown_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized ad-level rows from Snap stats envelopes."""
    rows: list[dict[str, Any]] = []
    blocks = payload.get("total_stats") or payload.get("timeseries_stats") or payload.get("stats") or []
    if isinstance(blocks, dict):
        blocks = [blocks]
    if not isinstance(blocks, list):
        return rows

    for block in blocks:
        if not isinstance(block, dict):
            continue
        inner = block.get("total_stat") or block.get("stat") or block
        breakdown = inner.get("breakdown_stats") if isinstance(inner, dict) else None
        ad_rows = breakdown.get("ad") if isinstance(breakdown, dict) else None
        if not isinstance(ad_rows, list):
            ad_rows = [inner] if isinstance(inner, dict) and inner.get("type") == "AD" else []
        for ad_row in ad_rows:
            if not isinstance(ad_row, dict):
                continue
            stats = ad_row.get("stats") if isinstance(ad_row.get("stats"), dict) else ad_row
            ad_id = ad_row.get("id") or stats.get("id")
            if not ad_id:
                continue
            spend = _money_from_snap(stats.get("spend"))
            purchases = int(_num(stats.get("conversion_purchases") or stats.get("purchases")))
            rows.append(
                {
                    "ad_id": str(ad_id),
                    "spend": spend,
                    "impressions": int(_num(stats.get("impressions"))),
                    "swipes": int(_num(stats.get("swipes") or stats.get("swipe_ups"))),
                    "purchases": purchases,
                }
            )
    return rows


def _unwrap_entity_list(payload: dict[str, Any], key: str, inner_key: str) -> list[dict[str, Any]]:
    items = payload.get(key) or []
    rows: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return rows
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(inner_key) if isinstance(item.get(inner_key), dict) else item
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _run_json(argv: list[str], *, timeout: int = 120, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip())
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command did not return JSON: {argv!r}\n{result.stdout[:1000]}") from exc


def _bridge_query(sql: str, reason: str, source_cfg: dict[str, Any]) -> tuple[list[str], dict[str, str] | None]:
    """argv + env for one read-only bridge lookup through warehouse/query.py.

    `search_schema` (default public, where the reference layout keeps its
    Meta/Shopify tables) becomes query.py's --warehouse-schema, i.e. the
    transaction's search path. `database_url_env` names an env var holding
    the DSN of the database the source tables live in (a Meta warehouse
    separate from the Snap warehouse); unset means DATABASE_URL."""
    schema = str(source_cfg.get("search_schema") or "public")
    argv = [sys.executable, str(DASHBOARD_SQL), "--sql", sql, "--reason", reason, "--warehouse-schema", schema]
    env_var = str(source_cfg.get("database_url_env") or "").strip()
    if not env_var:
        return argv, None
    dsn = os.environ.get(env_var)
    if not dsn:
        raise RuntimeError(f"{env_var} is not set (bridge source database)")
    return argv, {**os.environ, "DATABASE_URL": dsn}


_LP_INVENTORY_CFG: dict[str, Any] = _CONFIG.get("landing_page_inventory") or {}
# Brand-authored inventory SQL (landing_page_inventory.sql_template_file) for a
# Shopify warehouse without the reference shopify_products /
# shopify_product_variants / shopify_inventory_levels snapshot tables. It runs
# once per product with __HANDLE_PREFIX__ filled from that product's
# handle_prefix and must return rows of (handle, available); rows for the same
# handle are summed, negatives count as zero.
_HANDLE_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _render_inventory_template(template: str, handle_prefix: str) -> str:
    if not _HANDLE_PREFIX_RE.fullmatch(handle_prefix or ""):
        raise ValueError(
            "product handle_prefix must be lowercase letters, digits, '-' or '_' "
            f"(got {handle_prefix!r})"
        )
    rendered = template.replace("__HANDLE_PREFIX__", handle_prefix)
    unfilled = sorted(set(re.findall(r"__[A-Z][A-Z_]*__", rendered)))
    if unfilled:
        raise ValueError(f"landing-page inventory SQL template has unknown tokens: {unfilled}")
    return rendered.strip()


def _snap_cli(*args: str) -> list[str]:
    return [
        "uv",
        "run",
        "--directory",
        str(REPO_ROOT / "cli"),
        "snapchat-ads",
        "--account",
        CLI_ACCOUNT,
        *args,
    ]


def _load_registry_events(path: Path = BRIDGE_REGISTRY) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def detect_product(row: dict[str, Any]) -> str | None:
    explicit = str(row.get("product_type") or row.get("product") or "").strip().lower()
    if explicit in ROUTES:
        return explicit
    text = " ".join(str(row.get(k) or "") for k in ("ad_name", "name", "campaign_name", "adset_name", "link_url")).lower()
    matched = [product for product, keywords in _PRODUCT_KEYWORDS.items() if any(kw in text for kw in keywords)]
    if len(matched) == 1:
        return matched[0]
    return None


def resolve_product_route(row: dict[str, Any], *, placement: str) -> dict[str, str] | None:
    product = detect_product(row)
    if not product:
        return None
    route = ROUTES[product]
    if placement not in {"dpa", "nondpa"}:
        raise ValueError("placement must be dpa or nondpa")
    landing = str(row.get("link_url") or "").strip()
    keywords = _PRODUCT_KEYWORDS.get(product) or []
    if keywords and not any(kw in landing.lower() for kw in keywords):
        landing = route["landing_page"]
    return {
        "product": product,
        "label": route["label"],
        "campaign_id": route[f"{placement}_campaign_id"],
        "ad_squad_id": route[f"{placement}_ad_squad_id"],
        "product_set_id": route["product_set_id"],
        "landing_page": landing or route["landing_page"],
    }


def _is_active(ad: dict[str, Any]) -> bool:
    return str(ad.get("status") or "").upper() == "ACTIVE"


def _is_fresh(ad: dict[str, Any], *, now: dt.datetime, hours: float) -> bool:
    timestamps = [_parse_time(str(ad.get(key) or "")) for key in ("created_at", "updated_at")]
    timestamps = [value for value in timestamps if value is not None]
    if not timestamps:
        return False
    newest = max(timestamps)
    return (now - newest).total_seconds() < hours * 3600


def _with_derived_window_metrics(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    purchases = int(out.get("purchases") or 0)
    spend = float(out.get("spend") or 0)
    out["cpa"] = round(spend / purchases, 2) if purchases else None
    out["cpswipe"] = round(spend / max(int(out.get("swipes") or 0), 1), 2) if spend else None
    return out


def _candidate_reason(ad: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    w3 = ad.get("windows", {}).get("3", {})
    w7 = ad.get("windows", {}).get("7", {})
    w14 = ad.get("windows", {}).get("14", {})
    spend3 = float(w3.get("spend") or 0)
    spend7 = float(w7.get("spend") or 0)
    spend14 = float(w14.get("spend") or 0)
    p7 = int(w7.get("purchases") or 0)
    p14 = int(w14.get("purchases") or 0)
    cpa7 = w7.get("cpa")
    cpa14 = w14.get("cpa")

    if spend7 >= float(cfg["min_7d_spend_zero_purchase"]) and p7 == 0:
        if spend3 >= float(cfg["min_3d_spend_zero_purchase"]) or spend14 >= float(cfg["min_14d_spend_zero_purchase"]):
            return "zero purchases with material 7d spend"
    if spend14 >= float(cfg["min_14d_spend_zero_purchase"]) and p14 == 0:
        return "zero purchases with material 14d spend"
    high_cpa = float(cfg["target_cpa"]) * float(cfg["high_cpa_multiplier"])
    if (
        spend14 >= float(cfg["min_14d_spend_high_cpa"])
        and p14 > 0
        and cpa14 is not None
        and float(cpa14) >= high_cpa
        and (cpa7 is None or float(cpa7) >= high_cpa)
    ):
        return f"CPA above ${high_cpa:.0f} guardrail"
    return None


def _build_active_counts(ads: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ad in ads:
        if _is_active(ad):
            squad = str(ad.get("ad_squad_id") or "unknown")
            counts[squad] = counts.get(squad, 0) + 1
    return counts


def build_context(*, include_meta: bool = False, now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or _now_utc()
    cfg = dict(DEFAULT_CONFIG)

    ads_payload = _run_json(_snap_cli("ad", "list", "--limit", "500"))
    ads = _unwrap_entity_list(ads_payload, "ads", "ad")
    ads_by_id = {str(ad.get("id")): ad for ad in ads if ad.get("id")}

    squads_payload = _run_json(_snap_cli("adsquad", "list", "--limit", "500"))
    # CLI returns "ad_squads"; older builds used "adsquads" — accept both.
    squads = _unwrap_entity_list(squads_payload, "ad_squads", "adsquad") or _unwrap_entity_list(squads_payload, "adsquads", "adsquad")
    squads_by_id = {str(squad.get("id")): squad for squad in squads if squad.get("id")}

    stats_by_window: dict[str, dict[str, dict[str, Any]]] = {}
    for days in cfg["windows_days"]:
        start, end = _window_bounds(int(days), now)
        stats_payload = _run_json(
            _snap_cli(
                "report",
                "stats",
                "--entity",
                "ad_account",
                "--granularity",
                "TOTAL",
                "--start-time",
                start,
                "--end-time",
                end,
                "--fields",
                "spend,impressions,swipes,conversion_purchases",
                "--breakdown",
                "ad",
                "--include-empty",
            )
        )
        rows = [_with_derived_window_metrics(row) for row in extract_ad_breakdown_rows(stats_payload)]
        stats_by_window[str(days)] = {row["ad_id"]: row for row in rows}

    normalized_ads: list[dict[str, Any]] = []
    pause_candidates: list[str] = []
    guardrail_holds: list[dict[str, Any]] = []
    active_counts = _build_active_counts(ads)

    for ad_id, ad in sorted(ads_by_id.items(), key=lambda item: str(item[1].get("name") or item[0]).lower()):
        squad = squads_by_id.get(str(ad.get("ad_squad_id") or ""), {})
        windows = {days: stats_by_window.get(days, {}).get(ad_id, _with_derived_window_metrics({"spend": 0, "impressions": 0, "swipes": 0, "purchases": 0})) for days in ("3", "7", "14")}
        row = {
            "id": ad_id,
            "name": ad.get("name") or "",
            "ad_squad_id": ad.get("ad_squad_id") or "",
            "ad_squad_name": squad.get("name") or "",
            "campaign_id": squad.get("campaign_id") or "",
            "status": ad.get("status") or "",
            "review_status": ad.get("review_status") or "",
            "delivery_status": ad.get("delivery_status") or [],
            "type": ad.get("type") or "",
            "render_type": ad.get("render_type") or "",
            "created_at": ad.get("created_at") or "",
            "updated_at": ad.get("updated_at") or "",
            "product": detect_product(ad) or detect_product(squad) or "unknown",
            "windows": windows,
        }
        reason = _candidate_reason(row, cfg)
        if _is_active(row) and reason:
            if _is_fresh(row, now=now, hours=float(cfg["fresh_hold_hours"])):
                guardrail_holds.append({"ad_id": ad_id, "name": row["name"], "reason": "fresh hold", "signal": reason})
            elif active_counts.get(str(row["ad_squad_id"]), 0) <= int(cfg["min_active_ads_per_squad_after_pause"]):
                guardrail_holds.append({"ad_id": ad_id, "name": row["name"], "reason": "would leave too little active coverage", "signal": reason})
            else:
                pause_candidates.append(ad_id)
                row["pause_signal"] = reason
        normalized_ads.append(row)

    context = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "mode": "weekly_meta_refresh" if include_meta else "daily_optimizer",
        "config": cfg,
        "slack_channel_id": SLACK_CHANNEL_ID,
        "routes": ROUTES,
        "snap_shared": SNAP_SHARED,
        "ads": normalized_ads,
        "active_counts_by_ad_squad": active_counts,
        "pause_candidates": pause_candidates,
        "guardrail_holds": guardrail_holds,
    }
    try:
        squad_opt = build_squad_optimizer(ads, squads, cfg, now=now)
        squad_opt["config"] = cfg
        context["squad_optimizer"] = squad_opt
    except Exception as exc:  # noqa: BLE001
        context["squad_optimizer"] = {"error": str(exc)}
    if include_meta:
        context["meta_candidates"] = fetch_meta_winner_candidates()
        context["inventory_lp"] = fetch_inventory_lp_choice()
    return context


def validate_pause_plan(context: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(context.get("config") or {})
    ads = context.get("ads") or []
    ad_map = {str(ad.get("id")): ad for ad in ads if ad.get("id")}
    candidate_ids = {str(value) for value in context.get("pause_candidates") or []}
    active_counts = dict(context.get("active_counts_by_ad_squad") or _build_active_counts(ads))
    now = _parse_time(str(context.get("generated_at") or "")) or _now_utc()

    active_ads = [ad for ad in ads if _is_active(ad)]
    total_l7_spend = sum(float((ad.get("windows") or {}).get("7", {}).get("spend") or 0) for ad in active_ads)
    max_by_count = int(cfg["max_pause_ads_per_run"])
    max_by_share = max(1, math.floor(len(active_ads) * float(cfg["max_pause_ad_share"]))) if active_ads else 0
    max_pauses = max(0, min(max_by_count, max_by_share or max_by_count))
    max_pause_spend = total_l7_spend * float(cfg["max_pause_spend_share"])
    min_after = int(cfg["min_active_ads_per_squad_after_pause"])

    approved: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    paused_spend = 0.0
    mutable_counts = {str(k): int(v) for k, v in active_counts.items()}

    for item in plan:
        ad_id = str(item.get("ad_id") or "").strip()
        reason = str(item.get("reason") or "agent selected pause").strip()
        ad = ad_map.get(ad_id)
        if not ad:
            blocked.append({"ad_id": ad_id, "decision": "blocked_unknown_ad", "reason": reason})
            continue
        squad = str(ad.get("ad_squad_id") or "")
        l7_spend = float((ad.get("windows") or {}).get("7", {}).get("spend") or 0)
        base = {
            "ad_id": ad_id,
            "ad_squad_id": squad,
            "name": ad.get("name") or "",
            "l7_spend": round(l7_spend, 2),
            "reason": reason,
        }
        if ad_id not in candidate_ids:
            blocked.append({**base, "decision": "blocked_not_in_candidate_set"})
            continue
        if not _is_active(ad):
            blocked.append({**base, "decision": "blocked_not_active"})
            continue
        if _is_fresh(ad, now=now, hours=float(cfg["fresh_hold_hours"])):
            blocked.append({**base, "decision": "blocked_fresh"})
            continue
        if mutable_counts.get(squad, 0) - 1 < min_after:
            blocked.append({**base, "decision": "blocked_last_active_in_squad"})
            continue
        if len(approved) >= max_pauses:
            blocked.append({**base, "decision": "blocked_pause_count_cap"})
            continue
        if approved and paused_spend + l7_spend > max_pause_spend:
            blocked.append({**base, "decision": "blocked_spend_cap"})
            continue
        mutable_counts[squad] = mutable_counts.get(squad, 0) - 1
        paused_spend += l7_spend
        approved.append({**base, "decision": "approved"})

    return {
        "approved": approved,
        "blocked": blocked,
        "summary": {
            "active_ads": len(active_ads),
            "max_pauses": max_pauses,
            "total_l7_active_spend": round(total_l7_spend, 2),
            "max_pause_l7_spend": round(max_pause_spend, 2),
            "approved_l7_spend": round(paused_spend, 2),
        },
    }


def execute_pause_plan(context: dict[str, Any], plan: list[dict[str, Any]], *, execute: bool = False) -> dict[str, Any]:
    validation = validate_pause_plan(context, plan)
    results: list[dict[str, Any]] = []
    if not execute:
        validation["execute"] = False
        return validation
    env = os.environ.copy()
    env["SNAPCHAT_ADS_SAFE"] = "1"
    for row in validation["approved"]:
        cmd = [str(SNAP_SAFE), "ad", "pause", row["ad_squad_id"], row["ad_id"], "--execute"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)
        results.append(
            {
                **row,
                "returncode": proc.returncode,
                "stdout_tail": (proc.stdout or "")[-1000:],
                "stderr_tail": (proc.stderr or "")[-1000:],
            }
        )
    validation["execute"] = True
    validation["results"] = results
    return validation


# ---------------------------------------------------------------------------
# Squad-level full-auto optimizer (2026-07-01) — mirrors the Meta engine:
# blended-ROAS governor + per-squad master rules with two levers (daily budget
# and TARGET_COST CPA target) gated by budget utilization, plus a 48h cooldown
# ledger so the daily run never stacks changes on lagged attribution.
# ---------------------------------------------------------------------------


def _delayed_window_bounds(days: int, delay_days: int, now: dt.datetime | None = None) -> tuple[str, str]:
    """Window of `days` completed days ending `delay_days` ago (exclusive of
    yesterday/today when delay_days=2)."""
    midnight = (now or _now_utc()).replace(hour=0, minute=0, second=0, microsecond=0)
    end = midnight - dt.timedelta(days=max(int(delay_days) - 1, 0))
    start = end - dt.timedelta(days=int(days))
    return start.isoformat(), end.isoformat()


def _load_squad_ledger(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or SQUAD_LEDGER
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_squad_ledger(entries: list[dict[str, Any]], path: Path | None = None) -> None:
    path = path or SQUAD_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _squad_changed_within_hours(squad_id: str, ledger: list[dict[str, Any]], hours: float, now: dt.datetime) -> bool:
    cutoff = now - dt.timedelta(hours=hours)
    for row in reversed(ledger):
        if str(row.get("ad_squad_id")) != str(squad_id):
            continue
        ts = _parse_time(str(row.get("timestamp") or ""))
        if ts is not None and ts >= cutoff:
            return True
    return False


def _squad_governor(total_spend: float, total_revenue: float, cfg: dict[str, Any]) -> dict[str, Any]:
    target = float(cfg["target_roas"])
    band = float(cfg["governor_band"])
    roas = (total_revenue / total_spend) if total_spend > 0 else 0.0
    if roas < target - band:
        mode = "cut_only"
        rule = "blended window ROAS below target band: only cpa_decrease/budget_decrease/pauses allowed"
    elif roas <= target + band:
        mode = "rebalance"
        rule = "blended window ROAS inside target band: fund winners by tightening losers, net spend ~flat"
    else:
        mode = "scale"
        rule = "blended window ROAS above target band: net delivery increases allowed (budget or looser CPA targets)"
    return {
        "target_roas": target,
        "eventual_target_roas": float(cfg.get("eventual_target_roas") or target),
        "blended_roas_window": round(roas, 3),
        "window_spend": round(total_spend, 2),
        "window_revenue": round(total_revenue, 2),
        "mode": mode,
        "rule": rule,
    }


def classify_squad(*, budget: float, cpa_target: float, window: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Master rule for one ad squad. Returns an action dict or None (hold)."""
    target = float(cfg["target_roas"])
    band = float(cfg["governor_band"])
    spend = float(window.get("spend") or 0)
    revenue = float(window.get("revenue") or 0)
    purchases = int(window.get("purchases") or 0)
    roas = (revenue / spend) if spend > 0 else 0.0
    if budget <= 0 or spend < float(cfg["min_squad_window_spend"]):
        return None
    days = float(cfg["squad_window_days"])
    utilization = spend / (budget * days) if budget > 0 else 0.0

    if roas >= target + band and purchases >= int(cfg["min_squad_purchases_scale"]):
        if utilization >= float(cfg["utilization_full"]):
            pct = float(cfg["budget_up_pct_large"]) if budget > float(cfg["budget_large_threshold"]) else float(cfg["budget_up_pct"])
            return {
                "action": "budget_increase",
                "lever": "budget",
                "to_value": round(budget * (1 + pct), 2),
                "utilization": round(utilization, 3),
                "reason": f"window ROAS {roas:.2f} >= {target + band:.2f} with {purchases} purchases at {utilization:.0%} utilization",
            }
        new_cpa = min(round(cpa_target * (1 + float(cfg["cpa_step_up_pct"])), 2), float(cfg["cpa_ceiling"]))
        if cpa_target > 0 and new_cpa > cpa_target:
            return {
                "action": "cpa_increase",
                "lever": "cpa",
                "to_value": new_cpa,
                "utilization": round(utilization, 3),
                "reason": f"window ROAS {roas:.2f} above target but only {utilization:.0%} utilization — loosen CPA target to buy delivery instead of raising budget",
            }
        return None

    if roas < target - band:
        severe = roas < 0.8 * target
        step = float(cfg["cpa_step_down_pct_severe"]) if severe else float(cfg["cpa_step_down_pct"])
        new_cpa = max(round(cpa_target * (1 - step), 2), float(cfg["cpa_floor"]))
        if cpa_target > 0 and new_cpa < cpa_target:
            return {
                "action": "cpa_decrease",
                "lever": "cpa",
                "to_value": new_cpa,
                "utilization": round(utilization, 3),
                "reason": f"window ROAS {roas:.2f} below {target - band:.2f} — walk CPA target down {step:.0%} and monitor delivery",
            }
        return {
            "action": "budget_decrease",
            "lever": "budget",
            "to_value": round(budget * (1 - float(cfg["budget_down_pct"])), 2),
            "utilization": round(utilization, 3),
            "reason": f"window ROAS {roas:.2f} with CPA target already at floor ${float(cfg['cpa_floor']):.0f} — reduce budget",
        }
    return None


def build_squad_optimizer(
    ads: list[dict[str, Any]],
    squads: list[dict[str, Any]],
    cfg: dict[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or _now_utc()
    delay = int(cfg["attribution_delay_days"])
    days = int(cfg["squad_window_days"])
    ad_to_squad = {str(a.get("id")): str(a.get("ad_squad_id") or "") for a in ads if a.get("id")}

    def _window_rows(start: str, end: str) -> dict[str, dict[str, float]]:
        payload = _run_json(
            _snap_cli(
                "report", "stats", "--entity", "ad_account", "--granularity", "TOTAL",
                "--start-time", start, "--end-time", end,
                "--fields", "spend,impressions,swipes,conversion_purchases,conversion_purchases_value",
                "--breakdown", "ad", "--include-empty",
            )
        )
        agg: dict[str, dict[str, float]] = {}
        for block in payload.get("total_stats") or []:
            inner = block.get("total_stat") or {}
            for row in (inner.get("breakdown_stats") or {}).get("ad", []):
                stats = row.get("stats") or {}
                squad_id = ad_to_squad.get(str(row.get("id")), "")
                if not squad_id:
                    continue
                g = agg.setdefault(squad_id, {"spend": 0.0, "revenue": 0.0, "purchases": 0.0})
                g["spend"] += _money_from_snap(stats.get("spend"))
                g["revenue"] += _money_from_snap(stats.get("conversion_purchases_value"))
                g["purchases"] += _num(stats.get("conversion_purchases"))
        return agg

    cur_start, cur_end = _delayed_window_bounds(days, delay, now)
    prev_start, prev_end = _delayed_window_bounds(days * 2, delay, now)[0], cur_start
    current = _window_rows(cur_start, cur_end)
    previous = _window_rows(prev_start, prev_end)

    ledger = _load_squad_ledger()
    total_spend = sum(g["spend"] for g in current.values())
    total_revenue = sum(g["revenue"] for g in current.values())
    governor = _squad_governor(total_spend, total_revenue, cfg)

    squad_rows: list[dict[str, Any]] = []
    squad_actions: list[dict[str, Any]] = []
    for squad in squads:
        squad_id = str(squad.get("id") or "")
        if not squad_id:
            continue
        budget = _money_from_snap(squad.get("daily_budget_micro"))
        cpa_target = _money_from_snap(squad.get("bid_micro"))
        win = current.get(squad_id, {"spend": 0.0, "revenue": 0.0, "purchases": 0.0})
        prev = previous.get(squad_id, {"spend": 0.0, "revenue": 0.0, "purchases": 0.0})
        roas = (win["revenue"] / win["spend"]) if win["spend"] else 0.0
        prev_roas = (prev["revenue"] / prev["spend"]) if prev["spend"] else 0.0
        utilization = win["spend"] / (budget * days) if budget > 0 else 0.0
        row = {
            "ad_squad_id": squad_id,
            "name": squad.get("name") or "",
            "status": squad.get("status") or "",
            "campaign_id": squad.get("campaign_id") or "",
            "bid_strategy": squad.get("bid_strategy") or "",
            "daily_budget": budget,
            "cpa_target": cpa_target,
            "window": {
                "start": cur_start,
                "end": cur_end,
                "spend": round(win["spend"], 2),
                "revenue": round(win["revenue"], 2),
                "purchases": int(win["purchases"]),
                "roas": round(roas, 3),
                "cpa": round(win["spend"] / win["purchases"], 2) if win["purchases"] else None,
                "utilization": round(utilization, 3),
            },
            "previous_window": {
                "spend": round(prev["spend"], 2),
                "roas": round(prev_roas, 3),
                "purchases": int(prev["purchases"]),
            },
            "on_cooldown": _squad_changed_within_hours(squad_id, ledger, float(cfg["squad_cooldown_hours"]), now),
        }
        squad_rows.append(row)
        if str(squad.get("status") or "").upper() != "ACTIVE":
            continue
        action = classify_squad(budget=budget, cpa_target=cpa_target, window={"spend": win["spend"], "revenue": win["revenue"], "purchases": win["purchases"]}, cfg=cfg)
        if action:
            squad_actions.append({
                "ad_squad_id": squad_id,
                "name": row["name"],
                "daily_budget": budget,
                "cpa_target": cpa_target,
                "window_roas": row["window"]["roas"],
                "on_cooldown": row["on_cooldown"],
                **action,
            })

    return {
        "window": {"start": cur_start, "end": cur_end, "days": days, "attribution_delay_days": delay},
        "governor": governor,
        "squads": squad_rows,
        "squad_actions": squad_actions,
    }


def validate_squad_plan(squad_ctx: dict[str, Any], plan: list[dict[str, Any]], *, now: dt.datetime | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(squad_ctx.get("config") or {})
    now = now or _now_utc()
    squads = {str(r.get("ad_squad_id")): r for r in squad_ctx.get("squads") or []}
    governor = squad_ctx.get("governor") or {}
    mode = governor.get("mode") or "rebalance"
    ledger = _load_squad_ledger()
    increases = {"budget_increase", "cpa_increase"}
    valid_actions = {"budget_increase", "budget_decrease", "cpa_increase", "cpa_decrease"}

    approved: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for item in plan:
        squad_id = str(item.get("ad_squad_id") or "").strip()
        action = str(item.get("action") or "").strip()
        to_value = _num(item.get("to_value"))
        reason = str(item.get("reason") or "agent squad change").strip()
        row = squads.get(squad_id)
        base = {"ad_squad_id": squad_id, "action": action, "to_value": to_value, "reason": reason}
        if not row:
            blocked.append({**base, "decision": "blocked_unknown_squad"})
            continue
        base["name"] = row.get("name") or ""
        if action not in valid_actions:
            blocked.append({**base, "decision": "blocked_invalid_action"})
            continue
        if str(row.get("status") or "").upper() != "ACTIVE":
            blocked.append({**base, "decision": "blocked_not_active"})
            continue
        if mode == "cut_only" and action in increases:
            blocked.append({**base, "decision": "blocked_governor_cut_only"})
            continue
        if _squad_changed_within_hours(squad_id, ledger, float(cfg["squad_cooldown_hours"]), now):
            blocked.append({**base, "decision": "blocked_cooldown"})
            continue
        if action.startswith("budget"):
            current = _num(row.get("daily_budget"))
            cap = float(cfg["max_budget_change_pct_per_run"])
        else:
            current = _num(row.get("cpa_target"))
            cap = float(cfg["max_cpa_change_pct_per_run"])
            if not (float(cfg["cpa_floor"]) <= to_value <= float(cfg["cpa_ceiling"])):
                blocked.append({**base, "decision": "blocked_cpa_bounds"})
                continue
        if current <= 0 or to_value <= 0:
            blocked.append({**base, "decision": "blocked_bad_values"})
            continue
        change_pct = abs(to_value - current) / current
        if change_pct > cap:
            blocked.append({**base, "decision": "blocked_change_cap", "change_pct": round(change_pct, 3)})
            continue
        wrong_direction = (action.endswith("increase") and to_value <= current) or (action.endswith("decrease") and to_value >= current)
        if wrong_direction:
            blocked.append({**base, "decision": "blocked_wrong_direction"})
            continue
        if len(approved) >= int(cfg["max_squad_changes_per_run"]):
            blocked.append({**base, "decision": "blocked_change_count_cap"})
            continue
        approved.append({**base, "from_value": current, "decision": "approved"})

    return {
        "approved": approved,
        "blocked": blocked,
        "governor_mode": mode,
        "summary": {"approved": len(approved), "blocked": len(blocked), "max_changes": int(cfg["max_squad_changes_per_run"])},
    }


def execute_squad_plan(squad_ctx: dict[str, Any], plan: list[dict[str, Any]], *, execute: bool = False) -> dict[str, Any]:
    validation = validate_squad_plan(squad_ctx, plan)
    if not execute:
        validation["execute"] = False
        return validation
    env = os.environ.copy()
    env["SNAPCHAT_ADS_SAFE"] = "1"
    results: list[dict[str, Any]] = []
    ledger_entries: list[dict[str, Any]] = []
    for row in validation["approved"]:
        if row["action"].startswith("budget"):
            cmd = [str(SNAP_SAFE), "adsquad", "update", row["ad_squad_id"], "--daily-budget", f"{row['to_value']:.2f}", "--execute"]
        else:
            bid_micro = int(round(row["to_value"] * 1_000_000))
            cmd = [str(SNAP_SAFE), "adsquad", "update", row["ad_squad_id"], "--fields-json", json.dumps({"bid_micro": bid_micro}), "--execute"]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
        ok = proc.returncode == 0
        results.append({
            **row,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-800:],
            "stderr_tail": (proc.stderr or "")[-800:],
        })
        if ok:
            ledger_entries.append({
                "timestamp": _now_utc().isoformat().replace("+00:00", "Z"),
                "ad_squad_id": row["ad_squad_id"],
                "name": row.get("name") or "",
                "action": row["action"],
                "from_value": row["from_value"],
                "to_value": row["to_value"],
                "reason": row.get("reason") or "",
            })
    if ledger_entries:
        _append_squad_ledger(ledger_entries)
    validation["execute"] = True
    validation["results"] = results
    validation["ledger_appended"] = len(ledger_entries)
    return validation


def fetch_inventory_lp_choice() -> dict[str, Any]:
    """Pick the landing page per product by current Shopify available inventory."""
    if not DASHBOARD_SQL.exists():
        return {"error": f"dashboard SQL helper missing at {DASHBOARD_SQL}", "chosen": {}}
    template_file = str(_LP_INVENTORY_CFG.get("sql_template_file") or "").strip()
    try:
        template = _read_sql_template(template_file) if template_file else ""
        newest = ""
        if not template:
            argv, env = _bridge_query(INVENTORY_NEWEST_SQL, "Snap weekly bridge LP selection: newest inventory snapshot", _LP_INVENTORY_CFG)
            newest_payload = _run_json(argv, timeout=45, env=env)
            newest_rows = newest_payload.get("rows") or []
            newest = str((newest_rows[0] or {}).get("newest") or "")[:10] if newest_rows else ""
            if not newest:
                return {"error": "no inventory snapshots found", "chosen": {}}
        all_rows: list[dict[str, Any]] = []
        for product in LP_CHOICES:
            if template:
                sql = _render_inventory_template(template, LP_HANDLE_PREFIX[product])
            else:
                sql = INVENTORY_ROWS_SQL_TEMPLATE.format(
                    prefix=LP_HANDLE_PREFIX[product], newest=newest,
                )
            argv, env = _bridge_query(sql, f"Snap weekly bridge LP selection by Shopify inventory ({product})", _LP_INVENTORY_CFG)
            payload = _run_json(argv, timeout=45, env=env)
            all_rows.extend(r for r in payload.get("rows") or [] if isinstance(r, dict))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "chosen": {}}
    inventory: dict[str, float] = {}
    for r in all_rows:
        handle = str(r.get("handle"))
        inventory[handle] = inventory.get(handle, 0.0) + max(_num(r.get("available")), 0.0)
    chosen: dict[str, Any] = {}
    for product, choices in LP_CHOICES.items():
        ranked = sorted(choices, key=lambda c: inventory.get(c["handle"], 0.0), reverse=True)
        chosen[product] = {
            **ranked[0],
            "available": inventory.get(ranked[0]["handle"], 0.0),
            "alternatives": [{**c, "available": inventory.get(c["handle"], 0.0)} for c in ranked[1:]],
        }
    return {"inventory": inventory, "chosen": chosen}


def fetch_meta_winner_candidates() -> dict[str, Any]:
    registry = _load_registry_events()
    existing_ad_ids = {str(row.get("source_ad_id")) for row in registry if row.get("source_ad_id")}
    if not DASHBOARD_SQL.exists():
        return {"error": f"dashboard SQL helper missing at {DASHBOARD_SQL}", "rows": [], "new_rows": []}
    try:
        argv, env = _bridge_query(
            META_WINNERS_SQL,
            "Snap weekly Meta winner refresh candidates for "
            + ", ".join(route["label"] for route in ROUTES.values()),
            _META_WINNERS_CFG,
        )
        payload = _run_json(argv, timeout=45, env=env)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "rows": [], "new_rows": [], "registry_rows": len(registry)}
    rows = payload.get("rows") or []
    routed_rows: list[dict[str, Any]] = []
    new_rows: list[dict[str, Any]] = []
    held_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dpa_route = resolve_product_route(row, placement="dpa")
        nondpa_route = resolve_product_route(row, placement="nondpa")
        enriched = dict(row)
        enriched["already_in_registry"] = str(row.get("ad_id") or "") in existing_ad_ids
        enriched["dpa_route"] = dpa_route
        enriched["nondpa_route"] = nondpa_route
        routed_rows.append(enriched)
        if not dpa_route or not nondpa_route:
            held_rows.append(enriched)
        elif not enriched["already_in_registry"]:
            new_rows.append(enriched)
    return {
        "rows": routed_rows,
        "new_rows": new_rows,
        "held_rows": held_rows,
        "row_count": len(routed_rows),
        "new_count": len(new_rows),
        "registry_rows": len(registry),
        "sql_duration_ms": payload.get("durationMs"),
    }


def _summarize_context(context: dict[str, Any], context_path: Path) -> str:
    ads = context.get("ads") or []
    active = [ad for ad in ads if _is_active(ad)]
    candidates = [ad for ad in ads if ad.get("id") in set(context.get("pause_candidates") or [])]
    sample_plan = json.dumps([{"ad_id": "...", "reason": "..."}])
    lines = [
        f"SNAP_OPTIMIZER_CONTEXT_FILE={context_path}",
        f"mode={context.get('mode')} generated_at={context.get('generated_at')}",
        f"active_ads={len(active)} pause_candidates={len(candidates)} guardrail_holds={len(context.get('guardrail_holds') or [])}",
        "",
        "Pause candidates for agent review:",
        "| Ad | Product | 7d spend | 7d purchases | 14d spend | Signal |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for ad in sorted(candidates, key=lambda row: float(row.get("windows", {}).get("7", {}).get("spend") or 0), reverse=True)[:12]:
        w7 = ad.get("windows", {}).get("7", {})
        w14 = ad.get("windows", {}).get("14", {})
        lines.append(
            "| {name} | {product} | ${spend7:.0f} | {purchases7} | ${spend14:.0f} | {signal} |".format(
                name=str(ad.get("name") or ad.get("id") or "")[:70].replace("|", "/"),
                product=ad.get("product") or "unknown",
                spend7=float(w7.get("spend") or 0),
                purchases7=int(w7.get("purchases") or 0),
                spend14=float(w14.get("spend") or 0),
                signal=str(ad.get("pause_signal") or "")[:70].replace("|", "/"),
            )
        )
    if not candidates:
        lines.append("| None | - | $0 | 0 | $0 | No qualified candidates |")
    if context.get("meta_candidates") is not None:
        meta = context["meta_candidates"]
        lines.extend(
            [
                "",
                "Weekly Meta winner refresh context:",
                f"meta_rows={meta.get('row_count', 0)} new_after_registry_dedupe={meta.get('new_count', 0)} registry_rows={meta.get('registry_rows', 0)}",
                "| Product | Meta ad | Spend | ROAS | DPA squad | Non-DPA squad |",
                "| --- | --- | ---: | ---: | --- | --- |",
            ]
        )
        for row in (meta.get("new_rows") or [])[:12]:
            dpa = row.get("dpa_route") or {}
            nondpa = row.get("nondpa_route") or {}
            lines.append(
                "| {product} | {ad_id} | ${spend} | {roas} | {dpa} | {nondpa} |".format(
                    product=row.get("product_type") or "",
                    ad_id=row.get("ad_id") or "",
                    spend=row.get("spend") or "0",
                    roas=row.get("roas") or "-",
                    dpa=dpa.get("ad_squad_id", "hold"),
                    nondpa=nondpa.get("ad_squad_id", "hold"),
                )
            )
        if not meta.get("new_rows"):
            lines.append("| None | - | $0 | - | - | - |")
    squad_opt = context.get("squad_optimizer") or {}
    governor = squad_opt.get("governor") or {}
    if governor:
        lines.extend(
            [
                "",
                "Squad optimizer (delay-aware window {start}..{end}):".format(
                    start=str((squad_opt.get("window") or {}).get("start") or "")[:10],
                    end=str((squad_opt.get("window") or {}).get("end") or "")[:10],
                ),
                "governor mode={mode} blended_window_roas={roas} target={target} (eventual {eventual})".format(
                    mode=governor.get("mode"),
                    roas=governor.get("blended_roas_window"),
                    target=governor.get("target_roas"),
                    eventual=governor.get("eventual_target_roas"),
                ),
                "| Squad | Budget | CPA target | Win spend | ROAS | Util | Suggested action | Cooldown |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        actions_by_squad = {a["ad_squad_id"]: a for a in squad_opt.get("squad_actions") or []}
        for row in sorted(squad_opt.get("squads") or [], key=lambda r: -float((r.get("window") or {}).get("spend") or 0)):
            if str(row.get("status") or "").upper() != "ACTIVE":
                continue
            win = row.get("window") or {}
            act = actions_by_squad.get(row.get("ad_squad_id")) or {}
            suggestion = f"{act.get('action')} -> {act.get('to_value')}" if act else "hold"
            lines.append(
                "| {name} | ${budget:.0f} | ${cpa:.2f} | ${spend:.0f} | {roas:.2f} | {util:.0%} | {sugg} | {cool} |".format(
                    name=str(row.get("name") or row.get("ad_squad_id"))[:44].replace("|", "/"),
                    budget=float(row.get("daily_budget") or 0),
                    cpa=float(row.get("cpa_target") or 0),
                    spend=float(win.get("spend") or 0),
                    roas=float(win.get("roas") or 0),
                    util=float(win.get("utilization") or 0),
                    sugg=suggestion,
                    cool="yes" if row.get("on_cooldown") else "no",
                )
            )
    if context.get("inventory_lp"):
        chosen = (context["inventory_lp"] or {}).get("chosen") or {}
        lines.append("")
        lines.append("Inventory-selected landing pages this week:")
        for product, choice in chosen.items():
            lines.append(f"- {product}: {choice.get('landing_page')} ({choice.get('label')}, {choice.get('available'):.0f} units available)")
    sample_squad_plan = json.dumps([{"ad_squad_id": "...", "action": "cpa_decrease", "to_value": 60.0, "reason": "..."}])
    lines.extend(
        [
            "",
            "Guardrail executors:",
            f"python3 {__file__} apply-pause-plan --context-file {context_path} --plan-json '{sample_plan}' --execute",
            f"python3 {__file__} apply-squad-plan --context-file {context_path} --plan-json '{sample_squad_plan}' --execute",
            "",
            json.dumps({"wakeAgent": True}),
        ]
    )
    return "\n".join(lines)


def write_context(*, include_meta: bool = False, output_path: Path | None = None) -> Path:
    context = build_context(include_meta=include_meta)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = output_path or CACHE_DIR / ("weekly_latest_context.json" if include_meta else "daily_latest_context.json")
    path.write_text(json.dumps(context, indent=2), encoding="utf-8")
    return path


def collect_and_print(*, include_meta: bool = False) -> int:
    path = write_context(include_meta=include_meta)
    context = json.loads(path.read_text(encoding="utf-8"))
    print(_summarize_context(context, path))
    return 0


def _read_json_arg(value: str) -> Any:
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def build_cron_jobs() -> list[dict[str, Any]]:
    cfg = DEFAULT_CONFIG
    product_labels = [route["label"] for route in ROUTES.values()]
    product_label_list = " and ".join(product_labels) if len(product_labels) <= 2 else ", ".join(product_labels)
    target_roas = cfg["target_roas"]
    eventual_target_roas = cfg["eventual_target_roas"]
    utilization_full = cfg["utilization_full"]
    cpa_step_up_pct = cfg["cpa_step_up_pct"]
    cpa_step_down_pct = cfg["cpa_step_down_pct"]
    cpa_step_down_pct_severe = cfg["cpa_step_down_pct_severe"]
    cpa_floor = cfg["cpa_floor"]
    cpa_ceiling = cfg["cpa_ceiling"]
    max_budget_change_pct = cfg["max_budget_change_pct_per_run"]
    max_cpa_change_pct = cfg["max_cpa_change_pct_per_run"]
    squad_cooldown_hours = cfg["squad_cooldown_hours"]
    fresh_hold_hours = cfg["fresh_hold_hours"]
    min_spend = _META_WINNERS_CFG.get("min_spend", 250.0)
    min_roas = _META_WINNERS_CFG.get("min_roas", 1.5)
    lookback_days = _META_WINNERS_CFG.get("lookback_days", 30)

    daily_prompt = f"""
Fully autonomous Snapchat optimizer for {BRAND_NAME} (mirrors the Meta optimizer).

You are the master media buyer for {BRAND_NAME} Snapchat. Use the script output context and the full context JSON file. Execute without asking for approval — Slack is an audit trail, not an approval gate.

Snap attribution lags ~{cfg["attribution_delay_days"]} days: NEVER judge on yesterday/today. The `squad_optimizer` section of the context is built on the {cfg["squad_window_days"]} completed days ending {cfg["attribution_delay_days"]} days ago — that is your decision window. Goal: blended window ROAS >= {target_roas} now (eventual target {eventual_target_roas}) while spending as much as possible at that ROAS.

Decision playbook (per ad squad, all TARGET_COST):
- Obey `squad_optimizer.governor.mode`: cut_only = only cpa_decrease/budget_decrease/ad pauses; rebalance = fund winners by tightening losers; scale = net delivery increases allowed.
- Review `squad_optimizer.squad_actions` (pre-classified by the master rules) and sanity-check each against the squad table and prior window trend. Skip anything `on_cooldown` ({squad_cooldown_hours}h ledger).
- The CPA target is the primary spend lever: walk it DOWN ~{cpa_step_down_pct:.0%}-{cpa_step_down_pct_severe:.0%} to cut spend on sub-target squads (budgets were set high for delivery — cutting budget first just strands the CPA target); walk it UP ~{cpa_step_up_pct:.0%} to buy delivery ONLY when window ROAS is above target but budget utilization is under {utilization_full:.0%} (raising budget there does nothing).
- Raise budget only when ROAS >= target band AND utilization >= {utilization_full:.0%}.
- Execute squad changes via `snap_optimizer_support.py apply-squad-plan --context-file <ctx> --plan-json '[...]' --execute` — never raw CLI. The executor enforces governor mode, cooldowns, step caps (budget +/-{max_budget_change_pct:.0%}, CPA +/-{max_cpa_change_pct:.0%} per run), and CPA bounds (${cpa_floor:.0f}-${cpa_ceiling:.0f}).
- Ad pauses still go through `apply-pause-plan` exactly as before (zero-purchase / high-CPA candidates with fresh-hold ({fresh_hold_hours:.0f}h) and coverage guardrails).
- If an executor blocks something, report it as a protected hold — do not retry or bypass.

Slack output (ALWAYS post — never [SILENT]):
- Final response must be valid JSON only with string keys `main_message` and `thread_message`.
- Main message: governor mode, blended window ROAS vs {target_roas} target, count of changes (or 'no changes'), ending with `:thread: Full details in thread :point_down:`.
- Thread: At a glance, Actions taken (from -> to with reasons), Holds/skips, Delivery watch (utilization per squad after CPA walks), Next watchlist, Audit appendix.
- Use product labels from the config, not raw IDs, except in the audit appendix.

Preapproved scope:
- Automatic daily squad budget changes, CPA-target changes, and ad pauses inside the executor guardrails are approved. Deletions and archives are NOT approved.

Channel: {SLACK_CHANNEL_ID}
""".strip()

    weekly_prompt = f"""
Fully autonomous weekly Meta-to-Snap creative refresh for {BRAND_NAME}.

Use the script output context and full context JSON file. Pull this week's top-spending high-ROAS Meta ads (spend >= ${min_spend:.0f}, ROAS >= {min_roas}, trailing {lookback_days} days) for {product_label_list} into Snap. Inspect the deduped Meta winner list (`meta_candidates.new_rows`), the Snap bridge registry, and live Snap state before deciding what to add.

Execution scope (no approval step — execute):
- Add new Meta winners not already in the registry. The registry dedupe is mandatory: never re-create a source_ad_id that exists in the registry (including `snap_ad_removed_externally` tombstones).
- Landing pages come from `inventory_lp.chosen` in the context — each product routes to whichever configured landing page has more Shopify inventory this week. Use that LP for non-DPA web-view creatives; note the choice in the report.
- Each product's winners must route to that product's own campaigns/ad squads/product sets (use product labels from the config, not raw IDs). Ambiguous routing must be held, not created.
- Preserve the DPA and non-DPA split: DPA collection ads and non-DPA web-view ads when source media is available.
- Create Snap media/creatives/ads, preview each write, then execute. After creation succeeds and delivery_status has no blocking reasons, LAUNCH the new ads (status ACTIVE) — the daily optimizer's {fresh_hold_hours:.0f}h fresh-hold protects them from premature pausing, and squad budgets/CPA targets are managed by the daily run. If a created ad has invalid delivery reasons, leave it PAUSED and report why.
- Append every successful write to `{BRIDGE_REGISTRY}` with source_ad_id/source_video_id/snap_media_id/snap_creative_id/snap_ad_id/status (including the launch event).

Useful constants from the context:
- DPA dynamic template, interaction zone, profile, product sets, and ad squads are in `routes` and `snap_shared`.
- Use the existing `snapchat-ads` CLI and `snapchat_ads_safe.sh`.

Slack output (ALWAYS post — never [SILENT], even when nothing new):
- Final response must be valid JSON only with string keys `main_message` and `thread_message`.
- Main message: how many winners bridged + launched (or 'no new winners this week'), which LPs won on inventory, ending with `:thread: Full details in thread :point_down:`.
- Thread: At a glance, Created + launched ads, Holds, Inventory LP decision, Product routing checks, and Audit appendix.

Channel: {SLACK_CHANNEL_ID}
""".strip()

    cron_model = _CRON_CFG.get("model")
    cron_skills = list(_CRON_CFG.get("skills") or [])
    daily_expr = str(_CRON_CFG.get("daily_expr") or "20 8 * * *")
    weekly_expr = str(_CRON_CFG.get("weekly_expr") or "35 9 * * 1")

    model_fields: dict[str, Any] = {"model": cron_model}
    if cron_model:
        provider = cron_model.split("/", 1)[0] if "/" in cron_model else None
        if provider:
            model_fields["provider"] = provider

    return [
        {
            "id": "5ad17e01aa01",
            "name": "snapchat-optimizer-daily",
            "prompt": daily_prompt,
            "skills": cron_skills,
            "skill": "snapchat-ads",
            **model_fields,
            "script": "snap_optimizer_daily_context.py",
            "no_agent": False,
            "schedule": {"kind": "cron", "expr": daily_expr, "display": daily_expr, "tz": CRON_TZ},
            "schedule_display": daily_expr,
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "deliver": f"slack:{SLACK_CHANNEL_ID}",
            "workdir": str(SKILL_DIR),
            "agent_id": None,
            "threaded_slack": True,
        },
        {
            "id": "5ad17e01aa02",
            "name": "snapchat-meta-winners-weekly",
            "prompt": weekly_prompt,
            "skills": cron_skills,
            "skill": "snapchat-ads",
            **model_fields,
            "script": "snap_meta_winners_weekly_context.py",
            "no_agent": False,
            "schedule": {"kind": "cron", "expr": weekly_expr, "display": weekly_expr, "tz": CRON_TZ},
            "schedule_display": weekly_expr,
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "deliver": f"slack:{SLACK_CHANNEL_ID}",
            "workdir": str(SKILL_DIR),
            "agent_id": None,
            "threaded_slack": True,
        },
    ]


def _next_run_at(expr: str, now: dt.datetime | None = None) -> str:
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported cron expression: {expr}")
    minute_s, hour_s, day_s, month_s, dow_s = fields
    if day_s != "*" or month_s != "*":
        raise ValueError(f"unsupported cron day/month expression: {expr}")

    minute = int(minute_s)
    hour = int(hour_s)
    tz = ZoneInfo(CRON_TZ)
    current = (now or dt.datetime.now(tz)).astimezone(tz)

    if dow_s == "*":
        candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= current:
            candidate += dt.timedelta(days=1)
        return candidate.isoformat()

    cron_dow = int(dow_s)
    if cron_dow == 7:
        cron_dow = 0
    target_weekday = 6 if cron_dow == 0 else cron_dow - 1
    days_ahead = (target_weekday - current.weekday()) % 7
    candidate = (current + dt.timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += dt.timedelta(days=7)
    return candidate.isoformat()


def install_cron_jobs(jobs_path: Path | None = None) -> dict[str, Any]:
    jobs_path = jobs_path or CRON_JOBS
    if jobs_path is None:
        raise RuntimeError(
            f"No cron jobs path configured. Pass --jobs-path or set {CRON_JOBS_ENV_VAR}."
        )
    desired = build_cron_jobs()
    now = dt.datetime.now(ZoneInfo(CRON_TZ))
    now_iso = now.isoformat()
    for job in desired:
        job["next_run_at"] = _next_run_at(str(job["schedule"]["expr"]), now=now)
        job["created_at"] = job.get("created_at") or now_iso
        job["updated_at"] = now_iso

    data = {"jobs": [], "updated_at": now_iso}
    original_text = ""
    if jobs_path.exists():
        original_text = jobs_path.read_text(encoding="utf-8")
        data = json.loads(original_text)
    jobs = data.setdefault("jobs", [])
    desired_ids = {job["id"] for job in desired}
    desired_names = {job["name"] for job in desired}
    kept = [job for job in jobs if job.get("id") not in desired_ids and job.get("name") not in desired_names]
    removed = len(jobs) - len(kept)
    data["jobs"] = kept + desired
    data["updated_at"] = now_iso

    new_text = json.dumps(data, indent=2) + "\n"
    backup_path = None
    if new_text != original_text:
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        if original_text:
            backup_path = jobs_path.with_name(f"jobs.json.bak-{now.strftime('%Y%m%d%H%M%S')}")
            backup_path.write_text(original_text, encoding="utf-8")
        jobs_path.write_text(new_text, encoding="utf-8")

    return {
        "jobs_path": str(jobs_path),
        "backup_path": str(backup_path) if backup_path else None,
        "installed": [{"id": job["id"], "name": job["name"], "next_run_at": job["next_run_at"]} for job in desired],
        "removed_replaced": removed,
        "changed": new_text != original_text,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Snapchat optimizer support")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect-daily")
    sub.add_parser("collect-weekly")
    route_parser = sub.add_parser("route-candidates")
    route_parser.add_argument("--json", required=True)
    pause_parser = sub.add_parser("apply-pause-plan")
    pause_parser.add_argument("--context-file", required=True)
    pause_parser.add_argument("--plan-json", required=True)
    pause_parser.add_argument("--execute", action="store_true")
    squad_parser = sub.add_parser("apply-squad-plan")
    squad_parser.add_argument("--context-file", required=True)
    squad_parser.add_argument("--plan-json", required=True)
    squad_parser.add_argument("--execute", action="store_true")
    sub.add_parser("inventory-lp")
    cron_parser = sub.add_parser("print-cron-jobs")
    cron_parser.add_argument("--json", action="store_true")
    install_parser = sub.add_parser("install-cron-jobs")
    install_parser.add_argument("--jobs-path")
    args = parser.parse_args(argv)

    if args.cmd == "collect-daily":
        return collect_and_print(include_meta=False)
    if args.cmd == "collect-weekly":
        return collect_and_print(include_meta=True)
    if args.cmd == "route-candidates":
        rows = _read_json_arg(args.json)
        routed = []
        for row in rows:
            routed.append({"row": row, "dpa_route": resolve_product_route(row, placement="dpa"), "nondpa_route": resolve_product_route(row, placement="nondpa")})
        print(json.dumps(routed, indent=2))
        return 0
    if args.cmd == "apply-pause-plan":
        context = json.loads(Path(args.context_file).read_text(encoding="utf-8"))
        plan = _read_json_arg(args.plan_json)
        if not isinstance(plan, list):
            raise SystemExit("plan-json must be a JSON list")
        print(json.dumps(execute_pause_plan(context, plan, execute=args.execute), indent=2))
        return 0
    if args.cmd == "apply-squad-plan":
        context = json.loads(Path(args.context_file).read_text(encoding="utf-8"))
        squad_ctx = context.get("squad_optimizer") or context
        plan = _read_json_arg(args.plan_json)
        if not isinstance(plan, list):
            raise SystemExit("plan-json must be a JSON list")
        print(json.dumps(execute_squad_plan(squad_ctx, plan, execute=args.execute), indent=2))
        return 0
    if args.cmd == "inventory-lp":
        print(json.dumps(fetch_inventory_lp_choice(), indent=2))
        return 0
    if args.cmd == "print-cron-jobs":
        print(json.dumps(build_cron_jobs(), indent=2))
        return 0
    if args.cmd == "install-cron-jobs":
        jobs_path = Path(args.jobs_path).expanduser() if args.jobs_path else None
        print(json.dumps(install_cron_jobs(jobs_path), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
