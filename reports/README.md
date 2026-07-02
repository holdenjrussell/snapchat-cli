# Snapchat hourly heartbeat

An hourly Slack heartbeat for Snapchat Ads: a compact main message with
rolling-24h ROAS, CPA, spend, revenue, and purchase counts, plus a threaded
reply with account summary detail, top ads by spend, and invalid-ad delivery
warnings.

## Pipeline

- **Collector** — `snapchat_hourly_ads_report.py` runs the `snapchat-ads` CLI
  (via `uv run --directory <repo>/cli`) to pull account health, the trailing
  24h hourly spend series, ad-level breakdowns for the last 24h and
  today-so-far, and the active ad list. It prints a single JSON object to
  stdout. Set `SNAPCHAT_ADS_ACCOUNT` to select a configured account (defaults
  to `default`, matching the CLI's own default).
- **Poster** — `post_snapchat_hourly_heartbeat.py` runs the collector,
  formats the results into a Slack main message + thread using the
  `slack_format` Block Kit helper library at `<repo>/skills/slack-format`
  (falls back to plain-text `chat.postMessage` calls if that library can't be
  imported), and posts both via `chat.postMessage`.

## Environment variables

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `SNAPCHAT_HOURLY_SLACK_CHANNEL` | **Yes** | none | Destination Slack channel ID. The poster exits with code 2 and a clear error if this is unset and `--channel` isn't passed. |
| `SLACK_BOT_TOKEN` | Yes | none | Bot token used for `chat.postMessage`. |
| `SNAPCHAT_ADS_ACCOUNT` | No | `default` | Account key passed to the `snapchat-ads` CLI's `--account` flag. |
| `BRAND_NAME` | No | `My Brand` | Label used in the Slack heartbeat header/context lines. |
| `SNAPCHAT_ADS_ENV_FILE` | No | `~/.config/snapchat-ads-cli/.env` | Optional `KEY=VALUE` file loaded before posting (e.g. to supply `SLACK_BOT_TOKEN`). Lines starting with `#` are ignored; existing env vars are not overwritten. |
| `SNAPCHAT_HOURLY_COLLECTOR` | No | `<repo>/reports/snapchat_hourly_ads_report.py` | Path to the collector script the poster shells out to. |
| `SLACK_FORMAT_DIR` | No | `<repo>/skills/slack-format` | Path to the `slack_format` Block Kit library. |

## Install

1. Install the `snapchat-ads` CLI dependencies (see `<repo>/cli/README.md`)
   and configure at least one account in
   `~/.config/snapchat-ads-cli/accounts.toml`.
2. Create `~/.config/snapchat-ads-cli/.env` with your Slack bot token and any
   other secrets:

   ```
   SLACK_BOT_TOKEN=xoxb-...
   ```

3. Copy the systemd unit files into your user unit directory and fill in the
   placeholders:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp reports/systemd/snapchat-ads-hourly.service reports/systemd/snapchat-ads-hourly.timer \
     ~/.config/systemd/user/
   sed -i "s|__REPO__|$PWD|" ~/.config/systemd/user/snapchat-ads-hourly.service
   sed -i "s|__SLACK_CHANNEL__|C0123456789|" ~/.config/systemd/user/snapchat-ads-hourly.service
   sed -i "s|__BRAND__|My Brand|" ~/.config/systemd/user/snapchat-ads-hourly.service
   ```

4. Enable lingering so the timer runs even when you're logged out, then
   enable and start the timer:

   ```bash
   loginctl enable-linger "$USER"
   systemctl --user daemon-reload
   systemctl --user enable --now snapchat-ads-hourly.timer
   ```

5. Check status and logs:

   ```bash
   systemctl --user status snapchat-ads-hourly.timer
   journalctl --user -u snapchat-ads-hourly.service -f
   ```

### Plain-crontab alternative

If you'd rather not use systemd, a user crontab entry works too. Point
`EnvironmentFile`-style variables at a wrapper script or export them inline:

```cron
5 * * * * SNAPCHAT_HOURLY_SLACK_CHANNEL=C0123456789 BRAND_NAME="My Brand" \
  SNAPCHAT_ADS_ENV_FILE=$HOME/.config/snapchat-ads-cli/.env \
  /usr/bin/python3 /path/to/repo/reports/post_snapchat_hourly_heartbeat.py \
  >> $HOME/.local/state/snapchat-heartbeat/cron.log 2>&1
```

Adjust the `5 * * * *` schedule and timezone to match your crontab's
timezone handling (the systemd timer above pins `America/Los_Angeles`
explicitly; cron uses the system timezone).

### macOS (launchd) note

On macOS, systemd user timers aren't available; use a `launchd` `.plist`
under `~/Library/LaunchAgents/` instead, with `StartCalendarInterval` set to
`{"Minute": 5}` for an hourly trigger, `ProgramArguments` pointing at
`/usr/bin/python3 <repo>/reports/post_snapchat_hourly_heartbeat.py`, and the
same environment variables set via the plist's `EnvironmentVariables` key
(launchd has no `EnvironmentFile=` equivalent, so secrets need to be loaded
by the script itself via `SNAPCHAT_ADS_ENV_FILE` rather than injected by the
launcher). Load it with `launchctl load ~/Library/LaunchAgents/<name>.plist`.

## Optimizer context shims

`snap_optimizer_daily_context.py` and `snap_meta_winners_weekly_context.py`
are thin shims that import `snap_optimizer_support` from
`<repo>/skills/snapchat-ads/scripts` and invoke its `collect-daily` /
`collect-weekly` entry points. They exist so an external agent scheduler can
run daily/weekly optimizer context collection as plain Python scripts
without needing to know the skill's internal module layout. Full behavior of
the optimizer engine itself is documented alongside
`skills/snapchat-ads/SKILL.md`.
