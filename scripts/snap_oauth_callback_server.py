#!/usr/bin/env python3
"""Tiny OAuth callback catcher for Snap Marketing API authorize-code flow.

Serves /snap-oauth-callback behind Tailscale Funnel, captures ?code=...,
writes it to ~/.config/snapchat-ads-cli/oauth_code.txt (mode 0600), and
shows a success page. No secrets are logged.
"""
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT = os.path.expanduser("~/.config/snapchat-ads-cli/oauth_code.txt")
PORT = 8788


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        if code:  # Funnel strips the /snap-oauth-callback prefix before proxying
            fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(code)
            body = (
                "<html><body style='font-family:sans-serif;padding:3em'>"
                "<h2>Snapchat authorization captured.</h2>"
                "<p>You can close this tab and go back to the agent session.</p>"
                "</body></html>"
            )
            self.send_response(200)
        else:
            body = (
                "<html><body style='font-family:sans-serif;padding:3em'>"
                "<h2>Waiting for Snapchat OAuth redirect...</h2>"
                f"<p>No <code>code</code> parameter on this request (path: {parsed.path}).</p>"
                "</body></html>"
            )
            self.send_response(200)
        data = body.encode()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep codes out of stdout logs
        print(f"request: {self.path.split('?')[0]}")


if __name__ == "__main__":
    print(f"listening on 127.0.0.1:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
