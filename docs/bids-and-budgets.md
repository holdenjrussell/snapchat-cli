# Bids & Budgets — the optimizer's control model

This is how the autonomous optimizer (`skills/snapchat-ads/scripts/snap_optimizer_support.py`) manages spend, and how to make the same moves manually. The numbers below are the config defaults; your live values come from `config/optimizer.json` → `optimizer`.

## The core ideas

**1. Attribution delay-aware windows.** Snap purchase attribution lags roughly two days. The decision window is therefore the 7 completed days ending 2 days ago — yesterday and today are never trusted for bid/budget decisions. Judging on fresh data makes you cut winners whose conversions haven't landed yet.

**2. The CPA target is the primary lever, not the budget.** Squads run `TARGET_COST` bidding with budgets set high relative to spend. That means the CPA target throttles delivery:

- ROAS below target → **walk the CPA target down ~10%** (15% if severe, below 0.8× target). Cutting budget first just strands a too-loose CPA target — spend drops but efficiency doesn't improve.
- ROAS above target but budget utilization < 85% → **walk the CPA target up ~10%** to buy more delivery. Raising the budget there does nothing; the squad isn't budget-capped.
- ROAS above target **and** utilization ≥ 85% → the squad is genuinely budget-capped: **raise the budget** (+30%, or +20% for budgets already over the large-budget threshold).
- ROAS below target with the CPA target already at the floor → only then **cut the budget** (−30%).

**3. A blended-ROAS governor sets the account posture each run.** Blended window ROAS across all squads vs the target band (target ± band, default 1.2 ± 0.15):

| Governor mode | When | Allowed moves |
|---|---|---|
| `cut_only` | blended ROAS below the band | only CPA decreases, budget decreases, ad pauses |
| `rebalance` | inside the band | fund winners by tightening losers; net spend ~flat |
| `scale` | above the band | net delivery increases allowed (budget or looser CPA) |

**4. Guardrails are enforced in code, not prose.** Plans are executed via `apply-squad-plan` / `apply-pause-plan`, which block anything outside:

- Per-run caps: max 4 squad changes; budget moves capped at ±35% per run, CPA moves at ±20%.
- CPA bounds: floor $30, ceiling $150 (configurable).
- **48h cooldown ledger** (`~/.cache/snapchat-optimizer/squad_actions.jsonl`): a squad touched in the last 48h is skipped, so lagged attribution never gets double-punished.
- Minimum evidence: a squad needs ≥ $150 window spend before it's classified at all; scaling additionally needs ≥ 3 window purchases.
- Direction checks (an "increase" that lowers the value is blocked), governor-mode checks, unknown-squad checks.

**5. Ad-level pause rules** (separate from squad levers):

- Zero purchases with material spend: ≥ $40 over 7d (with ≥ $15 over 3d or ≥ $75 over 14d), or ≥ $75 over 14d.
- CPA above 2× target with ≥ $150 spend over 14d.
- Protections: 72h **fresh-hold** on new/edited ads, never pause below 2 active ads per squad, max 4 pauses per run, max 20% of active ads, max 30% of active 7d spend.

## Running it

```bash
# Build context + recommendations (read-only)
python3 skills/snapchat-ads/scripts/snap_optimizer_support.py collect-daily

# Execute a squad plan (preview first — drop --execute to validate only)
python3 skills/snapchat-ads/scripts/snap_optimizer_support.py apply-squad-plan \
  --context-file ~/.cache/snapchat-optimizer/daily_latest_context.json \
  --plan-json '[{"ad_squad_id":"<ID>","action":"cpa_decrease","to_value":54.0,"reason":"window ROAS 0.9 vs 1.2 target"}]' \
  --execute

# Execute pause plan
python3 skills/snapchat-ads/scripts/snap_optimizer_support.py apply-pause-plan \
  --context-file ~/.cache/snapchat-optimizer/daily_latest_context.json \
  --plan-json '[{"ad_id":"<ID>","reason":"zero purchases, $82 L7 spend"}]' \
  --execute
```

Actions: `budget_increase`, `budget_decrease`, `cpa_increase`, `cpa_decrease`. A blocked item comes back with a `decision: blocked_*` code — treat it as a protected hold and report it; never bypass with raw CLI.

## Manual equivalents

```bash
# Budget (dollars; CLI converts to micro)
uv run --directory cli snapchat-ads adsquad update <AD_SQUAD_ID> --daily-budget 97.50 --execute

# CPA target (bid_micro)
uv run --directory cli snapchat-ads adsquad update <AD_SQUAD_ID> --fields-json '{"bid_micro": 54000000}' --execute
```

If you adjust manually, append a line to the cooldown ledger (or accept that the next optimizer run may not know about your change for cooldown purposes).

## Change reporting

Every run posts to Slack (channel from `config/optimizer.json`): a main message with governor mode, blended window ROAS vs target, and change count, plus a thread with **Actions taken (from → to, with reasons)**, holds/skips, delivery watch, and an audit appendix. Slack is an audit trail, not an approval gate — the run always posts, even when it changes nothing. Raw history: `~/.config/snapchat-ads-cli/audit.jsonl` (every API mutation) and the squad ledger.
