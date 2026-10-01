#!/usr/bin/env python3
"""Local-only demo data source for device model specifications.

Serves GET /specs?model=<id> and requires a fixed bearer token when started
with --require-auth. It is a plain stdlib HTTP server so the demo needs no
extra dependency. Run:

    .venv/bin/python examples/device_spec_server.py --port 8091

Configure the sandbox to expose it under source id "device_specs" via
SANDBOX_SOURCES (see examples/sources.env).
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SPECS = {
    "alpha-1": {"model": "alpha-1", "display": "7-inch", "sensors": ["temp", "humidity"]},
    "beta-2": {"model": "beta-2", "display": "10-inch", "sensors": ["gps", "temp"]},
}


def make_handler(require_auth: bool):
    token = "demo-token-123"

    class SpecHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep demo output quiet
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/specs":
                self.send_response(404)
                self.end_headers()
                return
            if require_auth and self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return
            values = parse_qs(parsed.query).get("model", [])
            if len(values) != 1:
                self.send_response(400)
                self.end_headers()
                return
            model = values[0]
            spec = SPECS.get(model)
            if spec is None:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(spec, separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return SpecHandler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--require-auth", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.require_auth))
    print(f"device spec source on http://{args.host}:{args.port}/specs (auth={args.require_auth})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
