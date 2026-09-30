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
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
BRAND = os.environ.get("BRAND_NAME", "My Brand")
COLLECTOR = Path(os.environ.get("SNAPCHAT_HOURLY_COLLECTOR", str(REPO_ROOT / "reports" / "snapchat_hourly_ads_report.py")))
ENV_FILE = Path(os.environ.get("SNAPCHAT_ADS_ENV_FILE", str(Path.home() / ".config" / "snapchat-ads-cli" / ".env")))
PT = ZoneInfo("America/Los_Angeles")
ADS_MANAGER_URL = "https://ads.snapchat.com/"
SNAP_ACCOUNTS_FILE = Path.home() / ".config" / "snapchat-ads-cli" / "accounts.toml"

SNAP_SCRIPTS_DIR = REPO_ROOT / "skills" / "snapchat-ads" / "scripts"
if str(SNAP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SNAP_SCRIPTS_DIR))
from secret_scrubber import scrub_sensitive_value, scrub_then_truncate  # noqa: E402

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


def _mrkdwn_escape(value: object) -> str:
    text = str(value or "")
    for old, new in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(old, new)
    return text


def _account_id_from_toml(path: Path, *, preferred_account: str) -> str:
    """Read only the selected non-secret account ID from the CLI account file."""
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    accounts = payload.get("accounts") or {}
    if not isinstance(accounts, dict):
        return ""
    selected = accounts.get(preferred_account)
    if not isinstance(selected, dict) and len(accounts) == 1:
        selected = next(iter(accounts.values()))
    if not isinstance(selected, dict):
        return ""
    return str(selected.get("ad_account_id") or "").strip()


def _configured_snap_account_id(data: dict) -> str:
    account = data.get("account") or {}
    account_id_candidates = (
        data.get("snap_ad_account_id"),
        data.get("ad_account_id"),
        account.get("ad_account_id") if isinstance(account, dict) else None,
    )
    for value in account_id_candidates:
        account_id = str(value or "").strip()
        if account_id:
            return account_id
    env_account_id = str(os.environ.get("SNAPCHAT_AD_ACCOUNT_ID") or "").strip()
    if env_account_id:
        return env_account_id
    path = Path(
        os.environ.get("SNAPCHAT_ACCOUNTS_FILE") or SNAP_ACCOUNTS_FILE
    ).expanduser()
    preferred = str(os.environ.get("SNAPCHAT_ADS_ACCOUNT") or "default").strip()
    return _account_id_from_toml(path, preferred_account=preferred)


def _snap_ad_url(account_id: str, ad_id: str) -> str:
    if not account_id or not ad_id:
        return ""
    return (
        f"{ADS_MANAGER_URL}{quote(account_id, safe='')}/ads/"
        f"{quote(ad_id, safe='')}"
    )


def _ad_link(ad: dict, *, account_id: str) -> str:
    ad_id = str(ad.get("id") or "").strip()
    name = _short(str(ad.get("name") or "Ad").strip(), 76)
    label = _mrkdwn_escape(f"{name} ({ad_id})" if ad_id else name).replace("|", "/")
    url = _snap_ad_url(account_id, ad_id)
    return f"<{url}|{label}>" if url else label


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


# Load at import time: BRAND above and the argparse --channel default in main()
# both read os.environ, so the env file must be in place before either runs.
load_env_file(ENV_FILE)
BRAND = os.environ.get("BRAND_NAME", BRAND)


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


def redact_error_text(value: object) -> str:
    return scrub_then_truncate(value or "", 1500)


def parse_collector_json(raw: str) -> dict:
    idx = raw.find("{")
    if idx < 0:
        raise RuntimeError(f"collector returned no JSON: {redact_error_text(raw)}")
    payload = json.loads(raw[idx:])
    cleaned = scrub_sensitive_value(payload)
    if not isinstance(cleaned, dict):
        raise RuntimeError("collector returned a non-object JSON payload")
    return cleaned


def collect() -> dict:
    proc = subprocess.run([sys.executable, str(COLLECTOR)], text=True, capture_output=True, timeout=300)
    raw = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        raise RuntimeError(f"collector exit={proc.returncode}: {redact_error_text(raw)}")
    data = parse_collector_json(raw)
    if not data.get("ok"):
        raise RuntimeError(f"collector reported error: {redact_error_text(json.dumps(data))}")
    return data


def _report_time(value: str, zone: ZoneInfo = PT) -> datetime:
    if not value:
        raise ValueError("missing report timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def hour_label(latest: dict) -> str:
    end_pt = latest.get("end_pt_iso") or latest.get("end_pt") or ""
    try:
        return _report_time(end_pt, PT).strftime("%-I:%M %p PT")
    except Exception:
        return datetime.now(PT).strftime("%-I:%M %p PT")


def range_label(start: str, end: str, zone: ZoneInfo = PT) -> str:
    try:
        s = _report_time(start, zone)
        e = _report_time(end, zone)
        if s.date() != e.date():
            return f"{s.strftime('%a %-I:%M %p %Z')}–{e.strftime('%a %-I:%M %p %Z')}"
        if (s.strftime("%p"), s.tzname()) == (e.strftime("%p"), e.tzname()):
            return f"{s.strftime('%-I:%M')}–{e.strftime('%-I:%M %p %Z')}"
        return f"{s.strftime('%-I:%M %p %Z')}–{e.strftime('%-I:%M %p %Z')}"
    except Exception:
        return f"{start}–{end}"


def completed_window_label(data: dict) -> str:
    windows = data.get("report_windows") or {}
    hours = int(windows.get("elapsed_hours") or 24)
    boundary = windows.get("current_hour_pt") or ""
    try:
        end = _report_time(boundary, PT).strftime("%-I:%M %p PT")
        return f"{hours} completed hours ending {end}"
    except Exception:
        return f"{hours} completed hours"


def _account_zone(data: dict) -> tuple[str, ZoneInfo]:
    name = str((data.get("report_windows") or {}).get("account_timezone") or "America/Los_Angeles")
    try:
        return name, ZoneInfo(name)
    except Exception:
        return "America/Los_Angeles", PT


def _row_range(row: dict, data: dict) -> str:
    pt_range = range_label(
        row.get("start_pt_iso") or row.get("start_pt") or "",
        row.get("end_pt_iso") or row.get("end_pt") or "",
        PT,
    )
    account_name, account_zone = _account_zone(data)
    if account_name == "America/Los_Angeles":
        return pt_range
    account_range = range_label(
        row.get("start_account_iso") or "",
        row.get("end_account_iso") or "",
        account_zone,
    )
    return f"{pt_range} / {account_range} ({account_name})"


def build_messages(data: dict) -> tuple[str, str]:
    now = datetime.now(PT)
    latest = data.get("latest_hour") or {}
    previous = data.get("previous_hour") or {}
    today = data.get("today_account") or data.get("today_et") or {}
    last24 = data.get("last_24") or {}
    top_ads = data.get("top_ads_24h") or []
    invalid = data.get("invalid_active_ads") or []
    invalid_total = int(data.get("invalid_active_ads_total") or len(invalid))
    series = data.get("hourly_series") or []
    window_label = completed_window_label(data)
    snap_account_id = _configured_snap_account_id(data)

    date_label = now.strftime("%A %-m/%-d")
    main = (
        f":ghost: *{date_label} {hour_label(latest)} -- Snapchat heartbeat: "
        f"{roas(last24.get('roas'))} ROAS and {cpa(last24.get('cpa'))} CPA across {window_label}, "
        f"with {int(last24.get('purchases') or 0)} purchases; latest hour {money(latest.get('spend'))}, "
        f"Δ {pct(data.get('delta_vs_previous_hour_pct'))} vs previous hour.* "
        f":thread: See full details in thread below :point_down:"
    )

    top_lines = []
    for ad in top_ads[:6]:
        top_lines.append(
            f"• *{_ad_link(ad, account_id=snap_account_id)}*: "
            f"{money(ad.get('spend'))} spend | "
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
            f"• Spend is concentrated in the top two ads: "
            f"*{_ad_link(a, account_id=snap_account_id)}* at {money(a.get('spend'))} and "
            f"*{_ad_link(b, account_id=snap_account_id)}* at {money(b.get('spend'))}."
        )
    if not signal_lines:
        signal_lines.append("• No meaningful movement detected beyond normal hourly spend pacing.")

    if invalid_total:
        delivery_lines = [
            f"• {invalid_total} status-ACTIVE ads currently have INVALID* delivery status.",
        ]
        if invalid:
            delivery_lines.append(f"• Sample shown below: {len(invalid)} of {invalid_total}.")
        for item in invalid[:8]:
            statuses = ", ".join(item.get("delivery_status") or [])
            delivery_lines.append(
                f"• {_ad_link(item, account_id=snap_account_id)} - "
                f"{_mrkdwn_escape(statuses)}"
            )
    else:
        delivery_lines = ["• Active delivery looks clean; no invalid active ads detected."]

    hour_lines = []
    for row in series[-12:]:
        hour_lines.append(
            f"• {_row_range(row, data)}: {money(row.get('spend'))}"
        )

    thread = "\n".join([
        f"*Snapchat Ads Heartbeat -- {window_label}*",
        "",
        "*Rolling 24 Completed-Hour Account Summary*",
        f"• *Spend:* {money(last24.get('spend'))}",
        f"• *Revenue:* {money(last24.get('revenue'))}",
        f"• *ROAS:* {roas(last24.get('roas'))}",
        f"• *CPA:* {cpa(last24.get('cpa'))}",
        f"• *Purchases:* {int(last24.get('purchases') or 0)}",
        "",
        f"*Today so far ({_account_zone(data)[0]})*",
        f"• *Spend:* {money(today.get('spend'))}",
        f"• *Revenue:* {money(today.get('revenue'))}",
        f"• *ROAS:* {roas(today.get('roas'))}",
        f"• *CPA:* {cpa(today.get('cpa'))}",
        f"• *Purchases:* {int(today.get('purchases') or 0)}",
        f"• *Traffic:* {int(today.get('impressions') or 0):,} impressions | {int(today.get('swipes') or 0):,} swipes | {plain_pct(today.get('ctr_pct'))} swipe rate",
        "",
        "*Since last heartbeat*",
        f"• Latest hour: {_row_range(latest, data)} - {money(latest.get('spend'))} spend",
        f"• Previous hour: {_row_range(previous, data)} - {money(previous.get('spend'))} spend",
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
        f"• Rolling metrics cover {window_label}; the partial current hour is excluded.",
        "• ROAS uses Snap conversion_purchases_value divided by spend for the same report window.",
        "• Snap account-level hourly reporting is spend-only; an exact matching ad-level breakdown supplies revenue, purchases, CPA, impressions, and swipes.",
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
    today = data.get("today_account") or data.get("today_et") or {}
    last24 = data.get("last_24") or {}
    top_ads = data.get("top_ads_24h") or []
    invalid = data.get("invalid_active_ads") or []
    invalid_total = int(data.get("invalid_active_ads_total") or len(invalid))
    window_label = completed_window_label(data)
    snap_account_id = _configured_snap_account_id(data)

    date_label = now.strftime("%A %-m/%-d")
    main = (
        Message(main_text or "Snapchat heartbeat")
        .header(f"👻 {BRAND} Snapchat Heartbeat - {date_label} {hour_label(latest)}")
        .section(
            f"*{roas(last24.get('roas'))} rolling 24h ROAS* · *{cpa(last24.get('cpa'))} rolling 24h CPA* · "
            f"*{int(last24.get('purchases') or 0)} purchases* · latest hour *{money(latest.get('spend'))}* "
            f"({pct(data.get('delta_vs_previous_hour_pct'))} vs previous hour)"
        )
        .fields([
            ("24h Spend", money(last24.get("spend"))),
            ("24h Revenue", money(last24.get("revenue"))),
            ("24h ROAS", roas(last24.get("roas"))),
            ("24h CPA", cpa(last24.get("cpa"))),
            ("24h Purchases", str(int(last24.get("purchases") or 0))),
        ])
        .context(
            f"{BRAND} · Snapchat Ads · {window_label} · details in thread"
        )
    )

    thread = (
        Message("Snapchat heartbeat details")
        .header("👻 Snapchat - Rolling 24 Completed Hours")
        .context(
            f"Generated {now.strftime('%-I:%M %p PT')} · {window_label} · "
            f"account timezone {_account_zone(data)[0]}"
        )
        .divider()
        .h2("Rolling 24 Completed-Hour Summary")
        .fields([
            ("Spend", money(last24.get("spend"))),
            ("Revenue", money(last24.get("revenue"))),
            ("ROAS", roas(last24.get("roas"))),
            ("CPA", cpa(last24.get("cpa"))),
            ("Purchases", str(int(last24.get("purchases") or 0))),
        ])
        .divider()
        .h2(f"Today so far ({_account_zone(data)[0]})")
        .fields([
            ("Spend", money(today.get("spend"))),
            ("Revenue", money(today.get("revenue"))),
            ("ROAS", roas(today.get("roas"))),
            ("CPA", cpa(today.get("cpa"))),
            ("Purchases", str(int(today.get("purchases") or 0))),
        ])
    )
    rows = []
    for i, ad in enumerate(top_ads[:5], 1):
        rows.append(
            f"{i}. *{_ad_link(ad, account_id=snap_account_id)}* - "
            f"{money(ad.get('spend'))} · {roas(ad.get('roas'))} ROAS · {int(ad.get('purchases') or 0)} purch"
        )
    if rows:
        thread.divider().h2("Top Ads by Spend").section("\n".join(rows))
    if invalid_total:
        thread.context(
            f"⚠️ {invalid_total} status-ACTIVE ad(s) have INVALID* delivery; "
            f"collector retained a {len(invalid)}-row sample."
        )
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
