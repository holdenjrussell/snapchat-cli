"""Reach/frequency, bid estimate, audience size, ad-squad outcomes."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def reach_frequency_schedule(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/reach_frequency_schedule",
        json_body=payload,
    )
    return body


def bid_estimate(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/bid_estimate",
        json_body=payload,
    )
    return body


def audience_size(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/audience_size",
        json_body=payload,
    )
    return body


def adsquad_outcomes(
    client: SnapchatApiClient,
    ad_account_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body, _ = client.post(
        f"adaccounts/{ad_account_id}/adsquad_outcomes",
        json_body=payload,
    )
    return body
