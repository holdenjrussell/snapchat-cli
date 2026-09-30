from __future__ import annotations

import os
import unittest
from unittest import mock

from snapchat_ads_cli import api_client


class _Response:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self) -> dict:
        return self._body


class _HttpClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def request(self, *args: object, **kwargs: object) -> _Response:
        del args, kwargs
        self.calls += 1
        return self.responses.pop(0)

    def close(self) -> None:
        return None


class ApiAttemptContractTests(unittest.TestCase):
    def _client(
        self,
        responses: list[_Response],
        *,
        refresh_callback=None,
    ) -> tuple[api_client.SnapchatApiClient, _HttpClient]:
        client = api_client.SnapchatApiClient(
            "test-token",
            refresh_callback=refresh_callback,
        )
        client._client.close()
        fake = _HttpClient(responses)
        client._client = fake
        return client, fake

    def test_default_contract_is_two_http_attempts(self) -> None:
        self.assertEqual(api_client.MAX_ATTEMPTS, 2)
        with mock.patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            self.assertEqual(api_client._configured_max_attempts(), 2)

    def test_contract_accepts_only_one_or_two(self) -> None:
        for value, expected in (("1", 1), ("2", 2)):
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {"SNAPCHAT_API_MAX_ATTEMPTS": value},
            ):
                self.assertEqual(
                    api_client._configured_max_attempts(),
                    expected,
                )
        for value in ("0", "3", "many"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ,
                {"SNAPCHAT_API_MAX_ATTEMPTS": value},
            ):
                with self.assertRaisesRegex(ValueError, "must be 1 or 2"):
                    api_client._configured_max_attempts()

    def test_retryable_http_failure_stops_after_two_requests(self) -> None:
        client, fake = self._client(
            [_Response(500), _Response(500)]
        )
        with mock.patch.object(api_client.time, "sleep") as sleep_mock:
            with self.assertRaises(api_client.SnapApiError):
                client.get("/test")

        self.assertEqual(fake.calls, 2)
        sleep_mock.assert_called_once()

    def test_auth_refresh_is_one_separate_retry_path(self) -> None:
        refreshes: list[str] = []

        def refresh() -> str:
            refreshes.append("called")
            return "refreshed-token"

        client, fake = self._client(
            [
                _Response(401, {"error": "invalid_token"}),
                _Response(200, {"request_status": "SUCCESS"}),
            ],
            refresh_callback=refresh,
        )
        body, _ = client.get("/test")

        self.assertEqual(body["request_status"], "SUCCESS")
        self.assertEqual(fake.calls, 2)
        self.assertEqual(refreshes, ["called"])


if __name__ == "__main__":
    unittest.main()
