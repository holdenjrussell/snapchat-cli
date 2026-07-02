---
name: slack-format
description: Dependency-free Slack Block Kit builder for report/agent scripts. One-import drop-in (`from slack_format import Message, post`) that emits valid, document-style Slack messages — headers, sections, bullets, dividers, fields, native tables, context, buttons, images, cards, carousels — with built-in validation against the limits/elements live-tested in production Slack workspaces. Use when composing rich Slack messages/reports.
version: 1.0.0
license: MIT
---

# slack-format — Block Kit formatter for report/agent scripts

A small, dependency-free Python library that turns report data into
Slack-native, document-style messages. It is meant to be imported directly by
**report/agent scripts** so they post rich blocks on purpose, rather than
relying on a separate gateway-level auto-formatter for chat replies.

Stdlib only (`urllib`, no `requests` needed).

## Why

The brief: "use all the features from the Slack API… tables, buttons, content
cards, H1/H2, paragraph, bullets, line separators… test every element…" This
library covers every element in that brief, each shape validated by live
`chat.postMessage` tests in a production Slack workspace (see
`references/live-test-matrix.md`), and refuses (via `validate()`) the elements
that workspace rejects.

## Quick start (one-import drop-in)

```python
from slack_format import Message, post

msg = (
    Message("Daily Shipping Report")        # fallback text (notifications / a11y)
    .header("📦 Daily Shipping — Tier 1")    # H1
    .context("My Brand · 2026-06-09")        # small footnote
    .divider()                               # line separator
    .h2("Headline")                          # H2
    .section("*4 SKUs* below the free-ship threshold.")  # paragraph (mrkdwn)
    .bullets(["B07ABC — 2 days", "B09XYZ — restock filed"])  # rich_text_list
    .fields([("ASINs", "142"), ("OOS", "3 🔴")])  # 2-col key/value grid
    .table(["SKU", "Days", "Status"],            # real multi-col native table
           [["B07ABC", "2", "⚠️ low"]])
    .buttons([("Open dashboard", "https://example.com")])    # URL buttons
)
post("<SLACK_CHANNEL_ID>", msg)   # -> chat.postMessage with blocks + text fallback
```

## Helper API

All builders are module-level functions **and** chainable `Message` methods.

| Helper | Element | Notes |
| --- | --- | --- |
| `header(text)` | `header` (H1) | plain_text, ~150 char cap |
| `h2(text)` | H2 via `markdown` | `header` blocks are H1-only |
| `markdown(md)` | `markdown` block | full Markdown: H1–H3, bold/italic, lists, tables, code, quote, hr |
| `section(text, accessory=)` | `section` (mrkdwn) | paragraph; `paragraph` is an alias |
| `fields(pairs)` | `section.fields` | 2-column key/value grid; max 10 fields |
| `bullets(items)` | `rich_text_list` (bullet) | bulleted list element |
| `numbered(items)` | `rich_text_list` (ordered) | |
| `divider()` | `divider` | line separator |
| `table(headers, rows)` | native `table` | `raw_text` cells + `column_settings`; 100 rows / 20 cols |
| `md_table(headers, rows)` | Markdown table | reliable fallback for native table |
| `mono_table(headers, rows)` | aligned monospace | code-block table, universal render |
| `context(*text, images=)` | `context` | small footnote text + optional small images |
| `buttons(btns)` | `actions` (URL buttons) | `(text,url)` or dict; max 5; URL buttons are passive |
| `image(url, alt, title=)` | `image` | URL must be Slack-fetchable |
| `card(title, body, button=, image_url=)` | section + accessory | the portable "content card" |
| `carousel(cards)` | native `carousel` of `card` | swipeable deck; cards support `title`, `body`, `subtitle`, `subtext`, `image_url`/`hero_image_url`, and URL `button`; see limit below |
| `card_fallback(cards)` | multi-block | header+section per card — carousel substitute |

Builder + post plumbing:

- `Message(fallback)` — chainable builder; `.build(strict=True)` returns
  `{text, blocks}` (raises on validation errors); `.post(channel, thread_ts=)`.
- `post(channel, message=|text=|blocks=, thread_ts=, token=, strict=True)` —
  `chat.postMessage`. Returns the parsed Slack response; raises
  `SlackFormatError` on transport or API error (`ok != true`).
- `get_token(env_path=)` / `load_env(path)` — `SLACK_BOT_TOKEN` from env, else
  `~/.config/snapchat-ads-cli/.env`.
- `validate(blocks)` — returns human-readable warnings for limit/element
  violations *before* Slack rejects them. Limits exported as constants:
  `BLOCK_LIMIT=50`, `SECTION_TEXT_MAX=3000`, `FIELDS_MAX=10`,
  `MARKDOWN_CUMULATIVE_MAX=12000`, `TABLE_MAX_ROWS=100`, `TABLE_MAX_COLS=20`.

## How a report adopts it

Reports today build a string and call `chat.postMessage` with `{text}`. To
switch to rich blocks, replace the inline request with `slack_format.post()`
and keep the old string as the fallback:

```python
# before
requests.post(SLACK_API, headers=H, json={"channel": ch, "text": report_text})

# after
from slack_format import post, Message
post(ch, message=build_message(report_text))   # blocks + text fallback
```

For **threaded systemd reports** that post through
`post_threaded_slack_json.py`, the payload should include both plain fallbacks
and block payloads:

```python
main_msg = Message("fallback headline").header("📈 Report").section("*Summary*")
thread_msg = Message("fallback details").header("Details").table(["Metric", "Value"], rows)
payload = {
    "main_message": main_text,
    "thread_message": thread_text,
    "main_blocks": main_msg.build(strict=False)["blocks"],
    "thread_blocks": thread_msg.build(strict=False)["blocks"],
}
```

The threaded poster must pass `blocks` through to `chat.postMessage` for both
the root message and the thread reply; otherwise the report silently falls back
to plain text even though the generator built Block Kit. Verify with
`post_threaded_slack_json.py --dry-run` and confirm nonzero `main_blocks` and
`thread_blocks` counts.

`reference_shipping_report.py` is a worked example: it reads a **live** Daily
Shipping Tier-1 `output.txt` and re-renders it as a Block Kit document.

## Block Kit limits hit (document for reviewers)

- **No native carousel for arbitrary blocks.** Slack's `carousel` only nests
  `card` elements — you cannot put a table or image-grid into a swipe deck. Use
  `carousel()` for card decks; use `card_fallback()` (header+section per card,
  divider-separated) where carousel isn't desired/available.
- **Carousel card chart images**: `carousel()` supports card `image_url`/`hero_image_url`, which becomes `card.hero_image`. For charts inside cards, use a direct unauthenticated HTTPS URL returning image bytes (`image/png`/`image/jpeg`). `slack_file` and Slack private file/thumb URLs can API-pass but render as a warning icon in card heroes. For generated report charts, upload the PNG to a public/direct image endpoint (one heartbeat report implementation uses tmpfiles `/dl/...png` with a curl-like User-Agent, with QuickChart short URL as fallback) before building the carousel.
- **Image URLs must be Slack-fetchable.** Slack server-side downloads image
  URLs; un-fetchable/hotlink-blocked URLs (e.g. some CDN links) return
  `invalid_blocks: downloading image failed`. Use a stable public URL or upload
  the file and reference it in an official image block.
- **Native `table`**: use `raw_text` cells (this workspace rejects `raw_number`
  in `table`); `data_table` accepts `raw_number`. 100 rows / 20 cols max.
- **Rejected in messages** (use modals): multi-selects, `file`, `file_input`,
  `rich_text_input`, `workflow_button`. `validate()` flags these.
- 50 blocks / message; section text ≤3000; fields ≤10; markdown blocks ≤12000
  cumulative chars.

Full pass/fail matrix: `references/live-test-matrix.md` (re-verified for this
library on 2026-06-09).

Threaded systemd report pattern: `references/threaded-systemd-blocks.md`
documents the `main_blocks` / `thread_blocks` JSON contract and dry-run checks
for reports that post a root Slack message plus a thread reply through
`post_threaded_slack_json.py`.

Hourly paid-social adoption pattern: `references/hourly-paid-social-adoption.md`
documents the full surface area for hourly paid-social reports — direct
systemd scripts, threaded systemd posters, and cron-based optimizer prompts —
plus the verification checklist. Use it when the user asks for all hourly
paid-social reports to use Block Kit/rich Slack formatting.

## Testing

```bash
cd skills/slack-format
python3 -m py_compile slack_format.py demo_showcase.py reference_shipping_report.py
python3 selfcheck.py                      # offline: builds + validates every element
python3 demo_showcase.py --dry-run         # print payload, no post
python3 demo_showcase.py                    # post element showcase -> #sandbox-test
python3 reference_shipping_report.py        # post real shipping report -> #sandbox-test
```

**Sandbox-first guardrail:** demos default to `#sandbox-test`
(`<SANDBOX_SLACK_CHANNEL_ID>`), never a prod channel. Graduate a report to its
prod channel only after a reviewer approves the rendered output.

What was posted for review on 2026-06-09:
- Element showcase (every Block Kit element in one message).
- Reference Daily Shipping Tier-1 report, richly re-rendered from live data.

## Adoption notes

For any report, prefer adding `main_blocks` and `thread_blocks` to the
threaded JSON payload so the old fallback text remains available for
notifications/accessibility. Keep production report threads concise: do
**not** dump raw snapshots, local file paths, Ads Manager URL lists, or
wrapper/debug labels into Slack. Save raw output under the report state
directory and render only the decision-ready summary/tables in the thread.

## Portability

This library is a single dependency-free module (stdlib only). Copy
`skills/slack-format/slack_format.py` into any agent's skills directory and
`from slack_format import Message, post` — no install step, no external
packages, and no gateway-level patching required.

Run `python3 selfcheck.py` after any environment change to confirm every
builder still produces valid blocks.
