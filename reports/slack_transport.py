#!/usr/bin/env python3
"""Pinned, no-redirect Slack Web API transport for Snapchat reports."""

from __future__ import annotations

import json
import math
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

SLACK_HOST = "slack.com"
SLACK_ORIGIN = "https://slack.com"
CHAT_POST_MESSAGE_PATH = "/api/chat.postMessage"
CONVERSATIONS_REPLIES_PATH = "/api/conversations.replies"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


class SlackTransportError(RuntimeError):
    """Constant-shape transport failure that never contains credentials."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise SlackTransportError("Slack redirect rejected")

    def _reject(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any) -> None:
        raise SlackTransportError("Slack redirect rejected")

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


# Fixed OS bundle locations, tried only when the interpreter's compiled
# OpenSSL file is missing (e.g. standalone Python builds on Linux report
# /etc/ssl/cert.pem, which Debian/Ubuntu do not ship). Never environment-driven.
_OS_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/Alpine
    "/etc/pki/tls/certs/ca-bundle.crt",    # RHEL/Fedora
    "/etc/ssl/cert.pem",                   # macOS/BSD
)


def _is_file(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path)


def _is_dir(path: str | None) -> bool:
    return bool(path) and os.path.isdir(path)


def _tls_context() -> ssl.SSLContext:
    """Use compiled trust roots without honoring SSL_CERT_* overrides.

    Only compiled paths that exist are passed on; a missing compiled file
    falls back to a fixed list of OS bundles, so the roots never come from
    the environment."""
    defaults = ssl.get_default_verify_paths()
    cafile = defaults.openssl_cafile if _is_file(defaults.openssl_cafile) else None
    capath = defaults.openssl_capath if _is_dir(defaults.openssl_capath) else None
    if cafile is None:
        cafile = next((path for path in _OS_CA_BUNDLES if _is_file(path)), None)
    if not cafile and not capath:
        raise SlackTransportError("Slack TLS trust roots unavailable")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cafile=cafile, capath=capath)
    except (OSError, ssl.SSLError):
        raise SlackTransportError("Slack TLS trust roots unavailable") from None
    return context


def build_slack_opener() -> urllib.request.OpenerDirector:
    """Build an HTTPS opener with no ambient proxies and no redirects."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=_tls_context()),
    )


def _validated_token(token: str) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[\x21-\x7e]{1,8192}", token) is None:
        raise SlackTransportError("Slack token unavailable")
    return token


def _validated_url(path: str, *, query: dict[str, str] | None = None) -> str:
    if path not in {CHAT_POST_MESSAGE_PATH, CONVERSATIONS_REPLIES_PATH}:
        raise SlackTransportError("Slack API path rejected")
    encoded_query = urlencode(query or {})
    url = urlunsplit(("https", SLACK_HOST, path, encoded_query, ""))
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise SlackTransportError("Slack URL rejected") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != SLACK_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != path
    ):
        raise SlackTransportError("Slack URL rejected")
    return url


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON value")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON value")
        return parsed

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SlackTransportError("Slack response was not strict JSON") from None
    if not isinstance(value, dict):
        raise SlackTransportError("Slack response was not a JSON object")
    return value


def _open_json(
    request: urllib.request.Request,
    *,
    expected_url: str,
    timeout: int,
    max_response_bytes: int,
    opener: urllib.request.OpenerDirector | Any | None,
) -> dict[str, Any]:
    if not 1 <= int(timeout) <= 60:
        raise SlackTransportError("Slack timeout rejected")
    if not 1 <= int(max_response_bytes) <= 8 * 1024 * 1024:
        raise SlackTransportError("Slack response limit rejected")
    client = opener or build_slack_opener()
    try:
        with client.open(request, timeout=int(timeout)) as response:
            status = int(response.getcode())
            final_url = str(response.geturl() or "")
            content_type = str(response.headers.get("Content-Type") or "")
            if status != 200:
                raise SlackTransportError("Slack returned a non-200 status")
            if final_url != expected_url:
                raise SlackTransportError("Slack final origin changed")
            if content_type.partition(";")[0].strip().casefold() != "application/json":
                raise SlackTransportError("Slack returned a non-JSON content type")
            raw = response.read(int(max_response_bytes) + 1)
    except SlackTransportError:
        raise
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        ssl.SSLError,
        TypeError,
        ValueError,
        AttributeError,
    ):
        raise SlackTransportError("Slack request failed") from None
    if len(raw) > int(max_response_bytes):
        raise SlackTransportError("Slack response exceeded the byte limit")
    return _strict_json_object(raw)


def post_message(
    token: str,
    payload: dict[str, Any],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    opener: urllib.request.OpenerDirector | Any | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SlackTransportError("Slack payload rejected")
    try:
        body = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise SlackTransportError("Slack payload rejected") from None
    if len(body) > 4 * 1024 * 1024:
        raise SlackTransportError("Slack payload exceeded the byte limit")
    url = _validated_url(CHAT_POST_MESSAGE_PATH)
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + _validated_token(token),
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    return _open_json(
        request,
        expected_url=url,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        opener=opener,
    )


def conversations_replies(
    token: str,
    parameters: dict[str, str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = 4 * 1024 * 1024,
    opener: urllib.request.OpenerDirector | Any | None = None,
) -> dict[str, Any]:
    if not isinstance(parameters, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in parameters.items()
    ):
        raise SlackTransportError("Slack query rejected")
    url = _validated_url(CONVERSATIONS_REPLIES_PATH, query=parameters)
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": "Bearer " + _validated_token(token),
            "Accept": "application/json",
        },
    )
    return _open_json(
        request,
        expected_url=url,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        opener=opener,
    )


def slack_error_code(response: dict[str, Any]) -> str:
    """Return a bounded operator-safe Slack error label."""
    value = str((response or {}).get("error") or "unknown_error").strip()
    if re.fullmatch(r"[a-z0-9_.-]{1,80}", value.casefold()) is None:
        return "unknown_error"
    return value.casefold()


__all__ = [
    "CHAT_POST_MESSAGE_PATH",
    "CONVERSATIONS_REPLIES_PATH",
    "SlackTransportError",
    "build_slack_opener",
    "conversations_replies",
    "post_message",
    "slack_error_code",
]
