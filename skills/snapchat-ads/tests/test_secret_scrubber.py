from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import secret_scrubber as scrubber  # noqa: E402


CANARY = "TOPSECRET"


@pytest.mark.parametrize(
    "raw",
    [
        f"Authorization: Bearer {CANARY}",
        f"Proxy-Authorization: Basic {CANARY}",
        f"Bearer {CANARY}",
        f"x-api-key: {CANARY}",
        f"Cookie: session={CANARY}; other=1",
        f"Set-Cookie: sid={CANARY}; Secure; HttpOnly",
        f"api_key={CANARY}",
        f"CLIENT-KEY = {CANARY}",
        f"password: {CANARY}",
        f'{{"nested": {{"refresh_token": "{CANARY}"}}}}',
        f"https://user:{CANARY}@example.test/path",
        f"https://{CANARY}@example.test/path",
        f"https://example.test/path?api_key={CANARY}&keep=yes",
        f"https://example.test/path?session_token={CANARY}#fragment",
        "-----BEGIN PRIVATE KEY-----\n" + CANARY + "\n-----END PRIVATE KEY-----",
        f"ａｐｉ＿ｋｅｙ＝{CANARY}",
    ],
)
def test_scrub_sensitive_text_removes_required_secret_shapes(raw):
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert scrubber.REDACTION_TOKEN in cleaned


def test_recursive_scrubber_covers_nested_types_cycles_bytes_and_exceptions():
    cycle: list[object] = []
    cycle.append(cycle)
    value = {
        "api_key": CANARY,
        "ordinary": [
            f"Authorization: Bearer {CANARY}",
            (f"password={CANARY}",),
            {f"client_secret={CANARY}"},
            RuntimeError(f"refresh_token={CANARY}"),
            f"Cookie: sid={CANARY}".encode(),
            cycle,
        ],
    }

    cleaned = scrubber.scrub_sensitive_value(value)
    rendered = repr(cleaned)
    assert CANARY not in rendered
    assert cleaned["api_key"] == scrubber.REDACTION_TOKEN
    assert cleaned["ordinary"][-1] == [scrubber.REDACTION_TOKEN]


def test_depth_overflow_fails_closed_without_returning_deep_value():
    value: object = f"api_key={CANARY}"
    for _ in range(10):
        value = [value]
    cleaned = scrubber.scrub_sensitive_value(value, max_depth=3)
    assert CANARY not in repr(cleaned)
    assert scrubber.REDACTION_TOKEN in repr(cleaned)


@pytest.mark.parametrize(
    "key",
    [
        "token_count",
        "token_budget",
        "session_id",
        "request_id",
        "source_sha256",
        "model",
        "hash",
        "ad_id",
        "client_id",
    ],
)
def test_safe_metadata_keys_are_not_structurally_redacted(key):
    cleaned = scrubber.scrub_sensitive_value({key: "ordinary-value"})
    assert cleaned[key] == "ordinary-value"


def test_ordinary_evidence_and_url_query_are_preserved():
    value = {
        "entity_name": "Main ad squad | July",
        "source_sha256": "a" * 64,
        "url": "https://example.test/path?campaign_id=123&utm_source=snapchat",
        "metrics": {"spend": 123.45, "purchases": 7},
    }
    assert scrubber.scrub_sensitive_value(value) == value


def test_scrubber_is_idempotent():
    raw = {
        "stderr_tail": f"api_key={CANARY}",
        "nested": f"Authorization: Bearer {CANARY}",
    }
    once = scrubber.scrub_sensitive_value(raw)
    assert scrubber.scrub_sensitive_value(once) == once


def test_scrub_then_truncate_redacts_before_bounding():
    cleaned = scrubber.scrub_then_truncate("x" * 200 + f" api_key={CANARY}", 40)
    assert CANARY not in cleaned
    assert len(cleaned) <= 40


def test_scrubber_raises_only_constant_error_on_internal_failure(monkeypatch):
    monkeypatch.setattr(scrubber, "_scrub_text_pass", lambda _value: 1 / 0)
    with pytest.raises(scrubber.SecretScrubError) as exc:
        scrubber.scrub_sensitive_text(f"api_key={CANARY}")
    assert str(exc.value) == "secret scrubbing failed"
    assert CANARY not in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        "sk-" + "X" * 30,
        "ghp_" + "X" * 36,
        "xoxb-" + "X" * 30,
    ],
)
def test_known_vendor_credential_prefixes_are_redacted(raw):
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert raw not in cleaned
    assert cleaned == scrubber.REDACTION_TOKEN


@pytest.mark.parametrize(
    "query_key",
    [
        "signature",
        "sig",
        "code",
        "state",
        "x-amz-signature",
        "x-amz-credential",
        "x-amz-security-token",
    ],
)
def test_sensitive_signed_url_query_fields_and_fragment_are_redacted(query_key):
    raw = f"https://example.test/path?keep=1&{query_key}={CANARY}#signed-fragment"
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert "signed-fragment" not in cleaned
    assert f"{query_key}={scrubber.REDACTION_TOKEN}" in cleaned
    assert cleaned.endswith("#" + scrubber.REDACTION_TOKEN)


def test_escaped_quote_inside_raw_json_secret_is_redacted():
    raw = r'{"api_key":"prefix\"' + CANARY + r'"}'
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert cleaned == '{"api_key":"[REDACTED]"}'


@pytest.mark.parametrize(
    "raw",
    [
        f"api\u200b_key={CANARY}",
        f"api\x1b[31m_key={CANARY}",
    ],
)
def test_obfuscated_secret_keys_are_detected(raw):
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert scrubber.REDACTION_TOKEN in cleaned


def test_tokens_container_is_sensitive_but_numeric_usage_metadata_is_safe():
    opaque = scrubber.scrub_sensitive_value(
        {"tokens": {"access": CANARY, "refresh": CANARY}}
    )
    assert opaque["tokens"] == scrubber.REDACTION_TOKEN

    usage = {"tokens": {"input": 12, "output": 7}}
    assert scrubber.scrub_sensitive_value(usage) == usage
    assert scrubber.scrub_sensitive_value({"tokens": 19}) == {"tokens": 19}


@pytest.mark.parametrize(
    "ordinary",
    [
        "token count is 12",
        "basic metrics are stable",
        "digest mismatch observed",
    ],
)
def test_ordinary_prose_that_mentions_secret_adjacent_words_is_preserved(ordinary):
    assert scrubber.scrub_sensitive_text(ordinary) == ordinary


def test_zero_length_truncation_returns_empty_string():
    assert scrubber.scrub_then_truncate(f"api_key={CANARY}", 0) == ""


def test_contains_predicate_does_not_flag_clean_sets():
    assert not scrubber.contains_unredacted_secret({"alpha", "beta"})


def test_quoted_authorization_credential_is_redacted_and_quote_is_preserved():
    raw = f'Authorization: Bearer "{CANARY}"'
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert cleaned == 'Authorization: Bearer "[REDACTED]"'


@pytest.mark.parametrize(
    "raw",
    [
        f'api_key="TOP SECRET {CANARY}"',
        f"password='TOP SECRET {CANARY}'",
    ],
)
def test_unquoted_key_with_quoted_whitespace_value_is_redacted(raw):
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert "TOP SECRET" not in cleaned
    assert scrubber.REDACTION_TOKEN in cleaned


def test_encoded_semicolon_query_key_is_redacted():
    raw = f"https://example.test/path?keep=1;api%5Fkey={CANARY}"
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert f"api%5Fkey={scrubber.REDACTION_TOKEN}" in cleaned


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f'curl -H "Authorization: Bearer {CANARY}" https://api.example/v1',
            'curl -H "Authorization: Bearer [REDACTED]" https://api.example/v1',
        ),
        (
            f"curl -H 'Authorization: Bearer {CANARY}' https://api.example/v1",
            "curl -H 'Authorization: Bearer [REDACTED]' https://api.example/v1",
        ),
        (
            f'curl -H "Cookie: sid={CANARY}; other=1" https://api.example/v1',
            'curl -H "Cookie: [REDACTED]" https://api.example/v1',
        ),
        (
            f"curl -H 'Cookie: sid={CANARY}; other=1' https://api.example/v1",
            "curl -H 'Cookie: [REDACTED]' https://api.example/v1",
        ),
        (
            f"Authorization: Bearer {CANARY} status=401 request_id=req-safe",
            "Authorization: Bearer [REDACTED] status=401 request_id=req-safe",
        ),
    ],
)
def test_header_redaction_preserves_outer_quotes_and_trailing_diagnostics(raw, expected):
    assert scrubber.scrub_sensitive_text(raw) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"error": f"Authorization: Bearer {CANARY}", "safe": 1},
        {"error": f"Cookie: sid={CANARY}; other=1", "safe": 1},
    ],
)
def test_embedded_header_redaction_preserves_valid_json_and_safe_fields(payload):
    raw = json.dumps(payload, separators=(",", ":"))
    cleaned = scrubber.scrub_sensitive_text(raw)
    decoded = json.loads(cleaned)
    assert CANARY not in cleaned
    assert decoded["safe"] == 1
    assert decoded["error"].endswith(scrubber.REDACTION_TOKEN)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            f'curl "https://api.example/v1?keep=yes&api_key={CANARY}" --fail',
            'curl "https://api.example/v1?keep=yes&api_key=[REDACTED]" --fail',
        ),
        (
            f"curl 'https://api.example/v1?keep=yes&api_key={CANARY}' --fail",
            "curl 'https://api.example/v1?keep=yes&api_key=[REDACTED]' --fail",
        ),
    ],
)
def test_signed_url_redaction_preserves_outer_quotes_and_suffix(raw, expected):
    assert scrubber.scrub_sensitive_text(raw) == expected


def test_sensitive_final_url_query_preserves_valid_json():
    raw = json.dumps(
        {
            "error": f"https://api.example/v1?keep=yes&api_key={CANARY}",
            "safe": 1,
        },
        separators=(",", ":"),
    )
    cleaned = scrubber.scrub_sensitive_text(raw)
    decoded = json.loads(cleaned)
    assert CANARY not in cleaned
    assert decoded == {
        "error": "https://api.example/v1?keep=yes&api_key=[REDACTED]",
        "safe": 1,
    }


@pytest.mark.parametrize(
    "header_value",
    [
        f'Authorization: Bearer "{CANARY}"',
        f'Proxy-Authorization: Basic "{CANARY}"',
        f'Cookie: sid="{CANARY}"; other=1',
        f'x-api-key: "{CANARY}"',
        f'client-key: "{CANARY}"',
        f'x-goog-api-key: "{CANARY}"',
    ],
)
def test_escaped_quoted_header_inside_json_round_trips_safely(header_value):
    raw = json.dumps({"error": header_value, "safe": 1}, separators=(",", ":"))
    cleaned = scrubber.scrub_sensitive_text(raw)
    decoded = json.loads(cleaned)
    assert CANARY not in cleaned
    assert decoded["safe"] == 1
    assert scrubber.REDACTION_TOKEN in decoded["error"]
    assert scrubber.scrub_sensitive_text(cleaned) == cleaned


@pytest.mark.parametrize(
    "header",
    [
        "x-api-key",
        "client-key",
        "x-goog-api-key",
    ],
)
@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ('"', '"'),
        ("'", "'"),
        ('\\"', '\\"'),
        ("\\'", "\\'"),
    ],
)
def test_secret_header_alias_quote_matrix_is_safe_and_idempotent(
    header,
    opening,
    closing,
):
    raw = f"{header}: {opening}{CANARY}{closing} trailing=safe"
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert CANARY not in cleaned
    assert "trailing=safe" in cleaned
    assert scrubber.scrub_sensitive_text(cleaned) == cleaned


def test_bare_authorization_with_trailing_diagnostics_redacts_first_value():
    raw = f"Authorization: {CANARY} status=401 request_id=req-safe"
    cleaned = scrubber.scrub_sensitive_text(raw)
    assert cleaned == (
        "Authorization: [REDACTED] status=401 request_id=req-safe"
    )
    assert scrubber.scrub_sensitive_text(cleaned) == cleaned


@pytest.mark.parametrize(
    "payload",
    [
        {"error": f"api_key={CANARY}", "safe": 1},
        {"error": f'api_key="{CANARY}"', "safe": 1},
        {"x_goog_api_key": CANARY, "safe": 1},
        {"x-goog-api-key": CANARY, "safe": 1},
    ],
)
def test_assignment_and_structural_alias_json_matrix_is_valid(payload):
    raw = json.dumps(payload, separators=(",", ":"))
    cleaned = scrubber.scrub_sensitive_text(raw)
    decoded = json.loads(cleaned)
    assert CANARY not in cleaned
    assert decoded["safe"] == 1
    assert scrubber.scrub_sensitive_text(cleaned) == cleaned


def test_set_cookie_json_redaction_is_stable_across_repeated_passes():
    raw = json.dumps(
        {"error": f"Set-Cookie: sid={CANARY}; Secure", "safe": 1},
        separators=(",", ":"),
    )
    once = scrubber.scrub_sensitive_text(raw)
    twice = scrubber.scrub_sensitive_text(once)
    thrice = scrubber.scrub_sensitive_text(twice)
    assert CANARY not in once
    assert json.loads(once)["safe"] == 1
    assert once == twice == thrice


def test_contains_predicate_handles_clean_and_secret_byte_values():
    assert not scrubber.contains_unredacted_secret(b"hello")
    assert not scrubber.contains_unredacted_secret(bytearray(b"hello"))
    assert scrubber.contains_unredacted_secret(
        f"api_key={CANARY}".encode("utf-8")
    )
