"""Media upload (direct <32MB and chunked INIT/ADD/FINALIZE)."""

from __future__ import annotations

import os
import time
from math import ceil
from pathlib import Path
from typing import Any

from ..api_client import SnapchatApiClient
from ..safety import format_preview

CHUNK_THRESHOLD = 32 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024


def _guess_mime(file_path: Path) -> str:
    """Snap's media upload 415s on application/octet-stream; send the real type."""
    import mimetypes

    return mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"


def list_media(
    client: SnapchatApiClient,
    ad_account_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/media",
        "media",
        params={"limit": min(max(limit, 1), 1000)},
    )
    return {"media": items, "count": len(items), "ad_account_id": ad_account_id}


def get_media(client: SnapchatApiClient, media_id: str) -> dict[str, Any]:
    body, _ = client.get(f"media/{media_id}")
    items = body.get("media") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "media" in first:
            return first["media"]
        return first
    return body


def media_status(client: SnapchatApiClient, media_id: str) -> dict[str, Any]:
    body, _ = client.get(f"media/{media_id}")
    return body


def get_media_by_ids(
    client: SnapchatApiClient,
    ad_account_id: str,
    media_ids: list[str],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/get_media_by_ids",
        json_body={"media_ids": media_ids},
    )
    items = body.get("media") or []
    flat = []
    for e in items:
        if isinstance(e, dict) and "media" in e:
            flat.append(e["media"])
        elif isinstance(e, dict):
            flat.append(e)
    return {"media": flat, "count": len(flat)}


def media_preview(client: SnapchatApiClient, media_id: str) -> dict[str, Any]:
    body, _ = client.get(f"media/{media_id}/preview")
    return body


def media_thumbnail(client: SnapchatApiClient, media_id: str) -> dict[str, Any]:
    body, _ = client.get(f"media/{media_id}/thumbnail")
    return body


def lens_preview(client: SnapchatApiClient, media_id: str) -> dict[str, Any]:
    body, _ = client.get(f"media/{media_id}/lens_preview")
    return body


def copy_media(
    client: SnapchatApiClient,
    dest_ad_account_id: str,
    account_label: str,
    *,
    media_ids: list[str],
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"copy {len(media_ids)} media item(s) into ad account {dest_ad_account_id}",
            account_label,
            proposed_state={"media_ids": media_ids, "dest_ad_account_id": dest_ad_account_id},
        )
    body, _ = client.post(
        f"adaccounts/{dest_ad_account_id}/media_copy",
        json_body={"media_ids": media_ids},
    )
    return body


def claim_media(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    snap_reference: str,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"claim media by snap reference '{snap_reference}'",
            account_label,
            proposed_state={"snap_reference": snap_reference},
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/claim_media_by_snap_reference",
        json_body={"snap_reference": snap_reference},
    )
    return body


def _create_media_record(
    client: SnapchatApiClient,
    ad_account_id: str,
    name: str,
    media_type: str,
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/media",
        json_body={"media": [{"name": name, "type": media_type, "ad_account_id": ad_account_id}]},
    )
    items = body.get("media") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "media" in first:
            return first["media"]
        return first
    return body


def _direct_upload(
    client: SnapchatApiClient, media_id: str, file_path: Path
) -> dict[str, Any]:
    with open(file_path, "rb") as f:
        files = {"file": (file_path.name, f, _guess_mime(file_path))}
        body, _ = client.post_multipart(
            f"media/{media_id}/upload", data={}, files=files
        )
    return body


def _chunked_upload(
    client: SnapchatApiClient,
    media_id: str,
    file_path: Path,
    *,
    chunk_size: int = CHUNK_SIZE,
) -> dict[str, Any]:
    file_size = file_path.stat().st_size
    number_of_parts = max(1, ceil(file_size / chunk_size))

    init_body, _ = client.post_multipart(
        f"media/{media_id}/multipart-upload-v2",
        params={"action": "INIT"},
        data={
            "file_name": file_path.name,
            "file_size": str(file_size),
            "number_of_parts": str(number_of_parts),
        },
        files={},
    )
    upload_id = init_body.get("upload_id")
    if not upload_id:
        return {"error": {"message": "chunked upload INIT missing upload_id", "body": init_body}}
    add_path = init_body.get("add_path") or f"media/{media_id}/multipart-upload-v2"
    finalize_path = init_body.get("finalize_path") or f"media/{media_id}/multipart-upload-v2"

    parts: list[dict[str, Any]] = []
    with open(file_path, "rb") as f:
        index = 1
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            files = {"file": (f"{file_path.name}.part{index}", chunk, _guess_mime(file_path))}
            data = {"upload_id": str(upload_id), "part_number": str(index)}
            params = None if "action=ADD" in add_path else {"action": "ADD"}
            part_body, _ = client.post_multipart(
                add_path,
                data=data,
                files=files,
                params=params,
            )
            parts.append({"part_number": index, "body": part_body})
            index += 1

    finalize_params = None if "action=FINALIZE" in finalize_path else {"action": "FINALIZE"}
    finalize_body, _ = client.post_multipart(
        finalize_path,
        data={"upload_id": str(upload_id)},
        files={},
        params=finalize_params,
    )

    return {
        "init": init_body,
        "upload_id": upload_id,
        "parts_uploaded": len(parts),
        "finalize": finalize_body,
    }


def upload_media(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    file_path: str,
    media_type: str = "VIDEO",
    name: str | None = None,
    poll_until_ready: bool = True,
    poll_timeout: float = 300.0,
    execute: bool = False,
) -> dict[str, Any]:
    p = Path(os.path.expanduser(file_path))
    if not p.exists() or not p.is_file():
        return {"error": {"message": f"file not found: {p}"}}

    size = p.stat().st_size
    final_name = name or p.stem
    mode = "chunked" if size >= CHUNK_THRESHOLD else "direct"

    if not execute:
        return format_preview(
            f"upload {media_type.lower()} '{final_name}' ({size:,} bytes, {mode})",
            account_label,
            proposed_state={
                "ad_account_id": ad_account_id,
                "name": final_name,
                "type": media_type,
                "size_bytes": size,
                "mode": mode,
            },
        )

    record = _create_media_record(client, ad_account_id, final_name, media_type)
    media_id = record.get("id") or record.get("media_id")
    if not media_id:
        return {"error": {"message": "media record missing id", "record": record}}

    if mode == "chunked":
        upload_resp = _chunked_upload(client, media_id, p)
    else:
        upload_resp = _direct_upload(client, media_id, p)

    final = {"media_id": media_id, "record": record, "upload": upload_resp}

    if poll_until_ready:
        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            status_body = media_status(client, media_id)
            entries = status_body.get("media") or []
            entry = entries[0] if entries else {}
            inner = entry.get("media", entry) if isinstance(entry, dict) else {}
            if inner.get("media_status") == "READY":
                final["media_status"] = "READY"
                final["media"] = inner
                return final
            if inner.get("media_status") in {"ERROR", "FAILED"}:
                final["media_status"] = inner.get("media_status")
                final["media"] = inner
                return final
            time.sleep(3)
        final["media_status"] = "POLL_TIMEOUT"

    return final
