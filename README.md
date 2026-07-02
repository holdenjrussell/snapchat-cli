# snapchat-cli

A complete, brand-agnostic **Snapchat ads operations stack** designed to be run by Claude Code (or any capable coding agent):

- **`cli/`** — full-surface CLI for the Snap Marketing API: 24 command groups, ~172 commands (campaigns, ad squads, ads, creatives, media, segments, pixels, catalogs, CAPI, reports, bulk ops, targeting, estimates). JSON by default, `--human` for tables, two-phase preview/`--execute` on every write, append-only audit log.
- **`skills/`** — agent skills: the Snap API reference + safe wrapper + **autonomous optimizer engine**, a dependency-free **Slack Block Kit** library, and quick-command patterns.
- **`warehouse/`** — Postgres warehouse layer: schema, daily ad-level sync (upsert on `ad_id + recorded_at`), SELECT-only query helper, full table map + query cookbook.
- **`reports/`** — hourly Slack heartbeat (rolling-24h ROAS / CPA / spend, threaded detail, Block Kit) with systemd/cron install, plus the daily/weekly optimizer entry points.
- **`docs/`** — campaign setup, the bid/budget control model, ads strategy notes, and a docs-catalog template (Obsidian-ready).

The optimizer is the centerpiece: a delay-aware, guardrailed media buyer. It builds a decision context from the last 7 *completed* days ending 2 days ago (Snap attribution lags), classifies every squad under a blended-ROAS governor (`cut_only` / `rebalance` / `scale`), walks **CPA targets** as the primary lever and budgets second, pauses proven losers, and executes only through validators that enforce step caps, CPA bounds, 48h cooldowns, and coverage minimums. Every run posts a full change report to Slack. A weekly companion job bridges your **top-performing Meta ads into Snap** (DPA collection + web-view), deduped through a tombstone-aware registry, with landing pages chosen by live inventory.

## Quickstart

```bash
git clone https://github.com/holdenjrussell/snapchat-cli.git
cd snapchat-cli
claude
```

Then run:

```
/setup-snapchat-cli
```

Claude walks the full setup with verification gates: CLI install + OAuth → optimizer config → warehouse build + backfill → docs index → Slack reports (hourly timer) → campaign setup → optimizer scheduling. No agent? Follow the same phases manually in [`.claude/commands/setup-snapchat-cli.md`](.claude/commands/setup-snapchat-cli.md).

### Requirements

- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- A Snap Business app (client ID/secret) with Marketing API scope
- An HTTPS OAuth redirect URI — with [Tailscale](https://tailscale.com) installed, `snapchat-ads auth callback-url` provisions one automatically via Funnel (you register the printed URL on the Snap app), and `auth login --listen` captures the authorization code hands-free
- A Postgres database (Neon/Supabase/RDS/local) for the warehouse
- A Slack bot token (`chat:write`) for reports
- Optional: a Meta warehouse (`meta_daily_metrics`) or the companion meta-ads CLI, if you want the weekly Meta-winners bridge

## Architecture

```
                 ┌────────────────────────────────────────────────┐
                 │                Snap Marketing API              │
                 └──────┬──────────────────┬──────────────────────┘
                        │                  │
                 cli/ (snapchat-ads CLI, two-phase writes, audit log)
                        │                  │
        ┌───────────────┴───────┐   ┌──────┴────────────────────────┐
        │ warehouse/            │   │ skills/snapchat-ads/scripts/  │
        │ sync_snapchat_daily   │   │ snap_optimizer_support.py     │
        │  → Postgres           │   │  daily: governor + CPA-walk   │
        │ query.py (read-only)──┼──▶│  weekly: Meta→Snap bridge     │
        └──────────┬────────────┘   └──────┬────────────────────────┘
                   │                       │ apply-squad-plan / apply-pause-plan
                   │                       │ (guardrail executors)
        ┌──────────┴───────────┐   ┌───────┴────────────────────────┐
        │ reports/ hourly      │   │ Slack change reports           │
        │ heartbeat (systemd)  │──▶│ (skills/slack-format Block Kit)│
        └──────────────────────┘   └────────────────────────────────┘
```

## Configuration

Two files hold everything instance-specific (both gitignored):

| File | Contents |
|---|---|
| `~/.config/snapchat-ads-cli/.env` | OAuth app creds, tokens, org/account/pixel IDs, Slack token + channel, `DATABASE_URL`, brand name. Template: [`.env.example`](.env.example) |
| `config/optimizer.json` | Brand, ROAS/CPA targets, guardrail caps, product routing map (campaign/squad/product-set IDs, keywords, landing pages). Template: [`config/optimizer.example.json`](config/optimizer.example.json) |

## Safety model

Every write previews before `--execute`; deletes are never used (archive only); optimizer changes flow through validators that enforce governor mode, per-run caps (±35% budget, ±20% CPA), CPA floor/ceiling, 48h per-squad cooldowns, fresh-ad holds, and squad coverage minimums; every mutation lands in `~/.config/snapchat-ads-cli/audit.jsonl`; every optimizer run posts its changes to Slack. See [`CLAUDE.md`](CLAUDE.md) for the rules agents must follow.

## Key docs

| Doc | What it covers |
|---|---|
| [`docs/campaign-setup.md`](docs/campaign-setup.md) | Standing up campaigns/squads/creatives the optimizer can manage |
| [`docs/bids-and-budgets.md`](docs/bids-and-budgets.md) | The full control model: windows, governor, CPA-walking, guardrails |
| [`warehouse/table-map.md`](warehouse/table-map.md) | Warehouse schema, column derivations, query cookbook |
| [`reports/README.md`](reports/README.md) | Hourly heartbeat install (systemd/cron/launchd) |
| [`skills/snapchat-ads/SKILL.md`](skills/snapchat-ads/SKILL.md) | Complete API + CLI reference |
| [`skills/snapchat-ads/references/`](skills/snapchat-ads/references/) | Playbooks: Meta→Snap DPA bridge, hourly report patterns, non-DPA reactivation |
| [`docs/strategy-notes.md`](docs/strategy-notes.md) | Distilled Snapchat ads strategy notes |
