"""OAuth 2.0 authorization-code flow + token refresh.

Snap docs: https://developers.snap.com/api/marketing-api/Ads-API/authentication

The CLI's auth model:
  1. `auth login` prints the authorize URL, prompts for the code returned to
     the redirect URI, exchanges the code for access+refresh tokens, and writes
     them to the account's token_source path.
  2. `auth refresh` reads the stored refresh token and rotates the access
     token. The api_client also calls this automatically on a single 401.
  3. `auth status` reports token presence, expiry, and a masked tail.
  4. `auth revoke` posts to revoke and deletes the local token file.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import OAUTH_BASE, AccountConfig, AppConfig

logger = logging.getLogger(__name__)

AUTHORIZE_URL = f"{OAUTH_BASE}/authorize"
TOKEN_URL = f"{OAUTH_BASE}/access_token"
REVOKE_URL = f"{OAUTH_BASE}/revoke"


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str | None = None,
) -> str:
    """Build the URL the user opens in a browser to grant access."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    with httpx.Client(timeout=timeout) as c:
        resp = c.post(TOKEN_URL, data=payload)
    body = _safe_json(resp)
    if resp.status_code >= 400 or "error" in body:
        raise RuntimeError(
            f"Code exchange failed (HTTP {resp.status_code}): "
            f"{body.get('error_description') or body.get('error') or body}"
        )
    return _wrap_token_payload(body)


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Rotate the access token using the refresh token."""
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    with httpx.Client(timeout=timeout) as c:
        resp = c.post(TOKEN_URL, data=payload)
    body = _safe_json(resp)
    if resp.status_code >= 400 or "error" in body:
        raise RuntimeError(
            f"Token refresh failed (HTTP {resp.status_code}): "
            f"{body.get('error_description') or body.get('error') or body}"
        )
    out = _wrap_token_payload(body)
    if "refresh_token" not in out and refresh_token:
        out["refresh_token"] = refresh_token
    return out


def revoke_token(token: str, timeout: float = 30.0) -> dict[str, Any]:
    """Revoke an access or refresh token."""
    with httpx.Client(timeout=timeout) as c:
        resp = c.post(REVOKE_URL, data={"token": token})
    return {
        "status_code": resp.status_code,
        "body": _safe_json(resp),
    }


def make_refresh_callback(config: AppConfig, account: AccountConfig):
    """Return a zero-arg callable the api_client invokes on 401."""

    def _refresh() -> str:
        client_id = config.client_id
        client_secret = config.client_secret
        if not (client_id and client_secret):
            raise RuntimeError(
                "SNAPCHAT_CLIENT_ID / SNAPCHAT_CLIENT_SECRET not set; "
                "cannot auto-refresh"
            )
        rtoken = account.load_refresh_token()
        if not rtoken:
            raise RuntimeError(
                "No refresh token stored; run `snapchat-ads auth login`"
            )
        payload = refresh_access_token(client_id, client_secret, rtoken)
        account.save_token(payload)
        return payload["access_token"]

    return _refresh


def auth_status(account: AccountConfig) -> dict[str, Any]:
    """Report the on-disk token state for an account."""
    token_path = account.resolve_path(account.token_source)
    meta = account.load_token_metadata()
    access = meta.get("access_token") or account.load_token() or ""
    refresh = meta.get("refresh_token") or account.load_refresh_token() or ""
    expires_at = meta.get("expires_at")
    expires_in_human = "-"
    expires_in = None
    if expires_at:
        try:
            secs = float(expires_at) - time.time()
            expires_in = int(secs)
            expires_in_human = _format_duration(secs)
        except (TypeError, ValueError):
            pass

    has_access = bool(access)
    has_refresh = bool(refresh)
    if not has_access and not has_refresh:
        status = "NO_TOKEN"
    elif expires_in is not None and expires_in <= 0 and has_refresh:
        status = "EXPIRED_REFRESH_AVAILABLE"
    elif expires_in is not None and expires_in <= 300:
        status = "EXPIRING_SOON"
    elif has_access:
        status = "OK"
    else:
        status = "REFRESH_ONLY"

    return {
        "auth_status": status,
        "token_path": str(token_path) if token_path else "-",
        "has_access_token": has_access,
        "has_refresh_token": has_refresh,
        "expires_at": (
            datetime.fromtimestamp(float(expires_at), tz=timezone.utc).isoformat()
            if expires_at
            else None
        ),
        "expires_in_seconds": expires_in,
        "expires_in_human": expires_in_human,
        "scope": meta.get("scope"),
        "token_tail": access[-6:] if access else None,
    }


def _wrap_token_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Snap token response with an absolute expiry."""
    out = dict(body)
    expires_in = body.get("expires_in")
    if expires_in is not None:
        try:
            out["expires_at"] = int(time.time()) + int(expires_in)
        except (TypeError, ValueError):
            pass
    out["obtained_at"] = int(time.time())
    return out


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
        return {"data": data}
    except ValueError:
        return {"raw": resp.text}


def _format_duration(seconds: float) -> str:
    if seconds < 0:
        return "expired"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"
