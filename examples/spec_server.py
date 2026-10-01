#!/usr/bin/env python3
"""Local data-source server for the controlled-query demo.

Implements the exact server-side contract the host relies on:

  GET http://127.0.0.1:8090/spec?key=<device-model>

Returns HTTP 200 with a UTF-8 spec body, or 404 for unknown models. No other
paths, methods or parameters are needed; the sandbox host never sends any.

Run:  .venv/bin/python examples/spec_server.py
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SPECS = {
    "TH-100": {"weight_g": 320, "battery": "AAx2", "rated_v": 3.0},
    "TH-200": {"weight_g": 410, "battery": "Li-ion", "rated_v": 3.7},
    "AX-7": {"weight_g": 980, "battery": "mains", "rated_v": 12.0},
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path != "/spec":
            self.send_error(404)
            return
        keys = parse_qs(parts.query).get("key", [])
        if len(keys) != 1 or keys[0] not in SPECS:
            self.send_error(404, "unknown device model")
            return
        body = json.dumps(SPECS[keys[0]], separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        return


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 8090), Handler)
    print("spec source listening on http://127.0.0.1:8090/spec", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
