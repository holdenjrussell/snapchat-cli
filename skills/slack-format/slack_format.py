"""slack_format — reusable Slack Block Kit formatter.

A one-import drop-in for report/agent scripts. Build Slack-native,
document-style messages with clean helpers covering every Block Kit element
this library supports, then post them with a single call.

    from slack_format import Message, post

    msg = (
        Message("Daily Shipping Report")          # fallback text (notifications/a11y)
        .header("📦 Daily Shipping Report")        # H1
        .context("My Brand · 2026-06-09 · nova")
        .divider()
        .h2("Headline")
        .section("*4 SKUs* below the free-ship threshold.")
        .bullets(["B07ABC — 2 days left", "B09XYZ — restock filed"])
        .table(["SKU", "Days", "Status"], [["B07ABC", "2", "⚠️ low"]])
        .buttons([("Open dashboard", "https://example.com")])
    )
    post("<SLACK_CHANNEL_ID>", msg)

Every block shape used here was validated by live `chat.postMessage` tests in
a production Slack workspace — see references/live-test-matrix.md. The helpers
stay inside the verified-supported set and avoid the known-rejected elements
(multi-selects, file blocks, workflow buttons, file/rich_text inputs in
messages).

This module lives in skills/slack-format/ and has no external dependencies —
see SKILL.md "Portability".
"""

from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "Message",
    "post",
    "get_token",
    "load_env",
    # block builders
    "header",
    "h2",
    "markdown",
    "section",
    "paragraph",
    "fields",
    "bullets",
    "numbered",
    "divider",
    "table",
    "md_table",
    "mono_table",
    "context",
    "buttons",
    "image",
    "card",
    "carousel",
    "card_fallback",
    # validation + limits
    "validate",
    "SlackFormatError",
    "BLOCK_LIMIT",
    "SECTION_TEXT_MAX",
    "FIELDS_MAX",
    "MARKDOWN_CUMULATIVE_MAX",
    "TABLE_MAX_ROWS",
    "TABLE_MAX_COLS",
]

SLACK_API = "https://slack.com/api/"

# --------------------------------------------------------------------------- #
# Limits — discovered/confirmed by live tests in a production workspace.
# --------------------------------------------------------------------------- #
BLOCK_LIMIT = 50               # max blocks per message
SECTION_TEXT_MAX = 3000        # max chars in a section text field
FIELDS_MAX = 10                # max fields in a section.fields
MARKDOWN_CUMULATIVE_MAX = 12000  # cumulative chars across markdown blocks
TABLE_MAX_ROWS = 100           # native table block max rows
TABLE_MAX_COLS = 20            # native table block max cells per row

# Elements that this workspace rejected in normal messages (use modals
# instead). validate() warns if it sees these in raw blocks.
_REJECTED_IN_MESSAGES = {
    "multi_static_select",
    "multi_users_select",
    "multi_channels_select",
    "multi_conversations_select",
    "multi_external_select",
    "file",
    "file_input",
    "rich_text_input",
    "workflow_button",
}


class SlackFormatError(RuntimeError):
    """Raised on a Slack API failure or an invalid message build."""


# --------------------------------------------------------------------------- #
# Token / env loading: SLACK_BOT_TOKEN from the environment, falling back to
# ~/.config/snapchat-ads-cli/.env.
# --------------------------------------------------------------------------- #
def _default_env_path() -> pathlib.Path:
    return pathlib.Path.home() / ".config" / "snapchat-ads-cli" / ".env"


def _strip_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def load_env(path: Optional[Union[str, pathlib.Path]] = None) -> Dict[str, str]:
    """Parse a ``KEY=value`` env file."""
    p = pathlib.Path(path) if path else _default_env_path()
    env: Dict[str, str] = {}
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):]
        key, _, val = s.partition("=")
        key = key.strip()
        if key:
            env[key] = _strip_value(val)
    return env


def get_token(env_path: Optional[Union[str, pathlib.Path]] = None) -> str:
    """Return SLACK_BOT_TOKEN from the environment or ~/.config/snapchat-ads-cli/.env."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if token:
        return token
    token = load_env(env_path).get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise SlackFormatError(
            "SLACK_BOT_TOKEN missing from environment and "
            f"{env_path or _default_env_path()}"
        )
    return token


# --------------------------------------------------------------------------- #
# Block builders. Each returns a single block dict (or, for bullets/numbered,
# a rich_text block). Shapes match what passed live in a production workspace.
# --------------------------------------------------------------------------- #
def _plain(text: str, emoji: bool = True) -> Dict[str, Any]:
    return {"type": "plain_text", "text": str(text), "emoji": emoji}


def _mrkdwn(text: str) -> Dict[str, Any]:
    return {"type": "mrkdwn", "text": str(text)}


def header(text: str) -> Dict[str, Any]:
    """H1 title. Slack ``header`` blocks are plain_text only (max ~150 chars)."""
    return {"type": "header", "text": _plain(text[:150])}


def markdown(md: str) -> Dict[str, Any]:
    """Full-Markdown block: H1/H2/H3, bold/italic, lists, tables, code, quote, hr.

    Use this when you want real Markdown rendering rather than Slack mrkdwn.
    """
    return {"type": "markdown", "text": str(md)}


def h2(text: str) -> Dict[str, Any]:
    """H2 subheading, rendered via a markdown block (``header`` is H1-only)."""
    return markdown(f"## {text}")


def section(text: str, accessory: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A paragraph of Slack ``mrkdwn``. Optional accessory (button/image) on the right."""
    block: Dict[str, Any] = {"type": "section", "text": _mrkdwn(text)}
    if accessory:
        block["accessory"] = accessory
    return block


# Alias — "paragraph" was requested explicitly.
paragraph = section


def fields(pairs: Sequence[Tuple[str, str]]) -> Dict[str, Any]:
    """A two-column key/value grid (``section.fields``) — a compact table-ish layout.

    Slack renders fields in two columns, top-to-bottom. Max 10 fields total.
    """
    flat: List[Dict[str, Any]] = []
    for label, value in pairs:
        flat.append(_mrkdwn(f"*{label}*"))
        flat.append(_mrkdwn(str(value)))
    return {"type": "section", "fields": flat[: FIELDS_MAX]}


def _rich_list(items: Iterable[str], style: str) -> Dict[str, Any]:
    elements = [
        {
            "type": "rich_text_section",
            "elements": [{"type": "text", "text": str(item)}],
        }
        for item in items
    ]
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_list", "style": style, "elements": elements}],
    }


def bullets(items: Iterable[str]) -> Dict[str, Any]:
    """Bulleted list via ``rich_text_list`` (style=bullet)."""
    return _rich_list(items, "bullet")


def numbered(items: Iterable[str]) -> Dict[str, Any]:
    """Numbered list via ``rich_text_list`` (style=ordered)."""
    return _rich_list(items, "ordered")


def divider() -> Dict[str, Any]:
    """Horizontal line separator."""
    return {"type": "divider"}


def table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Real multi-column native ``table`` block.

    Uses ``raw_text`` cells + ``column_settings`` (the schema this workspace
    accepts; ``raw_number`` has been rejected here). Truncates to Slack's
    100-row / 20-column limits. If a native table is ever rejected, fall back
    to :func:`md_table` or :func:`mono_table`.
    """
    cols = min(len(headers), TABLE_MAX_COLS)
    header_cells = [{"type": "raw_text", "text": str(h)[:2000]} for h in headers[:cols]]
    out_rows: List[List[Dict[str, Any]]] = [header_cells]
    for r in rows[: TABLE_MAX_ROWS - 1]:
        cells = [{"type": "raw_text", "text": str(c)[:2000]} for c in list(r)[:cols]]
        # pad short rows so every row has the same column count
        while len(cells) < cols:
            cells.append({"type": "raw_text", "text": ""})
        out_rows.append(cells)
    column_settings = [{"is_wrapped": True} for _ in range(cols)]
    return {"type": "table", "rows": out_rows, "column_settings": column_settings}


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Multi-column table rendered as a Markdown pipe table inside a ``markdown`` block.

    Rock-solid fallback for the native :func:`table` — Markdown tables render
    reliably everywhere.
    """
    head = "| " + " | ".join(str(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return markdown(f"{head}\n{sep}\n{body}")


def mono_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Monospace, column-aligned table inside a fenced code block (universal render)."""
    cols = list(zip(*([list(headers)] + [list(r) for r in rows]))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    def fmt(row: Sequence[Any]) -> str:
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    return section("```\n" + "\n".join(lines) + "\n```")


def context(*elements: str, images: Optional[Sequence[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """Small footnote text (and optional small images). ``context`` block."""
    els: List[Dict[str, Any]] = []
    if images:
        for url, alt in images:
            els.append({"type": "image", "image_url": url, "alt_text": alt})
    for text in elements:
        els.append(_mrkdwn(text))
    return {"type": "context", "elements": els[:10]}


def _button(text: str, url: Optional[str] = None, style: Optional[str] = None,
            value: Optional[str] = None, action_id: Optional[str] = None) -> Dict[str, Any]:
    btn: Dict[str, Any] = {"type": "button", "text": _plain(text)}
    if url:
        btn["url"] = url
    if style in ("primary", "danger"):
        btn["style"] = style
    if value is not None:
        btn["value"] = value
    # URL buttons are passive links and don't require an action handler, but a
    # stable action_id keeps interaction payloads clean.
    btn["action_id"] = action_id or ("btn_" + "".join(c for c in text.lower() if c.isalnum())[:40] or "btn")
    return btn


def buttons(btns: Sequence[Union[Tuple[str, str], Dict[str, Any]]]) -> Dict[str, Any]:
    """Row of URL buttons (``actions`` block). Max 5 elements.

    Each item is ``(text, url)`` or a dict with keys
    ``text, url, style, value, action_id``.
    """
    elements: List[Dict[str, Any]] = []
    for b in btns[:5]:
        if isinstance(b, dict):
            elements.append(_button(**b))
        else:
            text, url = b
            elements.append(_button(text, url=url))
    return {"type": "actions", "elements": elements}


def image(url: str, alt: str = "", title: Optional[str] = None) -> Dict[str, Any]:
    """Standalone ``image`` block."""
    block: Dict[str, Any] = {"type": "image", "image_url": url, "alt_text": alt or title or "image"}
    if title:
        block["title"] = _plain(title)
    return block


def card(title: str, body: str, button: Optional[Tuple[str, str]] = None,
         image_url: Optional[str] = None) -> Dict[str, Any]:
    """A 'content card': a ``section`` (title + body) with an optional accessory.

    This is the portable card shape (section + accessory). For a swipeable
    deck of cards use :func:`carousel`. Slack has *no native carousel for a
    plain section*, so multiple cards become multiple blocks — see
    :func:`card_fallback`.
    """
    text = f"*{title}*\n{body}"
    accessory: Optional[Dict[str, Any]] = None
    if image_url:
        accessory = {"type": "image", "image_url": image_url, "alt_text": title}
    elif button:
        accessory = _button(button[0], url=button[1])
    return section(text, accessory=accessory)


def carousel(cards: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    """Swipeable deck via the native ``carousel`` block (card elements).

    Each card dict supports ``title``, ``body``, optional ``subtitle``,
    ``subtext``, ``image_url``/``hero_image_url`` (rendered as ``hero_image``),
    and optional URL ``button``. Confirmed supported in a production workspace.
    LIMIT: Slack has no carousel for arbitrary blocks — only ``card`` elements
    nest inside a carousel. For environments where carousel is unavailable, use
    :func:`card_fallback` (header+section per card, separated by dividers).
    """
    elements = []
    for c in cards[:10]:
        element: Dict[str, Any] = {
            "type": "card",
            "title": {"type": "mrkdwn", "text": str(c.get("title", ""))[:150]},
            "body": {"type": "mrkdwn", "text": str(c.get("body", ""))[:200]},
        }
        if c.get("subtitle"):
            element["subtitle"] = {"type": "mrkdwn", "text": str(c.get("subtitle"))[:150]}
        hero_url = c.get("hero_image_url") or c.get("image_url")
        if hero_url:
            element["hero_image"] = {"type": "image", "image_url": str(hero_url), "alt_text": str(c.get("alt_text") or c.get("title") or "image")}
        if c.get("subtext"):
            element["subtext"] = {"type": "mrkdwn", "text": str(c.get("subtext"))[:200]}
        button = c.get("button")
        if button:
            if isinstance(button, dict):
                element["actions"] = [_button(str(button.get("text") or "Open"), url=str(button.get("url") or ""))]
            else:
                text, url = button
                element["actions"] = [_button(str(text), url=str(url))]
        elements.append(element)
    return {"type": "carousel", "elements": elements}


def card_fallback(cards: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Multi-block 'swipe-through approximation' for clients without carousel:
    one header + section per card, separated by dividers. Returns a list of blocks."""
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(cards):
        if i:
            out.append(divider())
        out.append(header(c.get("title", "")))
        out.append(section(c.get("body", "")))
    return out


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate(blocks: Sequence[Dict[str, Any]]) -> List[str]:
    """Return a list of human-readable warnings/errors for a block list.

    Catches the limits and unsupported elements discovered by live testing
    *before* you hit Slack with an ``invalid_blocks`` error.
    """
    issues: List[str] = []
    if len(blocks) > BLOCK_LIMIT:
        issues.append(f"{len(blocks)} blocks > {BLOCK_LIMIT} block limit")
    md_total = 0
    for i, b in enumerate(blocks):
        btype = b.get("type")
        if btype == "section":
            txt = (b.get("text") or {}).get("text", "")
            if len(txt) > SECTION_TEXT_MAX:
                issues.append(f"block {i}: section text {len(txt)} > {SECTION_TEXT_MAX}")
            if len(b.get("fields", [])) > FIELDS_MAX:
                issues.append(f"block {i}: {len(b['fields'])} fields > {FIELDS_MAX}")
        elif btype == "markdown":
            md_total += len(b.get("text", ""))
        elif btype == "table":
            if len(b.get("rows", [])) > TABLE_MAX_ROWS:
                issues.append(f"block {i}: table rows > {TABLE_MAX_ROWS}")
        elif btype == "actions":
            if len(b.get("elements", [])) > 5:
                issues.append(f"block {i}: actions has >5 elements")
        # scan for rejected element types anywhere in the block
        for et in _find_types(b):
            if et in _REJECTED_IN_MESSAGES:
                issues.append(f"block {i}: '{et}' is rejected in this workspace's messages (use a modal)")
    if md_total > MARKDOWN_CUMULATIVE_MAX:
        issues.append(f"markdown blocks total {md_total} > {MARKDOWN_CUMULATIVE_MAX} chars")
    return issues


def _find_types(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        t = obj.get("type")
        if isinstance(t, str):
            yield t
        for v in obj.values():
            yield from _find_types(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_types(v)


# --------------------------------------------------------------------------- #
# Message builder — chainable, accumulates blocks, validates, posts.
# --------------------------------------------------------------------------- #
class Message:
    """Chainable Block Kit message builder.

    The constructor's ``fallback`` text becomes the top-level ``text`` field
    (used for notifications and screen readers — always keep it populated).
    """

    def __init__(self, fallback: str = "") -> None:
        self.fallback = fallback
        self.blocks: List[Dict[str, Any]] = []

    # -- generic --
    def add(self, block: Union[Dict[str, Any], List[Dict[str, Any]]]) -> "Message":
        if isinstance(block, list):
            self.blocks.extend(block)
        else:
            self.blocks.append(block)
        return self

    # -- typed convenience wrappers (mirror the module-level builders) --
    def header(self, text: str) -> "Message": return self.add(header(text))
    def h2(self, text: str) -> "Message": return self.add(h2(text))
    def markdown(self, md: str) -> "Message": return self.add(markdown(md))
    def section(self, text: str, accessory: Optional[Dict[str, Any]] = None) -> "Message":
        return self.add(section(text, accessory))
    paragraph = section
    def fields(self, pairs: Sequence[Tuple[str, str]]) -> "Message": return self.add(fields(pairs))
    def bullets(self, items: Iterable[str]) -> "Message": return self.add(bullets(items))
    def numbered(self, items: Iterable[str]) -> "Message": return self.add(numbered(items))
    def divider(self) -> "Message": return self.add(divider())
    def table(self, headers, rows) -> "Message": return self.add(table(headers, rows))
    def md_table(self, headers, rows) -> "Message": return self.add(md_table(headers, rows))
    def mono_table(self, headers, rows) -> "Message": return self.add(mono_table(headers, rows))
    def context(self, *elements: str, images=None) -> "Message": return self.add(context(*elements, images=images))
    def buttons(self, btns) -> "Message": return self.add(buttons(btns))
    def image(self, url, alt="", title=None) -> "Message": return self.add(image(url, alt, title))
    def card(self, title, body, button=None, image_url=None) -> "Message":
        return self.add(card(title, body, button, image_url))
    def carousel(self, cards) -> "Message": return self.add(carousel(cards))
    def card_fallback(self, cards) -> "Message": return self.add(card_fallback(cards))

    # -- output --
    def build(self, strict: bool = True) -> Dict[str, Any]:
        """Return the ``{text, blocks}`` payload. Raises on validation errors
        when ``strict`` (default)."""
        issues = validate(self.blocks)
        if issues and strict:
            raise SlackFormatError("invalid message: " + "; ".join(issues))
        payload: Dict[str, Any] = {"blocks": self.blocks}
        if self.fallback:
            payload["text"] = self.fallback
        return payload

    def post(self, channel: str, thread_ts: Optional[str] = None,
             token: Optional[str] = None, strict: bool = True) -> Dict[str, Any]:
        return post(channel, message=self, thread_ts=thread_ts, token=token, strict=strict)

    def __repr__(self) -> str:
        return f"<Message blocks={len(self.blocks)} fallback={self.fallback!r}>"


# --------------------------------------------------------------------------- #
# Poster — single seam that touches Slack. chat.postMessage with blocks.
# --------------------------------------------------------------------------- #
def post(channel: str,
         message: Optional[Message] = None,
         *,
         text: Optional[str] = None,
         blocks: Optional[Sequence[Dict[str, Any]]] = None,
         thread_ts: Optional[str] = None,
         token: Optional[str] = None,
         strict: bool = True,
         timeout: int = 15) -> Dict[str, Any]:
    """Post a message to Slack via ``chat.postMessage``.

    Drop-in for the inline ``requests.post(... chat.postMessage ...)`` reports
    use today. Pass a :class:`Message`, or raw ``text`` / ``blocks``.

    Returns the parsed Slack response (``ok: true`` on success). Raises
    :class:`SlackFormatError` on transport or API failure.
    """
    if message is not None:
        payload = message.build(strict=strict)
    elif blocks is not None:
        if strict:
            issues = validate(blocks)
            if issues:
                raise SlackFormatError("invalid blocks: " + "; ".join(issues))
        payload = {"blocks": list(blocks)}
        if text:
            payload["text"] = text
    elif text is not None:
        payload = {"text": text}
    else:
        raise SlackFormatError("post() needs a message, blocks, or text")

    payload["channel"] = channel
    if thread_ts:
        payload["thread_ts"] = thread_ts

    tok = token or get_token()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API + "chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:  # transport failure
        raise SlackFormatError(f"chat.postMessage transport error: {exc}") from exc

    if not body.get("ok"):
        err = body.get("error", "unknown_error")
        detail = body.get("response_metadata", {}).get("messages")
        raise SlackFormatError(
            f"chat.postMessage rejected: {err}" + (f" {detail}" if detail else "")
        )
    return body
