# snapchat-cli — Claude Code instructions

This repo is a self-contained Snapchat ads operations stack: a full Marketing-API CLI, an autonomous bid/budget optimizer, a Meta→Snap creative bridge, a Postgres warehouse layer, and Slack Block Kit reporting. Everything is brand-agnostic; instance specifics live in `~/.config/snapchat-ads-cli/.env` and `config/optimizer.json` (both gitignored).

**First time on a machine?** Run `/setup-snapchat-cli` (defined in `.claude/commands/`) — it walks OAuth, warehouse build, docs index, Slack reports, campaigns, and the optimizer schedules with verification gates.

## Repo map

| Path | What it is |
|---|---|
| `cli/` | `snapchat-ads` CLI — 24 groups, ~172 commands, mirrors the Snap Marketing API 1:1. Run: `uv run --directory cli snapchat-ads --account default --human <group> <cmd>` |
| `skills/snapchat-ads/` | API reference skill (`SKILL.md`), safe wrapper (`scripts/snapchat_ads_safe.sh`), optimizer engine (`scripts/snap_optimizer_support.py`), workflow playbooks (`references/`) |
| `skills/slack-format/` | Dependency-free Slack Block Kit builder (`slack_format.py`) — use it for any Slack output |
| `skills/snap-command/` | Quick-command patterns for ad-hoc Snap queries |
| `config/` | `optimizer.example.json` → copy to `optimizer.json`, fill brand/products/targets |
| `warehouse/` | `schema.sql`, `sync_snapchat_daily.py` (API→Postgres upsert), `query.py` (SELECT-only helper), `table-map.md` |
| `reports/` | Hourly Slack heartbeat (collector + poster + systemd units), daily/weekly optimizer context shims |
| `docs/` | `campaign-setup.md`, `bids-and-budgets.md`, `strategy-notes.md`, `obsidian-index-template.md` |

## Non-negotiable safety rules

1. **Two-phase writes.** Every mutating CLI command returns a preview by default; only re-run with `--execute` after the change has been shown and approved (or when acting inside the optimizer executors' guardrails on a pre-approved schedule).
2. **Never delete** campaigns/ad squads/ads — archive (`manage bulk-archive`). Deletions are never pre-approved.
3. **Money is micro-currency** (1 USD = 1,000,000 micro). The CLI converts dollar strings at the boundary; never hand-build micro values in payloads unless using `micro:` syntax.
4. **Optimizer mutations go through the executors** (`apply-squad-plan`, `apply-pause-plan`) — never raw CLI for bid/budget/pause changes. The executors enforce governor mode, 48h cooldowns, step caps, and CPA bounds. A blocked action is a protected hold: report it, don't bypass it.
5. **Snap attribution lags ~2 days.** Never judge performance on yesterday/today; the decision window is 7 completed days ending 2 days ago.
6. **Secrets stay out of the repo.** Tokens live in `~/.config/snapchat-ads-cli/` (mode 0600). Never commit `.env`, `config/optimizer.json`, or `.token.json`; never print token values.
7. **Audit trail:** every CLI mutation appends to `~/.config/snapchat-ads-cli/audit.jsonl`; optimizer squad changes also append to `~/.cache/snapchat-optimizer/squad_actions.jsonl`. Slack reports are an audit surface, not an approval gate — always post, never silent.

## Common entry points

```bash
# Account state
uv run --directory cli snapchat-ads --human auth status
uv run --directory cli snapchat-ads --human report daily --days 7
uv run --directory cli snapchat-ads --human report top-ads --days 7 --by spend --limit 25

# Optimizer (read-only context build + recommendations)
python3 skills/snapchat-ads/scripts/snap_optimizer_support.py collect-daily
python3 skills/snapchat-ads/scripts/snap_optimizer_support.py collect-weekly   # + Meta winners

# Warehouse
uv run warehouse/sync_snapchat_daily.py --days 7
uv run warehouse/query.py --sql "select ..." --reason "why"

# Slack heartbeat (one-shot)
python3 reports/post_snapchat_hourly_heartbeat.py
```

## When editing

- The optimizer's tests live at `skills/snapchat-ads/tests/`; the CLI's at `cli/tests/`. Run both after touching either.
- The CLI's structure mirrors the Snap API one-to-one — new endpoints get a function in `cli/src/snapchat_ads_cli/tools/<resource>.py` plus a subcommand in `cli.py`.
- Keep this stack brand-agnostic: anything account-specific belongs in `config/optimizer.json` or the env file, never in code or docs.
