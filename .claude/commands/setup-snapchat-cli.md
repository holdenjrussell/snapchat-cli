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
2. Ask the user for `SNAPCHAT_CLIENT_ID` and `SNAPCHAT_CLIENT_SECRET` (from Snap Business Manager → Business Details → Apps; they may need to create an app with the Marketing API scope). Fill them into the env file — never echo secrets back.
3. **Redirect URI — provision it yourself, don't ask the user for one.** Snap requires an HTTPS redirect URI and the CLI needs the browser redirect to land back on this machine. If `tailscale` is on PATH (check with `which tailscale`), Tailscale Funnel is the default answer:
   - Dry-run first: `uv run --directory cli snapchat-ads auth callback-url --check` — confirm `backend_state` is `Running` and `conflicts` is empty. If the default port (8443) has conflicts, retry `--check` with `--https-port 443` or `--https-port 10000` until you find a clean port; **never pass `--force`** — funnel exposure is per-port and would publish every conflicting path to the open internet.
   - Provision: `uv run --directory cli snapchat-ads auth callback-url` (plus the `--https-port` you chose). The JSON output contains `callback_url`.
   - **Hand the `callback_url` to the user** and have them register it in Snap Business Manager → Business Details → Apps → their OAuth app → Redirect URI (the value must match exactly). Wait for them to confirm it's saved.
   - Write the same URL into the env file as `SNAPCHAT_REDIRECT_URI=<callback_url>`.
   - If funnel errors about the `funnel` node attribute, the tailnet ACLs don't allow Funnel yet — send the user to https://tailscale.com/kb/1223/funnel, then retry.
   - No Tailscale on this machine? Ask the user for an HTTPS redirect URI they control, set `SNAPCHAT_REDIRECT_URI`, and use the manual paste flow in step 5.
4. `uv run --directory cli snapchat-ads init` (writes `~/.config/snapchat-ads-cli/accounts.toml` with the `default` account).
5. `uv run --directory cli snapchat-ads --account default auth login --listen` — give the user the printed authorize URL; when they approve in the browser, the funnel delivers the `code=` straight to the CLI (no paste) and tears the route back down. Re-auth later works the same way — the URL stays registered on the Snap app, and `auth login --listen` re-provisions the route on demand. Without Tailscale, run plain `auth login` and have the user paste the `code=` value from the redirect URL.
6. Discover IDs and write them into both the env file and `accounts.toml`:
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

1. Ask for `DATABASE_URL` (any Postgres — Neon/Supabase/local). Add it and `SNAP_WAREHOUSE_SCHEMA` (default `snapchat_ads`; an install that already has `public.snapchat_ad_daily_metrics` sets `public` to upgrade that table in place) to the env file.
2. Preview, then show the user the schema before writing: `uv run warehouse/sync_snapchat_daily.py --dry-run --days 30` and `uv run warehouse/sync_snapchat_entities.py`.
3. After approval, create the schema with the entity baseline, then backfill metrics: `uv run warehouse/sync_snapchat_entities.py --execute --apply-schema`, then `uv run warehouse/sync_snapchat_daily.py --days 30`. Both read `SNAP_WAREHOUSE_SCHEMA`.
4. Verify against the account: per-day `SUM(spend)` in `<schema>.snapchat_ad_daily_metrics` must equal `snapchat-ads report stats --entity ad_account --id <AD_ACCOUNT_ID> --granularity DAY --fields spend` for the same account-timezone days.
5. Schedule `warehouse/run_snapchat_warehouse_cycle.py --mode closed --execute` daily (trailing closed days) and `--mode recent --execute` hourly (today so far, stored as provisional rows) via cron/systemd/launchd. The cycle takes a file lock and records each run in `snapchat_sync_runs`. Anything that reports today's spend, such as a P&L, needs the hourly recent cycle.

**Gate:** closed days reconcile to the account-level API to the cent, today's provisional row exists after a recent cycle, and `snapchat_entity_state` has a current baseline. Read `warehouse/table-map.md` for the column contract.

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
