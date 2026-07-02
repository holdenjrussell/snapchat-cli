# Snapchat hourly Slack report cron pattern

Use this when the user asks for a Snapchat Ads hourly report that mimics the Meta hourly/current-day Slack report style: concise main message in the channel, full details in the thread.

## Durable pattern

1. Build a deterministic data-collection script under `reports/` and attach it to the cron job with `script=...` and `no_agent=false` so the script output is injected into the LLM prompt.
2. Keep the job read-only. Snapchat report crons must never mutate campaigns, ad squads, ads, budgets, or status.
3. Use the scheduler's threaded JSON delivery rather than manual Slack posting:
   - final response must be valid JSON only
   - exact keys: `main_message` and `thread_message`
   - `main_message` is the short channel headline
   - `thread_message` is the detailed report
4. Schedule hourly reports at a stable minute such as `5 * * * *` so they do not collide exactly with top-of-hour platform data refreshes.
5. Deliver directly to the requested Slack channel, e.g. `deliver=slack:<SLACK_CHANNEL_ID>`.
6. After creating, run once and verify job state via the cron manager's list action or wrapper: last status should be `ok` and `last_delivery_error` should be null.

## Snapchat API quirks observed

- The ad-account hourly endpoint only supports `spend` at account level. If the CLI call includes other account-level fields it may fail with: `Only field 'spend' should be used when querying AdAccount stats.`
- Top-ad breakdown reports can supply ad-level `impressions`, `swipes`, `spend`, and `conversion_purchases`; use those for the detailed thread when available.
- Snap account timezone may be different from the user's operational timezone. Reports should display the latest finalized hour in both PT and the account timezone when useful.
- Daily granularity may require start/end times aligned to the account timezone midnight. Prefer the hourly endpoint for hourly report crons.

## Suggested report shape

Main message:

`*:ghost: Snapchat Ads Hourly — {latest PT hour}* $X latest hour | $Y today | Δ Z% vs prior hour :thread: See full details in thread below :point_down:`

Thread sections:

- *Snapshot* — account name/status, generated time, latest finalized hour in PT + account timezone
- *Spend pulse* — latest hour, previous hour, delta vs previous, delta vs prior 23h average, today spend, 24h spend
- *Top ads, last 24h* — spend, purchases, CPA if available, CTR if available
- *Delivery health* — active ad count and invalid active delivery statuses
- *Last 12 hours* — compact hourly spend list
- *Notes / caveats* — mention account-level hourly is spend-only and conversion detail comes from ad breakdowns when available

## Minimal cron prompt contract

Include these constraints in the cron prompt:

- `Use the script output; do not mutate anything in Snapchat Ads. This job is read-only.`
- `Do NOT call send_message or Slack APIs yourself.`
- `Your FINAL RESPONSE must be valid JSON only, with exactly these string keys: {"main_message":"...","thread_message":"..."}`
- `If data collection fails, return JSON with a concise failure main_message and sanitized diagnostic details in thread_message.`
