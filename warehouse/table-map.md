# Table Map: `snapchat_ad_daily_metrics`

One row per (ad, calendar day). This is the only table this package ships;
see `README.md` for how to extend the warehouse with entity-mirror tables.

## Columns

| Column | Type | Source Snap API field | Derivation |
|---|---|---|---|
| `id` | `uuid` (PK) | — | `gen_random_uuid()` default. |
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
| `cpm` | `numeric(8,4)` | derived | `spend / impressions * 1000`, `NULL` when `impressions = 0`. |
| `cost_per_swipe` | `numeric(8,4)` | derived | `spend / swipes`, `NULL` when `swipes = 0`. |
| `video_views` | `integer` | `stats.video_views` | Direct. |
| `video_views_p50` | `integer` | `stats.video_views_time_based` (best effort) | Extracted from the `video_views_time_based` object; Snap does not document one canonical key shape for this field across API versions, so the sync script tries a handful of plausible key aliases and leaves this `NULL` if none match. Verify against a live payload sample for your API version and extend `_P50_KEYS` in `sync_snapchat_daily.py` if needed. |
| `video_views_p100` | `integer` | `stats.video_views_time_based` (best effort) | Same caveat as `video_views_p50`, via `_P100_KEYS`. |
| `conversions` | `integer` | `stats.conversion_purchases` | Direct. |
| `revenue` | `numeric(12,2)` | `stats.conversion_purchases_value` (micro) | `value_micro / 1_000_000`, rounded to cents on write. |
| `roas` | `numeric(8,4)` | derived | `revenue / spend`, `NULL` when `spend = 0`. |
| `created_at` | `timestamp` | — | `now()` default; not updated on conflict (first-insert timestamp only). |

## Indexes and upsert key

```sql
CREATE UNIQUE INDEX snapchat_ad_daily_ad_date_idx  ON snapchat_ad_daily_metrics (ad_id, recorded_at);
CREATE INDEX        snapchat_ad_daily_campaign_date_idx ON snapchat_ad_daily_metrics (campaign_id, recorded_at);
CREATE INDEX        snapchat_ad_daily_date_idx      ON snapchat_ad_daily_metrics (recorded_at);
```

- **Upsert key:** `(ad_id, recorded_at)` — the unique index doubles as the
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
  `recorded_at` within the last 2 days as provisional — purchases/revenue/
  ROAS for those days will keep updating on subsequent syncs.
- The sync script re-pulls the trailing window on every run (default 7
  days), so a daily cron naturally "catches up" late-attributed
  conversions without any special backfill logic.
- Each run also pulls **today so far** (a partial row, overwritten by every
  run until the day closes) unless `--closed-days-only` is passed. On the 437
  box the timer runs hourly at :40 because the dashboard P&L reads today's
  spend from this table.
- With `breakdown=ad`, Snap nests each day under
  `breakdown_stats.ad[].timeseries[]`; the parser reads the day from each
  entry. Before 2026-09-29 it did not, and every row landed on the window's
  start date with zero spend. `SUM(spend)` per day should equal the
  `ad_account` spend from `report stats` to the cent.
- There is no built-in freshness/watermark table in this minimal package.
  If you need one, the standard pattern is a `sync_watermarks`-style table
  with `(source, resource, watermark_at, last_run_at, last_status,
  rows_upserted, error)` columns, updated by `sync_snapchat_daily.py` on
  both success and failure — add it as a follow-up if your deployment
  needs automated staleness alerting.

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
FROM snapchat_ad_daily_metrics
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
FROM snapchat_ad_daily_metrics
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
FROM snapchat_ad_daily_metrics
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
FROM snapchat_ad_daily_metrics
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
FROM snapchat_ad_daily_metrics
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
FROM snapchat_ad_daily_metrics
WHERE recorded_at >= current_date - 7
  AND video_views_p100 IS NOT NULL
GROUP BY ad_id
ORDER BY video_views DESC
LIMIT 25;
```
