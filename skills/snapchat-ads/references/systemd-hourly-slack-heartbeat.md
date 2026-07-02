# Systemd hourly Slack heartbeat for Snapchat Ads

Use this when the user asks for a Snapchat Ads hourly report that should behave like the Meta Ads *systemd* cron/timer jobs.

## Key lesson

Some cron/agent job runners can produce a `Cronjob Response: ...` wrapper in Slack. That is wrong when the user wants the exact Meta systemd-style output: one clean main channel message and one thread reply, with no raw JSON and no cron management footer.

For this class of report, use a systemd user timer/service that posts directly to Slack via `chat.postMessage`, just like the Meta systemd jobs wrapped by `slack-fail-notify.sh`.

## Discovery steps

1. Inspect the existing Meta systemd jobs instead of guessing:
   - `systemctl --user list-timers --all | grep -i meta`
   - `systemctl --user list-units --all | grep -i meta`
   - Read relevant files under `~/.config/systemd/user/*.service` and `*.timer`.
2. Mirror the shape:
   - `Type=oneshot`
   - `EnvironmentFile=%h/.config/snapchat-ads-cli/.env`
   - `WorkingDirectory` set explicitly
   - `ExecStart=%h/.config/systemd/user/scripts/slack-fail-notify.sh <job-name> <channel-id> -- <real command>`
   - timer with `OnCalendar=... America/Los_Angeles`, `Persistent=true`, `AccuracySec=30s`
3. The real command should post to Slack itself:
   - First `chat.postMessage` to the target channel for the headline.
   - Capture returned `ts`.
   - Second `chat.postMessage` with `thread_ts=<main ts>` for the full details.

## Delivery requirements

- No cron scheduler `deliver=` for the user-facing post.
- No JSON final response sent to Slack.
- No `Cronjob Response` wrapper.
- No management footer like "To stop or manage this job...".
- Main message must be concise and Slack mrkdwn.
- Full details go only in the thread.

## Example files from the Snapchat implementation

- Collector: `reports/snapchat_hourly_ads_report.py`
- Slack poster: `~/.config/systemd/user/scripts/post-snapchat-hourly-heartbeat.py`
- Service: `~/.config/systemd/user/snapchat-ads-hourly.service`
- Timer: `~/.config/systemd/user/snapchat-ads-hourly.timer`

## Validation

After writing units:

```bash
systemctl --user daemon-reload
systemctl --user enable --now snapchat-ads-hourly.timer
systemctl --user start snapchat-ads-hourly.service
systemctl --user status snapchat-ads-hourly.service --no-pager -l
systemctl --user list-timers --all | grep snapchat-ads-hourly
```

Success looks like the service log printing a Slack `thread_ts` and exiting with `status=0/SUCCESS`.
