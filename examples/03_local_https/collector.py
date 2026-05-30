"""Tiny stdlib HTTP collector that accepts Baton events on ``/v0/events``.

Stand-alone — no external dependencies. Mirrors the wire contract that a
production Console (or any compatible collector) implements. Prints each
event as JSON to stdout so you can pipe it through ``jq`` for inspection.

Run in one terminal:

    python collector.py             # listens on 127.0.0.1:8765

Then in another:

    python demo.py                  # ships events to 127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v0/events":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"missing or malformed Authorization header")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        event = json.loads(body)
        # Echo the event to stdout so the operator can see what's landing.
        print(json.dumps(event), flush=True)

        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        # Silence the default per-request access log; we print events instead.
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"Listening on http://127.0.0.1:{args.port}/v0/events", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
