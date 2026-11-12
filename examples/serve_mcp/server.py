"""An MCP server written by hand, with Baton through ``client.serve()``.

One POST route speaking stateless Streamable HTTP: one JSON-RPC request in,
one JSON answer out. Events go to stderr. Run it, then see README.md.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from baton import Client, Organization, Principal
from baton.sinks import StdoutSink

TOOLS = [
    {
        "name": "get_weather",
        "description": "Today's weather for a city.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]
PARSE_ERROR = {"code": -32700, "message": "Parse error"}
USERS = {"token-alice": ("usr_alice", "org_small", "starter")}

baton = Client(
    vendor_id="weather-demo",
    sink=StdoutSink(),
    vendor_display_name="Weather",
    report_mode="on",
    tools=TOOLS,
)


def dispatch(message: dict[str, Any]) -> dict[str, Any] | None:
    """The server's own JSON-RPC handling, with no Baton in it."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:  # a notification gets no answer
        return None
    if method == "ping":
        result: dict[str, Any] = {}
    elif method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "weather", "version": "1.0"},
            "instructions": "Weather for any city.",
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and params.get("name") == "get_weather":
        city = (params.get("arguments") or {}).get("city", "")
        result = {"content": [{"type": "text", "text": f"Sunny in {city}"}]}
    else:
        error = {"code": -32601, "message": f"not found: {method}"}
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": error}
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        user = USERS.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
        if user is None:
            self.send_error(401)
            return
        try:
            message = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            self._send(400, {"jsonrpc": "2.0", "id": None, "error": PARSE_ERROR})
            return
        user_id, org_id, plan = user
        answer = baton.serve(
            message,
            dispatch,
            principal=Principal(
                principal_id=user_id,
                organization=Organization(organization_id=org_id, labels=[plan]),
            ),
            headers=dict(self.headers),
        )
        if answer is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, answer)

    def _send(self, status: int, answer: dict[str, Any]) -> None:
        body = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    try:
        ThreadingHTTPServer(("127.0.0.1", 8770), Handler).serve_forever()
    finally:
        baton.close()
