-- Snapchat Ads data warehouse schema
-- ------------------------------------------------------------------
-- Portable PostgreSQL DDL template for the Snapchat warehouse. The
-- migration helpers replace __SNAP_WAREHOUSE_SCHEMA__ with one validated,
-- quoted schema identifier before execution. Do not execute this template
-- directly with psql. Idempotent after rendering: safe to re-run.
--
-- gen_random_uuid() is built in on the supported PostgreSQL 13+ versions,
-- so this migration does not create or alter extensions outside its schema.

CREATE SCHEMA IF NOT EXISTS __SNAP_WAREHOUSE_SCHEMA__;

CREATE TABLE IF NOT EXISTS __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics (
    id uuid PRIMARY KEY DEFAULT pg_catalog.gen_random_uuid(),
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
    cpm numeric(14, 4),
    cost_per_swipe numeric(14, 4),
    video_views integer,
    video_views_p50 integer,
    video_views_p100 integer,
    conversions integer,
    revenue numeric(12, 2),
    roas numeric(14, 4),
    source_window_end timestamptz,
    provisional boolean NOT NULL DEFAULT false,
    last_synced_at timestamptz,
    created_at timestamp DEFAULT pg_catalog.now()
);

-- Compatibility migration for installations created before intraday refreshes.
-- Re-running this DDL is safe, and closed-day readers remain compatible because
-- every new column is nullable or has a stable default.
ALTER TABLE __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics
    ADD COLUMN IF NOT EXISTS source_window_end timestamptz;
ALTER TABLE __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics
    ADD COLUMN IF NOT EXISTS provisional boolean NOT NULL DEFAULT false;
ALTER TABLE __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics
    ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;

-- Upsert key: one row per (ad, day). The sync script conflicts on
-- this index to keep re-runs idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS snapchat_ad_daily_ad_date_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics (ad_id, recorded_at);

-- Campaign rollups and campaign-scoped date-range scans.
CREATE INDEX IF NOT EXISTS snapchat_ad_daily_campaign_date_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics (campaign_id, recorded_at);

-- Whole-account date-range scans (daily spend trend, freshness checks).
CREATE INDEX IF NOT EXISTS snapchat_ad_daily_date_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_ad_daily_metrics (recorded_at);

-- Current configuration and delivery state for every Snap entity. The table
-- keeps one latest row per account/entity and preserves the complete sanitized
-- API object for fields that are not promoted to first-class columns yet.
CREATE TABLE IF NOT EXISTS __SNAP_WAREHOUSE_SCHEMA__.snapchat_entity_state (
    account_id text NOT NULL,
    account_key text NOT NULL,
    entity_type text NOT NULL
        CHECK (entity_type IN ('campaign', 'ad_squad', 'ad')),
    entity_id text NOT NULL,
    entity_name text,
    campaign_id text,
    ad_squad_id text,
    configured_status text,
    effective_status text,
    delivery_status jsonb NOT NULL DEFAULT '[]'::jsonb,
    review_status text,
    daily_budget numeric(14, 2),
    daily_budget_micro bigint,
    bid numeric(14, 2),
    bid_micro bigint,
    target_cpa numeric(14, 2),
    target_cpa_micro bigint,
    bid_strategy text,
    source_created_at timestamptz,
    source_updated_at timestamptz,
    raw_payload jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    PRIMARY KEY (account_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS snapchat_entity_state_parent_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_entity_state (
        account_id,
        campaign_id,
        ad_squad_id,
        entity_type
    );

CREATE INDEX IF NOT EXISTS snapchat_entity_state_status_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_entity_state (
        account_id,
        entity_type,
        configured_status
    );

CREATE INDEX IF NOT EXISTS snapchat_entity_state_seen_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_entity_state (
        account_id,
        last_seen_at DESC
    );

-- Immutable field-level events derived by comparing the latest API snapshot to
-- the prior entity state. The event key is a deterministic SHA-256 digest, so
-- retrying the same observation cannot duplicate an event.
CREATE TABLE IF NOT EXISTS __SNAP_WAREHOUSE_SCHEMA__.snapchat_change_history (
    event_key text PRIMARY KEY CHECK (pg_catalog.length(event_key) = 64),
    account_id text NOT NULL,
    account_key text NOT NULL,
    entity_type text NOT NULL
        CHECK (entity_type IN ('campaign', 'ad_squad', 'ad')),
    entity_id text NOT NULL,
    entity_name text,
    field_name text NOT NULL,
    old_value jsonb NOT NULL,
    new_value jsonb NOT NULL,
    source text NOT NULL
        CHECK (source IN ('HUMAN_MANUAL', 'AGENT_API', 'SNAP_SYSTEM')),
    occurred_at timestamptz NOT NULL,
    observed_at timestamptz NOT NULL,
    source_tool text,
    source_event_key text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);

CREATE INDEX IF NOT EXISTS snapchat_change_history_entity_time_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_change_history (
        account_id,
        entity_type,
        entity_id,
        occurred_at DESC
    );

CREATE INDEX IF NOT EXISTS snapchat_change_history_field_time_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_change_history (
        account_id,
        entity_type,
        field_name,
        occurred_at DESC
    );

CREATE INDEX IF NOT EXISTS snapchat_change_history_source_time_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_change_history (
        account_id,
        source,
        occurred_at DESC
    );

-- One terminal receipt per orchestrated warehouse cycle. The runner inserts a
-- running row only after taking the shared file lock, then updates that same row
-- to a terminal status. A later cycle marks stale running rows abandoned before
-- starting so an interrupted process is visible instead of silently hanging.
CREATE TABLE IF NOT EXISTS __SNAP_WAREHOUSE_SCHEMA__.snapchat_sync_runs (
    run_id uuid PRIMARY KEY,
    sync_kind text NOT NULL
        CHECK (sync_kind IN ('closed', 'recent')),
    status text NOT NULL
        CHECK (status IN ('running', 'succeeded', 'failed', 'lock_timeout', 'abandoned')),
    account_key text NOT NULL,
    warehouse_schema text NOT NULL,
    window_start timestamptz,
    window_end timestamptz,
    metrics_rows integer NOT NULL DEFAULT 0,
    entity_rows integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS snapchat_sync_runs_kind_started_idx
    ON __SNAP_WAREHOUSE_SCHEMA__.snapchat_sync_runs (
        sync_kind,
        started_at DESC
    );
