---
name: snap-command
description: Quick snapchat-ads CLI shortcut. Use when the user types /snap, "snap status", "snapchat quick report", or another natural-language request about Snapchat ad campaigns, performance, audience uploads, creative builders, CAPI events, or account management.
activates_on: /snap
user-invocable: true
---

# /snap -- Snapchat Ads Quick Command

Shortcut for `snapchat-ads` CLI operations. Parse `$ARGUMENTS` and run the appropriate command.

## Argument Patterns

| User says | What to run |
|-----------|-------------|
| `/snap` (no args) | Token health + today's spend: `snapchat-ads --account default --human auth status` then `report daily --days 1` |
| `/snap status` | Token / API health: `snapchat-ads --human auth status` and `account health-check` |
| `/snap daily` | Daily report: `snapchat-ads --account default --human report daily --days 7` |
| `/snap top` | Top ads by spend: `snapchat-ads --account default --human report top-ads --days 7 --by spend --limit 25` |
| `/snap hourly` | Hourly report: `snapchat-ads --account default --human report hourly --hours 24` |
| `/snap video` | Video metrics: `snapchat-ads --account default --human report video --days 7` |
| `/snap campaigns` | Campaign list: `snapchat-ads --account default --human campaign list` |
| `/snap adsquads CAMPAIGN_ID` | Ad squads under a campaign: `adsquad list --campaign-id CAMPAIGN_ID` |
| `/snap ads SQUAD_ID` | Ads under a squad: `ad list --ad-squad-id SQUAD_ID` |
| `/snap orgs` | Organizations: `snapchat-ads --human org list` |
| `/snap accounts ORG_ID` | Ad accounts under an org: `org list-accounts ORG_ID` |
| `/snap creatives` | Creative library: `creative list` |
| `/snap segments` | Audience segments: `segment list` |
| `/snap pixels` | Snap Pixels: `pixel list` |
| `/snap pixel-stats PIXEL_ID` | Pixel stats: `pixel stats PIXEL_ID --granularity DAY` |
| `/snap pause AD_ID` | Pause an ad (preview, then ask before --execute) |
| `/snap launch AD_ID` | Launch an ad (preview, then ask before --execute) |
| `/snap bulk-pause AD1 AD2 ...` | Bulk pause: `manage bulk-pause AD1 AD2 ...` (preview first) |
| `/snap bulk-archive AD1 AD2 ...` | Archive many ads: `manage bulk-archive ...` (preview first) |
| `/snap auth login` | Bootstrap OAuth: `auth login` (interactive) |
| `/snap auth refresh` | Rotate access token: `auth refresh` |
| `/snap auth status` | Token state: `auth status` |
| `/snap insights JSON` | Targeting insights: `targeting insights --spec-json '<JSON>'` |
| `/snap capi FILE` | Send CAPI events from file: `capi send --pixel-id $SNAPCHAT_PIXEL_ID --events-file FILE` (preview first) |
| `/snap me` | Authenticated user: `snapchat-ads --human me` |

## How to Execute

1. Parse `$ARGUMENTS` to determine intent.
2. If no token exists yet (`auth status` returns `NO_TOKEN`), instruct the user to run `/snap auth login` first.
3. Run via:
   ```bash
   uv run --directory cli snapchat-ads --account default --human <group> <command> [...]
   ```
4. **All write operations are two-phase.** Default returns a preview. Show the preview to the user, get explicit approval, then re-run with `--execute`.
5. Money values: pass dollars (`--daily-budget 50`) or `micro:50000000`. Never raw cents.
6. Summarize findings conversationally after showing the data.

## CLI Reference

```
uv run --directory cli snapchat-ads [--account NAME] [--human] <group> <command>

Groups: auth, account, org, campaign, adsquad, ad, manage, creative, media, segment,
        pixel, targeting, report, catalog, creative-element, interaction-zone,
        mobileapp, capi, audit, estimate, billing, study, me, init
```

## Account Keys

- `default` -- the account key written by `snapchat-ads init` (see `~/.config/snapchat-ads-cli/accounts.toml`)

## Safety

- Never call `--execute` without showing the preview first.
- `manage bulk-archive` is the soft-delete pattern -- Snap has no native multi-delete.
- Audit log: `~/.config/snapchat-ads-cli/audit.jsonl`.
