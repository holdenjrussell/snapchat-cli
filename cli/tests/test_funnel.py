"""Tests for the Tailscale funnel OAuth-callback helper.

Everything runs offline: tailscale invocations are mocked with canned JSON,
and the callback listener is exercised over loopback HTTP.
"""

import json
import threading
import unittest
import urllib.request
from unittest.mock import patch

from snapchat_ads_cli import funnel

DNS = "node.tailnet-example.ts.net"

STATUS_JSON = json.dumps(
    {
        "BackendState": "Running",
        "Self": {"DNSName": f"{DNS}.", "Online": True},
    }
)

SERVE_JSON = json.dumps(
    {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            f"{DNS}:443": {
                "Handlers": {
                    "/": {"Proxy": "http://127.0.0.1:9000"},
                    "/other": {"Proxy": "http://127.0.0.1:9001"},
                }
            },
            f"{DNS}:8443": {
                "Handlers": {
                    "/snapchat-oauth": {"Proxy": "http://127.0.0.1:8686"},
                }
            },
        },
        "AllowFunnel": {f"{DNS}:8443": True},
    }
)


def _fake_tailscale(responses):
    """Return a _tailscale stub keyed on the first CLI arg pair."""

    def fake(args, timeout=30.0):
        key = " ".join(args[:2])
        if key in responses:
            return responses[key]
        raise AssertionError(f"unexpected tailscale invocation: {args}")

    return fake


class BuildCallbackUrlTests(unittest.TestCase):
    def test_port_443_omitted(self):
        self.assertEqual(
            funnel.build_callback_url(DNS, 443, "/snapchat-oauth"),
            f"https://{DNS}/snapchat-oauth",
        )

    def test_non_default_port_and_bare_path(self):
        self.assertEqual(
            funnel.build_callback_url(DNS, 8443, "snapchat-oauth"),
            f"https://{DNS}:8443/snapchat-oauth",
        )


class ServeConfigParsingTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(SERVE_JSON)

    def test_port_handlers(self):
        handlers = funnel.port_handlers(self.config, DNS, 443)
        self.assertEqual(
            handlers,
            {"/": "http://127.0.0.1:9000", "/other": "http://127.0.0.1:9001"},
        )
        self.assertEqual(funnel.port_handlers(self.config, DNS, 10000), {})

    def test_funnel_on(self):
        self.assertTrue(funnel.funnel_on(self.config, DNS, 8443))
        self.assertFalse(funnel.funnel_on(self.config, DNS, 443))


class PlanCallbackTests(unittest.TestCase):
    def _plan(self, **kwargs):
        responses = {
            "status --json": (0, STATUS_JSON, ""),
            "serve status": (0, SERVE_JSON, ""),
        }
        with patch.object(funnel, "_tailscale", _fake_tailscale(responses)):
            return funnel.plan_callback(**kwargs)

    def test_clean_port_already_live(self):
        plan = self._plan(https_port=8443, local_port=8686, path="/snapchat-oauth")
        self.assertEqual(plan["callback_url"], f"https://{DNS}:8443/snapchat-oauth")
        self.assertEqual(plan["conflicts"], {})
        self.assertTrue(plan["already_live"])

    def test_conflicting_port_reports_other_handlers(self):
        plan = self._plan(https_port=443, path="/snapchat-oauth")
        self.assertEqual(
            set(plan["conflicts"]),
            {"/", "/other"},
        )
        self.assertFalse(plan["already_live"])

    def test_enable_refuses_conflicts_without_force(self):
        responses = {
            "status --json": (0, STATUS_JSON, ""),
            "serve status": (0, SERVE_JSON, ""),
        }
        with patch.object(funnel, "_tailscale", _fake_tailscale(responses)):
            with self.assertRaises(funnel.TailscaleError):
                funnel.enable_callback_funnel(https_port=443)

    def test_enable_noop_when_already_live(self):
        responses = {
            "status --json": (0, STATUS_JSON, ""),
            "serve status": (0, SERVE_JSON, ""),
        }
        with patch.object(funnel, "_tailscale", _fake_tailscale(responses)):
            result = funnel.enable_callback_funnel(
                https_port=8443, local_port=8686, path="/snapchat-oauth"
            )
        self.assertTrue(result["enabled"])
        self.assertFalse(result["changed"])

    def test_enable_runs_funnel_command_on_clean_port(self):
        calls = []

        def fake(args, timeout=30.0):
            key = " ".join(args[:2])
            if key == "status --json":
                return 0, STATUS_JSON, ""
            if key == "serve status":
                return 0, SERVE_JSON, ""
            calls.append(args)
            return 0, "", ""

        with patch.object(funnel, "_tailscale", fake):
            result = funnel.enable_callback_funnel(https_port=10000, local_port=8686)
        self.assertTrue(result["changed"])
        self.assertEqual(len(calls), 1)
        self.assertIn("--https=10000", calls[0])
        self.assertIn("--set-path=/snapchat-oauth", calls[0])
        self.assertIn("8686", calls[0])

    def test_missing_dns_name_raises(self):
        responses = {
            "status --json": (0, json.dumps({"Self": {}}), ""),
        }
        with patch.object(funnel, "_tailscale", _fake_tailscale(responses)):
            with self.assertRaises(funnel.TailscaleError):
                funnel.node_status()


class WaitForCodeTests(unittest.TestCase):
    PORT = 18686  # test-only loopback port

    def _hit(self, query, delay=0.1):
        def go():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.PORT}/anything?{query}", timeout=5
                )
            except urllib.error.HTTPError:
                pass  # 400 for invalid hits is expected

        t = threading.Timer(delay, go)
        t.start()
        return t

    def test_captures_code_with_matching_state(self):
        self._hit("code=abc123&state=xyz")
        result = funnel.wait_for_code(self.PORT, expected_state="xyz", timeout=10)
        self.assertEqual(result["code"], "abc123")
        self.assertEqual(result["state"], "xyz")

    def test_rejects_state_mismatch_then_times_out(self):
        self._hit("code=abc123&state=WRONG")
        with self.assertRaises(TimeoutError):
            funnel.wait_for_code(self.PORT, expected_state="xyz", timeout=1.5)

    def test_accepts_any_path_without_state_requirement(self):
        self._hit("code=zzz")
        result = funnel.wait_for_code(self.PORT, expected_state=None, timeout=10)
        self.assertEqual(result["code"], "zzz")


if __name__ == "__main__":
    unittest.main()
