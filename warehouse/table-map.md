# Snapchat Ads Warehouse Table Map

Examples use the default live schema, `snapchat_ads`. If
`SNAP_WAREHOUSE_SCHEMA` selects another validated schema, replace that prefix.
Repository-generated DDL and SQL always qualify these names explicitly.

## `snapchat_ads.snapchat_ad_daily_metrics`

One row per (ad, calendar day).

## Columns

| Column | Type | Source Snap API field | Derivation |
|---|---|---|---|
| `id` | `uuid` (PK) | - | `pg_catalog.gen_random_uuid()` default. |
| `recorded_at` | `date` NOT NULL | `timeseries[].start_time` (from `report stats`, `granularity=DAY`) | Date portion of the per-day bucket's `start_time`. |
| `campaign_id` | `text` | `adsquad.campaign_id` | Looked up via `ad.ad_squad_id -> adsquad.campaign_id`, not present directly on the stats row. |
| `campaign_name` | `text` | `campaign.name` | Looked up from `campaign list` by `campaign_id`. |
| `ad_squad_id` | `text` | `ad.ad_squad_id` | Looked up from `ad list` by `ad_id`. |
| `ad_squad_name` | `text` | `adsquad.name` | Looked up from `adsquad list` by `ad_squad_id`. |
| `ad_id` | `text` | `breakdown_stats.ad[].id` | Direct from the stats breakdown row. Half of the upsert key. |
| `ad_name` | `text` | `ad.name` | Looked up from `ad list` by `ad_id`. |
| `spend` | `numeric(12,2)` | `stats.spend` (micro) | `spend_micro / 1_000_000`, rounded to cents on write. |
| `impressions` | `integer` | `stats.impressions` | Direct. |
| `swipes` | `integer` | `stats.swipes` | Direct. |
| `swipe_up_rate` | `numeric(6,4)` | derived | `swipes / impressions`, `NULL` when `impressions = 0`. |
| `cpm` | `numeric(14,4)` | derived | `spend / impressions * 1000`, `NULL` when `impressions = 0`. |
| `cost_per_swipe` | `numeric(14,4)` | derived | `spend / swipes`, `NULL` when `swipes = 0`. |
| `video_views` | `integer` | `stats.video_views` | Direct. |
| `video_views_p50` | `integer` | `stats.video_views_time_based` (best effort) | Extracted from the `video_views_time_based` object; Snap does not document one canonical key shape for this field across API versions, so the sync script tries a handful of plausible key aliases and leaves this `NULL` if none match. Verify against a live payload sample for your API version and extend `_P50_KEYS` in `sync_snapchat_daily.py` if needed. |
| `video_views_p100` | `integer` | `stats.video_views_time_based` (best effort) | Same caveat as `video_views_p50`, via `_P100_KEYS`. |
| `conversions` | `integer` | `stats.conversion_purchases` | Direct. |
| `revenue` | `numeric(12,2)` | `stats.conversion_purchases_value` (micro) | `value_micro / 1_000_000`, rounded to cents on write. |
| `roas` | `numeric(14,4)` | derived | `revenue / spend`, `NULL` when `spend = 0`. |
| `created_at` | `timestamp` | - | `pg_catalog.now()` default; not updated on conflict (first-insert timestamp only). |

## Indexes and upsert key

```sql
CREATE UNIQUE INDEX snapchat_ad_daily_ad_date_idx ON snapchat_ads.snapchat_ad_daily_metrics (ad_id, recorded_at);
CREATE INDEX snapchat_ad_daily_campaign_date_idx ON snapchat_ads.snapchat_ad_daily_metrics (campaign_id, recorded_at);
CREATE INDEX snapchat_ad_daily_date_idx ON snapchat_ads.snapchat_ad_daily_metrics (recorded_at);
```

- **Upsert key:** `(ad_id, recorded_at)` - the unique index doubles as the
  `ON CONFLICT` target in `sync_snapchat_daily.py`. Every metric and every
  name column (`campaign_name`, `ad_squad_name`, `ad_name`, etc.) is
  overwritten on conflict; `id` and `created_at` are left untouched so the
  row keeps its original identity and first-seen timestamp across re-syncs.
- **`snapchat_ad_daily_campaign_date_idx`** supports campaign-scoped
  rollups and date-range scans without a full-table scan.
- **`snapchat_ad_daily_date_idx`** supports account-wide date-range scans
  (daily trend queries, freshness checks) independent of any campaign/ad
  filter.

## Freshness expectations

- Snap attribution has a reporting lag of roughly 1-2 days. Treat
  `recorded_at` within the last 2 days as provisional - purchases/revenue/
  ROAS for those days will keep updating on subsequent syncs.
- `--mode closed` (default) re-pulls the trailing closed days on every run
  (`--days`, default 7), so a daily run naturally "catches up"
  late-attributed conversions without any special backfill logic.
- `--mode intraday` pulls **today so far**, through the latest completed hour,
  as rows with `provisional = true` and `source_window_end` set. Each run
  overwrites them; the next closed run replaces them with final values. Run
  it hourly (the cycle's `--mode recent`) if anything, such as a P&L, reports
  today's spend.
- With `breakdown=ad`, Snap nests each day under
  `breakdown_stats.ad[].timeseries[]`; the parser reads the day from each
  entry. `SUM(spend)` per closed day should equal the `ad_account` spend from
  `report stats` to the cent.
- Every `run_snapchat_warehouse_cycle.py` run writes a receipt to
  `snapchat_sync_runs` (running, then succeeded or failed; a later cycle marks
  stale running receipts abandoned). Alert on failed or missing receipts.

## `snapchat_ads.snapchat_entity_state`

One latest row per `(account_id, entity_type, entity_id)`. Campaigns, ad
squads, and ads share one table so a report or reviewer can inspect the
current hierarchy and its control settings with one query.

| Column | Type | Meaning |
|---|---|---|
| `account_id` | `text` | Snap ad account ID. Part of the primary key. |
| `account_key` | `text` | Local `snapchat-ads` CLI account key used for the read. |
| `entity_type` | `text` | `campaign`, `ad_squad`, or `ad`. Part of the primary key. |
| `entity_id` | `text` | Snap entity ID. Part of the primary key. |
| `entity_name` | `text` | Current Snap name. |
| `campaign_id` | `text` | Campaign lineage. For a campaign row, this is the entity's own ID. |
| `ad_squad_id` | `text` | Parent ad squad for ad rows. `NULL` for campaigns and ad squads. |
| `configured_status` | `text` | Operator/API configured `status`, such as `ACTIVE` or `PAUSED`. |
| `effective_status` | `text` | Native `effective_status` when supplied. Otherwise a stable summary derived from configured status, delivery reasons, and review state. |
| `delivery_status` | `jsonb` | Sorted Snap delivery-reason array. |
| `review_status` | `text` | Snap review state, primarily populated on ads. |
| `daily_budget` | `numeric(14,2)` | Daily budget in account currency units. |
| `daily_budget_micro` | `bigint` | Exact source budget in Snap micro-currency. |
| `bid` | `numeric(14,2)` | Current bid amount in account currency units, regardless of bid strategy. |
| `bid_micro` | `bigint` | Exact source `bid_micro` value. |
| `target_cpa` | `numeric(14,2)` | Target CPA in account currency units. Populated from `target_cost_micro`, or from `bid_micro` when `bid_strategy = 'TARGET_COST'`. |
| `target_cpa_micro` | `bigint` | Exact source Target Cost amount in micro-currency. |
| `bid_strategy` | `text` | Current bid strategy. |
| `source_created_at` | `timestamptz` | Entity `created_at` from Snap. |
| `source_updated_at` | `timestamptz` | Entity `updated_at` from Snap. |
| `raw_payload` | `jsonb` | Full Snap entity object after recursive credential-key redaction. |
| `first_seen_at` | `timestamptz` | First successful warehouse observation. Preserved on upsert. |
| `last_seen_at` | `timestamptz` | Latest successful warehouse observation. |

Indexes support parent hierarchy scans, configured-status filters, and
freshness checks. The sync upserts the primary key and never deletes history.

## `snapchat_ads.snapchat_change_history`

One immutable field diff per deterministic `event_key`. The first entity sync
is a baseline and creates no history events. Every later diff is classified as
one of these sources:

- `AGENT_API`: the new value exactly matches an allowlisted local CLI audit
  mutation observed since the prior state snapshot.
- `HUMAN_MANUAL`: a configurable value changed without a matching local CLI
  audit mutation. This includes changes made in Snap Ads Manager and older
  bulk-budget audit entries that did not record IDs/values.
- `SNAP_SYSTEM`: delivery, effective-delivery, or review state changed.

| Column | Type | Meaning |
|---|---|---|
| `event_key` | `text` (PK) | SHA-256 of account, entity, field, old/new values, source, and occurrence time. |
| `account_id` | `text` | Snap ad account ID. |
| `account_key` | `text` | Local CLI account key. |
| `entity_type` | `text` | `campaign`, `ad_squad`, or `ad`. |
| `entity_id` | `text` | Changed Snap entity ID. |
| `entity_name` | `text` | Entity name at observation time. |
| `field_name` | `text` | Logical changed field, such as `target_cpa`, `daily_budget`, or `configured_status`. |
| `old_value` | `jsonb` | Previous normalized scalar/array/object. JSON `null` is stored as a value when appropriate. |
| `new_value` | `jsonb` | New normalized scalar/array/object. |
| `source` | `text` | `HUMAN_MANUAL`, `AGENT_API`, or `SNAP_SYSTEM`. |
| `occurred_at` | `timestamptz` | Local audit timestamp when matched, Snap update time for a reliable config diff, otherwise observation time. |
| `observed_at` | `timestamptz` | Time the sync saw the change. |
| `source_tool` | `text` | Matching local CLI tool, when source is `AGENT_API`. |
| `source_event_key` | `text` | Safe deterministic identity of the matching local audit mutation. |
| `metadata` | `jsonb` | Classification explanation without raw audit params. |
| `created_at` | `timestamptz` | Database insert time. |

The execute path acquires `pg_try_advisory_xact_lock` and writes history plus
current state in one transaction. `ON CONFLICT (event_key) DO NOTHING` makes a
retry safe even if the same event is proposed again.

### Recent change-history contract

Anything that proposes a budget, target CPA, or status change should read
recent squad control changes first (printed by the entity sync as
`recent_change_history_contract`):

```sql
SELECT
  entity_type,
  entity_id,
  entity_name,
  field_name,
  old_value,
  new_value,
  source,
  occurred_at,
  observed_at,
  event_key
FROM snapchat_ads.snapchat_change_history
WHERE account_id = '<SNAP_AD_ACCOUNT_ID>'
  AND entity_type = 'ad_squad'
  AND field_name IN ('target_cpa', 'daily_budget', 'configured_status')
  AND occurred_at >= pg_catalog.now() - interval '14 days'
ORDER BY occurred_at DESC, event_key;
```

When run through `query.py`, stdout follows the existing read contract:

```json
{
  "rows": [
    {
      "entity_type": "ad_squad",
      "entity_id": "...",
      "entity_name": "...",
      "field_name": "target_cpa",
      "old_value": 90.0,
      "new_value": 100.0,
      "source": "AGENT_API",
      "occurred_at": "2026-07-14T15:31:00+00:00",
      "observed_at": "2026-07-14T16:00:00+00:00",
      "event_key": "<64-char-sha256>"
    }
  ],
  "durationMs": 4,
  "rowCount": 1
}
```

## Query cookbook

All examples run through `query.py`:

```bash
uv run warehouse/query.py --sql "<SQL>" --reason "<why>"
```

### 1. Daily spend / ROAS trend (last 30 days, account-wide)

```sql
SELECT
  recorded_at,
  SUM(spend)::numeric(12,2) AS spend,
  SUM(revenue)::numeric(12,2) AS revenue,
  CASE WHEN SUM(spend) > 0 THEN ROUND(SUM(revenue) / SUM(spend), 4) END AS roas
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 30
GROUP BY 1
ORDER BY 1;
```

### 2. Top ads by spend, trailing 7 days

```sql
SELECT
  ad_id,
  MAX(ad_name) AS ad_name,
  MAX(ad_squad_name) AS ad_squad_name,
  SUM(spend)::numeric(12,2) AS spend,
  SUM(revenue)::numeric(12,2) AS revenue,
  SUM(conversions) AS conversions
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 7
GROUP BY ad_id
ORDER BY spend DESC
LIMIT 25;
```

### 3. Campaign rollup, trailing 14 days

```sql
SELECT
  campaign_id,
  MAX(campaign_name) AS campaign_name,
  SUM(spend)::numeric(12,2) AS spend,
  SUM(impressions) AS impressions,
  SUM(swipes) AS swipes,
  SUM(conversions) AS conversions,
  SUM(revenue)::numeric(12,2) AS revenue,
  CASE WHEN SUM(spend) > 0 THEN ROUND(SUM(revenue) / SUM(spend), 4) END AS roas
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 14
GROUP BY campaign_id
ORDER BY spend DESC;
```

### 4. Zero-purchase spenders (material spend, no conversions, trailing 7 days)

```sql
SELECT
  ad_id,
  MAX(ad_name) AS ad_name,
  MAX(ad_squad_name) AS ad_squad_name,
  SUM(spend)::numeric(12,2) AS spend
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 7
GROUP BY ad_id
HAVING SUM(spend) >= 40 AND SUM(conversions) = 0
ORDER BY spend DESC;
```

### 5. CPA by ad squad, trailing 7 days

```sql
SELECT
  ad_squad_id,
  MAX(ad_squad_name) AS ad_squad_name,
  SUM(spend)::numeric(12,2) AS spend,
  SUM(conversions) AS conversions,
  CASE WHEN SUM(conversions) > 0 THEN ROUND(SUM(spend) / SUM(conversions), 2) END AS cpa
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 7
GROUP BY ad_squad_id
ORDER BY spend DESC;
```

### 6. Video completion rate, trailing 7 days (where p100 data is populated)

```sql
SELECT
  ad_id,
  MAX(ad_name) AS ad_name,
  SUM(video_views) AS video_views,
  SUM(video_views_p100) AS video_views_p100,
  CASE WHEN SUM(video_views) > 0
    THEN ROUND(SUM(video_views_p100)::numeric / SUM(video_views), 4)
  END AS completion_rate
FROM snapchat_ads.snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 7
  AND video_views_p100 IS NOT NULL
GROUP BY ad_id
ORDER BY video_views DESC
LIMIT 25;
```
