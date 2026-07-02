# Snapchat Ads Warehouse

A minimal, portable Postgres warehouse layer for Snapchat Ads daily
performance data. Three scripts, one table, no framework dependency —
point it at any Postgres instance and it works.

## Contents

| File | Purpose |
|---|---|
| `schema.sql` | DDL for `snapchat_ad_daily_metrics` + its indexes. |
| `sync_snapchat_daily.py` | Pulls trailing N days of ad-level daily stats from the Snap API (via the `snapchat-ads` CLI) and upserts into the warehouse. |
| `query.py` | Single-file guarded read-only SQL runner. This is the contract downstream automation (an optimizer engine, a reporting bot, etc.) calls to read warehouse data. |
| `table-map.md` | Full column reference, index/upsert-key documentation, and a query cookbook. |

## Prerequisites

- Any Postgres 13+ instance. Neon, Supabase, RDS, and local Postgres all work — nothing here is provider-specific.
- [`uv`](https://docs.astral.sh/uv/) installed (both scripts are self-contained `uv run --script` files; `uv` resolves `psycopg[binary]` automatically, no virtualenv setup needed).
- The `snapchat-ads` CLI at `cli/` (sibling directory of this one) configured with a working account — see the CLI's own docs/`skills/snapchat-ads/SKILL.md` for `auth login` and `accounts.toml` setup.

## Standing up the warehouse

1. Provision a Postgres database (Neon/Supabase/local — anything works) and grab its connection string.
2. Export it:
   ```bash
   export DATABASE_URL="postgres://user:pass@host/dbname?sslmode=require"
   ```
3. Set the Snap account key (matches an entry in `~/.config/snapchat-ads-cli/accounts.toml`; defaults to `default` if unset):
   ```bash
   export SNAPCHAT_ADS_ACCOUNT=myaccount
   ```
4. Run an initial backfill, applying the schema in the same pass:
   ```bash
   uv run warehouse/sync_snapchat_daily.py --apply-schema --days 30
   ```
   This creates `snapchat_ad_daily_metrics` (if it doesn't already exist) and backfills the trailing 30 days.
5. Verify:
   ```bash
   uv run warehouse/query.py \
     --sql "SELECT recorded_at, SUM(spend) AS spend FROM snapchat_ad_daily_metrics GROUP BY 1 ORDER BY 1 DESC LIMIT 10" \
     --reason "verify backfill landed"
   ```

You can also apply the schema independently of any sync run:

```bash
psql "$DATABASE_URL" -f warehouse/schema.sql
```

## Ongoing sync

Snap attribution has a reporting lag of roughly 1-2 days, so a daily sync
that re-pulls a small trailing window (not just "yesterday") keeps
attribution-adjusted numbers (purchases, revenue, ROAS) from silently
under-reporting. Re-running the sync for the same day is safe — the
upsert key is `(ad_id, recorded_at)`, so a re-pull just refreshes the row.

Recommended cadence:

```bash
# Daily, e.g. via cron/systemd timer at whatever hour your scheduler runs:
uv run warehouse/sync_snapchat_daily.py --days 7
```

- `--days 7` re-pulls the trailing week every run, which comfortably covers Snap's attribution lag without re-pulling the whole account history.
- For a lighter-weight hourly heartbeat, run with a smaller window (e.g. `--days 2`) — the upsert key makes this cheap and idempotent.
- Always dry-run a schedule before wiring it up:
  ```bash
  uv run warehouse/sync_snapchat_daily.py --days 7 --dry-run
  ```
  This calls the Snap API and prints sample rows + counts, but never touches the database.

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--days N` | `7` | Trailing window size, in days, ending at today (UTC midnight boundary). |
| `--dry-run` | off | Fetch + transform only; print sample rows and row count; no DB connection at all. |
| `--apply-schema` | off | Execute `schema.sql` before upserting (idempotent — safe on every run if you want belt-and-suspenders). |
| `--database-url URL` | — | Override `DATABASE_URL` for one-off runs against a different database. |

## How `query.py` is used by downstream automation

`query.py` is the single guarded entry point for reading this warehouse.
Anything that wants Snap data — an optimizer engine, a Slack report bot, an
ad-hoc analysis notebook — should shell out to it rather than opening its
own connection:

```bash
uv run warehouse/query.py \
  --sql "SELECT ad_id, ad_name, SUM(spend) AS spend, SUM(revenue) AS revenue FROM snapchat_ad_daily_metrics WHERE recorded_at >= current_date - 7 GROUP BY 1, 2 ORDER BY spend DESC LIMIT 25" \
  --reason "top ads by L7 spend for scaling review"
```

Contract: stdout is always a single JSON object,
`{"rows": [...], "durationMs": <int>, "rowCount": <int>}`, on success.
On failure, an error is printed to stderr as `{"error": "..."}` and the
process exits non-zero (`1` for a rejected/failed query, `2` for a missing
`DATABASE_URL`). `--reason` is required and is written to stderr as an
audit-trail line — it doesn't change query behavior, but it means every
warehouse read leaves a one-line "who asked and why" breadcrumb in logs.

The guard only allows a single `SELECT`/`WITH ... SELECT` statement (no
writes, no DDL, no stacked statements) and auto-injects a `LIMIT` when the
query doesn't specify one, capped by `--limit-guard` (default 5000). This
makes it safe to give an LLM-driven caller free-form SQL access without it
being able to mutate or exfiltrate unbounded row counts.

## Extending the warehouse

This package ships one fact table by design — ad-level daily metrics are
the grain almost everything downstream needs. Two natural extensions:

1. **Entity-mirror tables.** If you need campaign/ad-squad metadata beyond
   what's denormalized onto each metrics row (budgets, targeting, bid
   strategy, status/pause state), add `snapchat_campaigns` and
   `snapchat_ad_squads` mirror tables populated from `campaign list` /
   `adsquad list`, refreshed on the same cadence as the daily sync. Follow
   the same idempotent-upsert pattern (`id` as the natural key) and add
   their DDL to `schema.sql`.
2. **Creative-level facts.** If you need creative/media-level breakdowns
   instead of (or in addition to) ad-level, add a `--breakdown creative`
   pull to a sibling sync script and a `snapchat_creative_daily_metrics`
   table with the same shape.

### Meta-winners bridge note

If you're building a cross-platform "what's winning on Meta, bridge it to
Snap" workflow, that pattern expects a warehouse that *also* mirrors Meta
Ads data — specifically `meta_daily_metrics`, `meta_ads`, and
`meta_creatives` tables — so the bridge can join Meta-side winners against
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
