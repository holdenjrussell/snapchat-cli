"""Tailscale Funnel helper for the Snap OAuth redirect URI.

Snap Business Manager requires an HTTPS redirect URI for the OAuth
authorization-code flow. On machines running Tailscale, `tailscale funnel`
can publish a stable public HTTPS URL that proxies to a short-lived local
callback listener — no domain, reverse proxy, or cert management needed.

Flow (what `auth callback-url` / `auth login --listen` orchestrate):
  1. Inspect `tailscale serve status --json` for the node's DNS name and any
     existing serve config on the chosen HTTPS port. Funnel exposure is
     per-port: enabling it on a port publishes EVERY path mounted there, so
     a port that already carries other handlers is rejected unless --force.
  2. `tailscale funnel --bg --https=<port> --set-path=<path> <local_port>`
     publishes https://<node-dns>:<port><path> → 127.0.0.1:<local_port>.
  3. The user registers that URL as the redirect URI in Snap Business
     Manager (Business Details → Apps → the OAuth app).
  4. During login, a one-shot local HTTP listener captures the ?code=...
     redirect (validating `state`), and the funnel route can be torn down.

All tailscale invocations shell out to the local binary; nothing here talks
to the network directly except the loopback callback listener.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_CALLBACK_PATH = "/snapchat-oauth"
DEFAULT_HTTPS_PORT = 8443
DEFAULT_LOCAL_PORT = 8686


class TailscaleError(RuntimeError):
    """Tailscale binary missing, not running, or a funnel command failed."""


def _tailscale(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    binary = shutil.which("tailscale")
    if not binary:
        raise TailscaleError(
            "tailscale binary not found on PATH. Install it "
            "(https://tailscale.com/download) or register a redirect URI "
            "you host elsewhere and set SNAPCHAT_REDIRECT_URI manually."
        )
    proc = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def node_status() -> dict[str, Any]:
    """Return {dns_name, backend_state, online} for this tailscale node."""
    rc, out, err = _tailscale(["status", "--json"])
    if rc != 0:
        raise TailscaleError(f"`tailscale status` failed: {err.strip() or out.strip()}")
    data = json.loads(out)
    self_node = data.get("Self") or {}
    dns_name = (self_node.get("DNSName") or "").rstrip(".")
    if not dns_name:
        raise TailscaleError(
            "Tailscale is installed but reports no DNS name for this node. "
            "Is the machine logged in (`tailscale up`) with MagicDNS enabled?"
        )
    return {
        "dns_name": dns_name,
        "backend_state": data.get("BackendState"),
        "online": bool(self_node.get("Online")),
    }


def serve_config() -> dict[str, Any]:
    """Parsed `tailscale serve status --json` ({} when nothing is served)."""
    rc, out, err = _tailscale(["serve", "status", "--json"])
    if rc != 0:
        raise TailscaleError(f"`tailscale serve status` failed: {err.strip() or out.strip()}")
    out = out.strip()
    if not out:
        return {}
    return json.loads(out)


def port_handlers(config: dict[str, Any], dns_name: str, https_port: int) -> dict[str, str]:
    """Map of mount path -> proxy target already served on dns_name:port."""
    web = config.get("Web") or {}
    entry = web.get(f"{dns_name}:{https_port}") or {}
    handlers = entry.get("Handlers") or {}
    out: dict[str, str] = {}
    for path, handler in handlers.items():
        out[path] = handler.get("Proxy") or json.dumps(handler)
    return out


def funnel_on(config: dict[str, Any], dns_name: str, https_port: int) -> bool:
    allow = config.get("AllowFunnel") or {}
    return bool(allow.get(f"{dns_name}:{https_port}"))


def build_callback_url(dns_name: str, https_port: int, path: str) -> str:
    if not path.startswith("/"):
        path = f"/{path}"
    host = dns_name if https_port == 443 else f"{dns_name}:{https_port}"
    return f"https://{host}{path}"


def plan_callback(
    https_port: int = DEFAULT_HTTPS_PORT,
    local_port: int = DEFAULT_LOCAL_PORT,
    path: str = DEFAULT_CALLBACK_PATH,
) -> dict[str, Any]:
    """Inspect current state and describe what enabling the funnel would do.

    Returns callback_url plus `conflicts`: handlers on other paths of the
    same port that funnel would also expose publicly. `already_live` means
    the exact path+proxy+funnel combination is in place already.
    """
    if not path.startswith("/"):
        path = f"/{path}"
    status = node_status()
    config = serve_config()
    dns_name = status["dns_name"]
    handlers = port_handlers(config, dns_name, https_port)
    expected_proxy = f"http://127.0.0.1:{local_port}"
    existing = handlers.get(path)
    conflicts = {p: t for p, t in handlers.items() if p != path}
    already_funnel = funnel_on(config, dns_name, https_port)
    return {
        "dns_name": dns_name,
        "backend_state": status["backend_state"],
        "https_port": https_port,
        "local_port": local_port,
        "path": path,
        "callback_url": build_callback_url(dns_name, https_port, path),
        "existing_handler": existing,
        "conflicts": conflicts,
        "funnel_already_on": already_funnel,
        "already_live": bool(already_funnel and existing == expected_proxy),
    }


def enable_callback_funnel(
    https_port: int = DEFAULT_HTTPS_PORT,
    local_port: int = DEFAULT_LOCAL_PORT,
    path: str = DEFAULT_CALLBACK_PATH,
    force: bool = False,
) -> dict[str, Any]:
    """Publish the funnel callback route and return the plan with the URL.

    Refuses (TailscaleError) when the port already serves other paths and
    force is False — funnel would expose those handlers to the internet.
    """
    plan = plan_callback(https_port=https_port, local_port=local_port, path=path)
    if plan["conflicts"] and not force:
        listing = ", ".join(f"{p} -> {t}" for p, t in plan["conflicts"].items())
        raise TailscaleError(
            f"Port {https_port} already serves other paths ({listing}); enabling "
            f"funnel there would publish them all to the internet. Pick a free "
            f"HTTPS port (e.g. --https-port 443/8443/10000) or pass --force if "
            f"public exposure of those paths is intended."
        )
    if plan["already_live"]:
        plan["enabled"] = True
        plan["changed"] = False
        return plan
    rc, out, err = _tailscale(
        [
            "funnel",
            "--bg",
            "--yes",
            f"--https={https_port}",
            f"--set-path={plan['path']}",
            str(local_port),
        ]
    )
    if rc != 0:
        message = (err.strip() or out.strip() or "unknown error")
        raise TailscaleError(
            f"`tailscale funnel` failed: {message}\n"
            "If the error mentions the `funnel` node attribute, Funnel must be "
            "enabled for this tailnet in the Tailscale admin console ACLs "
            "(https://tailscale.com/kb/1223/funnel)."
        )
    plan["enabled"] = True
    plan["changed"] = True
    return plan


def disable_callback_funnel(
    https_port: int = DEFAULT_HTTPS_PORT,
    path: str = DEFAULT_CALLBACK_PATH,
) -> dict[str, Any]:
    """Remove the callback route (and its funnel exposure) from serve config."""
    if not path.startswith("/"):
        path = f"/{path}"
    rc, out, err = _tailscale(
        ["funnel", f"--https={https_port}", f"--set-path={path}", "off"]
    )
    if rc != 0:
        raise TailscaleError(
            f"`tailscale funnel ... off` failed: {err.strip() or out.strip()}. "
            f"Inspect with `tailscale serve status`."
        )
    return {"disabled": True, "https_port": https_port, "path": path}


_SUCCESS_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Snapchat CLI</title></head>
<body style="font-family: system-ui, sans-serif; margin: 4rem auto; max-width: 32rem;">
<h2>Authorization received</h2>
<p>The Snapchat authorization code was delivered to the CLI on this machine.
You can close this tab and return to the terminal.</p>
</body></html>
"""

_ERROR_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Snapchat CLI</title></head>
<body style="font-family: system-ui, sans-serif; margin: 4rem auto; max-width: 32rem;">
<h2>Missing or invalid authorization response</h2>
<p>This callback expected <code>?code=...</code> from Snapchat. Restart the
login flow in the terminal and approve access again.</p>
</body></html>
"""


def wait_for_code(
    local_port: int,
    expected_state: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run a one-shot loopback HTTP listener until Snap redirects with a code.

    Accepts the code on any request path (tailscale's path handling varies by
    version), requires `state` to match when expected_state is set, and shuts
    down after the first valid hit. Raises TimeoutError when nothing valid
    arrives within `timeout` seconds.
    """
    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            params = parse_qs(urlsplit(self.path).query)
            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            valid = bool(code) and (expected_state is None or state == expected_state)
            body = _SUCCESS_PAGE if valid else _ERROR_PAGE
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            if valid:
                result["code"] = code
                result["state"] = state
                done.set()

        def log_message(self, fmt: str, *args: Any) -> None:
            pass  # keep CLI output clean

    server = ThreadingHTTPServer(("127.0.0.1", local_port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise TimeoutError(
                f"No authorization code arrived on 127.0.0.1:{local_port} "
                f"within {int(timeout)}s"
            )
    finally:
        server.shutdown()
        server.server_close()
    return result
