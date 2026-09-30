"""Canonical fail-closed secret scrubbing for Snapchat optimizer artifacts.

The optimizer moves provider, API, subprocess, agent, audit, and Slack values
through several independently validated layers. Every layer imports this one
module so redaction happens before truncation, hashing, prompting, persistence,
or user-visible rendering.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote_plus

REDACTION_TOKEN = "[REDACTED]"
MAX_SCRUB_DEPTH = 48
MAX_SCRUB_ITEMS = 100_000


class SecretScrubError(ValueError):
    """A constant-message failure that never includes the rejected value."""


_SAFE_METADATA_KEYS = frozenset(
    {
        "access_token_expires_at",
        "account_id",
        "ad_id",
        "ad_squad_id",
        "artifact_id",
        "client_id",
        "context_digest",
        "context_sha256",
        "credential_expires_at",
        "hash",
        "http_request_id",
        "invocation_id",
        "model",
        "model_id",
        "prompt_sha256",
        "provider_response_id",
        "request_id",
        "response_digest",
        "row_digest",
        "run_id",
        "session_id",
        "source_sha256",
        "token_budget",
        "token_count",
        "token_counts",
        "universe_digest",
        "x_client_request_id",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "authorization",
        "bearer",
        "bearer_token",
        "client_key",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "key",
        "password",
        "passwd",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "session_cookie",
        "session_secret",
        "session_token",
        "set_cookie",
        "token",
        "tokens",
        "x_access_token",
        "x_api_key",
        "x_auth_token",
    }
)

_SENSITIVE_QUERY_KEYS = frozenset(
    {
        *_SENSITIVE_KEYS,
        "auth",
        "code",
        "key",
        "oauth_code",
        "session",
        "sig",
        "signature",
        "state",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
    }
)

_SENSITIVE_KEY_SUFFIXES = (
    "_access_key",
    "_access_token",
    "_api_key",
    "_auth_token",
    "_client_key",
    "_client_secret",
    "_cookie",
    "_credential",
    "_credentials",
    "_id_token",
    "_password",
    "_passwd",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_session_cookie",
    "_session_secret",
    "_session_token",
    "_security_token",
    "_token",
)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
    r"-----END[A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_COOKIE_UNWRAPPED_LINE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_\"'-])((?:set-cookie|cookie)\s*:\s*)([^\r\n]+)"
)
_COOKIE_WRAPPED_HEADER_RE = re.compile(
    r"(?i)\b((?:set-cookie|cookie)\s*:\s*)([^\r\n\"']+)"
)
_AUTH_ESCAPED_DOUBLE_QUOTED_CREDENTIAL_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
    r"((?:bearer|basic|digest|negotiate|ntlm|token|hawk|"
    r"aws4-hmac-sha256)\s+)?"
    r'\\"(?:\\\\.|[^"\\])*\\"'
)
_AUTH_ESCAPED_SINGLE_QUOTED_CREDENTIAL_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
    r"((?:bearer|basic|digest|negotiate|ntlm|token|hawk|"
    r"aws4-hmac-sha256)\s+)?"
    r"\\'(?:\\\\.|[^'\\])*\\'"
)
_AUTH_DOUBLE_QUOTED_CREDENTIAL_RE = re.compile(
    r'(?i)\b((?:proxy-)?authorization\s*:\s*)'
    r'((?:bearer|basic|digest|negotiate|ntlm|token|hawk|'
    r'aws4-hmac-sha256)\s+)?'
    r'"(?:\\.|[^"\\])*"'
)
_AUTH_SINGLE_QUOTED_CREDENTIAL_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
    r"((?:bearer|basic|digest|negotiate|ntlm|token|hawk|"
    r"aws4-hmac-sha256)\s+)?"
    r"'(?:\\.|[^'\\])*'"
)
_AUTH_SCHEME_TOKEN_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
    r"((?:bearer|basic|digest|negotiate|ntlm|token|hawk|"
    r"aws4-hmac-sha256)\s+)"
    r"([^\s,;\"'\\]+)"
)
_AUTH_BARE_TOKEN_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*:\s*)"
    r"(?!(?:bearer|basic|digest|negotiate|ntlm|token|hawk|"
    r"aws4-hmac-sha256)(?:\s|$))"
    r"([^\s,;\"'\\]+)"
)
_SECRET_HEADER_ESCAPED_DOUBLE_QUOTED_RE = re.compile(
    r"(?i)\b((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|"
    r"x-auth-token|x-access-token|client-key)\s*:\s*)"
    r'\\"(?:\\\\.|[^"\\])*\\"'
)
_SECRET_HEADER_ESCAPED_SINGLE_QUOTED_RE = re.compile(
    r"(?i)\b((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|"
    r"x-auth-token|x-access-token|client-key)\s*:\s*)"
    r"\\'(?:\\\\.|[^'\\])*\\'"
)
_SECRET_HEADER_DOUBLE_QUOTED_RE = re.compile(
    r"(?i)\b((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|"
    r"x-auth-token|x-access-token|client-key)\s*:\s*)"
    r'"(?:\\.|[^"\\])*"'
)
_SECRET_HEADER_SINGLE_QUOTED_RE = re.compile(
    r"(?i)\b((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|"
    r"x-auth-token|x-access-token|client-key)\s*:\s*)"
    r"'(?:\\.|[^'\\])*'"
)
_SECRET_HEADER_RE = re.compile(
    r"(?i)\b((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|"
    r"x-auth-token|x-access-token|client-key)\s*:\s*)([^\s,;\"'\\]+)"
)
_BEARER_RE = re.compile(
    r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){1,2}\b"
)
_URL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/?#\"']+)([^\s\"']*)"
)
_QUERY_PAIR_RE = re.compile(r"([?&;])([^=&#;\s]+)=([^&#;\s]*)")
_DOUBLE_QUOTED_KEY_ASSIGN_RE = re.compile(
    r'(?P<prefix>"(?P<key>(?:\\.|[^"\\]){1,160})"\s*[:=]\s*)'
    r'"(?P<value>(?:\\.|[^"\\])*)"',
    re.DOTALL,
)
_SINGLE_QUOTED_KEY_ASSIGN_RE = re.compile(
    r"(?P<prefix>'(?P<key>(?:\\.|[^'\\]){1,160})'\s*[:=]\s*)"
    r"'(?P<value>(?:\\.|[^'\\])*)'",
    re.DOTALL,
)
_BARE_KEY_DOUBLE_QUOTED_VALUE_RE = re.compile(
    r'(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z_]'
    r'[A-Za-z0-9_. -]{0,159})\s*[:=]\s*)'
    r'"(?P<value>(?:\\.|[^"\\])*)"',
    re.DOTALL,
)
_BARE_KEY_SINGLE_QUOTED_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_.-])(?P<key>[A-Za-z_]"
    r"[A-Za-z0-9_. -]{0,159})\s*[:=]\s*)"
    r"'(?P<value>(?:\\.|[^'\\])*)'",
    re.DOTALL,
)
_UNQUOTED_ASSIGN_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_.-])"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_. -]{0,159})\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|[^\s,;&}\]\"'\\]+)"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_KNOWN_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{10,}|"
    r"github_pat_[A-Za-z0-9_]{10,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[A-Za-z0-9_-]{20,}|"
    r"AKIA[A-Z0-9]{16}"
    r")(?![A-Za-z0-9_-])"
)


def _normalized_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return re.sub(r"_+", "_", text)


def is_sensitive_key(value: Any) -> bool:
    """Return whether a structural key carries a secret value."""
    key = _normalized_key(value)
    if not key or key in _SAFE_METADATA_KEYS:
        return False
    if key in _SENSITIVE_KEYS:
        return True
    return any(key.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _redact_assignment(match: re.Match[str]) -> str:
    normalized_key = _normalized_key(match.group("key"))
    if normalized_key in {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
    }:
        return match.group(0)
    if not is_sensitive_key(normalized_key):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTION_TOKEN}"


def _redact_quoted_assignment(match: re.Match[str], quote: str) -> str:
    if not is_sensitive_key(match.group("key")):
        return match.group(0)
    return f"{match.group('prefix')}{quote}{REDACTION_TOKEN}{quote}"


def _redact_url(match: re.Match[str]) -> str:
    scheme, authority, remainder = match.groups()
    if "@" in authority:
        _userinfo, separator, host = authority.rpartition("@")
        authority = REDACTION_TOKEN + separator + host

    sensitive_query = False

    def redact_query(pair: re.Match[str]) -> str:
        nonlocal sensitive_query
        separator, raw_key, raw_value = pair.groups()
        try:
            key = unquote_plus(raw_key)
        except Exception:
            key = raw_key
        if _normalized_key(key) not in _SENSITIVE_QUERY_KEYS and not is_sensitive_key(key):
            return pair.group(0)
        sensitive_query = True
        return f"{separator}{raw_key}={REDACTION_TOKEN}"

    remainder = _QUERY_PAIR_RE.sub(redact_query, remainder)
    if sensitive_query and "#" in remainder:
        remainder = remainder.split("#", 1)[0] + "#" + REDACTION_TOKEN
    return scheme + authority + remainder


def _scrub_text_pass(text: str) -> str:
    value = _PRIVATE_KEY_RE.sub(REDACTION_TOKEN, text)
    value = _KNOWN_CREDENTIAL_RE.sub(REDACTION_TOKEN, value)
    value = _COOKIE_UNWRAPPED_LINE_RE.sub(
        lambda match: match.group(1) + REDACTION_TOKEN,
        value,
    )
    value = _COOKIE_WRAPPED_HEADER_RE.sub(
        lambda match: match.group(1) + REDACTION_TOKEN,
        value,
    )
    value = _AUTH_ESCAPED_DOUBLE_QUOTED_CREDENTIAL_RE.sub(
        lambda match: (
            match.group(1)
            + (match.group(2) or "")
            + '\\"'
            + REDACTION_TOKEN
            + '\\"'
        ),
        value,
    )
    value = _AUTH_ESCAPED_SINGLE_QUOTED_CREDENTIAL_RE.sub(
        lambda match: (
            match.group(1)
            + (match.group(2) or "")
            + "\\'"
            + REDACTION_TOKEN
            + "\\'"
        ),
        value,
    )
    value = _AUTH_DOUBLE_QUOTED_CREDENTIAL_RE.sub(
        lambda match: (
            match.group(1)
            + (match.group(2) or "")
            + '"'
            + REDACTION_TOKEN
            + '"'
        ),
        value,
    )
    value = _AUTH_SINGLE_QUOTED_CREDENTIAL_RE.sub(
        lambda match: (
            match.group(1)
            + (match.group(2) or "")
            + "'"
            + REDACTION_TOKEN
            + "'"
        ),
        value,
    )
    value = _AUTH_SCHEME_TOKEN_RE.sub(
        lambda match: match.group(1) + match.group(2) + REDACTION_TOKEN,
        value,
    )
    value = _AUTH_BARE_TOKEN_RE.sub(
        lambda match: match.group(1) + REDACTION_TOKEN,
        value,
    )
    value = _SECRET_HEADER_ESCAPED_DOUBLE_QUOTED_RE.sub(
        lambda match: match.group(1) + '\\"' + REDACTION_TOKEN + '\\"',
        value,
    )
    value = _SECRET_HEADER_ESCAPED_SINGLE_QUOTED_RE.sub(
        lambda match: match.group(1) + "\\'" + REDACTION_TOKEN + "\\'",
        value,
    )
    value = _SECRET_HEADER_DOUBLE_QUOTED_RE.sub(
        lambda match: match.group(1) + '"' + REDACTION_TOKEN + '"',
        value,
    )
    value = _SECRET_HEADER_SINGLE_QUOTED_RE.sub(
        lambda match: match.group(1) + "'" + REDACTION_TOKEN + "'",
        value,
    )
    value = _SECRET_HEADER_RE.sub(
        lambda match: match.group(1) + REDACTION_TOKEN,
        value,
    )
    value = _BEARER_RE.sub(
        lambda match: match.group(1) + REDACTION_TOKEN,
        value,
    )
    value = _URL_RE.sub(_redact_url, value)
    value = _DOUBLE_QUOTED_KEY_ASSIGN_RE.sub(
        lambda match: _redact_quoted_assignment(match, '"'),
        value,
    )
    value = _SINGLE_QUOTED_KEY_ASSIGN_RE.sub(
        lambda match: _redact_quoted_assignment(match, "'"),
        value,
    )
    value = _BARE_KEY_DOUBLE_QUOTED_VALUE_RE.sub(
        lambda match: _redact_quoted_assignment(match, '"'),
        value,
    )
    value = _BARE_KEY_SINGLE_QUOTED_VALUE_RE.sub(
        lambda match: _redact_quoted_assignment(match, "'"),
        value,
    )
    value = _UNQUOTED_ASSIGN_RE.sub(_redact_assignment, value)
    value = _JWT_RE.sub(REDACTION_TOKEN, value)
    return value


def _detection_normalize(text: str) -> str:
    value = _ANSI_ESCAPE_RE.sub("", text)
    value = "".join(
        child
        for child in value
        if unicodedata.category(child) not in {"Cf", "Cc"}
        or child in "\n\r\t"
    )
    return unicodedata.normalize("NFKC", value)


def scrub_sensitive_text(value: Any) -> str:
    """Redact credential shapes from arbitrary text without returning raw on error."""
    try:
        if isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        elif isinstance(value, bytearray):
            text = bytes(value).decode("utf-8", errors="replace")
        else:
            text = str(value)
        try:
            parsed_json = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed_json = None
        if isinstance(parsed_json, (dict, list)):
            cleaned_json = scrub_sensitive_value(parsed_json)
            if cleaned_json != parsed_json:
                return json.dumps(
                    cleaned_json,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
            return text
        scrubbed = _scrub_text_pass(text)
        normalized = _detection_normalize(text)
        normalized_scrubbed = _scrub_text_pass(normalized)
        if normalized_scrubbed != normalized:
            scrubbed = normalized_scrubbed
        return scrubbed
    except Exception as exc:
        raise SecretScrubError("secret scrubbing failed") from None


def scrub_sensitive_value(value: Any, *, max_depth: int = MAX_SCRUB_DEPTH) -> Any:
    """Recursively scrub structural keys and scalar values.

    Containers retain their ordinary shape where JSON-safe. Tuples become
    tuples, while sets become deterministic lists. Cycles and depth overflow
    fail closed to the fixed redaction token.
    """
    try:
        seen: set[int] = set()
        visited = 0

        def walk(item: Any, depth: int) -> Any:
            nonlocal visited
            visited += 1
            if visited > MAX_SCRUB_ITEMS or depth > max_depth:
                return REDACTION_TOKEN
            if item is None or isinstance(item, (bool, int, float)):
                return item
            if isinstance(item, str):
                return scrub_sensitive_text(item)
            if isinstance(item, (bytes, bytearray, BaseException)):
                return scrub_sensitive_text(item)

            if isinstance(item, Mapping):
                identity = id(item)
                if identity in seen:
                    return REDACTION_TOKEN
                seen.add(identity)
                try:
                    output: dict[str, Any] = {}
                    for raw_key, child in item.items():
                        key = scrub_sensitive_text(raw_key)
                        normalized_key = _normalized_key(raw_key)
                        safe_token_usage = (
                            normalized_key == "tokens"
                            and (
                                isinstance(child, (int, float))
                                and not isinstance(child, bool)
                                or isinstance(child, Mapping)
                                and bool(child)
                                and all(
                                    isinstance(token_value, (int, float))
                                    and not isinstance(token_value, bool)
                                    for token_value in child.values()
                                )
                            )
                        )
                        output[key] = (
                            REDACTION_TOKEN
                            if is_sensitive_key(raw_key) and not safe_token_usage
                            else walk(child, depth + 1)
                        )
                    return output
                finally:
                    seen.remove(identity)

            if isinstance(item, (list, tuple, set, frozenset)):
                identity = id(item)
                if identity in seen:
                    return REDACTION_TOKEN
                seen.add(identity)
                try:
                    values = [walk(child, depth + 1) for child in item]
                    if isinstance(item, tuple):
                        return tuple(values)
                    if isinstance(item, (set, frozenset)):
                        return sorted(values, key=lambda child: repr(child))
                    return values
                finally:
                    seen.remove(identity)

            return scrub_sensitive_text(item)

        return walk(value, 0)
    except SecretScrubError:
        raise
    except Exception:
        raise SecretScrubError("secret scrubbing failed") from None


def scrub_then_truncate(value: Any, limit: int) -> str:
    """Scrub before applying a deterministic character bound."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise SecretScrubError("secret scrubbing failed")
    scrubbed = scrub_sensitive_text(value)
    if limit == 0:
        return ""
    return scrubbed if len(scrubbed) <= limit else scrubbed[-limit:]


def contains_unredacted_secret(value: Any) -> bool:
    """Return true when canonical scrubbing would change a value."""
    scrubbed = scrub_sensitive_value(value)

    def comparable(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): comparable(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple, set, frozenset)):
            values = [comparable(child) for child in item]
            if isinstance(item, (set, frozenset)):
                return sorted(values, key=repr)
            return values
        if isinstance(item, (bytes, bytearray)):
            return bytes(item).decode("utf-8", errors="replace")
        if isinstance(item, BaseException):
            return str(item)
        return item

    return comparable(scrubbed) != comparable(value)


__all__ = [
    "MAX_SCRUB_DEPTH",
    "REDACTION_TOKEN",
    "SecretScrubError",
    "contains_unredacted_secret",
    "is_sensitive_key",
    "scrub_sensitive_text",
    "scrub_sensitive_value",
    "scrub_then_truncate",
]
