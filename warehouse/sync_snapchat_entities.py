#!/usr/bin/env python3
"""Mirror Snapchat entity state and record field-level changes.

The default mode is a read-only dry run. It reads live campaign, ad squad,
and ad state, optionally compares that snapshot with Postgres, and prints the
planned baseline/diff result. Database writes require ``--execute``. Applying
``schema.sql`` additionally requires ``--apply-schema``.

Examples:
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_entities.py --warehouse-schema snapchat_ads
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_entities.py --execute --warehouse-schema snapchat_ads
    "$SNAPCHAT_WAREHOUSE_PYTHON" warehouse/sync_snapchat_entities.py --execute --apply-schema
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

try:
    from warehouse.schema_config import (
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from warehouse.delivery_status import (
        derive_effective_status,
        normalize_delivery_status,
    )
    from warehouse.runtime_paths import (
        RuntimePathError,
        resolve_snapchat_cli,
    )
except ModuleNotFoundError:  # Direct `python warehouse/...` execution.
    from schema_config import (  # type: ignore[no-redef]
        DEFAULT_WAREHOUSE_SCHEMA,
        WarehouseSchemaError,
        qualified_table,
        render_schema_sql,
        validate_warehouse_schema,
    )
    from delivery_status import (  # type: ignore[no-redef]
        derive_effective_status,
        normalize_delivery_status,
    )
    from runtime_paths import (  # type: ignore[no-redef]
        RuntimePathError,
        resolve_snapchat_cli,
    )


REPO_ROOT = Path(__file__).resolve().parent.parent
SNAP_SCRIPTS_DIR = REPO_ROOT / "skills" / "snapchat-ads" / "scripts"
if str(SNAP_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SNAP_SCRIPTS_DIR))
from secret_scrubber import scrub_sensitive_text, scrub_sensitive_value  # noqa: E402

SCHEMA_SQL = Path(__file__).parent / "schema.sql"
DEFAULT_AUDIT_FILE = Path.home() / ".config" / "snapchat-ads-cli" / "audit.jsonl"

ENTITY_TYPES = ("campaign", "ad_squad", "ad")
DIFF_FIELDS = (
    "entity_name",
    "campaign_id",
    "ad_squad_id",
    "configured_status",
    "effective_status",
    "delivery_status",
    "review_status",
    "daily_budget",
    "bid",
    "target_cpa",
    "bid_strategy",
)
SYSTEM_FIELDS = {"effective_status", "delivery_status", "review_status"}
AUDIT_FIELD_MAP = {
    "name": "entity_name",
    "status": "configured_status",
    "daily_budget_micro": "daily_budget",
    "bid_micro": ("bid", "target_cpa"),
    "target_cost_micro": ("target_cpa",),
    "bid_strategy": "bid_strategy",
}
LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"snapchat_entity_state_sync_v1").digest()[:8],
    byteorder="big",
    signed=True,
)


class SyncError(RuntimeError):
    """A safe, operator-facing sync failure."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_timestamp(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_timestamp(value: Any) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed else None


def canonicalize(value: Any) -> Any:
    """Return a deterministic, JSON-compatible representation."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return iso_timestamp(value)
    if isinstance(value, dict):
        return {str(key): canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _scrub_modeled_row(value: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Scrub every modeled scalar while retaining top-level time objects."""
    cleaned: dict[str, Any] = {}
    for key, child in value.items():
        if isinstance(child, dt.datetime):
            cleaned[str(key)] = child
        else:
            cleaned[str(key)] = scrub_sensitive_value(canonicalize(child))
    if not isinstance(cleaned, dict):
        raise SyncError(f"{label} scrubber returned a non-object")
    return cleaned


def sanitize_payload(value: Any) -> Any:
    """Recursively redact secret-bearing keys before storage or preview."""
    return scrub_sensitive_value(canonicalize(value))


def redact_error_text(value: str) -> str:
    """Remove common credential forms from an error before printing it."""
    return scrub_sensitive_text(value)


def money_from_micro(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(int(value) / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


def integer_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalized_delivery_status(value: Any) -> list[str]:
    return normalize_delivery_status(value)


def effective_status(raw: dict[str, Any], delivery: list[str]) -> str:
    """Prefer a native effective state, otherwise derive a stable summary."""
    return derive_effective_status(raw, delivery)


def unwrap_entities(
    payload: dict[str, Any], collection: str, singular: str
) -> list[dict[str, Any]]:
    """Flatten Snap's ``{sub_request_status, singular: {...}}`` wrappers."""
    result: list[dict[str, Any]] = []
    items = payload.get(collection)
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get(singular)
        entity = nested if isinstance(nested, dict) else item
        if entity.get("id"):
            result.append(entity)
    return result


def normalize_entity(
    entity_type: str,
    raw: dict[str, Any],
    *,
    account_id: str,
    account_key: str,
    observed_at: dt.datetime,
    squads_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"Unsupported entity type: {entity_type}")
    entity_id = str(raw.get("id") or "")
    if not entity_id:
        raise ValueError("Entity is missing id")

    campaign_id: str | None = None
    ad_squad_id: str | None = None
    if entity_type == "campaign":
        campaign_id = entity_id
    elif entity_type == "ad_squad":
        campaign_id = str(raw.get("campaign_id") or "") or None
    else:
        ad_squad_id = str(raw.get("ad_squad_id") or "") or None
        squad = (squads_by_id or {}).get(ad_squad_id or "", {})
        campaign_id = str(squad.get("campaign_id") or "") or None

    delivery = normalized_delivery_status(raw.get("delivery_status"))
    daily_budget_micro = integer_or_none(raw.get("daily_budget_micro"))
    bid_micro = integer_or_none(raw.get("bid_micro"))
    bid_strategy = str(raw.get("bid_strategy") or "") or None
    target_cpa_micro = integer_or_none(raw.get("target_cost_micro"))
    if target_cpa_micro is None and bid_strategy == "TARGET_COST":
        target_cpa_micro = integer_or_none(raw.get("bid_micro"))

    return _scrub_modeled_row({
        "account_id": account_id,
        "account_key": account_key,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": str(raw.get("name") or "") or None,
        "campaign_id": campaign_id,
        "ad_squad_id": ad_squad_id,
        "configured_status": str(raw.get("status") or "") or None,
        "effective_status": effective_status(raw, delivery),
        "delivery_status": delivery,
        "review_status": str(raw.get("review_status") or "") or None,
        "daily_budget": money_from_micro(daily_budget_micro),
        "daily_budget_micro": daily_budget_micro,
        "bid": money_from_micro(bid_micro),
        "bid_micro": bid_micro,
        "target_cpa": money_from_micro(target_cpa_micro),
        "target_cpa_micro": target_cpa_micro,
        "bid_strategy": bid_strategy,
        "source_created_at": parse_timestamp(raw.get("created_at")),
        "source_updated_at": parse_timestamp(raw.get("updated_at")),
        "raw_payload": sanitize_payload(raw),
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
    }, label="Snap entity")


def run_cli_json(account_key: str, *args: str, timeout: int = 180) -> dict[str, Any]:
    try:
        cli_executable = resolve_snapchat_cli(REPO_ROOT)
    except RuntimePathError as exc:
        raise SyncError(str(exc)) from exc
    argv = [
        str(cli_executable),
        "--account",
        account_key,
        *args,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        detail = redact_error_text((result.stderr or result.stdout).strip())
        raise SyncError(f"Snap CLI read failed for {' '.join(args)}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Snap CLI returned invalid JSON for {' '.join(args)}") from exc
    if not isinstance(payload, dict):
        raise SyncError(f"Snap CLI returned a non-object for {' '.join(args)}")
    if payload.get("error"):
        raise SyncError(f"Snap API read failed for {' '.join(args)}")
    cleaned = scrub_sensitive_value(payload)
    if not isinstance(cleaned, dict):
        raise SyncError(
            f"Snap CLI scrubber returned a non-object for {' '.join(args)}"
        )
    return cleaned


def fetch_live_entities(
    account_key: str, observed_at: dt.datetime
) -> tuple[str, list[dict[str, Any]]]:
    campaigns_payload = run_cli_json(account_key, "campaign", "list", "--limit", "1000")
    squads_payload = run_cli_json(account_key, "adsquad", "list", "--limit", "1000")
    ads_payload = run_cli_json(account_key, "ad", "list", "--limit", "1000")

    campaigns = unwrap_entities(campaigns_payload, "campaigns", "campaign")
    squads = unwrap_entities(squads_payload, "ad_squads", "adsquad")
    if not squads:
        squads = unwrap_entities(squads_payload, "adsquads", "adsquad")
    ads = unwrap_entities(ads_payload, "ads", "ad")

    account_id = str(campaigns_payload.get("ad_account_id") or "")
    if not account_id and campaigns:
        account_id = str(campaigns[0].get("ad_account_id") or "")
    if not account_id:
        raise SyncError(
            "Could not determine ad account id from the campaign list response"
        )

    squads_by_id = {str(item["id"]): item for item in squads}
    entities = [
        *(
            normalize_entity(
                "campaign",
                item,
                account_id=account_id,
                account_key=account_key,
                observed_at=observed_at,
            )
            for item in campaigns
        ),
        *(
            normalize_entity(
                "ad_squad",
                item,
                account_id=account_id,
                account_key=account_key,
                observed_at=observed_at,
            )
            for item in squads
        ),
        *(
            normalize_entity(
                "ad",
                item,
                account_id=account_id,
                account_key=account_key,
                observed_at=observed_at,
                squads_by_id=squads_by_id,
            )
            for item in ads
        ),
    ]
    entities.sort(key=lambda row: (row["entity_type"], row["entity_id"]))
    return account_id, entities


def audit_event_key(event: dict[str, Any]) -> str:
    safe_identity = {
        "timestamp": iso_timestamp(event.get("timestamp")),
        "tool": event.get("tool"),
        "entity_type": event.get("entity_type"),
        "entity_id": event.get("entity_id"),
        "field_name": event.get("field_name"),
        "new_value": event.get("new_value"),
    }
    safe_identity = scrub_sensitive_value(safe_identity)
    return hashlib.sha256(canonical_json(safe_identity).encode("utf-8")).hexdigest()


def _audit_mutation(
    *,
    timestamp: dt.datetime,
    tool: str,
    entity_type: str,
    entity_id: str,
    field_name: str,
    raw_value: Any,
) -> dict[str, Any] | None:
    if field_name in {"daily_budget", "bid", "target_cpa"}:
        new_value = money_from_micro(raw_value)
        if new_value is None:
            return None
    elif field_name in {"entity_name", "configured_status", "bid_strategy"}:
        new_value = (
            scrub_sensitive_text(raw_value)
            if raw_value is not None
            else None
        )
    else:
        return None
    event = {
        "timestamp": timestamp,
        "tool": tool,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "field_name": field_name,
        "new_value": new_value,
    }
    event = _scrub_modeled_row(event, label="local audit event")
    event["source_event_key"] = audit_event_key(event)
    return event


def load_local_audit_events(
    path: Path,
    *,
    account_key: str,
) -> list[dict[str, Any]]:
    """Read only the small allowlisted mutation surface from the CLI log.

    The returned records never include raw params, file paths, tokens, or
    credential-like keys. Malformed and unrelated audit lines are ignored.
    """
    if not path.exists():
        return []
    mutations: list[dict[str, Any]] = []
    try:
        audit_file = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise SyncError(
            f"Could not read local Snap audit log: {exc.__class__.__name__}"
        ) from exc

    with audit_file:
        for line in audit_file:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("result") != "ok":
                continue
            if str(entry.get("account") or "") != account_key:
                continue
            timestamp = parse_timestamp(entry.get("ts"))
            tool = str(entry.get("tool") or "")
            params = entry.get("params")
            if timestamp is None or not isinstance(params, dict):
                continue

            if tool in {"ad.bulk_pause", "ad.bulk_launch"}:
                status = "PAUSED" if tool.endswith("pause") else "ACTIVE"
                entity_ids = params.get("ad_ids")
                if not isinstance(entity_ids, list):
                    continue
                for entity_id in entity_ids:
                    mutation = _audit_mutation(
                        timestamp=timestamp,
                        tool=tool,
                        entity_type="ad",
                        entity_id=str(entity_id),
                        field_name="configured_status",
                        raw_value=status,
                    )
                    if mutation:
                        mutations.append(mutation)
                continue

            prefix = tool.split(".", 1)[0]
            entity_type = {
                "campaign": "campaign",
                "adsquad": "ad_squad",
                "ad": "ad",
            }.get(prefix)
            if entity_type is None or not tool.endswith(".update"):
                continue
            entity_id = str(params.get("id") or "")
            fields = params.get("fields")
            if not entity_id or not isinstance(fields, dict):
                continue
            for raw_field, raw_value in fields.items():
                field_names = AUDIT_FIELD_MAP.get(str(raw_field))
                if not field_names:
                    continue
                if isinstance(field_names, str):
                    field_names = (field_names,)
                for field_name in field_names:
                    mutation = _audit_mutation(
                        timestamp=timestamp,
                        tool=tool,
                        entity_type=entity_type,
                        entity_id=entity_id,
                        field_name=field_name,
                        raw_value=raw_value,
                    )
                    if mutation:
                        mutations.append(mutation)

    mutations.sort(key=lambda item: item["timestamp"])
    return mutations


def state_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row["account_id"]), str(row["entity_type"]), str(row["entity_id"])


def values_equal(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)


def matching_audit_event(
    audits: Iterable[dict[str, Any]],
    *,
    entity: dict[str, Any],
    prior: dict[str, Any],
    field_name: str,
    new_value: Any,
    observed_at: dt.datetime,
) -> dict[str, Any] | None:
    prior_seen = parse_timestamp(prior.get("last_seen_at"))
    lower_bound = (
        prior_seen or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    ) - dt.timedelta(minutes=5)
    upper_bound = observed_at + dt.timedelta(minutes=5)
    matches = [
        audit
        for audit in audits
        if audit.get("entity_type") == entity.get("entity_type")
        and audit.get("entity_id") == entity.get("entity_id")
        and audit.get("field_name") == field_name
        and values_equal(audit.get("new_value"), new_value)
        and lower_bound <= audit["timestamp"] <= upper_bound
    ]
    return max(matches, key=lambda item: item["timestamp"]) if matches else None


def classify_change(
    *,
    entity: dict[str, Any],
    prior: dict[str, Any],
    field_name: str,
    new_value: Any,
    audits: Iterable[dict[str, Any]],
    observed_at: dt.datetime,
) -> tuple[str, dict[str, Any] | None]:
    if field_name in SYSTEM_FIELDS:
        return "SNAP_SYSTEM", None
    matched = matching_audit_event(
        audits,
        entity=entity,
        prior=prior,
        field_name=field_name,
        new_value=new_value,
        observed_at=observed_at,
    )
    if matched:
        return "AGENT_API", matched
    return "HUMAN_MANUAL", None


def change_occurred_at(
    *,
    entity: dict[str, Any],
    prior: dict[str, Any],
    source: str,
    matched_audit: dict[str, Any] | None,
    observed_at: dt.datetime,
) -> dt.datetime:
    if matched_audit:
        return matched_audit["timestamp"]
    if source == "SNAP_SYSTEM":
        return observed_at
    current_updated = parse_timestamp(entity.get("source_updated_at"))
    prior_updated = parse_timestamp(prior.get("source_updated_at"))
    if current_updated and (prior_updated is None or current_updated > prior_updated):
        if current_updated <= observed_at + dt.timedelta(minutes=5):
            return current_updated
    return observed_at


def history_event_key(event: dict[str, Any]) -> str:
    identity = {
        "account_id": event["account_id"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "field_name": event["field_name"],
        "old_value": event["old_value"],
        "new_value": event["new_value"],
        "source": event["source"],
        "occurred_at": iso_timestamp(event["occurred_at"]),
    }
    identity = scrub_sensitive_value(identity)
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def plan_sync(
    entities: list[dict[str, Any]],
    prior_rows: Iterable[dict[str, Any]],
    audits: Iterable[dict[str, Any]],
    *,
    observed_at: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Return state upserts, new history events, and baseline entity count."""
    prior_by_key = {
        state_key(row): _scrub_modeled_row(dict(row), label="prior entity state")
        for row in prior_rows
    }
    incoming_by_key = {
        state_key(row): _scrub_modeled_row(dict(row), label="incoming entity state")
        for row in entities
    }
    newer_prior_rows = [
        (key, prior_seen)
        for key, row in prior_by_key.items()
        if (prior_seen := parse_timestamp(row.get("last_seen_at"))) is not None
        and prior_seen > observed_at
    ]
    if newer_prior_rows:
        newest_key, newest_seen = max(newer_prior_rows, key=lambda item: item[1])
        raise SyncError(
            "Refusing a stale Snapchat snapshot observed at "
            f"{iso_timestamp(observed_at)} because stored state for "
            f"{newest_key[1]} {newest_key[2]} was already observed at "
            f"{iso_timestamp(newest_seen)}"
        )
    equal_timestamp_conflicts = []
    for key, prior in prior_by_key.items():
        incoming = incoming_by_key.get(key)
        prior_seen = parse_timestamp(prior.get("last_seen_at"))
        if incoming is None or prior_seen != observed_at:
            continue
        prior_snapshot = {
            field: prior.get(field)
            for field in prior
            if field not in {"first_seen_at", "last_seen_at"}
        }
        incoming_snapshot = {
            field: incoming.get(field)
            for field in incoming
            if field not in {"first_seen_at", "last_seen_at"}
        }
        if not values_equal(prior_snapshot, incoming_snapshot):
            equal_timestamp_conflicts.append(key)
    if equal_timestamp_conflicts:
        conflict = sorted(equal_timestamp_conflicts)[0]
        raise SyncError(
            "Refusing conflicting Snapchat snapshots with the same observed_at "
            f"for {conflict[1]} {conflict[2]} at {iso_timestamp(observed_at)}"
        )
    state_rows: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    baseline_count = 0

    for raw_incoming in entities:
        entity = _scrub_modeled_row(
            dict(raw_incoming),
            label="incoming entity state",
        )
        prior = prior_by_key.get(state_key(entity))
        if prior is None:
            baseline_count += 1
            entity["first_seen_at"] = observed_at
            entity["last_seen_at"] = observed_at
            state_rows.append(entity)
            continue

        entity["first_seen_at"] = (
            parse_timestamp(prior.get("first_seen_at")) or observed_at
        )
        entity["last_seen_at"] = observed_at
        for field_name in DIFF_FIELDS:
            old_value = scrub_sensitive_value(canonicalize(prior.get(field_name)))
            new_value = scrub_sensitive_value(canonicalize(entity.get(field_name)))
            if values_equal(old_value, new_value):
                continue
            source, matched = classify_change(
                entity=entity,
                prior=prior,
                field_name=field_name,
                new_value=new_value,
                audits=audits,
                observed_at=observed_at,
            )
            occurred_at = change_occurred_at(
                entity=entity,
                prior=prior,
                source=source,
                matched_audit=matched,
                observed_at=observed_at,
            )
            metadata = {
                "classification": (
                    "matched_local_cli_audit"
                    if matched
                    else "snap_delivery_or_review_diff"
                    if source == "SNAP_SYSTEM"
                    else "unmatched_configuration_diff"
                )
            }
            event = {
                "account_id": entity["account_id"],
                "account_key": entity["account_key"],
                "entity_type": entity["entity_type"],
                "entity_id": entity["entity_id"],
                "entity_name": entity.get("entity_name"),
                "field_name": field_name,
                "old_value": old_value,
                "new_value": new_value,
                "source": source,
                "occurred_at": occurred_at,
                "observed_at": observed_at,
                "source_tool": matched.get("tool") if matched else None,
                "source_event_key": matched.get("source_event_key")
                if matched
                else None,
                "metadata": metadata,
            }
            event = _scrub_modeled_row(event, label="history event")
            event["event_key"] = history_event_key(event)
            events.append(event)
        state_rows.append(entity)

    events.sort(
        key=lambda row: (
            row["occurred_at"],
            row["entity_type"],
            row["entity_id"],
            row["field_name"],
        )
    )
    return state_rows, events, baseline_count


STATE_COLUMNS = (
    "account_id",
    "account_key",
    "entity_type",
    "entity_id",
    "entity_name",
    "campaign_id",
    "ad_squad_id",
    "configured_status",
    "effective_status",
    "delivery_status",
    "review_status",
    "daily_budget",
    "daily_budget_micro",
    "bid",
    "bid_micro",
    "target_cpa",
    "target_cpa_micro",
    "bid_strategy",
    "source_created_at",
    "source_updated_at",
    "raw_payload",
    "first_seen_at",
    "last_seen_at",
)


def table_exists(
    conn: Any,
    table_name: str,
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> bool:
    relation = qualified_table(warehouse_schema, table_name)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (relation,))
        row = cur.fetchone()
    return bool(row and row[0])


def load_prior_states(
    conn: Any,
    account_id: str,
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> list[dict[str, Any]]:
    columns = ", ".join(STATE_COLUMNS)
    state_table = qualified_table(warehouse_schema, "snapchat_entity_state")
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns} FROM {state_table} WHERE account_id = %s",
            (account_id,),
        )
        names = [description.name for description in cur.description]
        rows = cur.fetchall()
    return [dict(zip(names, row)) for row in rows]


def acquire_sync_lock(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise SyncError(
            "Another Snapchat entity sync currently holds the transaction lock"
        )


def apply_schema(
    conn: Any,
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> None:
    rendered = render_schema_sql(
        SCHEMA_SQL.read_text(encoding="utf-8"),
        warehouse_schema,
    )
    with conn.cursor() as cur:
        cur.execute(rendered)


def upsert_entity_states(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> int:
    if not rows:
        return 0
    insert_columns = ", ".join(STATE_COLUMNS)
    placeholders = ", ".join(
        f"%({column})s::jsonb"
        if column in {"delivery_status", "raw_payload"}
        else f"%({column})s"
        for column in STATE_COLUMNS
    )
    update_columns = [
        column
        for column in STATE_COLUMNS
        if column not in {"account_id", "entity_type", "entity_id", "first_seen_at"}
    ]
    update_clause = ", ".join(
        f"{column} = EXCLUDED.{column}" for column in update_columns
    )
    state_table = qualified_table(warehouse_schema, "snapchat_entity_state")
    sql = (
        f"INSERT INTO {state_table} AS current_state "
        f"({insert_columns}) VALUES ({placeholders}) "
        "ON CONFLICT (account_id, entity_type, entity_id) DO UPDATE SET "
        f"{update_clause} "
        "WHERE current_state.last_seen_at < EXCLUDED.last_seen_at"
    )
    prepared = []
    for row in rows:
        item = _scrub_modeled_row(dict(row), label="entity state write")
        item["delivery_status"] = canonical_json(item.get("delivery_status") or [])
        item["raw_payload"] = canonical_json(item.get("raw_payload") or {})
        prepared.append(item)
    written = 0
    with conn.cursor() as cur:
        for item in prepared:
            cur.execute(sql, item)
            written += max(cur.rowcount, 0)
    return written


def insert_history_events(
    conn: Any,
    events: list[dict[str, Any]],
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> int:
    if not events:
        return 0
    history_table = qualified_table(warehouse_schema, "snapchat_change_history")
    statement = f"""
        INSERT INTO {history_table} (
            event_key, account_id, account_key, entity_type, entity_id,
            entity_name, field_name, old_value, new_value, source,
            occurred_at, observed_at, source_tool, source_event_key, metadata
        ) VALUES (
            %(event_key)s, %(account_id)s, %(account_key)s, %(entity_type)s,
            %(entity_id)s, %(entity_name)s, %(field_name)s,
            %(old_value)s::jsonb, %(new_value)s::jsonb, %(source)s,
            %(occurred_at)s, %(observed_at)s, %(source_tool)s,
            %(source_event_key)s, %(metadata)s::jsonb
        )
        ON CONFLICT (event_key) DO NOTHING
    """
    inserted = 0
    with conn.cursor() as cur:
        for event in events:
            item = _scrub_modeled_row(dict(event), label="history event write")
            item["old_value"] = canonical_json(item.get("old_value"))
            item["new_value"] = canonical_json(item.get("new_value"))
            item["metadata"] = canonical_json(item.get("metadata") or {})
            cur.execute(statement, item)
            inserted += max(cur.rowcount, 0)
    return inserted


def recent_change_history_sql(
    days: int = 14,
    account_id: str | None = None,
    *,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
) -> str:
    if days < 1 or days > 365:
        raise ValueError("days must be between 1 and 365")
    account_clause = ""
    if account_id:
        escaped_account_id = account_id.replace("'", "''")
        account_clause = f"  AND account_id = '{escaped_account_id}'\n"
    history_table = qualified_table(warehouse_schema, "snapchat_change_history")
    return f"""SELECT
  entity_type,
  entity_id,
  entity_name,
  field_name,
  old_value,
  new_value,
  source,
  occurred_at,
  observed_at,
  event_key
FROM {history_table}
WHERE entity_type = 'ad_squad'
{account_clause}  AND field_name IN ('target_cpa', 'daily_budget', 'configured_status')
  AND occurred_at >= pg_catalog.now() - interval '{days} days'
ORDER BY occurred_at DESC, event_key;"""


def preview_event(event: dict[str, Any]) -> dict[str, Any]:
    return _scrub_modeled_row({
        "event_key": event["event_key"],
        "entity_type": event["entity_type"],
        "entity_id": event["entity_id"],
        "entity_name": event.get("entity_name"),
        "field_name": event["field_name"],
        "old_value": event.get("old_value"),
        "new_value": event.get("new_value"),
        "source": event["source"],
        "occurred_at": iso_timestamp(event["occurred_at"]),
        "observed_at": iso_timestamp(event["observed_at"]),
        "source_tool": event.get("source_tool"),
    }, label="history event preview")


def summary_payload(
    *,
    execute: bool,
    account_id: str,
    entities: list[dict[str, Any]],
    events: list[dict[str, Any]],
    baseline_count: int,
    audit_event_count: int,
    database_state_loaded: bool,
    schema_ready: bool,
    warehouse_schema: str = DEFAULT_WAREHOUSE_SCHEMA,
    states_written: int = 0,
    events_inserted: int = 0,
) -> dict[str, Any]:
    entity_counts = {
        entity_type: sum(1 for row in entities if row["entity_type"] == entity_type)
        for entity_type in ENTITY_TYPES
    }
    source_counts = {
        source: sum(1 for event in events if event["source"] == source)
        for source in ("HUMAN_MANUAL", "AGENT_API", "SNAP_SYSTEM")
    }
    payload = {
        "mode": "execute" if execute else "dry_run",
        "writes_performed": execute,
        "warehouse_schema": warehouse_schema,
        "account_id": account_id,
        "entity_counts": entity_counts,
        "entity_count": len(entities),
        "baseline_entity_count": baseline_count,
        "planned_change_count": len(events),
        "planned_change_sources": source_counts,
        "local_audit_mutation_count": audit_event_count,
        "database_state_loaded": database_state_loaded,
        "schema_ready": schema_ready,
        "states_written": states_written,
        "history_events_inserted": events_inserted,
        "planned_changes": [preview_event(event) for event in events[:100]],
        "planned_changes_truncated": len(events) > 100,
        "recent_change_history_contract": {
            "table": qualified_table(
                warehouse_schema,
                "snapchat_change_history",
            ),
            "fields": [
                "entity_type",
                "entity_id",
                "entity_name",
                "field_name",
                "old_value",
                "new_value",
                "source",
                "occurred_at",
                "observed_at",
                "event_key",
            ],
            "query": recent_change_history_sql(
                14,
                account_id=account_id,
                warehouse_schema=warehouse_schema,
            ),
        },
    }
    cleaned = scrub_sensitive_value(payload)
    if not isinstance(cleaned, dict):
        raise SyncError("summary scrubber returned a non-object")
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror Snapchat campaign/ad squad/ad state and classify field changes."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Write state and history in one locked transaction.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly select the default read-only mode.",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Apply warehouse/schema.sql inside the execute transaction.",
    )
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL.")
    parser.add_argument(
        "--warehouse-schema",
        default=os.environ.get(
            "SNAP_WAREHOUSE_SCHEMA",
            DEFAULT_WAREHOUSE_SCHEMA,
        ),
        help=(
            "Validated PostgreSQL schema for every warehouse relation "
            f"(default {DEFAULT_WAREHOUSE_SCHEMA})."
        ),
    )
    parser.add_argument(
        "--account",
        default=os.environ.get("SNAPCHAT_ADS_ACCOUNT", "default"),
        help="Configured snapchat-ads CLI account key.",
    )
    parser.add_argument(
        "--audit-file",
        type=Path,
        default=DEFAULT_AUDIT_FILE,
        help="Local snapchat-ads CLI audit JSONL path.",
    )
    args = parser.parse_args()
    if args.apply_schema and not args.execute:
        parser.error(
            "--apply-schema requires --execute because dry-run never writes DDL"
        )
    try:
        warehouse_schema = validate_warehouse_schema(args.warehouse_schema)
    except WarehouseSchemaError as exc:
        parser.error(str(exc))

    observed_at = utc_now()
    try:
        account_id, entities = fetch_live_entities(args.account, observed_at)
        audits = load_local_audit_events(args.audit_file, account_key=args.account)
    except (SyncError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": redact_error_text(str(exc))}), file=sys.stderr)
        raise SystemExit(1) from exc

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    prior_rows: list[dict[str, Any]] = []
    database_state_loaded = False
    schema_ready = False

    if not args.execute:
        if database_url:
            import psycopg

            try:
                with psycopg.connect(database_url) as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            cur.execute("SET TRANSACTION READ ONLY")
                        state_table_ready = table_exists(
                            conn,
                            "snapchat_entity_state",
                            warehouse_schema=warehouse_schema,
                        )
                        history_table_ready = table_exists(
                            conn,
                            "snapchat_change_history",
                            warehouse_schema=warehouse_schema,
                        )
                        schema_ready = state_table_ready and history_table_ready
                        if state_table_ready:
                            prior_rows = load_prior_states(
                                conn,
                                account_id,
                                warehouse_schema=warehouse_schema,
                            )
                            database_state_loaded = True
            except psycopg.Error as exc:
                error = redact_error_text(str(exc))
                print(
                    json.dumps({"error": f"Read-only database check failed: {error}"}),
                    file=sys.stderr,
                )
                raise SystemExit(1) from exc
        try:
            states, events, baseline_count = plan_sync(
                entities,
                prior_rows,
                audits,
                observed_at=observed_at,
            )
        except SyncError as exc:
            print(json.dumps({"error": redact_error_text(str(exc))}), file=sys.stderr)
            raise SystemExit(1) from exc
        print(
            json.dumps(
                summary_payload(
                    execute=False,
                    account_id=account_id,
                    entities=states,
                    events=events,
                    baseline_count=baseline_count,
                    audit_event_count=len(audits),
                    database_state_loaded=database_state_loaded,
                    schema_ready=schema_ready,
                    warehouse_schema=warehouse_schema,
                ),
                indent=2,
                default=str,
            )
        )
        return

    if not database_url:
        print(
            json.dumps({"error": "DATABASE_URL is required with --execute"}),
            file=sys.stderr,
        )
        raise SystemExit(2)

    import psycopg

    try:
        with psycopg.connect(database_url) as conn:
            with conn.transaction():
                acquire_sync_lock(conn)
                if args.apply_schema:
                    apply_schema(
                        conn,
                        warehouse_schema=warehouse_schema,
                    )
                schema_ready = table_exists(
                    conn,
                    "snapchat_entity_state",
                    warehouse_schema=warehouse_schema,
                ) and table_exists(
                    conn,
                    "snapchat_change_history",
                    warehouse_schema=warehouse_schema,
                )
                if not schema_ready:
                    raise SyncError(
                        "Entity tables are missing. Preview, approve, then rerun with --execute --apply-schema"
                    )
                prior_rows = load_prior_states(
                    conn,
                    account_id,
                    warehouse_schema=warehouse_schema,
                )
                database_state_loaded = True
                states, events, baseline_count = plan_sync(
                    entities,
                    prior_rows,
                    audits,
                    observed_at=observed_at,
                )
                events_inserted = insert_history_events(
                    conn,
                    events,
                    warehouse_schema=warehouse_schema,
                )
                states_written = upsert_entity_states(
                    conn,
                    states,
                    warehouse_schema=warehouse_schema,
                )
    except (psycopg.Error, SyncError, WarehouseSchemaError) as exc:
        print(json.dumps({"error": redact_error_text(str(exc))}), file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        json.dumps(
            summary_payload(
                execute=True,
                account_id=account_id,
                entities=states,
                events=events,
                baseline_count=baseline_count,
                audit_event_count=len(audits),
                database_state_loaded=database_state_loaded,
                schema_ready=schema_ready,
                warehouse_schema=warehouse_schema,
                states_written=states_written,
                events_inserted=events_inserted,
            ),
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
