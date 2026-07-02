---
description: Wire up the full Snapchat ads stack — CLI auth, data warehouse, docs index, Slack reports, optimizer crons, campaigns
---

You are setting up the snapchat-cli stack on this machine. Work through the phases below **in order** — each phase has a verification gate; do not advance past a failing gate. Ask the user for values you cannot discover yourself (OAuth credentials, Slack channel, DATABASE_URL, brand/product details). Every Snap API write in this stack is two-phase: preview first, then re-run with `--execute` after the user approves. Never skip that gate during setup.

Repo layout (all paths relative to this repo root):
- `cli/` — the `snapchat-ads` CLI (~172 commands over the Snap Marketing API)
- `skills/snapchat-ads/` — API reference skill + safe wrapper + the optimizer engine (`scripts/snap_optimizer_support.py`)
- `skills/slack-format/` — dependency-free Slack Block Kit library
- `skills/snap-command/` — quick-command skill
- `config/optimizer.example.json` — optimizer/product-routing config template
- `warehouse/` — Postgres schema, sync script, query helper, table map
- `reports/` — hourly Slack heartbeat (collector + poster + systemd units), optimizer cron context shims
- `docs/` — campaign setup, bids & budgets, strategy notes, Obsidian catalog template

## Phase 0 — Preflight

1. Check `uv`, `python3` (>= 3.11), and `git` are available. Install uv if missing (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
2. `uv sync` inside `cli/`, then `uv run --directory cli snapchat-ads --help` must print the command groups.
3. Install the skills so they load in future sessions: copy each directory under `skills/` to `~/.claude/skills/` (or the project `.claude/skills/` if the user prefers project-scoped).

**Gate:** CLI help renders; skills copied.

## Phase 1 — Credentials & OAuth

1. `mkdir -p ~/.config/snapchat-ads-cli && cp .env.example ~/.config/snapchat-ads-cli/.env && chmod 0600 ~/.config/snapchat-ads-cli/.env`
2. Ask the user for `SNAPCHAT_CLIENT_ID`, `SNAPCHAT_CLIENT_SECRET`, and their redirect URI (from Snap Business Manager → Business Details → Apps; they may need to create an app with the Marketing API scope). Fill them into the env file — never echo secrets back.
3. `uv run --directory cli snapchat-ads init` (writes `~/.config/snapchat-ads-cli/accounts.toml` with the `default` account).
4. `uv run --directory cli snapchat-ads --account default auth login` — give the user the printed authorize URL, have them approve and paste the `code=` value back.
5. Discover IDs and write them into both the env file and `accounts.toml`:
   - `snapchat-ads --human org list` → `SNAPCHAT_ORGANIZATION_ID`
   - `snapchat-ads --human org list-accounts <ORG_ID>` → `SNAPCHAT_AD_ACCOUNT_ID`
   - `snapchat-ads --human pixel list` → `SNAPCHAT_PIXEL_ID` (may not exist yet — fine)

**Gate:** `uv run --directory cli snapchat-ads --human auth status` shows a valid token, and `campaign list` returns without error.

## Phase 2 — Optimizer config

1. `cp config/optimizer.example.json config/optimizer.json`
2. Interview the user: brand name, headline, target ROAS (now + eventual), target CPA band, timezone, Slack channel ID for reports.
3. Build the `products` map — one entry per product line they advertise. For each: a label, name-matching `keywords`, landing pages, and (if they already run DPA) campaign/ad-squad/product-set IDs discovered via `campaign list`, `adsquad list --campaign-id ...`, and `catalog product-sets`. Products without live campaigns yet can leave IDs empty — Phase 6 creates them.
4. Fill `snap_shared` (pixel, public profile via `snapchat-ads me` / Business Manager, dynamic template + interaction zone if using Collection/DPA ads — discoverable via `catalog dynamic-templates` and `interaction-zone list`).

**Gate:** `python3 skills/snapchat-ads/scripts/snap_optimizer_support.py print-cron-jobs` runs cleanly against the new config.

## Phase 3 — Data warehouse

1. Ask for `DATABASE_URL` (any Postgres — Neon/Supabase/local). Add to the env file.
2. Apply schema + backfill: `uv run warehouse/sync_snapchat_daily.py --apply-schema --days 30`
3. Verify: `uv run warehouse/query.py --sql "select count(*) as rows, max(recorded_at) as newest from snapchat_ad_daily_metrics" --reason "setup verification"`
4. Schedule the sync (daily at minimum, hourly if they want fresh intraday data) via cron/systemd — mirror the pattern in `reports/systemd/`.

**Gate:** row count > 0 (or 0 with a clean run if the account is brand new) and the newest date is within the backfill window. Read `warehouse/table-map.md` for the column contract.

## Phase 4 — Docs index (Obsidian or docs/)

1. Ask where setup docs live (Obsidian vault path, wiki, or this repo's `docs/`).
2. Instantiate `docs/obsidian-index-template.md` there: fill in the instance's table inventory (from Phase 3), connection notes (never paste the DATABASE_URL itself), report channels, and links to `warehouse/table-map.md`, `reports/README.md`, and the skill docs.
3. Keep this index updated whenever you add tables or reports — it is the map future sessions navigate by.

**Gate:** the index file exists and its table list matches `warehouse/table-map.md`.

## Phase 5 — Slack reports (hourly heartbeat)

1. Ask for a Slack bot token (`chat:write`, bot invited to the channel) and the channel ID → `SLACK_BOT_TOKEN`, `SNAPCHAT_HOURLY_SLACK_CHANNEL` in the env file.
2. Test the formatting library offline: `python3 skills/slack-format/selfcheck.py`.
3. One-shot test post (ask the user to confirm the message arrived): `python3 reports/post_snapchat_hourly_heartbeat.py`
4. Install the hourly timer (Linux): follow `reports/README.md` — sed the `__REPO__`/`__SLACK_CHANNEL__`/`__BRAND__` placeholders in `reports/systemd/snapchat-ads-hourly.{service,timer}`, copy to `~/.config/systemd/user/`, `systemctl --user daemon-reload && systemctl --user enable --now snapchat-ads-hourly.timer`, and `loginctl enable-linger $USER`. On macOS use the launchd note in the same README; anywhere else, plain cron works.

**Gate:** `systemctl --user list-timers | grep snapchat` shows the next run (or the cron entry exists), and a real heartbeat rendered in the channel with Block Kit formatting (header, fields grid, threaded detail).

## Phase 6 — Campaigns (if starting fresh)

Follow `docs/campaign-setup.md`. Summary: create PAUSED campaigns per product (`objective_v2_type WEB_CONVERSION`, AUCTION), ad squads via `adsquad smart-create` with `PIXEL_PURCHASE` optimization and TARGET_COST-style CPA bidding, upload media, create web-view or collection creatives, attach ads PAUSED, review previews with the user, then launch. Record the resulting campaign/squad/product-set IDs back into `config/optimizer.json` so the optimizer can route.

**Gate:** every new object was previewed before `--execute`, everything created PAUSED until the user approves launch.

## Phase 7 — Autonomous optimizer (daily) + Meta-winners bridge (weekly)

1. Dry-run the daily context: `python3 skills/snapchat-ads/scripts/snap_optimizer_support.py collect-daily` — review the squad table, governor mode, and pause candidates with the user. Explain the model (see `docs/bids-and-budgets.md`): 7-day decision window ending 2 days ago (attribution lag), blended-ROAS governor (cut_only / rebalance / scale), CPA target as the primary lever, budget raises only at ≥85% utilization, 48h cooldown ledger, hard step caps enforced by `apply-squad-plan` / `apply-pause-plan` executors.
2. Decide autonomy level with the user: report-only (agent posts recommendations), or full-auto (agent executes within the guardrails). The executor guardrails hold either way.
3. Schedule the two jobs with whatever agent scheduler exists here (Claude Code scheduled agents, cron + `claude -p`, or another runner):
   - daily optimizer: run `reports/snap_optimizer_daily_context.py`, feed its output to the agent with the prompt from `print-cron-jobs`, post the result to Slack (`main_message` + threaded `thread_message` — the change report: every action taken, from → to, with reasons, plus holds and watchlist).
   - weekly Meta-winners bridge (only if a Meta warehouse or meta-ads CLI exists): `reports/snap_meta_winners_weekly_context.py` + the weekly prompt. Playbook: `skills/snapchat-ads/references/meta-to-snap-dpa-video-bridge.md`.
4. First scheduled run: watch it end-to-end and verify the Slack change report arrives.

**Gate:** one full scheduled cycle completed; the change report posted; every mutation appears in `~/.config/snapchat-ads-cli/audit.jsonl`.

## Wrap-up

Write a short setup record into the docs index from Phase 4: what was configured, schedules, channel IDs, and any IDs created. Remind the user: rotating the Snap OAuth app or Slack token means updating `~/.config/snapchat-ads-cli/.env` only — nothing in the repo holds secrets.
