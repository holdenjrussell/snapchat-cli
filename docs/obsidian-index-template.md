# Snapchat Ads Data Catalog

> Copy this file into your notes vault (e.g. `<VAULT_NAME>/Data Access/Snapchat Ads Catalog.md`)
> and fill in the `<PLACEHOLDER>` slots. Everything else is copy-paste ready.

Generated: `<GENERATION_DATE>` (`<TIMEZONE>`)

## Purpose

This note is the human- and agent-facing index for the Snapchat Ads
warehouse: what's in it, how fresh it is, and what to ask for it. It exists
so anyone (or any LLM-driven agent) can answer "what's our Snapchat spend
doing" without re-deriving the schema from scratch every time.

Owner: `<OWNER_NAME_OR_TEAM>`
Warehouse instance: `<POSTGRES_PROVIDER>` (e.g. Neon / Supabase / self-hosted)

## Connection

| What | Value |
|---|---|
| Connection string env var | `DATABASE_URL` |
| Where it's set | `<ENV_FILE_OR_SECRETS_MANAGER_PATH>` |
| Query entry point | `<REPO_PATH>/warehouse/query.py` |
| Sync entry point | `<REPO_PATH>/warehouse/sync_snapchat_daily.py` |
| Sync schedule | `<CRON_OR_SCHEDULER_DESCRIPTION>` (e.g. "daily at 06:00 <TZ> via systemd timer") |
| Snap account key | `<SNAPCHAT_ADS_ACCOUNT_VALUE>` (matches `~/.config/snapchat-ads-cli/accounts.toml`) |

Read access for agents/automation should always go through `query.py`
(guarded, read-only, JSON-out) rather than opening a direct database
connection — see the warehouse's own `README.md` for the guardrail
contract.

## Source Query Rules

- Pick the narrowest table/query that already contains the requested grain — this catalog currently ships one table (`snapchat_ad_daily_metrics`, ad-level daily grain).
- Use `recorded_at` for any "last N days" / date-range request.
- Default time filter for exploratory questions: `recorded_at >= CURRENT_DATE - INTERVAL '30 days'`.
- Always scope with a `LIMIT` (see `query.py`'s `--limit-guard`, default 5000) — don't return unbounded row counts to a chat surface.
- Return scoped, purpose-fit output: a trend chart for time series, a table for rankings, a single KPI card for one headline number.
- Do not expose this catalog's raw SQL cookbook queries verbatim to end users without adapting the date range/filters to what was actually asked.

## Table Inventory

| Table | Grain | Approx rows | Date column | Key metrics | Join/drill columns |
|---|---|---:|---|---|---|
| `snapchat_ad_daily_metrics` | ad × day | `<APPROX_ROW_COUNT>` | `recorded_at` | `spend`, `impressions`, `swipes`, `conversions`, `revenue`, `roas` | `ad_id`, `ad_squad_id`, `campaign_id` |

Full column-by-column reference (types, Snap API source field, derivation
formulas) lives in `<REPO_PATH>/warehouse/table-map.md` — don't duplicate
it here; link to it.

## Column Quick Reference

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` | Surrogate PK. |
| `recorded_at` | `date` | Calendar day, not a timestamp. Primary date filter column. |
| `campaign_id` / `campaign_name` | `text` | Denormalized from the campaign entity via ad squad lookup. |
| `ad_squad_id` / `ad_squad_name` | `text` | Denormalized from the ad squad entity. |
| `ad_id` / `ad_name` | `text` | `ad_id` is half of the upsert key `(ad_id, recorded_at)`. |
| `spend`, `revenue` | `numeric` | USD, converted from Snap's micro-currency at sync time. |
| `impressions`, `swipes`, `conversions`, `video_views`, `video_views_p50`, `video_views_p100` | `integer` | Raw counts. |
| `swipe_up_rate`, `cpm`, `cost_per_swipe`, `roas` | `numeric` | Derived ratios; `NULL` when the denominator is zero. |

## Natural-Language Query Prompts

Use these as few-shot examples for an LLM-driven query agent, or as a
manual checklist of what this catalog should be able to answer:

- "Show Snapchat ad spend for the last 30 days."
- "Which Snapchat ads spent the most in the last 7 days?"
- "What's our blended Snapchat ROAS this week vs last week?"
- "Which Snapchat campaigns are spending with zero purchases?"
- "What's the CPA by ad squad over the last 7 days?"
- "Chart daily Snapchat spend and revenue for the last 30 days."

Matching SQL for each of these lives in the query cookbook in
`<REPO_PATH>/warehouse/table-map.md`.

## Freshness / Health Checks

- **Expected lag:** Snap attribution lags roughly 1-2 days; treat the most
  recent 2 days of `recorded_at` as provisional.
- **Sync cadence:** `<SYNC_CADENCE>` (fill in once the scheduler is wired
  up — e.g. "daily, trailing 7-day re-pull").
- **Manual freshness check:**
  ```bash
  uv run <REPO_PATH>/warehouse/query.py \
    --sql "SELECT MAX(recorded_at) AS latest_day, MAX(created_at) AS last_sync_write FROM snapchat_ad_daily_metrics" \
    --reason "freshness check"
  ```
  Alert if `latest_day` is more than `<STALENESS_THRESHOLD>` behind
  `CURRENT_DATE`.
- **Row-count sanity check:**
  ```bash
  uv run <REPO_PATH>/warehouse/query.py \
    --sql "SELECT recorded_at, COUNT(*) AS ad_rows FROM snapchat_ad_daily_metrics WHERE recorded_at >= current_date - 7 GROUP BY 1 ORDER BY 1" \
    --reason "freshness check: row counts per day"
  ```
  A day with an unexpectedly low row count (compared to your typical
  active-ad count) usually means the sync ran but the Snap API returned a
  partial/empty payload for that window — re-run
  `sync_snapchat_daily.py --days <N>` covering that day.

## Related Docs

- Column reference + query cookbook: `<REPO_PATH>/warehouse/table-map.md`
- Warehouse setup + sync scheduling: `<REPO_PATH>/warehouse/README.md`
- Snapchat Ads CLI reference: `<REPO_PATH>/skills/snapchat-ads/SKILL.md`
- CLI report command reference: `<REPO_PATH>/cli/src/snapchat_ads_cli/tools/reports.py`
