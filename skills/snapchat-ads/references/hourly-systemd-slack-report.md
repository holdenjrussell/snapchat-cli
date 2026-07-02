# Snapchat hourly Slack heartbeat — systemd direct-post pattern

Use this when the user asks for a Snapchat hourly report that mimics the Meta hourly/systemd report format, or when they explicitly object to cron `Cronjob Response` wrappers/raw JSON.

## Core lesson

Some cron/agent job runners wrap threaded JSON delivery as:

`Cronjob Response: <job>` followed by raw `{"main_message":...,"thread_message":...}`.

That is unacceptable for Meta-style ad heartbeats. For this class of report, use a user-level systemd timer/service that runs a script which posts directly to Slack via `chat.postMessage`:

1. Post clean main message to the channel.
2. Capture returned `ts`.
3. Post the full report as a thread reply using `thread_ts=ts`.
4. Successful runs should be silent in journal except a compact `posted ... thread_ts=...` line.
5. Wrap with `slack-fail-notify.sh` so failures DM/alert without polluting the report channel.

## Files / shape

Recommended files:

- Collector: `reports/snapchat_hourly_ads_report.py`
- Poster: `~/.config/systemd/user/scripts/post-snapchat-hourly-heartbeat.py`
- Service: `~/.config/systemd/user/snapchat-ads-hourly.service`
- Timer: `~/.config/systemd/user/snapchat-ads-hourly.timer`

Example service:

```ini
[Unit]
Description=Snapchat Ads hourly Slack heartbeat (main message + details thread)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=%h/.config/snapchat-ads-cli/.env
Environment=SNAPCHAT_HOURLY_SLACK_CHANNEL=<SLACK_CHANNEL_ID>
WorkingDirectory=%h/snapchat-cli
ExecStart=%h/.config/systemd/user/scripts/slack-fail-notify.sh snapchat-ads-hourly <SLACK_CHANNEL_ID> -- /usr/bin/python3 %h/.config/systemd/user/scripts/post-snapchat-hourly-heartbeat.py
StandardOutput=journal
StandardError=journal
TimeoutStartSec=600
```

Example timer:

```ini
[Unit]
Description=Trigger Snapchat Ads hourly Slack heartbeat (:05 America/Los_Angeles)

[Timer]
OnCalendar=*-*-* *:05:00 America/Los_Angeles
Persistent=true
AccuracySec=30s

[Install]
WantedBy=timers.target
```

Enable/run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now snapchat-ads-hourly.timer
systemctl --user start snapchat-ads-hourly.service
systemctl --user status snapchat-ads-hourly.service --no-pager -l
```

## Snapchat metrics

For Meta-style Snapchat heartbeat reporting, include ROAS and CPA when requested:

- Pull `conversion_purchases_value` alongside `spend,impressions,swipes,conversion_purchases`.
- ROAS = `conversion_purchases_value / spend` for the same report window.
- CPA = `spend / conversion_purchases`.
- Snap money fields are micro-currency; divide by `1_000_000`.

Source query pattern:

```bash
snapchat-ads report stats --entity ad_account --granularity TOTAL \
  --start-time <ET aligned start> \
  --end-time <ET aligned current hour> \
  --fields spend,impressions,swipes,conversion_purchases,conversion_purchases_value \
  --breakdown ad --omit-empty
```

For hourly pacing, account-level hourly supports spend-only:

```bash
snapchat-ads report hourly --hours 24 --fields spend
```

## Output style

Copy the Meta heartbeat style exactly:

- Main channel message: one concise narrative headline, not JSON and not a data dump.
- Thread sections: `*Snapchat Ads Heartbeat -- Today so far*`, `*Account Summary*`, `*Since last heartbeat*`, `*Ad Readout*`, `*Top Ad Signals*`, `*Delivery Health*`, optional `*Last 12 Hours*`, `*Notes / Caveats*`.
- Prioritize ROAS and CPA over CTR/snap-specific vanity metrics when the user asks for Meta-like output.
- Use Slack mrkdwn: `*bold*`, bullets, no Markdown headers, no code fences, no raw IDs unless needed for debugging.

## Verification

- `systemctl --user list-timers --all | grep snapchat-ads-hourly`
- `systemctl --user start snapchat-ads-hourly.service`
- `systemctl --user status snapchat-ads-hourly.service --no-pager -l`
- Confirm Slack shows a normal main message plus threaded details, with no `Cronjob Response`, no raw JSON, and no "To stop or manage this job" footer.
