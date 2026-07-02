# snapchat-ads CLI

Full-surface CLI for the Snap Marketing API.

- JSON output by default; `--human` for tables.
- All write operations are two-phase: default returns a preview; re-run with `--execute` to apply.
- Audit log: `~/.config/snapchat-ads-cli/audit.jsonl`.
- Token persistence: `~/.config/snapchat-ads-cli/.token.json` (mode 0600).

## Install

```bash
cd cli
uv sync
uv run snapchat-ads --help
```

## Auth bootstrap

1. Register a Snap Business app at https://business.snapchat.com -> Apps. Note the
   client ID and secret.
2. Get an HTTPS redirect URI (Snap requires one). On a machine running
   [Tailscale](https://tailscale.com) the CLI provisions it for you via Funnel:
   ```bash
   uv run snapchat-ads auth callback-url --check   # inspect; conflicts must be empty
   uv run snapchat-ads auth callback-url           # publish; prints callback_url
   ```
   Register the printed `callback_url` as the Redirect URI on the Snap app
   (Business Details -> Apps -> your app) — the value must match exactly.
   If `conflicts` is non-empty, pick another port (`--https-port 443|8443|10000`)
   rather than `--force`: funnel exposure is per-port, so forcing would publish
   every other path served on that port to the internet. `--off` tears the
   route down. Without Tailscale, use any HTTPS URL you control.
3. Fill in `~/.config/snapchat-ads-cli/.env` (or export the vars in your shell) —
   template at the repo root `.env.example`. Required for first login:
   `SNAPCHAT_CLIENT_ID`, `SNAPCHAT_CLIENT_SECRET`, `SNAPCHAT_REDIRECT_URI`.
4. Create the local account config:
   ```bash
   uv run snapchat-ads init
   ```
   This writes `~/.config/snapchat-ads-cli/accounts.toml` with the `default` account.
5. Run the OAuth flow:
   ```bash
   uv run snapchat-ads --account default auth login --listen
   ```
   Open the printed URL, sign in, and approve. With `--listen`, the funnel
   route delivers the `code=` redirect straight to a one-shot local listener
   (state-validated), the CLI exchanges it, persists access + refresh tokens,
   and tears down any funnel route it created (`--keep-funnel` to keep it).
   Without Tailscale, run plain `auth login` and paste the `code=` value from
   the redirect URL at the prompt.
6. Verify:
   ```bash
   uv run snapchat-ads --human auth status
   uv run snapchat-ads --human org list
   ```
7. Fill in `organization_id`, `ad_account_id`, and (later) `pixel_id` in
   `~/.config/snapchat-ads-cli/accounts.toml` so subsequent commands don't need
   the IDs as arguments.

## Command surface

```text
auth              login, callback-url, exchange, refresh, status, revoke
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
creative          list, get, create, update, preview, create-snap-ad,
                  create-web-view, create-app-install, create-deep-link,
                  create-ad-to-call, create-ad-to-message, create-ad-to-lens,
                  create-collection, create-longform-video, create-lead-generation,
                  create-reminder, create-story-preview, create-story-composite,
                  create-lens, create-lens-web-view, create-lens-app-install,
                  create-lens-deep-link, bulk-create, bulk-update
media             list, get, status, upload, get-by-ids, preview, thumbnail,
                  lens-preview, copy, claim
segment           list, get, create, update, delete, clear, add-users,
                  remove-users, lookalike-create, bulk-create, bulk-update
pixel             list, get, create, update, domain-stats, custom-conversions,
                  stats, send-events, bulk-create, bulk-update
targeting         insights, geo-search, demo-reference, geo-reference,
                  interests-reference, device-names, options-by-country,
                  insights-breakdown
report            stats, daily, hourly, top-ads, video, async-submit, async-status,
                  async-download, lead-gen-submit, lead-gen-status, lead-gen-download
catalog           list, get, product-sets, product-set-get, create,
                  create-product-set, update-product-set, dynamic-templates,
                  dynamic-template-get, feeds, feed-get, create-feed, delete-feed
creative-element  list, get, create, update, delete, bulk-create
interaction-zone  list, get, create, update, delete, bulk-create
mobileapp         list, get, create, ecid-status, custom-conversions
capi              send                     # CAPI v3 server-side conversions
audit             changelog
estimate          bid, audience-size, reach-frequency, adsquad-outcomes
billing           invoices, invoice, transactions, transaction
study             list, get, create, results, conversion-lift-list, conversion-lift-get
me                                         # GET /v1/me
init                                       # write default accounts.toml
```

## Money values

Snap stores money as micro-currency (1 USD = 1_000_000 micro). The CLI accepts:

- A plain dollar amount: `--daily-budget 50` or `--daily-budget '50.00'`.
- An explicit micro override: `--daily-budget micro:50000000`.

Reports display micro values as `$x.xx` in `--human` mode.

## Reporting

Sync stats use Snap's documented `/{entity}/{id}/stats` endpoints. Async stats
are created by the same stats endpoint with `async=true`, then checked through
`/{entity}/{id}/stats_report?report_run_id=...`.

```bash
uv run snapchat-ads report async-submit \
  --entity ad_account \
  --granularity DAY \
  --start-time 2026-04-01T00:00:00Z \
  --end-time 2026-04-02T00:00:00Z \
  --fields spend,impressions,swipes,conversion_purchases \
  --breakdown ad

uv run snapchat-ads report async-status ASYNC_STATS:...
uv run snapchat-ads report async-download ASYNC_STATS:... --out snap-report.csv
```

Lead-gen reports use Snap's separate `adaccounts/{id}/leads_report` endpoint:

```bash
uv run snapchat-ads report lead-gen-submit \
  --start-time 2026-04-01T00:00:00Z \
  --end-time 2026-04-02T00:00:00Z
uv run snapchat-ads report lead-gen-download ASYNC_STATS:... --out leads.csv
```

## Audience uploads

`segment add-users` and `remove-users` read a CSV/text file of identifiers,
normalize them (lowercase + trim for emails, digits-only for phones), and
SHA256-hash each before posting. Pass `--pre-hashed` if your input is already
SHA256 hex.

## Error semantics

- 401 `invalid_token` triggers a single auto-refresh, then hard-fails with exit 2
  if the refresh token is revoked.
- 429 + 5xx retry with exponential backoff (max 5 attempts, honors `Retry-After`).
- Other 4xx surface immediately as structured errors with `request_status`,
  `error_code`, `request_id`, and `debug_message`.

## Hooks

`hooks/check-token.sh` is a session-start health check. Drop a hook entry in
`~/.claude/settings.json` to surface token state on every Claude Code session start.

## Files written

| Path | Purpose |
|---|---|
| `~/.config/snapchat-ads-cli/accounts.toml` | Per-account config |
| `~/.config/snapchat-ads-cli/audit.jsonl`   | Append-only audit log |
| `~/.config/snapchat-ads-cli/.token.json`   | OAuth tokens (mode 0600) |
