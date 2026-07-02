"""Shared helpers for bulk create / update / delete / get-by-ids operations.

Snap caps are codified per-endpoint family:
  - get_*_by_ids: 2000 per request
  - segment users add/remove: 10,000 per request (handled in segments.py)
  - CAPI events: 1000 per request (handled in conversions_api.py)
  - create/update array wrappers: conservative 10 per request unless overridden
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Callable, Iterable

from ..api_client import SnapApiError, SnapchatApiClient
from ..safety import format_preview

DEFAULT_CHUNK = 10
GETBYIDS_CHUNK = 2000


def chunk(items: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def load_json_array(file_path: str | None, *, key: str | None = None) -> list[Any]:
    """Read a JSON file. Accept either a top-level array or `{key: [...]}`."""
    if not file_path:
        return []
    p = Path(file_path).expanduser()
    text = p.read_text()
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if key and key in data and isinstance(data[key], list):
            return data[key]
        for v in data.values():
            if isinstance(v, list):
                return v
    raise ValueError(
        f"{p}: expected JSON array or {{key: [...]}}, got {type(data).__name__}"
    )


def load_ids(
    *,
    positional: tuple[str, ...] | list[str] = (),
    file_path: str | None = None,
) -> list[str]:
    """Resolve a list of IDs from positional args, a JSON array, or one-per-line text/csv."""
    if positional:
        return [s for s in positional if s]
    if not file_path:
        return []
    p = Path(file_path).expanduser()
    text = p.read_text()
    stripped = text.strip()
    if stripped.startswith("["):
        data = json.loads(stripped)
        return [str(x) for x in data if x]
    if p.suffix.lower() == ".csv":
        out: list[str] = []
        for row in csv.reader(text.splitlines()):
            if row and row[0].strip():
                out.append(row[0].strip())
        return out
    return [line.strip() for line in text.splitlines() if line.strip()]


def bulk_post_array(
    client: SnapchatApiClient,
    *,
    path: str,
    items: list[dict[str, Any]],
    array_key: str,
    chunk_size: int = DEFAULT_CHUNK,
) -> dict[str, Any]:
    """POST `items` in chunks to `path` wrapping each batch as `{array_key: chunk}`."""
    return _bulk_invoke(
        client,
        method="POST",
        path=path,
        items=items,
        array_key=array_key,
        chunk_size=chunk_size,
    )


def bulk_put_array(
    client: SnapchatApiClient,
    *,
    path: str,
    items: list[dict[str, Any]],
    array_key: str,
    chunk_size: int = DEFAULT_CHUNK,
) -> dict[str, Any]:
    """PUT `items` in chunks to `path` wrapping each batch as `{array_key: chunk}`."""
    return _bulk_invoke(
        client,
        method="PUT",
        path=path,
        items=items,
        array_key=array_key,
        chunk_size=chunk_size,
    )


def _bulk_invoke(
    client: SnapchatApiClient,
    *,
    method: str,
    path: str,
    items: list[dict[str, Any]],
    array_key: str,
    chunk_size: int,
) -> dict[str, Any]:
    if not items:
        return {"error": {"message": "no items to send"}}
    batches: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for c in chunk(items, chunk_size):
        try:
            if method == "POST":
                body, _ = client.post(path, json_body={array_key: c})
            elif method == "PUT":
                body, _ = client.put(path, json_body={array_key: c})
            else:
                raise ValueError(f"unsupported method {method}")
            batches.append(body)
            success += len(c)
        except SnapApiError as e:
            batches.append(e.to_dict())
            failed += len(c)
    return {
        "method": method,
        "path": path,
        "items_attempted": len(items),
        "items_succeeded": success,
        "items_failed": failed,
        "batches": batches,
    }


def bulk_get_by_ids(
    client: SnapchatApiClient,
    *,
    path: str,
    ids: list[str],
    id_array_key: str,
    response_array_key: str,
    inner_singular: str | None = None,
    chunk_size: int = GETBYIDS_CHUNK,
) -> dict[str, Any]:
    """POST batches of IDs to a `get_*_by_ids` endpoint and flatten responses."""
    flat: list[dict[str, Any]] = []
    failed_batches: list[dict[str, Any]] = []
    for c in chunk(ids, chunk_size):
        try:
            body, _ = client.post(path, json_body={id_array_key: c})
        except SnapApiError as e:
            failed_batches.append(e.to_dict())
            continue
        for entry in body.get(response_array_key, []) or []:
            if isinstance(entry, dict):
                if inner_singular and inner_singular in entry:
                    flat.append(entry[inner_singular])
                else:
                    flat.append(entry)
    return {
        response_array_key: flat,
        "count": len(flat),
        "ids_requested": len(ids),
        "failed_batches": failed_batches,
    }


def preview_bulk(
    *,
    action: str,
    account_label: str,
    items: list[Any],
    chunk_size: int,
    sample_count: int = 2,
) -> dict[str, Any]:
    return format_preview(
        action,
        account_label,
        proposed_state={
            "item_count": len(items),
            "chunk_size": chunk_size,
            "batches": (len(items) + chunk_size - 1) // chunk_size,
            "sample": items[:sample_count],
        },
    )


def run_per_item(
    client: SnapchatApiClient,
    *,
    fn: Callable[[SnapchatApiClient, dict[str, Any]], dict[str, Any]],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sequentially call `fn(client, item)` for each item; aggregate results.

    Used when no native bulk endpoint exists (e.g. duplications, deletes).
    """
    results: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for item in items:
        try:
            r = fn(client, item)
            results.append({"item": item, "ok": True, "response": r})
            success += 1
        except SnapApiError as e:
            results.append({"item": item, "ok": False, "error": e.to_dict()["error"]})
            failed += 1
    return {
        "items_attempted": len(items),
        "items_succeeded": success,
        "items_failed": failed,
        "results": results,
    }
