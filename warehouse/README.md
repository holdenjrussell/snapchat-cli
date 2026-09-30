# Snapchat Ads Warehouse

A minimal, portable Postgres warehouse layer for Snapchat Ads performance,
current entity state, and append-only configuration history. Three operational
scripts, three tables, and no framework dependency.

## Contents

| File | Purpose |
|---|---|
| `schema.sql` | Schema-tokenized DDL template for daily metrics, current entity state, change history, and their indexes. The sync scripts validate and render it; do not pass it directly to `psql`. |
| `schema_config.py` | Shared schema validation, qualification, search-path defense, and DDL rendering. |
| `sync_snapchat_daily.py` | Pulls trailing N days of ad-level daily stats from the Snap API (via the `snapchat-ads` CLI) and upserts into the warehouse. |
| `sync_snapchat_entities.py` | Mirrors campaign/ad squad/ad state and records later field changes as `HUMAN_MANUAL`, `AGENT_API`, or `SNAP_SYSTEM`. Dry-run is the default. |
| `query.py` | Single-file guarded read-only SQL runner. This is the contract downstream automation (an optimizer engine, a reporting bot, etc.) calls to read warehouse data. |
| `table-map.md` | Full column reference, index/upsert-key documentation, and a query cookbook. |

## Prerequisites

- Any Postgres 13+ instance. Neon, Supabase, RDS, and local Postgres all work; nothing here is provider-specific.
- [`uv`](https://docs.astral.sh/uv/) installed (the sync and query scripts are self-contained `uv run --script` files; `uv` resolves `psycopg[binary]` automatically, no virtualenv setup needed).
- The `snapchat-ads` CLI at `cli/` (sibling directory of this one) configured with a working account. See the CLI's own docs/`skills/snapchat-ads/SKILL.md` for `auth login` and `accounts.toml` setup.

## Standing up the warehouse

1. Provision a Postgres database (Neon/Supabase/local; anything works) and grab its connection string.
2. Export it:
   ```bash
   export DATABASE_URL="postgres://user:pass@host/dbname?sslmode=require"
   export SNAP_WAREHOUSE_SCHEMA="snapchat_ads"
   ```
3. Set the Snap account key (matches an entry in `~/.config/snapchat-ads-cli/accounts.toml`; defaults to `default` if unset):
   ```bash
   export SNAPCHAT_ADS_ACCOUNT=myaccount
   ```
4. Preview the metrics backfill. This fetches and transforms data without a
   database connection or write:
   ```bash
   uv run warehouse/sync_snapchat_daily.py \
     --dry-run \
     --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
     --days 30
   ```
5. Preview the first entity snapshot. This is read-only and establishes what
   the baseline would contain:
   ```bash
   uv run warehouse/sync_snapchat_entities.py \
     --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"
   ```
6. Complete the PostgreSQL 17 backup in the production migration section
   below. After reviewing both previews and obtaining approval, apply the
   schema and first entity baseline in one locked transaction:
   ```bash
   uv run warehouse/sync_snapchat_entities.py \
     --execute \
     --apply-schema \
     --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"
   ```
   The first observation writes current state but deliberately creates no
   change-history events. History starts with later observed diffs.
7. Backfill the trailing 30 days of metrics after the schema transaction
   succeeds:
   ```bash
   uv run warehouse/sync_snapchat_daily.py \
     --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
     --days 30
   ```
8. Verify:
   ```bash
   uv run warehouse/query.py \
     --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
     --sql 'SELECT recorded_at, SUM(spend) AS spend FROM snapchat_ad_daily_metrics GROUP BY 1 ORDER BY 1 DESC LIMIT 10' \
     --reason "verify backfill landed"
   ```

`schema.sql` is a checked-in template, not a directly executable `psql` file.
Only the repository scripts render its schema token, after validating the
identifier. Every repository-generated DDL and DML statement uses a fully
qualified relation name. The default live schema is `snapchat_ads`; portable
deployments can select another lowercase schema with `SNAP_WAREHOUSE_SCHEMA`
or `--warehouse-schema`. The scripts do not depend on ambient `search_path` or
`PGOPTIONS`.

The `query.py` examples use unqualified caller SQL only because the helper
sets a validated, transaction-local path with `pg_catalog` first and the
configured warehouse schema second. This is not the connection's ambient
path. Repository-generated DDL, DML, state, and history SQL remains fully
qualified.

## Production migration and rollback

Use the PostgreSQL 17 client tools for this database. The system's older
`pg_dump` cannot back up a PostgreSQL 17 server. These commands create an
owner-only pre-migration archive and validate its table of contents without
printing the database URL. Run during a quiet window with any automated Snap
write jobs paused. Stop if either preflight count is nonzero:

```bash
set -euo pipefail
cd "${SNAPCHAT_CLI_REPO:-$HOME/snapchat-cli}"
umask 077
export SNAP_WAREHOUSE_SCHEMA="${SNAP_WAREHOUSE_SCHEMA:-snapchat_ads}"
: "${DATABASE_URL:?export DATABASE_URL for the warehouse first}"
PG17_BIN="${PG17_BIN:-/usr/lib/postgresql/17/bin}"  # macOS Homebrew: /opt/homebrew/opt/postgresql@17/bin
python3 -c 'import sys; from warehouse.schema_config import validate_warehouse_schema; validate_warehouse_schema(sys.argv[1])' "$SNAP_WAREHOUSE_SCHEMA"
"$PG17_BIN/psql" \
  "$DATABASE_URL" \
  -X \
  -v ON_ERROR_STOP=1 \
  -v warehouse_schema="$SNAP_WAREHOUSE_SCHEMA" <<'SQL'
BEGIN READ ONLY;
SELECT
  pg_catalog.count(*) AS other_nonidle_sessions,
  CASE WHEN pg_catalog.count(*) = 0 THEN 'false' ELSE 'true' END AS preflight_busy
FROM pg_catalog.pg_stat_activity
WHERE datname = pg_catalog.current_database()
  AND pid <> pg_catalog.pg_backend_pid()
  AND state <> 'idle'
\gset
\echo other_nonidle_sessions :other_nonidle_sessions
\if :preflight_busy
  \echo 'Stop: another non-idle database session exists.'
  \quit 3
\endif
SELECT
  pg_catalog.count(*) AS warehouse_relation_locks,
  CASE WHEN pg_catalog.count(*) = 0 THEN 'false' ELSE 'true' END AS preflight_busy
FROM pg_catalog.pg_locks AS locks
JOIN pg_catalog.pg_class AS relation ON relation.oid = locks.relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = :'warehouse_schema'
  AND locks.pid <> pg_catalog.pg_backend_pid()
\gset
\echo warehouse_relation_locks :warehouse_relation_locks
\if :preflight_busy
  \echo 'Stop: another session holds a warehouse relation lock.'
  \quit 3
\endif
ROLLBACK;
SQL
stamp=$(date +%Y%m%dT%H%M%S)
backup_dir="${SNAPCHAT_BACKUP_DIR:-$HOME/.local/share/snapchat-cli/backups}/$stamp"
mkdir -p "$backup_dir"

"$PG17_BIN/pg_dump" \
  "$DATABASE_URL" \
  --format=custom \
  --schema="$SNAP_WAREHOUSE_SCHEMA" \
  --no-owner \
  --no-acl \
  --file="$backup_dir/pre-migration.dump"
"$PG17_BIN/pg_restore" \
  --list "$backup_dir/pre-migration.dump" \
  > "$backup_dir/pre-migration.list"
```

Preview again after the backup. This command reads live Snap and the configured
Postgres schema but performs no database writes:

```bash
uv run warehouse/sync_snapchat_entities.py \
  --dry-run \
  --database-url "$DATABASE_URL" \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"
```

Only after explicit approval of that preview, apply the DDL and first entity
baseline in the entity sync's single advisory-locked transaction:

```bash
uv run warehouse/sync_snapchat_entities.py \
  --execute \
  --apply-schema \
  --database-url "$DATABASE_URL" \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"
```

An error during that transaction rolls back its DDL, history inserts, and
state upserts automatically. After a successful migration, the preferred
operational rollback is to disable the sync scheduler
and preserve both audit tables. If structural rollback is separately approved,
first archive the two new tables, then drop only those tables. Never drop the
schema or `snapchat_ad_daily_metrics`:

```bash
set -euo pipefail
cd "${SNAPCHAT_CLI_REPO:-$HOME/snapchat-cli}"
umask 077
export SNAP_WAREHOUSE_SCHEMA="${SNAP_WAREHOUSE_SCHEMA:-snapchat_ads}"
: "${DATABASE_URL:?export DATABASE_URL for the warehouse first}"
PG17_BIN="${PG17_BIN:-/usr/lib/postgresql/17/bin}"  # macOS Homebrew: /opt/homebrew/opt/postgresql@17/bin
python3 -c 'import sys; from warehouse.schema_config import validate_warehouse_schema; validate_warehouse_schema(sys.argv[1])' "$SNAP_WAREHOUSE_SCHEMA"
rollback_stamp=$(date +%Y%m%dT%H%M%S)
rollback_dir="${SNAPCHAT_BACKUP_DIR:-$HOME/.local/share/snapchat-cli/backups}/$rollback_stamp"
mkdir -p "$rollback_dir"
"$PG17_BIN/pg_dump" \
  "$DATABASE_URL" \
  --format=custom \
  --table="$SNAP_WAREHOUSE_SCHEMA.snapchat_entity_state" \
  --table="$SNAP_WAREHOUSE_SCHEMA.snapchat_change_history" \
  --no-owner \
  --no-acl \
  --file="$rollback_dir/pre-rollback.dump"

"$PG17_BIN/psql" \
  "$DATABASE_URL" \
  -v ON_ERROR_STOP=1 \
  -v warehouse_schema="$SNAP_WAREHOUSE_SCHEMA" <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '5s';
DROP TABLE IF EXISTS :"warehouse_schema".snapchat_change_history;
DROP TABLE IF EXISTS :"warehouse_schema".snapchat_entity_state;
COMMIT;
SQL
```

Do not run `pg_restore --clean` against production as a generic rollback. Use
the pre-migration archive only in a separately reviewed recovery plan if an
existing object was unexpectedly changed.

## Ongoing sync

Snap attribution has a reporting lag of roughly 1-2 days, so a daily sync
that re-pulls a small trailing window (not just "yesterday") keeps
attribution-adjusted numbers (purchases, revenue, ROAS) from silently
under-reporting. Re-running the sync for the same day is safe; the
upsert key is `(ad_id, recorded_at)`, so a re-pull just refreshes the row.

Recommended cadence:

```bash
# Daily, e.g. via cron/systemd timer at whatever hour your scheduler runs:
uv run warehouse/sync_snapchat_daily.py \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
  --days 7
```

- `--days 7` re-pulls the trailing week every run, which comfortably covers Snap's attribution lag without re-pulling the whole account history.
- For a lighter-weight hourly heartbeat, run with a smaller window (e.g. `--days 2`); the upsert key makes this cheap and idempotent.
- Always dry-run a schedule before wiring it up:
  ```bash
  uv run warehouse/sync_snapchat_daily.py \
    --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
    --days 7 \
    --dry-run
  ```
  This calls the Snap API and prints sample rows + counts, but never touches the database.

Run the entity sync after any workflow that can change campaigns, ad squads,
or ads. Its default mode reads Snap plus existing Postgres state and prints a
diff without writing:

```bash
uv run warehouse/sync_snapchat_entities.py \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"
uv run warehouse/sync_snapchat_entities.py \
  --execute \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA"  # only after approval
```

The execute path performs the complete compare, history insert, and current
state upsert in one transaction. It uses `pg_try_advisory_xact_lock`, so two
concurrent entity syncs cannot race. Repeating an observation is safe because
the current-state key is `(account_id, entity_type, entity_id)` and every
history `event_key` is deterministic.

The script reads `~/.config/snapchat-ads-cli/audit.jsonl` to distinguish local
CLI mutations from unmatched changes. Only allowlisted entity, field, value,
tool, and timestamp data are retained. Credential-like keys are ignored, and
raw Snap payloads are recursively redacted before storage. The older
`campaign.bulk_budget` and `adsquad.bulk_budget` audit entries contain only a
count, not entity IDs or values, so those legacy entries cannot be matched and
will be classified as `HUMAN_MANUAL` if they produce a later diff.

Anything that changes budgets or target CPA should read recent ad-squad rows
from the configured `snapchat_change_history` relation first (the entity sync
summary prints the query as `recent_change_history_contract`), so manual UI
changes and API changes share one cooldown clock, and it should fail closed
when the tables, database connection, or current baseline are unavailable.

### Entity sync flags

| Flag | Default | Purpose |
|---|---|---|
| `--dry-run` | on implicitly | Explicitly select read-only preview mode. |
| `--execute` | off | Write state and history inside one locked transaction. |
| `--apply-schema` | off | Apply `schema.sql` inside the execute transaction. Requires `--execute`. |
| `--database-url URL` | `DATABASE_URL` | Override the warehouse connection string. A dry run can operate without a database, but then every live entity is reported as a baseline. |
| `--warehouse-schema NAME` | `SNAP_WAREHOUSE_SCHEMA` or `snapchat_ads` | Select the validated schema used by every qualified relation. |
| `--account KEY` | `SNAPCHAT_ADS_ACCOUNT` or `default` | Select the configured CLI account. |
| `--audit-file PATH` | local CLI audit path | Override the local JSONL mutation log. |

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--days N` | `7` | Trailing window size, in days, ending at today (UTC midnight boundary). |
| `--dry-run` | off | Fetch + transform only; print sample rows and row count; no DB connection at all. |
| `--apply-schema` | off | Render and commit `schema.sql` before the metrics upsert. This convenience path is not the production migration path; use the entity sync's locked transaction above for migration. |
| `--database-url URL` | none | Override `DATABASE_URL` for one-off runs against a different database. |
| `--warehouse-schema NAME` | `SNAP_WAREHOUSE_SCHEMA` or `snapchat_ads` | Select the validated schema used by every qualified relation. |

## How `query.py` is used by downstream automation

`query.py` is the single guarded entry point for reading this warehouse.
Anything that wants Snap data, such as an optimizer engine, a Slack report bot,
or an ad-hoc analysis notebook, should shell out to it rather than opening its
own connection:

```bash
uv run warehouse/query.py \
  --warehouse-schema "$SNAP_WAREHOUSE_SCHEMA" \
  --sql 'SELECT ad_id, ad_name, SUM(spend) AS spend, SUM(revenue) AS revenue FROM snapchat_ad_daily_metrics WHERE recorded_at >= current_date - 7 GROUP BY 1, 2 ORDER BY spend DESC LIMIT 25' \
  --reason "top ads by L7 spend for scaling review"
```

Contract: stdout is always a single JSON object,
`{"rows": [...], "durationMs": <int>, "rowCount": <int>}`, on success.
On failure, an error is printed to stderr as `{"error": "..."}` and the
process exits non-zero (`1` for a rejected/failed query, `2` for a missing
`DATABASE_URL` or invalid schema). `--reason` is required and is written to stderr as an
audit-trail line. It doesn't change query behavior, but it means every
warehouse read leaves a one-line "who asked and why" breadcrumb in logs.

The guard only allows a single `SELECT`/`WITH ... SELECT` statement (no
writes, no DDL, no stacked statements) and auto-injects a `LIMIT` when the
query doesn't specify one, capped by `--limit-guard` (default 5000). The
database transaction is also set read-only before caller SQL executes. The
helper sets a transaction-local path to the validated schema as a compatibility
defense, but repository-generated SQL still uses fully qualified names and
does not rely on that path.

`query.py` also accepts `--warehouse-schema NAME`, with the same
`SNAP_WAREHOUSE_SCHEMA` or `snapchat_ads` default as the sync scripts.

## Extending the warehouse

This package ships the ad-level daily fact table plus current entity state and
field-level change history. A natural next extension is creative-level facts.
If you need creative/media-level breakdowns
   instead of (or in addition to) ad-level, add a `--breakdown creative`
   pull to a sibling sync script and a `snapchat_creative_daily_metrics`
   table with the same shape.

### Meta-winners bridge note

If you're building a cross-platform "what's winning on Meta, bridge it to
Snap" workflow, that pattern expects a warehouse that *also* mirrors Meta
Ads data, specifically `meta_daily_metrics`, `meta_ads`, and
`meta_creatives` tables, so the bridge can join Meta-side winners against
Snap-side candidates in one SQL query. This package does not ship those
tables (it's Snap-only). Two options:

- **Warehouse-driven path:** stand up the equivalent Meta mirror tables in
  the same Postgres instance (not covered by this package) so cross-platform
  SQL joins work directly.
- **Live-API path:** skip the Meta mirror and pull Meta-side winner data
  live via a Meta Ads API CLI/skill at bridge-run time instead of joining
  through the warehouse. See your Meta Ads CLI/skill's own reference docs
  for the live-query shape.

Either way, the Snap-side half of the bridge (this package) doesn't change.
