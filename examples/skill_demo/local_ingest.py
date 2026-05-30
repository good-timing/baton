"""Local collector emulator for the skill-demo example.

Mimics a Console-style ``POST /v0/events`` endpoint so a Baton-instrumented
MCP server can ship events to a real HTTP target during local development.
Each received event is logged to a JSONL file + printed to stderr so you can
``tail -f`` and watch events arrive in real time.

Pure stdlib — no external deps. Sync (one request at a time); fine for dev/test.

Run:
    python local_ingest.py

Env vars:
    BATON_INGEST_API_KEY     bearer token the SDK must send (default: "dev-key")
    BATON_INGEST_LOG         path to JSONL log file (default: ./events.jsonl)
    BATON_INGEST_HOST        bind host (default: 127.0.0.1)
    BATON_INGEST_PORT        bind port (default: 8000)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

API_KEY = os.environ.get("BATON_INGEST_API_KEY", "dev-key")
LOG_FILE = Path(os.environ.get("BATON_INGEST_LOG", "events.jsonl")).absolute()
HOST = os.environ.get("BATON_INGEST_HOST", "127.0.0.1")
PORT = int(os.environ.get("BATON_INGEST_PORT", "8000"))

_write_lock = threading.Lock()


class IngestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Silence default access log; we log meaningful events ourselves on stderr.
        pass

    def do_POST(self) -> None:  # noqa: N802 (stdlib name)
        if self.path != "/v0/events":
            self._respond(404, {"error": "not found"})
            return

        # Bearer auth
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._respond(401, {"error": "missing Authorization header"})
            return
        token = auth.removeprefix("Bearer ").strip()
        if token != API_KEY:
            self._respond(401, {"error": "invalid api key"})
            return

        # Body
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            self._respond(400, {"error": "empty body"})
            return
        raw = self.rfile.read(length)
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._respond(400, {"error": f"malformed json: {exc}"})
            return

        self._log_event(event)
        self._respond(201, {})

    def _respond(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _log_event(self, event: dict) -> None:
        record = {"received_at": datetime.now(UTC).isoformat(), "event": event}
        with _write_lock:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a") as f:
                f.write(json.dumps(record) + "\n")
        # Concise stderr line — easy to scan when watching live
        event_type = event.get("event_type", "?")
        session = event.get("session_id", "?")
        seq = event.get("sequence_number", "?")
        runtime = event.get("agent_runtime", "?")
        payload = event.get("payload") or {}
        tool = payload.get("tool_name", "")
        signal_type = payload.get("signal_type")
        extra = f" tool={tool}" if tool else ""
        if signal_type:
            extra += f" signal_type={signal_type}"
        # For annotation events, show which fields were populated (names
        # only; values stay in events.jsonl) so live tail tells you whether
        # the agent actually filled the payload.
        if event_type == "annotation":
            present = [
                k
                for k in ("intent", "expected_outcome", "workflow", "suggested_improvement", "context")
                if payload.get(k) is not None
            ]
            if present:
                extra += f" fields={','.join(present)}"
        print(  # noqa: T201 stderr is OK in spike scripts
            f"[{record['received_at']}] {event_type:18s} "
            f"runtime={runtime:14s} seq={seq:3} session={session}{extra}",
            file=sys.stderr,
        )


def main() -> None:
    server = HTTPServer((HOST, PORT), IngestHandler)
    print(  # noqa: T201
        f"Baton local ingest emulator listening on http://{HOST}:{PORT}",
        file=sys.stderr,
    )
    print(f"Bearer key: {API_KEY}", file=sys.stderr)  # noqa: T201
    print(f"Events logged to: {LOG_FILE}", file=sys.stderr)  # noqa: T201
    print(file=sys.stderr)  # noqa: T201
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)  # noqa: T201


if __name__ == "__main__":
    main()
