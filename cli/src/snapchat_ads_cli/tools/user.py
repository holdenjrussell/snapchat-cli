"""GET /v1/me -- the authenticated user."""

from __future__ import annotations

from typing import Any

from ..api_client import SnapchatApiClient


def me(client: SnapchatApiClient) -> dict[str, Any]:
    body, _ = client.get("me")
    inner = body.get("me") or body
    if isinstance(inner, dict) and "me" in inner:
        return inner["me"]
    return inner if isinstance(inner, dict) else body
