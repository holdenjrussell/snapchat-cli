#!/usr/bin/env python3
"""Post the Snapchat hourly heartbeat to Slack as a main message + thread.

This intentionally bypasses generic cron-runner delivery so Slack receives a
clean message shape: one channel message, one thread reply, no wrapper text.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAND = os.environ.get("BRAND_NAME", "My Brand")
COLLECTOR = Path(os.environ.get("SNAPCHAT_HOURLY_COLLECTOR", str(REPO_ROOT / "reports" / "snapchat_hourly_ads_report.py")))
ENV_FILE = Path(os.environ.get("SNAPCHAT_ADS_ENV_FILE", str(Path.home() / ".config" / "snapchat-ads-cli" / ".env")))
PT = ZoneInfo("America/Los_Angeles")

# Block Kit formatter (repo-local skill dir; degrades to plain text if absent).
SLACK_FORMAT_DIR = Path(os.environ.get("SLACK_FORMAT_DIR", str(REPO_ROOT / "skills" / "slack-format")))
if str(SLACK_FORMAT_DIR) not in sys.path:
    sys.path.insert(0, str(SLACK_FORMAT_DIR))
try:
    from slack_format import Message, post as slack_format_post  # type: ignore
except Exception:  # pragma: no cover - fall back to plain-text-only delivery
    Message = None  # type: ignore
    slack_format_post = None  # type: ignore


def _short(text: str, width: int = 58) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"


def pct(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):+.1f}%"
    except Exception:
        return "n/a"


def plain_pct(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}%"
    except Exception:
        return "n/a"


def roas(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.2f}x"
    except Exception:
        return "n/a"


def cpa(v) -> str:
    return "n/a" if v is None else money(v)


def parse_collector_json(raw: str) -> dict:
    idx = raw.find("{")
    if idx < 0:
        raise RuntimeError(f"collector returned no JSON: {raw[-500:]}")
    return json.loads(raw[idx:])


def collect() -> dict:
    proc = subprocess.run([sys.executable, str(COLLECTOR)], text=True, capture_output=True, timeout=300)
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(f"collector exit={proc.returncode}: {raw[-1500:]}")
    data = parse_collector_json(raw)
    if not data.get("ok"):
        raise RuntimeError(f"collector reported error: {json.dumps(data)[:1500]}")
    return data


def hour_label(latest: dict) -> str:
    end_pt = latest.get("end_pt") or ""
    try:
        dt = datetime.strptime(end_pt, "%Y-%m-%d %H:%M").replace(tzinfo=PT)
        return dt.strftime("%-I:00 %p PT")
    except Exception:
        return datetime.now(PT).strftime("%-I:%M %p PT")


def range_label(start: str, end: str) -> str:
    try:
        s = datetime.strptime(start, "%Y-%m-%d %H:%M")
        e = datetime.strptime(end, "%Y-%m-%d %H:%M")
        return f"{s.strftime('%-I:00')}–{e.strftime('%-I:00 %p')}"
    except Exception:
        return f"{start}–{end}"


def build_messages(data: dict) -> tuple[str, str]:
    now = datetime.now(PT)
    latest = data.get("latest_hour") or {}
    previous = data.get("previous_hour") or {}
    today = data.get("today_et") or {}
    last24 = data.get("last_24") or {}
    account = data.get("account") or {}
    top_ads = data.get("top_ads_24h") or []
    invalid = data.get("invalid_active_ads") or []
    series = data.get("hourly_series") or []

    date_label = now.strftime("%A %-m/%-d")
    main = (
        f":ghost: *{date_label} {hour_label(latest)} -- Snapchat heartbeat: "
        f"{roas(last24.get('roas'))} rolling 24h ROAS, {cpa(today.get('cpa'))} CPA, "
        f"{int(today.get('purchases') or 0)} purchases; latest hour {money(latest.get('spend'))}, "
        f"Δ {pct(data.get('delta_vs_previous_hour_pct'))} vs previous hour.* "
        f":thread: See full details in thread below :point_down:"
    )

    top_lines = []
    for ad in top_ads[:6]:
        top_lines.append(
            f"• *{ad.get('name') or ad.get('id')}*: {money(ad.get('spend'))} spend | "
            f"{money(ad.get('revenue'))} revenue | {roas(ad.get('roas'))} ROAS | "
            f"{int(ad.get('purchases') or 0)} purchases | {cpa(ad.get('cpa'))} CPA"
        )
    if not top_lines:
        top_lines.append("• No ad-level spend in the current 24h window.")

    signal_lines = []
    if data.get("delta_vs_previous_hour_pct") is not None:
        signal_lines.append(
            f"• Latest-hour spend is {pct(data.get('delta_vs_previous_hour_pct'))} vs the prior hour "
            f"({money(latest.get('spend'))} vs {money(previous.get('spend'))})."
        )
    if today.get("purchases") is not None:
        signal_lines.append(
            f"• Today is at {roas(today.get('roas'))} ROAS on {money(today.get('spend'))} spend and {money(today.get('revenue'))} revenue, "
            f"with {int(today.get('purchases') or 0)} purchases at {cpa(today.get('cpa'))} CPA. Rolling 24h is {roas(last24.get('roas'))} ROAS at {cpa(last24.get('cpa'))} CPA."
        )
    if len(top_ads) >= 2:
        a, b = top_ads[0], top_ads[1]
        signal_lines.append(
            f"• Spend is concentrated in the top two ads: *{a.get('name')}* at {money(a.get('spend'))} and *{b.get('name')}* at {money(b.get('spend'))}."
        )
    if not signal_lines:
        signal_lines.append("• No meaningful movement detected beyond normal hourly spend pacing.")

    if invalid:
        delivery_lines = [f"• {len(invalid)} active ads are currently flagged invalid:"]
        for item in invalid[:8]:
            statuses = ", ".join(item.get("delivery_status") or [])
            delivery_lines.append(f"• {item.get('name') or item.get('id')} — {statuses}")
    else:
        delivery_lines = ["• Active delivery looks clean; no invalid active ads detected."]

    hour_lines = []
    for row in series[-12:]:
        hour_lines.append(
            f"• {range_label(row.get('start_pt',''), row.get('end_pt',''))} PT: {money(row.get('spend'))}"
        )

    thread = "\n".join([
        "*Snapchat Ads Heartbeat -- Today so far*",
        "",
        "*Account Summary*",
        f"• *Spend:* {money(today.get('spend'))}",
        f"• *Revenue:* {money(today.get('revenue'))}",
        f"• *ROAS:* {roas(today.get('roas'))}",
        f"• *CPA:* {cpa(today.get('cpa'))}",
        f"• *Purchases:* {int(today.get('purchases') or 0)}",
        f"• *Traffic:* {int(today.get('impressions') or 0):,} impressions | {int(today.get('swipes') or 0):,} swipes | {plain_pct(today.get('ctr_pct'))} swipe rate",
        f"• *Rolling 24h:* {money(last24.get('spend'))} spend | {money(last24.get('revenue'))} revenue | {roas(last24.get('roas'))} ROAS | {cpa(last24.get('cpa'))} CPA | {int(last24.get('purchases') or 0)} purchases",
        "",
        "*Since last heartbeat*",
        f"• Latest hour: {range_label(latest.get('start_pt',''), latest.get('end_pt',''))} PT / {range_label(latest.get('start_et',''), latest.get('end_et',''))} ET — {money(latest.get('spend'))} spend",
        f"• Previous hour: {range_label(previous.get('start_pt',''), previous.get('end_pt',''))} PT / {range_label(previous.get('start_et',''), previous.get('end_et',''))} ET — {money(previous.get('spend'))} spend",
        f"• Delta vs previous hour: {pct(data.get('delta_vs_previous_hour_pct'))}",
        f"• Delta vs prior 23h average: {pct(data.get('delta_vs_prior_23h_avg_pct'))} vs {money(data.get('avg_prior_hour_spend'))}/hr average",
        "",
        "*Ad Readout*",
        *top_lines,
        "",
        "*Top Ad Signals*",
        *signal_lines[:4],
        "",
        "*Delivery Health*",
        *delivery_lines,
        "",
        "*Last 12 Hours*",
        *(hour_lines or ["• No hourly series returned."]),
        "",
        "*Notes / Caveats*",
        "• ROAS uses Snap conversion_purchases_value divided by spend for the same report window.",
        "• Snap account-level hourly reporting is spend-only; ad-level breakdown supplies revenue, purchases, CPA, impressions, and swipes for exact 24h and today windows.",
    ])
    return main, thread


def build_blocks(data: dict, main_text: str | None = None):
    """Clean Block Kit: tight headline + concise detail thread. No raw firehose.

    Returns (main_message_obj, thread_message_obj) or (None, None) when the
    slack_format library is unavailable (caller falls back to plain text).
    """
    if Message is None:
        return None, None

    now = datetime.now(PT)
    latest = data.get("latest_hour") or {}
    previous = data.get("previous_hour") or {}
    today = data.get("today_et") or {}
    last24 = data.get("last_24") or {}
    top_ads = data.get("top_ads_24h") or []
    invalid = data.get("invalid_active_ads") or []

    date_label = now.strftime("%A %-m/%-d")
    main = (
        Message(main_text or "Snapchat heartbeat")
        .header(f":ghost: {BRAND} Snapchat Heartbeat — {date_label} {hour_label(latest)}")
        .section(
            f"*{roas(last24.get('roas'))} rolling 24h ROAS* · *{cpa(today.get('cpa'))} CPA* · "
            f"*{int(today.get('purchases') or 0)} purchases* · latest hour *{money(latest.get('spend'))}* "
            f"({pct(data.get('delta_vs_previous_hour_pct'))} vs previous hour)"
        )
        .fields([
            ("Spend", money(today.get("spend"))),
            ("Revenue", money(today.get("revenue"))),
            ("24h ROAS", roas(last24.get("roas"))),
            ("CPA", cpa(today.get("cpa"))),
            ("Purchases", str(int(today.get("purchases") or 0))),
        ])
        .context(
            f"{BRAND} · Snapchat Ads · hourly heartbeat · details in thread"
        )
    )

    thread = (
        Message("Snapchat heartbeat details")
        .header("👻 Snapchat — Today so far")
        .context(f"Generated {now.strftime('%-I:%M %p PT')} · {BRAND} · Snapchat Ads")
        .divider()
        .h2("Account Summary")
        .fields([
            ("Spend", money(today.get("spend"))),
            ("Revenue", money(today.get("revenue"))),
            ("ROAS", roas(today.get("roas"))),
            ("CPA", cpa(today.get("cpa"))),
            ("Purchases", str(int(today.get("purchases") or 0))),
            ("Rolling 24h", f"{money(last24.get('spend'))} · {roas(last24.get('roas'))} ROAS"),
        ])
    )
    rows = []
    for i, ad in enumerate(top_ads[:5], 1):
        rows.append(
            f"{i}. *{_short(ad.get('name') or ad.get('id') or 'Unnamed')}* — "
            f"{money(ad.get('spend'))} · {roas(ad.get('roas'))} ROAS · {int(ad.get('purchases') or 0)} purch"
        )
    if rows:
        thread.divider().h2("Top Ads by Spend").section("\n".join(rows))
    if invalid:
        thread.context(f":warning: {len(invalid)} active ad(s) flagged invalid — see Ads Manager.")
    else:
        thread.context("Delivery healthy · no invalid active ads.")
    return main, thread


def slack_post(channel: str, text: str, thread_ts: str | None = None) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is missing")
    payload = {"channel": channel, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack HTTP {exc.code}: {body}") from exc
    data = json.loads(body)
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data}")
    return data


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=os.environ.get("SNAPCHAT_HOURLY_SLACK_CHANNEL"))
    args = parser.parse_args()
    channel = args.channel
    if not channel:
        print(
            "Error: SNAPCHAT_HOURLY_SLACK_CHANNEL is not set and --channel was not "
            "provided. Refusing to post without an explicit Slack channel.",
            file=sys.stderr,
        )
        return 2

    load_env_file(ENV_FILE)
    data = collect()
    main_text, thread_text = build_messages(data)
    main_block, thread_block = build_blocks(data, main_text)

    if main_block is not None and slack_format_post is not None:
        res = slack_format_post(channel, message=main_block, strict=False)
        ts = res.get("ts")
        if not ts:
            raise RuntimeError(f"Slack response missing ts: {res}")
        slack_format_post(channel, message=thread_block, thread_ts=ts, strict=False)
    else:
        res = slack_post(channel, main_text)
        ts = res.get("ts")
        if not ts:
            raise RuntimeError(f"Slack response missing ts: {res}")
        slack_post(channel, thread_text, thread_ts=ts)
    print(f"posted Snapchat hourly heartbeat to {channel} thread_ts={ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
