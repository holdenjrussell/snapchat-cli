"""Snapchat Marketing API HTTP client.

Sync httpx wrapper with retry/backoff, cursor pagination, error normalization,
async report polling. 401 with invalid_token triggers a single auto-refresh.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Callable, Iterator

import httpx

from .config import BASE_URL

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BASE_DELAY = 1.0
MAX_BACKOFF = 60.0
DEFAULT_TIMEOUT = 30.0
UPLOAD_TIMEOUT = 300.0


class SnapApiError(Exception):
    """Snap API returned a non-retryable error."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        error_code: str = "",
        request_id: str = "",
        body: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.body = body or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "message": str(self),
                "status_code": self.status_code,
                "error_code": self.error_code,
                "request_id": self.request_id,
                "body": self.body,
            }
        }


class SnapAuthError(SnapApiError):
    """Token is missing, expired, or revoked. Caller should re-auth."""


def _is_retryable_status(status: int) -> bool:
    return status == 429 or 500 <= status < 600


def _backoff_delay(attempt: int, retry_after: float | None = None) -> float:
    if retry_after is not None and retry_after > 0:
        jitter = random.uniform(0.5, 2.0)
        return min(retry_after + jitter, MAX_BACKOFF + 30)
    delay = RETRY_BASE_DELAY * (2 ** attempt)
    jitter = random.uniform(0.0, RETRY_BASE_DELAY)
    return min(delay + jitter, MAX_BACKOFF)


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _normalize_error(
    body: dict[str, Any] | None, status_code: int, headers: dict[str, str]
) -> SnapApiError:
    body = body or {}
    msg = (
        body.get("debug_message")
        or body.get("error_description")
        or body.get("error")
        or body.get("message")
        or f"HTTP {status_code}"
    )
    if isinstance(msg, dict):
        msg = json.dumps(msg)
    code = str(body.get("error_code") or body.get("error") or "")
    req_id = body.get("request_id") or headers.get("x-snap-request-id", "")
    if status_code == 401 or code in {"invalid_token", "expired_token"}:
        return SnapAuthError(str(msg), status_code, code, req_id, body)
    return SnapApiError(str(msg), status_code, code, req_id, body)


class SnapchatApiClient:
    """Snap Marketing API client.

    Construct with an access_token. If `refresh_callback` is provided and a
    request returns 401 invalid_token, the client invokes it once to obtain a
    new access token, then retries the failed request a single time.
    """

    def __init__(
        self,
        access_token: str,
        refresh_callback: Callable[[], str] | None = None,
        base_url: str = BASE_URL,
    ):
        self.access_token = access_token
        self._refresh_callback = refresh_callback
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)

    def __enter__(self) -> "SnapchatApiClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _full_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
        _retried_after_refresh: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Execute one HTTP request with retry/backoff.

        Returns (body, lowercased_headers). Raises SnapApiError on
        non-retryable failures or after MAX_RETRIES.
        """
        url = self._full_url(path)
        timeout = timeout if timeout is not None else DEFAULT_TIMEOUT

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                    headers=self._headers(),
                    timeout=timeout,
                )
            except httpx.TimeoutException as e:
                if attempt < MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning("Timeout on %s %s, retry in %.1fs", method, path, delay)
                    time.sleep(delay)
                    continue
                raise SnapApiError(f"Timeout: {e}", 0, "timeout") from e
            except httpx.HTTPError as e:
                if attempt < MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning("HTTP error on %s %s: %s, retry in %.1fs", method, path, e, delay)
                    time.sleep(delay)
                    continue
                raise SnapApiError(f"HTTP error: {e}", 0, "transport") from e

            headers = {k.lower(): v for k, v in resp.headers.items()}

            if _is_retryable_status(resp.status_code) and attempt < MAX_RETRIES:
                delay = _backoff_delay(attempt, _parse_retry_after(headers))
                logger.warning(
                    "HTTP %d on %s %s, retry in %.1fs (attempt %d/%d)",
                    resp.status_code,
                    method,
                    path,
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(delay)
                continue

            try:
                body = resp.json() if resp.content else {}
            except (ValueError, json.JSONDecodeError):
                body = {"raw": resp.text}

            if resp.status_code == 401 and not _retried_after_refresh and self._refresh_callback:
                try:
                    new_token = self._refresh_callback()
                except Exception as e:
                    raise SnapAuthError(
                        f"Token refresh failed: {e}", 401, "refresh_failed"
                    ) from e
                if new_token:
                    self.access_token = new_token
                    return self.request(
                        method,
                        path,
                        params=params,
                        json_body=json_body,
                        data=data,
                        files=files,
                        timeout=timeout,
                        _retried_after_refresh=True,
                    )

            if resp.status_code >= 400:
                raise _normalize_error(body if isinstance(body, dict) else None, resp.status_code, headers)

            if isinstance(body, dict) and body.get("request_status") == "ERROR":
                raise _normalize_error(body, resp.status_code, headers)

            return body if isinstance(body, dict) else {"data": body}, headers

        raise SnapApiError("Max retries exceeded", 0, "max_retries")

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return self.request("GET", path, params=params, timeout=timeout)

    def post(
        self,
        path: str,
        json_body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return self.request(
            "POST", path, params=params, json_body=json_body, timeout=timeout
        )

    def put(
        self,
        path: str,
        json_body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return self.request(
            "PUT", path, params=params, json_body=json_body, timeout=timeout
        )

    def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return self.request("DELETE", path, params=params, timeout=timeout)

    def post_multipart(
        self,
        path: str,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = UPLOAD_TIMEOUT,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        return self.request(
            "POST", path, params=params, data=data, files=files or {}, timeout=timeout
        )

    def get_paginated(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Yield response bodies page by page until paging.next_link is empty.

        Snap returns `paging: {next_link: "..."}` -- next_link is a full URL we
        re-issue verbatim.
        """
        next_url: str | None = None
        page_count = 0
        while page_count < max_pages:
            if next_url is None:
                body, _ = self.get(path, params=params)
            else:
                body, _ = self.get(next_url)
            yield body
            paging = body.get("paging") or {}
            next_url = paging.get("next_link")
            if not next_url:
                return
            page_count += 1

    def collect_paginated(
        self,
        path: str,
        item_key: str,
        params: dict[str, Any] | None = None,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        """Walk pagination and flatten the nested entity list under `item_key`.

        Snap envelopes are like `{"campaigns": [{"campaign": {...}}, ...]}`.
        Each item is unwrapped one level so the caller gets a flat list of
        entity dicts.
        """
        out: list[dict[str, Any]] = []
        for body in self.get_paginated(path, params=params, max_pages=max_pages):
            entries = body.get(item_key, [])
            for e in entries:
                if isinstance(e, dict) and len(e) == 1:
                    inner_key = next(iter(e))
                    inner = e[inner_key]
                    if isinstance(inner, dict):
                        out.append(inner)
                        continue
                if isinstance(e, dict):
                    out.append(e)
        return out

    def poll_report(
        self,
        report_run_id: str,
        max_wait: float = 600.0,
        interval: float = 5.0,
    ) -> dict[str, Any]:
        """Poll an async report job until it completes or times out."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            body, _ = self.get(f"reports/{report_run_id}")
            report = (body.get("report") or body).get("report", body.get("report", body))
            status = (
                report.get("status")
                or body.get("status")
                or body.get("request_status")
            )
            if status in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
                return body
            if status in {"FAILED", "ERROR", "CANCELLED"}:
                raise SnapApiError(
                    f"Report {report_run_id} failed with status {status}",
                    0,
                    "report_failed",
                    "",
                    body,
                )
            time.sleep(interval)
        raise SnapApiError(
            f"Report {report_run_id} did not complete within {max_wait}s",
            0,
            "report_timeout",
        )
