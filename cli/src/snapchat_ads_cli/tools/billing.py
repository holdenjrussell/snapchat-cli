"""Invoices + transactions (read-only)."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def list_invoices(client: SnapchatApiClient, organization_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{organization_id}/invoices", "invoices"
    )
    return {"invoices": items, "count": len(items), "organization_id": organization_id}


def get_invoice(client: SnapchatApiClient, invoice_id: str) -> dict[str, Any]:
    body, _ = client.get(f"invoices/{invoice_id}")
    return body


def list_transactions(client: SnapchatApiClient, organization_id: str) -> dict[str, Any]:
    items = client.collect_paginated(
        f"organizations/{organization_id}/transactions", "transactions"
    )
    return {"transactions": items, "count": len(items), "organization_id": organization_id}


def get_transaction(client: SnapchatApiClient, transaction_id: str) -> dict[str, Any]:
    body, _ = client.get(f"transactions/{transaction_id}")
    return body
