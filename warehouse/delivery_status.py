"""Canonical fail-closed Snapchat ACTIVE and delivery-state semantics."""

from __future__ import annotations

from typing import Any


def normalize_delivery_status(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value if item not in (None, "")]
    else:
        items = []
    return sorted({item.strip().upper() for item in items if item.strip()})


def delivery_is_active_valid(value: Any) -> bool:
    """Accept only the exact known-good delivery state.

    Mixed, pending, not-delivering, invalid, unknown, and missing states fail
    closed even when Snap also includes ``VALID``.
    """
    return normalize_delivery_status(value) == ["VALID"]


def entity_is_effectively_active(
    *,
    configured_status: Any,
    delivery_status: Any,
    review_status: Any = None,
    native_effective_status: Any = None,
) -> bool:
    if str(configured_status or "").strip().upper() != "ACTIVE":
        return False
    native = str(native_effective_status or "").strip().upper()
    if native and native != "ACTIVE":
        return False
    if not delivery_is_active_valid(delivery_status):
        return False
    review = str(review_status or "").strip().upper()
    return not review or review == "APPROVED"


def derive_effective_status(raw: dict[str, Any], delivery_status: Any) -> str:
    """Derive mirror state without letting native ACTIVE override blockers."""
    configured = str(raw.get("status") or "UNKNOWN").strip().upper()
    native = str(raw.get("effective_status") or "").strip().upper()
    review = str(raw.get("review_status") or "").strip().upper()
    if configured != "ACTIVE":
        return configured
    if review in {"PENDING", "PENDING_REVIEW", "IN_REVIEW"}:
        return "PENDING_REVIEW"
    if native and native != "ACTIVE":
        return native
    if entity_is_effectively_active(
        configured_status=configured,
        delivery_status=delivery_status,
        review_status=review,
        native_effective_status=native,
    ):
        return "ACTIVE"
    if normalize_delivery_status(delivery_status):
        return "NOT_DELIVERING"
    return "UNKNOWN"
