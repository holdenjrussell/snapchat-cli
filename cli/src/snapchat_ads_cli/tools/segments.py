"""Audience segment CRUD + SHA256-hashed user upload."""

from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from ..api_client import SnapchatApiClient
from ..safety import format_preview

SCHEMAS = {
    "EMAIL_SHA256",
    "PHONE_SHA256",
    "MOBILE_AD_ID_SHA256",
    "HASHED_EMAIL",
    "HASHED_PHONE_NUMBER",
    "HASHED_MOBILE_AD_ID",
}
USER_BATCH = 10000


def list_segments(
    client: SnapchatApiClient,
    ad_account_id: str,
    limit: int = 100,
) -> dict[str, Any]:
    items = client.collect_paginated(
        f"adaccounts/{ad_account_id}/segments",
        "segments",
        params={"limit": min(max(limit, 1), 1000)},
    )
    return {"segments": items, "count": len(items), "ad_account_id": ad_account_id}


def get_segment(client: SnapchatApiClient, segment_id: str) -> dict[str, Any]:
    body, _ = client.get(f"segments/{segment_id}")
    items = body.get("segments") or []
    if items:
        first = items[0]
        if isinstance(first, dict) and "segment" in first:
            return first["segment"]
        return first
    return body


def create_segment(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    payload: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("ad_account_id", ad_account_id)
    if not execute:
        return format_preview(
            f"create segment '{payload.get('name', '?')}'",
            account_label,
            proposed_state=payload,
        )
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/segments",
        json_body={"segments": [payload]},
    )
    return body


def update_segment(
    client: SnapchatApiClient,
    ad_account_id: str,
    segment_id: str,
    account_label: str,
    *,
    fields: dict[str, Any],
    execute: bool = False,
) -> dict[str, Any]:
    if not fields:
        return {"error": {"message": "no fields to update"}}
    payload = dict(fields)
    payload["id"] = segment_id

    if not execute:
        return format_preview(
            f"update segment {segment_id}",
            account_label,
            proposed_state=fields,
        )

    body, _ = client.put(
        f"adaccounts/{ad_account_id}/segments",
        json_body={"segments": [payload]},
    )
    return body


def delete_segment(
    client: SnapchatApiClient,
    segment_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"delete segment {segment_id}",
            account_label,
            details="DELETE is irreversible.",
        )
    body, _ = client.delete(f"segments/{segment_id}")
    return body


def clear_segment_users(
    client: SnapchatApiClient,
    segment_id: str,
    account_label: str,
    *,
    execute: bool = False,
) -> dict[str, Any]:
    if not execute:
        return format_preview(
            f"clear ALL users from segment {segment_id}",
            account_label,
            details="Removes every user record from the segment.",
        )
    body, _ = client.delete(f"segments/{segment_id}/all_users")
    return body


def add_users(
    client: SnapchatApiClient,
    segment_id: str,
    account_label: str,
    *,
    file_path: str,
    schema: str,
    pre_hashed: bool = False,
    execute: bool = False,
) -> dict[str, Any]:
    if schema not in SCHEMAS:
        return {"error": {"message": f"schema must be one of {sorted(SCHEMAS)}"}}

    p = Path(os.path.expanduser(file_path))
    if not p.exists() or not p.is_file():
        return {"error": {"message": f"file not found: {p}"}}

    raw = list(_read_identifier_file(p))
    hashed = list(raw if pre_hashed else _hash_values(raw, schema))

    summary = {
        "segment_id": segment_id,
        "schema": schema,
        "input_count": len(raw),
        "batches": (len(hashed) + USER_BATCH - 1) // USER_BATCH,
        "sample_hashed": hashed[:3],
    }

    if not execute:
        return format_preview(
            f"add {len(hashed)} users to segment {segment_id} (schema={schema})",
            account_label,
            proposed_state=summary,
        )

    results: list[dict[str, Any]] = []
    for i in range(0, len(hashed), USER_BATCH):
        batch = hashed[i : i + USER_BATCH]
        body, _ = client.post(
            f"segments/{segment_id}/users",
            json_body={
                "users": [
                    {
                        "schema": [schema],
                        "data": [[v] for v in batch],
                    }
                ]
            },
        )
        results.append(body)
    return {"segment_id": segment_id, "schema": schema, "uploaded": len(hashed), "responses": results}


def remove_users(
    client: SnapchatApiClient,
    segment_id: str,
    account_label: str,
    *,
    file_path: str,
    schema: str,
    pre_hashed: bool = False,
    execute: bool = False,
) -> dict[str, Any]:
    if schema not in SCHEMAS:
        return {"error": {"message": f"schema must be one of {sorted(SCHEMAS)}"}}

    p = Path(os.path.expanduser(file_path))
    if not p.exists() or not p.is_file():
        return {"error": {"message": f"file not found: {p}"}}

    raw = list(_read_identifier_file(p))
    hashed = list(raw if pre_hashed else _hash_values(raw, schema))

    if not execute:
        return format_preview(
            f"remove {len(hashed)} users from segment {segment_id} (schema={schema})",
            account_label,
            proposed_state={
                "segment_id": segment_id,
                "schema": schema,
                "count": len(hashed),
                "sample": hashed[:3],
            },
        )

    results: list[dict[str, Any]] = []
    for i in range(0, len(hashed), USER_BATCH):
        batch = hashed[i : i + USER_BATCH]
        body, _ = client.request(
            "DELETE",
            f"segments/{segment_id}/users",
            json_body={
                "users": [
                    {
                        "schema": [schema],
                        "data": [[v] for v in batch],
                    }
                ]
            },
        )
        results.append(body)
    return {"segment_id": segment_id, "removed": len(hashed), "responses": results}


def _read_identifier_file(path: Path) -> Iterable[str]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row:
                    continue
                yield row[0].strip()
    else:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    yield line


def _hash_values(values: Iterable[str], schema: str) -> Iterable[str]:
    for v in values:
        normalized = _normalize(v, schema)
        if not normalized:
            continue
        yield hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize(value: str, schema: str) -> str:
    if not value:
        return ""
    v = value.strip()
    if schema in {"EMAIL_SHA256", "HASHED_EMAIL"}:
        return v.lower()
    if schema in {"PHONE_SHA256", "HASHED_PHONE_NUMBER"}:
        return "".join(ch for ch in v if ch.isdigit())
    if schema in {"MOBILE_AD_ID_SHA256", "HASHED_MOBILE_AD_ID"}:
        return v.lower()
    return v


def bulk_create_segments(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    items = [{"ad_account_id": ad_account_id, **i} for i in items]
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-create {len(items)} segments",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_post_array(
        client,
        path=f"adaccounts/{ad_account_id}/segments",
        items=items,
        array_key="segments",
        chunk_size=chunk_size,
    )


def bulk_update_segments(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    items: list[dict[str, Any]],
    chunk_size: int = 10,
    execute: bool = False,
) -> dict[str, Any]:
    from . import _bulk
    missing = [i for i in items if not i.get("id")]
    if missing:
        return {"error": {"message": f"{len(missing)} item(s) missing required 'id' field"}}
    if not execute:
        return _bulk.preview_bulk(
            action=f"bulk-update {len(items)} segments",
            account_label=account_label,
            items=items,
            chunk_size=chunk_size,
        )
    return _bulk.bulk_put_array(
        client,
        path=f"adaccounts/{ad_account_id}/segments",
        items=items,
        array_key="segments",
        chunk_size=chunk_size,
    )


def create_lookalike_segment(
    client: SnapchatApiClient,
    ad_account_id: str,
    account_label: str,
    *,
    name: str,
    seed_segment_id: str,
    countries: list[str],
    lookalike_type: str = "BALANCE",
    retention_in_days: int = 180,
    description: str | None = None,
    extra: dict[str, Any] | None = None,
    execute: bool = False,
) -> dict[str, Any]:
    """Create a lookalike segment from a seed.

    `lookalike_type` is one of BALANCE, SIMILARITY, REACH.
    `retention_in_days` capped at 180 by Snap.
    """
    valid_types = {"BALANCE", "SIMILARITY", "REACH"}
    if lookalike_type not in valid_types:
        return {"error": {"message": f"lookalike_type must be one of {sorted(valid_types)}"}}

    payload: dict[str, Any] = {
        "name": name,
        "ad_account_id": ad_account_id,
        "source_type": "LOOKALIKE",
        "retention_in_days": min(max(retention_in_days, 1), 180),
        "creation_spec": {
            "seed_segment_id": seed_segment_id,
            "country": [c.upper() for c in countries],
            "type": lookalike_type,
        },
    }
    if description:
        payload["description"] = description
    if extra:
        payload.update(extra)

    if not execute:
        return format_preview(
            f"create lookalike segment '{name}' (type={lookalike_type}, seed={seed_segment_id})",
            account_label,
            proposed_state=payload,
        )

    body, _ = client.post(
        f"adaccounts/{ad_account_id}/segments",
        json_body={"segments": [payload]},
    )
    return body
