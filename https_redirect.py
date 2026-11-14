#!/usr/bin/env python3
"""Plain-HTTP listener that sends browsers to the HTTPS panel.

run.py starts this in a background thread on the HTTP port whenever HTTPS is on,
so old http:// links keep working. It can also run on its own:
    https_redirect.py [http_port=8080] [https_port=443]
"""
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d+)?$")


def make_server(http_port, https_port, bind="0.0.0.0"):
    class Redirect(BaseHTTPRequestHandler):
        def _redirect(self, code):
            m = HOST_RE.match((self.headers.get("Host") or "").strip())
            # Missing/garbled Host header: use the address the client actually connected to
            host = m.group(1) if m else self.connection.getsockname()[0]
            port = "" if https_port == 443 else f":{https_port}"
            path = self.path if self.path.startswith("/") else "/"
            self.send_response(code)
            self.send_header("Location", f"https://{host}{port}{path}")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        # Temporary redirects on purpose: HTTPS can be switched off again in Panel Settings,
        # and browsers remember a permanent (301/308) redirect indefinitely.
        def do_GET(self):
            self._redirect(302)

        def do_HEAD(self):
            self._redirect(302)

        def do_POST(self):
            self._redirect(307)   # 307 keeps the method and body

        do_PUT = do_PATCH = do_DELETE = do_POST

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((bind, http_port), Redirect)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    http_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    https_port = int(sys.argv[2]) if len(sys.argv) > 2 else 443
    make_server(http_port, https_port).serve_forever()
