---
name: snapchat-ads
description: Complete Snapchat Marketing API reference + safe wrapper around the snapchat-ads CLI for campaign creation, insights queries, audience uploads, creative builders, bulk operations, async reports, CAPI, Meta-to-Snap DPA video bridges, and all Snap Ads tasks. Read for ANY Snap Ads API work.
---

# Snapchat Ads API Skill

Complete reference for the Snapchat Marketing API and the `snapchat-ads` CLI. Use this skill for ANY task involving Snap Ads -- campaign creation, ad squad targeting, creative management, audience segments, pixel events, server-side conversions (CAPI), reports, bulk operations, or moving Meta winner videos into Snap catalog/DPA collection ads.

CLI source of truth: `cli/` (repo root). All commands run via `uv run --directory cli snapchat-ads ...`.

---

## CLI invocation

```bash
uv run --directory cli snapchat-ads --account default --human <group> <command> [...]
```

- `--account default` -- account key from `~/.config/snapchat-ads-cli/accounts.toml`
- `--human` -- table output (default is JSON)
- All write operations are **two-phase**: default returns a preview; re-run with `--execute` to apply.
- Audit log: every mutation appends to `~/.config/snapchat-ads-cli/audit.jsonl`.

## Environment Variables

| Variable                   | Description                                                         | Source                   |
| -------------------------- | ------------------------------------------------------------------- | ------------------------ |
| `SNAPCHAT_CLIENT_ID`       | OAuth client ID from Snap Business Manager -> Apps                  | manual                   |
| `SNAPCHAT_CLIENT_SECRET`   | OAuth client secret                                                 | manual (sensitive)       |
| `SNAPCHAT_REDIRECT_URI`    | Registered HTTPS redirect URI (`auth callback-url` provisions one via Tailscale Funnel) | manual          |
| `SNAPCHAT_ACCESS_TOKEN`    | Bearer access token (1-hour TTL)                                    | written by `auth login`  |
| `SNAPCHAT_REFRESH_TOKEN`   | Long-lived refresh token                                            | written by `auth login`  |
| `SNAPCHAT_ORGANIZATION_ID` | Organization ID (from `org list`)                                   | manual after first login |
| `SNAPCHAT_AD_ACCOUNT_ID`   | Ad account ID (from `org list-accounts`)                            | manual after first login |
| `SNAPCHAT_PIXEL_ID`        | Snap Pixel ID (from `pixel list`)                                   | manual when pixel exists |

All env vars live in `~/.config/snapchat-ads-cli/.env` (copied from the repo's `.env.example`).

---

## Auth bootstrap (first-time setup)

```bash
# 1. Fill in client creds
mkdir -p ~/.config/snapchat-ads-cli
cp skills/snapchat-ads/.env.example ~/.config/snapchat-ads-cli/.env
$EDITOR ~/.config/snapchat-ads-cli/.env   # set CLIENT_ID, CLIENT_SECRET

# 2. Provision the HTTPS redirect URI (machine has Tailscale? use Funnel)
uv run --directory cli snapchat-ads auth callback-url --check   # `conflicts` must be {} — else try --https-port 443/10000; never --force
uv run --directory cli snapchat-ads auth callback-url           # prints callback_url
# Give callback_url to the user -> they register it as the Redirect URI on the
# Snap OAuth app (Business Manager -> Business Details -> Apps). Then:
$EDITOR ~/.config/snapchat-ads-cli/.env   # set SNAPCHAT_REDIRECT_URI=<callback_url>
# No Tailscale: ask the user for an HTTPS redirect URI they control instead.

# 3. Initialize accounts.toml
uv run --directory cli snapchat-ads init

# 4. Run OAuth code-grant flow (auto-captures the code via the funnel)
uv run --directory cli snapchat-ads --account default auth login --listen
# CLI prints an authorize URL. Open in a browser, sign in, approve.
# The funnel delivers ?code=... to the CLI's one-shot listener (state-checked).
# Manual fallback (no Tailscale): `auth login` and paste the code at the prompt.

# 5. Verify + discover IDs
uv run --directory cli snapchat-ads --human auth status
uv run --directory cli snapchat-ads --human org list
uv run --directory cli snapchat-ads --human org list-accounts <ORG_ID>

# 6. Edit accounts.toml -- fill in organization_id, ad_account_id, pixel_id
$EDITOR ~/.config/snapchat-ads-cli/accounts.toml
```

`auth refresh` rotates the access token; the CLI also auto-refreshes on a single 401.

---

## Resource Hierarchy

```
Organization (top-level; created in Snap Business Manager, not via API)
  Funding Source / Billing Center / Members / Roles
  Ad Account
    Campaign  (objective_v2_properties.objective_v2_type, buy_model)
      Ad Squad (targeting, optimization_goal, bid_strategy, budget)
        Ad
          Creative (linked by creative_id)
              creative_elements + interaction_zones (Collection / DPA)
    Media (videos, images, lenses, playables)
    Segment (Customer List / Lookalike / Pixel / App / Engagement)
    Pixel + Custom Conversions
    Catalog (Org-scoped) + Product Sets + Product Feeds + Dynamic Templates
```

Money is **micro-currency** (1 USD = 1_000_000 micro). The CLI accepts dollar strings (`50`, `'50.00'`, `'$50'`) or `micro:50000000` and converts at the boundary.

---

## Command Surface (24 groups, ~172 commands)

```
auth              login, exchange, refresh, status, revoke
account           list, info, health-check, phone-numbers, assign-role, remove-role
org               list, get, list-accounts, members, member-roles, roles,
                  funding-sources, billing-centers, create-account, update-account,
                  assign-role, invite-member, revoke-member
campaign          list, get, get-by-ids, create, update, pause, launch, delete,
                  bulk-create, bulk-update, duplicate
adsquad           list, get, create, update, pause, launch, delete, spend-guidance,
                  smart-create, restrictions, bulk-create, bulk-update, duplicate
ad                list, get, get-by-ids, create, update, pause, launch, delete,
                  bulk-create, bulk-update, duplicate
manage            bulk-pause, bulk-launch, bulk-archive, bulk-budget
creative          list, get, create, update, preview,
                  create-snap-ad, create-app-install, create-web-view,
                  create-deep-link, create-ad-to-call, create-ad-to-message,
                  create-ad-to-lens, create-collection, create-longform-video,
                  create-lead-generation, create-reminder, create-story-preview,
                  create-story-composite, create-lens, create-lens-web-view,
                  create-lens-app-install, create-lens-deep-link,
                  bulk-create, bulk-update
media             list, get, status, upload, get-by-ids, preview, thumbnail,
                  lens-preview, copy, claim
segment           list, get, create, update, delete, clear, add-users,
                  remove-users, lookalike-create, bulk-create, bulk-update
pixel             list, get, create, update, domain-stats, custom-conversions,
                  stats, send-events, bulk-create, bulk-update
targeting         insights, geo-search, demo-reference, geo-reference,
                  interests-reference, device-names, options-by-country,
                  insights-breakdown
report            stats, daily, hourly, top-ads, video,
                  async-submit, async-status, async-download,
                  lead-gen-submit, lead-gen-status, lead-gen-download
catalog           list, get, product-sets, product-set-get, create,
                  create-product-set, update-product-set, dynamic-templates,
                  dynamic-template-get, feeds, feed-get, create-feed, delete-feed
creative-element  list, get, create, update, delete, bulk-create
interaction-zone  list, get, create, update, delete, bulk-create
mobileapp         list, get, create, ecid-status, custom-conversions
capi              send                       # CAPI v3 server-side conversions
audit             changelog
estimate          bid, audience-size, reach-frequency, adsquad-outcomes
billing           invoices, invoice, transactions, transaction
study             list, get, create, results,
                  conversion-lift-list, conversion-lift-get
me                                              # GET /v1/me
init                                            # write default accounts.toml
```

---

## Safety rules (ALWAYS FOLLOW)

1. **Confirm before any write.** All mutations default to preview. Show the user the proposed change and require explicit approval before re-running with `--execute`.
2. **Never delete campaigns / ad squads / ads.** Use `manage bulk-archive` (status=ARCHIVED) instead. Snap has no native multi-delete; archive is the documented soft-delete pattern.
3. **Budget changes show before/after via the preview block.** Always read current state, present the delta, then `--execute`.
4. **Status change to ACTIVE requires confirmation** even on individual ads.
5. **Audit-log every successful mutation** -- already automatic; reference `~/.config/snapchat-ads-cli/audit.jsonl` when investigating.
6. **All money is micro.** Never pass raw cents -- the CLI converts dollars to micro automatically. If something says "budget is 50000", that's 50 micro = $0.00005, not $50.
7. **Do not bypass `--execute`.** Agents auto-piping `--execute` defeats the safety gate. Show the preview, wait for user approval, then re-run.

---

## Common workflows

### Meta winner videos -> Snap DPA collection ads

When the user asks to move top Meta videos into Snapchat while keeping the existing catalog/product-set/DPA setup, read `references/meta-to-snap-dpa-video-bridge.md` first. It contains an example DPA defaults block, the Meta winner selection commands, the media download/validation flow, and the Snap preview/execute sequence.

Key rule: keep every new Snap object `PAUSED`; media uploads, ad squad creation, collection creative creation, and ad creation must all be previewed before any `--execute` run.

### Reactivate old non-DPA Snap campaign from DPA + Meta winners

When the user asks to reactivate an old non-DPA Snapchat campaign, keep ads off, pull through top DPA ads, and add unique Meta ads for specific product lines (e.g. Product A / Product B), read `references/non-dpa-reactivation-from-dpa-and-meta-winners.md` first. It walks through the canonical old campaign/ad-squad IDs, correct product-line LP routing, the ad-squad-level budget split pattern for requested campaign budgets, and the safe preview/execute sequence.

Key rules: reactivation may set the campaign/ad squads active, but existing and newly created ads stay `PAUSED`; split requested budget across product-line ad squads when the campaign is ad-squad paced; reuse existing Snap `top_snap_media_id` / registry media instead of re-uploading bridged videos; never run `--execute` before explicit approval.

### Daily spend snapshot

```bash
snapchat-ads --human report daily --days 7
snapchat-ads --human report top-ads --days 7 --by spend --limit 25
```

### Create a campaign + ad squad + ad (ABO, web conversion)

```bash
# 1. Campaign (objective_v2 is the future-proof field; --objective is the legacy path)
snapchat-ads campaign create \
  --name "Q3 Bedding Acquisition" \
  --objective-v2-type WEB_CONVERSION \
  --promotion-type SALES \
  --buy-model AUCTION \
  --status PAUSED \
  --execute

# 2. Ad squad with first-class flags (no hand-rolled JSON)
snapchat-ads adsquad smart-create <CAMPAIGN_ID> \
  --name "US 25-44 Cold" \
  --optimization-goal PIXEL_PURCHASE \
  --bid-strategy LOWEST_COST_WITH_MAX_BID \
  --bid 4 \
  --daily-budget 75 \
  --conversion-window SWIPE_28DAY_VIEW_1DAY \
  --pixel-id $SNAPCHAT_PIXEL_ID \
  --targeting-json '{"geos":[{"country_code":"us"}],"demographics":[{"min_age":25,"max_age":44}]}' \
  --execute

# 3. Upload media + create app-install creative + attach
snapchat-ads media upload --file ad.mp4 --type VIDEO --execute   # returns media_id
snapchat-ads creative create-web-view \
  --name "Bedding LP" \
  --headline "Hotel-quality sheets" \
  --brand-name "My Brand" \
  --top-snap-media-id <MEDIA_ID> \
  --url "https://example.com/bedding" \
  --profile-id <PUBLIC_PROFILE_ID> \
  --call-to-action SHOP_NOW \
  --execute
snapchat-ads ad create <AD_SQUAD_ID> \
  --payload-json '{"name":"Bedding A","creative_id":"<CREATIVE_ID>","status":"PAUSED","type":"SNAP_AD"}' \
  --execute
```

### Bulk operations

```bash
# Bulk-create 200 campaigns from a JSON file (auto-chunked at 10/batch)
snapchat-ads campaign bulk-create --file campaigns.json --execute

# Bulk-update budgets across many campaigns
echo '[{"id":"c1","daily_budget":75},{"id":"c2","daily_budget":50}]' > /tmp/budgets.json
snapchat-ads manage bulk-budget --entity campaign --file /tmp/budgets.json --execute

# Pause / launch / archive many ads at once (chunked at 100/batch)
snapchat-ads manage bulk-pause AD1 AD2 AD3 --execute
snapchat-ads manage bulk-archive --file ad-ids.txt --execute      # one ID per line

# Fetch many entities in one go (auto-chunked at 2000/batch)
snapchat-ads campaign get-by-ids --file campaign-ids.json
snapchat-ads ad get-by-ids --file ad-ids.csv
```

### Audience uploads

```bash
# CSV/text file of identifiers -- auto-normalized + SHA256-hashed + batched at 10k
snapchat-ads segment add-users <SEG_ID> \
  --file customers.csv --schema EMAIL_SHA256 --execute

# Lookalike from a seed segment
snapchat-ads segment lookalike-create \
  --name "LAL Q3 Buyers" \
  --seed-segment-id <SEED_SEG_ID> \
  --countries US,CA \
  --type BALANCE \
  --retention-days 180 \
  --execute
```

### CAPI / server-side conversion events

```bash
# Events file: JSON array or NDJSON. Identifiers (em, ph, idfa, aaid, external_id)
# are auto-SHA256-hashed unless --no-hash is passed. Auto-batched at 1000/event.
snapchat-ads capi send \
  --pixel-id $SNAPCHAT_PIXEL_ID \
  --events-file ./events.ndjson \
  --execute

# Pixel-scoped alias:
snapchat-ads pixel send-events <PIXEL_ID> --events-file ./events.ndjson --execute
```

### Hourly heartbeat / Meta-style Slack report data

For hourly Snapchat Ads heartbeat crons that mimic the Meta heartbeat format, use a small pre-run collector script plus your scheduler's threaded JSON delivery (main_message/thread_message contract):

- Account-level `report hourly` only supports `spend` fields. If you request impressions/swipes/purchases at ad-account hourly granularity, Snap returns `Unsupported Stats Query: Only field 'spend' should be used when querying AdAccount stats.`
- Use `report hourly --hours 24 --fields spend` for the hourly spend pulse.
- Use `report stats --entity ad_account --granularity TOTAL --breakdown ad --fields spend,impressions,swipes,conversion_purchases,conversion_purchases_value` over exact ET-aligned windows for ad-level ROAS/CPA/purchase readouts.
- Prefer exact windows like `America/New_York now rounded to the hour minus 24h -> current hour` over `top-ads --days 1`; `--days 1` can land on a prior 24h/calendar window and is too loose for heartbeat reporting.
- Snap account timezones matter. The example account below reports in `America/New_York`; present the latest hour in both PT and ET when posting in Slack.
- `conversion_purchases_value` is available and should be used for Meta-style heartbeats. ROAS = `conversion_purchases_value / spend`; CPA = `spend / conversion_purchases`. Prioritize ROAS and CPA in the headline and Account Summary when the user asks for Meta-like output.

Example source commands:

```bash
snapchat-ads report hourly --hours 24 --fields spend
snapchat-ads report stats --entity ad_account --granularity TOTAL \
  --start-time 2026-05-14T17:00:00-04:00 \
  --end-time 2026-05-15T17:00:00-04:00 \
  --fields spend,impressions,swipes,conversion_purchases \
  --breakdown ad --omit-empty
```

### Reports

```bash
# Sync stats (entity-scoped)
snapchat-ads report stats --entity ad_account --granularity DAY \
  --start-time 2026-04-01T00:00:00Z --end-time 2026-04-25T00:00:00Z \
  --fields spend,impressions,swipes,conversion_purchases \
  --csv-out /tmp/snap-april.csv

# Hourly ad-account spend. Snap's ad-account hourly endpoint supports spend-only.
snapchat-ads report hourly --hours 24 --fields spend

# Ad-level breakdowns can provide impressions/swipes/purchases for report detail.
snapchat-ads report top-ads --days 1 --by spend --limit 25

# Async report job for large pulls
snapchat-ads report async-submit \
  --entity ad_account \
  --granularity DAY \
  --start-time 2026-04-01T00:00:00Z \
  --end-time 2026-04-25T00:00:00Z \
  --fields spend,impressions,swipes,conversion_purchases \
  --breakdown ad
snapchat-ads report async-status <REPORT_RUN_ID>
snapchat-ads report async-download <REPORT_RUN_ID> --out report.csv

# Lead-gen reports use Snap's separate leads_report endpoint.
snapchat-ads report lead-gen-submit \
  --start-time 2026-04-01T00:00:00Z \
  --end-time 2026-04-25T00:00:00Z
snapchat-ads report lead-gen-download <REPORT_RUN_ID> --out leads.csv
```

### Hourly Slack report cron pattern

When asked to create a Snapchat Ads hourly report that mimics the Meta hourly/current-day Slack format, first determine whether the user means a cron-based JSON-delivery report or a direct-post *systemd* timer/service job.

- If the user says "exact systemd format," or complains about a `Cronjob Response` wrapper / raw JSON / job-management footers, use `references/systemd-hourly-slack-heartbeat.md` or `references/hourly-systemd-slack-report.md`. This is the canonical pattern for a clean Slack main message plus thread reply with no cron wrapper.
- If the user explicitly wants cron-based threaded JSON delivery, use `references/hourly-slack-report-cron.md` and ensure the final response is JSON with `main_message` and `thread_message`.
- Keep the report read-only; never mutate campaigns, ad squads, ads, budgets, or statuses from a report job.
- For account-level hourly data, request only `--fields spend`; use exact ET-aligned `report stats --entity ad_account --granularity TOTAL --breakdown ad` windows for ad-level purchases, purchase value, CPA, and ROAS. Avoid loose `top-ads --days 1` when the heartbeat needs exact 24h/current-day context.
- For Snapchat hourly main-channel headlines, use **rolling 24h ROAS** as the lead ROAS metric (`last_24.roas`), not today-so-far ROAS. Today-so-far ROAS can stay in the thread/account details. When changing this, patch the live systemd script (`~/.config/systemd/user/scripts/post-snapchat-hourly-heartbeat.py`) and any other deployed copy, then verify with `python3 -m py_compile` plus a dry `build_messages()` sample.
- Display account timezone and user/operational timezone when they differ, but keep the report legible and Meta-like: headline + Account Summary should emphasize ROAS, CPA, spend/revenue, and purchases before lower-priority delivery-health details.

### Targeting insights

```bash
snapchat-ads targeting insights-breakdown \
  --spec-json '{"geos":[{"country_code":"us"}],"demographics":[{"min_age":25,"max_age":44}]}' \
  --breakdown AGE_GENDER
```

---

## Batch-size reference (auto-chunked by the CLI)

| Endpoint family                                                                   | Max per batch     | CLI default                       |
| --------------------------------------------------------------------------------- | ----------------- | --------------------------------- |
| `get_*_by_ids` (campaigns, ads, media)                                            | 2000              | 2000                              |
| Segment users add/remove (`/segments/{id}/users`)                                 | 10,000            | 10,000                            |
| CAPI events (`tr.snapchat.com/v3/conversion`)                                     | 1,000             | 1,000                             |
| Ads bulk status / bulk-update PUT                                                 | ~100 (inferred)   | 100                               |
| Create / update arrays for campaigns/adsquads/ads/creatives/segments/pixels/CE/IZ | 10-30 (Snap docs) | 10 (override with `--chunk-size`) |

---

## Auto-refresh + error semantics

- 401 with `invalid_token` triggers a single auto-refresh inside the API client. After that retry, hard-bails with "refresh token revoked, re-run auth login" and exit 2.
- 429 + 5xx auto-retried with exponential backoff (max 5 attempts, capped at 60s, honors `Retry-After`).
- 4xx (non-401, non-429) surfaces immediately as a structured `SnapApiError` with `request_status`, `error_code`, `request_id`, and `debug_message`.

---

## Files written

| Path                                          | Purpose                                                            |
| --------------------------------------------- | ------------------------------------------------------------------ |
| `~/.config/snapchat-ads-cli/accounts.toml`    | Per-account config (org_id, ad_account_id, pixel_id, token_source) |
| `~/.config/snapchat-ads-cli/audit.jsonl`      | Append-only audit log of every mutation                            |
| `~/.config/snapchat-ads-cli/.token.json`      | OAuth tokens (mode 0600)                                            |
| `~/.config/snapchat-ads-cli/.env`             | Credentials copied from the repo's `.env.example`                  |

---

## When to read what

- **API endpoint behavior, payload shapes, status enums** -> https://developers.snap.com/api/marketing-api/
- **CLI command surface, flag semantics** -> this file + `snapchat-ads <group> <command> --help`
- **Per-write audit trail** -> `tail -f ~/.config/snapchat-ads-cli/audit.jsonl | jq`
- **Live token state** -> `snapchat-ads --human auth status`

For any new endpoint Snap ships, check whether the CLI exposes it: `snapchat-ads --help` lists every group; `<group> --help` lists every subcommand. If a documented endpoint is missing, file a bounty to extend the corresponding `tools/<resource>.py` module + wire a CLI subcommand. The CLI's structure mirrors the API one-to-one.
