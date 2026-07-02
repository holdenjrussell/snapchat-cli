"""Server-side Conversions API (CAPI v3).

Snap CAPI lives at a different host: https://tr.snapchat.com/v3/conversion
Bearer token same as Marketing API. Events are POSTed in batches of up to
1000. Identifiers (em, ph, idfa/aaid, external_id) MUST be SHA256-hashed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import httpx

from ..safety import format_preview

logger = logging.getLogger(__name__)

CAPI_BASE = "https://tr.snapchat.com"
CAPI_PATH = "/v3/conversion"
BATCH_SIZE = 1000

HASH_FIELDS = {"em", "ph", "idfa", "aaid", "external_id"}


def _hash(v: str | None) -> str | None:
    if not v:
        return None
    s = v.strip().lower()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def hash_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `event` with PII identifiers SHA256-hashed.

    Skips fields already 64-char hex.
    """
    out = dict(event)
    user = out.get("user_data") or {}
    user_out = dict(user)
    for k in HASH_FIELDS:
        v = user_out.get(k)
        if isinstance(v, list):
            user_out[k] = [_safe_hash(x) for x in v]
        elif isinstance(v, str):
            user_out[k] = _safe_hash(v)
    if user_out:
        out["user_data"] = user_out
    return out


def _safe_hash(v: str) -> str:
    if not v:
        return v
    if len(v) == 64 and all(c in "0123456789abcdef" for c in v.lower()):
        return v.lower()
    return hashlib.sha256(v.strip().lower().encode("utf-8")).hexdigest()


def send_events(
    *,
    access_token: str,
    pixel_id: str | None = None,
    snap_app_id: str | None = None,
    events: list[dict[str, Any]],
    test_event_code: str | None = None,
    auto_hash: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST events to CAPI v3. Splits into 1000-event batches."""
    if not (pixel_id or snap_app_id):
        return {"error": {"message": "Provide --pixel-id or --snap-app-id"}}

    prepared = [hash_event(e) for e in events] if auto_hash else events
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    results: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout) as c:
        for i in range(0, len(prepared), BATCH_SIZE):
            batch = prepared[i : i + BATCH_SIZE]
            body: dict[str, Any] = {"data": batch}
            if pixel_id:
                body["pixel_id"] = pixel_id
            if snap_app_id:
                body["snap_app_id"] = snap_app_id
            if test_event_code:
                body["test_event_code"] = test_event_code
            attempt = 0
            while True:
                resp = c.post(f"{CAPI_BASE}{CAPI_PATH}", json=body, headers=headers)
                if resp.status_code in {429, 500, 502, 503, 504} and attempt < 4:
                    time.sleep(2 ** attempt)
                    attempt += 1
                    continue
                try:
                    resp_json = resp.json()
                except ValueError:
                    resp_json = {"raw": resp.text}
                results.append(
                    {
                        "status_code": resp.status_code,
                        "events_in_batch": len(batch),
                        "response": resp_json,
                    }
                )
                break
    return {
        "events_sent": len(prepared),
        "batches": len(results),
        "responses": results,
    }


def send_events_preview(
    *,
    pixel_id: str | None,
    snap_app_id: str | None,
    events: list[dict[str, Any]],
    auto_hash: bool,
    account_label: str,
    test_event_code: str | None,
) -> dict[str, Any]:
    sample = [hash_event(events[0])] if events and auto_hash else events[:1]
    return format_preview(
        f"send {len(events)} CAPI event(s)",
        account_label,
        proposed_state={
            "pixel_id": pixel_id,
            "snap_app_id": snap_app_id,
            "auto_hash": auto_hash,
            "test_event_code": test_event_code,
            "event_count": len(events),
            "sample_after_hash": sample,
        },
    )


def load_events_from_file(path: str) -> Iterable[dict[str, Any]]:
    """Read events from JSON (list or NDJSON)."""
    p = Path(path).expanduser()
    text = p.read_text()
    text_stripped = text.strip()
    if text_stripped.startswith("["):
        data = json.loads(text_stripped)
        if isinstance(data, list):
            for e in data:
                if isinstance(e, dict):
                    yield e
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("Skipping bad CAPI line: %s", e)
            continue
        if isinstance(obj, dict):
            yield obj
