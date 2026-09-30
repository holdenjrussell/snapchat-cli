from __future__ import annotations

import io
import json
import os
import types
import urllib.request
from email.message import Message

import pytest

from reports import slack_transport


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self._body = io.BytesIO(body)
        self._url = url
        self._status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def getcode(self) -> int:
        return self._status

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[urllib.request.Request, int]] = []

    def open(self, request: urllib.request.Request, *, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        return self.response


def post_url() -> str:
    return "https://slack.com/api/chat.postMessage"


def test_post_message_is_exact_origin_json_and_bounded() -> None:
    opener = FakeOpener(
        FakeResponse(b'{"ok":true,"ts":"1.2"}', url=post_url())
    )
    result = slack_transport.post_message(
        "xoxb-test-token",
        {"channel": "C123", "text": "hello"},
        opener=opener,
    )
    assert result == {"ok": True, "ts": "1.2"}
    request, timeout = opener.requests[0]
    assert request.full_url == post_url()
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer xoxb-test-token"
    assert request.get_header("Content-type") == "application/json; charset=utf-8"
    assert timeout == 15
    assert json.loads(request.data or b"") == {"channel": "C123", "text": "hello"}


def test_conversations_replies_is_exact_origin_get() -> None:
    opener = FakeOpener(
        FakeResponse(
            b'{"ok":true,"messages":[]}',
            url=(
                "https://slack.com/api/conversations.replies?"
                "channel=C123&ts=1.2"
            ),
        )
    )
    result = slack_transport.conversations_replies(
        "xoxb-test-token",
        {"channel": "C123", "ts": "1.2"},
        opener=opener,
    )
    assert result["ok"] is True
    request, _timeout = opener.requests[0]
    assert request.get_method() == "GET"
    assert request.full_url.startswith(
        "https://slack.com/api/conversations.replies?"
    )


@pytest.mark.parametrize("status", [201, 301, 302, 307, 308, 500])
def test_non_200_status_fails_without_token(status: int) -> None:
    opener = FakeOpener(
        FakeResponse(b'{"ok":false}', url=post_url(), status=status)
    )
    with pytest.raises(slack_transport.SlackTransportError) as caught:
        slack_transport.post_message(
            "xoxb-super-secret-canary",
            {"channel": "C123", "text": "hello"},
            opener=opener,
        )
    assert "xoxb-super-secret-canary" not in str(caught.value)


def test_cross_host_final_url_is_rejected_without_token() -> None:
    opener = FakeOpener(
        FakeResponse(
            b'{"ok":true}',
            url="https://attacker.invalid/collect",
        )
    )
    with pytest.raises(
        slack_transport.SlackTransportError,
        match="final origin changed",
    ) as caught:
        slack_transport.post_message(
            "xoxb-super-secret-canary",
            {"channel": "C123", "text": "hello"},
            opener=opener,
        )
    assert "xoxb-super-secret-canary" not in str(caught.value)


def test_non_json_content_type_is_rejected() -> None:
    opener = FakeOpener(
        FakeResponse(
            b'{"ok":true}',
            url=post_url(),
            content_type="text/html",
        )
    )
    with pytest.raises(
        slack_transport.SlackTransportError,
        match="non-JSON content type",
    ):
        slack_transport.post_message(
            "xoxb-test-token",
            {"channel": "C123", "text": "hello"},
            opener=opener,
        )


def test_response_byte_limit_is_enforced() -> None:
    opener = FakeOpener(
        FakeResponse(b'{"ok":true,"pad":"123456"}', url=post_url())
    )
    with pytest.raises(
        slack_transport.SlackTransportError,
        match="exceeded the byte limit",
    ):
        slack_transport.post_message(
            "xoxb-test-token",
            {"channel": "C123", "text": "hello"},
            opener=opener,
            max_response_bytes=8,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'{"ok":true,"ok":false}',
        b'{"ok":NaN}',
        b'{"ok":1e999}',
        b'[]',
        b'not-json',
        b'\xff',
    ],
)
def test_response_requires_strict_json_object(body: bytes) -> None:
    opener = FakeOpener(FakeResponse(body, url=post_url()))
    with pytest.raises(slack_transport.SlackTransportError):
        slack_transport.post_message(
            "xoxb-test-token",
            {"channel": "C123", "text": "hello"},
            opener=opener,
        )


def test_redirect_handler_rejects_before_building_cross_host_request() -> None:
    handler = slack_transport._NoRedirectHandler()
    request = urllib.request.Request(
        post_url(),
        headers={"Authorization": "Bearer xoxb-super-secret-canary"},
    )
    with pytest.raises(slack_transport.SlackTransportError, match="redirect rejected"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/collect",
        )


def test_opener_ignores_ambient_proxy_and_ca_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/attacker-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/tmp/attacker-certs")
    opener = slack_transport.build_slack_opener()
    proxy_handlers = [
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    # Passing ProxyHandler({}) suppresses urllib's default environment-backed
    # proxy handler. Because the explicit handler has no proxy methods,
    # build_opener intentionally leaves no ProxyHandler in the final chain.
    assert proxy_handlers == []


def test_tls_context_uses_compiled_roots_not_environment_resolved_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = types.SimpleNamespace(
        cafile="/attacker/ca.pem",
        capath="/attacker/certs",
        openssl_cafile="/compiled/ca.pem",
        openssl_capath="/compiled/certs",
    )
    loaded: list[tuple[str | None, str | None]] = []

    class FakeContext:
        check_hostname = False
        verify_mode = None

        def load_verify_locations(
            self,
            *,
            cafile: str | None,
            capath: str | None,
        ) -> None:
            loaded.append((cafile, capath))

    monkeypatch.setattr(slack_transport.ssl, "get_default_verify_paths", lambda: defaults)
    monkeypatch.setattr(
        slack_transport.ssl,
        "SSLContext",
        lambda _protocol: FakeContext(),
    )
    monkeypatch.setattr(slack_transport, "_is_file", lambda path: path == "/compiled/ca.pem")
    monkeypatch.setattr(slack_transport, "_is_dir", lambda path: path == "/compiled/certs")

    context = slack_transport._tls_context()

    assert context.check_hostname is True
    assert context.verify_mode == slack_transport.ssl.CERT_REQUIRED
    assert loaded == [("/compiled/ca.pem", "/compiled/certs")]


def test_missing_compiled_cafile_falls_back_to_os_bundle_not_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/attacker-ca.pem")
    defaults = types.SimpleNamespace(
        cafile="/tmp/attacker-ca.pem",
        capath=None,
        openssl_cafile="/compiled/missing.pem",
        openssl_capath="/compiled/certs",
    )
    loaded: list[tuple[str | None, str | None]] = []

    class FakeContext:
        check_hostname = False
        verify_mode = None

        def load_verify_locations(self, *, cafile: str | None, capath: str | None) -> None:
            loaded.append((cafile, capath))

    bundle = slack_transport._OS_CA_BUNDLES[0]
    monkeypatch.setattr(slack_transport.ssl, "get_default_verify_paths", lambda: defaults)
    monkeypatch.setattr(slack_transport.ssl, "SSLContext", lambda _protocol: FakeContext())
    monkeypatch.setattr(slack_transport, "_is_file", lambda path: path == bundle)
    monkeypatch.setattr(slack_transport, "_is_dir", lambda path: path == "/compiled/certs")

    slack_transport._tls_context()

    assert loaded == [(bundle, "/compiled/certs")]


@pytest.mark.parametrize(
    "token",
    [
        "",
        "has space",
        "line\nbreak",
        "tab\tvalue",
        "nul\x00value",
        "delete\x7fvalue",
        "unicode-\u2603",
    ],
)
def test_token_validation_fails_closed(token: str) -> None:
    opener = FakeOpener(FakeResponse(b'{"ok":true}', url=post_url()))
    with pytest.raises(slack_transport.SlackTransportError, match="token unavailable"):
        slack_transport.post_message(
            token,
            {"channel": "C123", "text": "hello"},
            opener=opener,
        )


def test_slack_error_code_is_bounded_and_safe() -> None:
    assert slack_transport.slack_error_code({"error": "channel_not_found"}) == "channel_not_found"
    assert slack_transport.slack_error_code({"error": "Bearer xoxb-secret"}) == "unknown_error"


def test_environment_is_not_modified() -> None:
    before = dict(os.environ)
    slack_transport.build_slack_opener()
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "response",
    [
        object(),
        FakeResponse(b'{}', url=post_url(), status="not-an-integer"),
    ],
)
def test_malformed_response_fails_with_constant_error(response: object) -> None:
    class MalformedOpener:
        def open(self, request: urllib.request.Request, *, timeout: int) -> object:
            return response

    with pytest.raises(slack_transport.SlackTransportError, match="request failed"):
        slack_transport.post_message(
            "xoxb-super-secret-canary",
            {"channel": "C123", "text": "hello"},
            opener=MalformedOpener(),
        )
