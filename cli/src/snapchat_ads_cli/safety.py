"""Safety layer: two-phase mutations (preview -> execute), audit log, micro-currency.

Snapchat money values are micro-currency (1 USD = 1_000_000 micro). Convert at
the CLI boundary so commands accept dollars or cents and the API always sees
micro.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import AUDIT_FILE, CONFIG_DIR

logger = logging.getLogger(__name__)


def audit_log(
    tool: str,
    account: str,
    params: dict[str, Any],
    result: str,
    changes: dict[str, Any] | None = None,
) -> None:
    """Append a JSONL entry to the audit log."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "account": account,
        "params": params,
        "result": result,
    }
    if changes:
        entry["changes"] = changes
    try:
        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        logger.warning("Failed to write audit log: %s", e)


def format_preview(
    action: str,
    account: str,
    current_state: dict[str, Any] | None = None,
    proposed_state: dict[str, Any] | None = None,
    details: str = "",
) -> dict[str, Any]:
    """Format a mutation preview for user approval.

    Default shape for every write operation. The caller re-runs the command
    with --execute (sets execute=True) once the user approves.
    """
    preview: dict[str, Any] = {
        "status": "preview",
        "action": action,
        "account": account,
        "message": (
            f"This will {action}. Re-run with --execute to apply."
        ),
    }
    if current_state is not None:
        preview["current"] = current_state
    if proposed_state is not None:
        preview["proposed"] = proposed_state
    if details:
        preview["details"] = details
    return preview


def to_micro(value: int | float | str | Decimal) -> int:
    """Convert a dollar value to micro-currency (1 USD = 1_000_000 micro).

    Accepts:
      - int/float dollars (5 -> 5_000_000)
      - str dollars ("5.25" -> 5_250_000)
      - str with explicit "micro:" prefix (no conversion)
      - str with "$" prefix (stripped)
    """
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("micro:"):
            return int(v[6:])
        v = v.lstrip("$").replace(",", "")
        try:
            dec = Decimal(v)
        except InvalidOperation as e:
            raise ValueError(f"Cannot parse '{value}' as money") from e
        return int(dec * 1_000_000)
    if isinstance(value, Decimal):
        return int(value * 1_000_000)
    return int(float(value) * 1_000_000)


def from_micro(micro: int | str | None) -> float:
    """Convert micro-currency to dollars as float (display use only)."""
    if micro is None:
        return 0.0
    try:
        return int(micro) / 1_000_000
    except (ValueError, TypeError):
        return 0.0


def format_micro(micro: int | str | None) -> str:
    """Format micro-currency as a dollar string ($1,234.56)."""
    return f"${from_micro(micro):,.2f}"
