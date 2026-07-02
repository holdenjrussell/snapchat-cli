-- Snapchat Ads data warehouse schema
-- ------------------------------------------------------------------
-- Portable PostgreSQL DDL for the Snapchat ad-level daily metrics
-- table. Works against any Postgres 13+ instance (Neon, Supabase,
-- RDS, local Postgres). Idempotent: safe to re-run.
--
-- gen_random_uuid() ships built in on Postgres 13+. On older
-- Postgres it lives in the pgcrypto extension:
--   CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- The guard below is a no-op on modern Postgres and a required
-- prerequisite on anything older.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS snapchat_ad_daily_metrics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recorded_at date NOT NULL,
    campaign_id text,
    campaign_name text,
    ad_squad_id text,
    ad_squad_name text,
    ad_id text,
    ad_name text,
    spend numeric(12, 2),
    impressions integer,
    swipes integer,
    swipe_up_rate numeric(6, 4),
    cpm numeric(8, 4),
    cost_per_swipe numeric(8, 4),
    video_views integer,
    video_views_p50 integer,
    video_views_p100 integer,
    conversions integer,
    revenue numeric(12, 2),
    roas numeric(8, 4),
    created_at timestamp DEFAULT now()
);

-- Upsert key: one row per (ad, day). The sync script conflicts on
-- this index to keep re-runs idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS snapchat_ad_daily_ad_date_idx
    ON snapchat_ad_daily_metrics (ad_id, recorded_at);

-- Campaign rollups and campaign-scoped date-range scans.
CREATE INDEX IF NOT EXISTS snapchat_ad_daily_campaign_date_idx
    ON snapchat_ad_daily_metrics (campaign_id, recorded_at);

-- Whole-account date-range scans (daily spend trend, freshness checks).
CREATE INDEX IF NOT EXISTS snapchat_ad_daily_date_idx
    ON snapchat_ad_daily_metrics (recorded_at);
