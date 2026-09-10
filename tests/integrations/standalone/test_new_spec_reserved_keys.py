"""What a NEW-SPEC client actually puts on the wire — measured, not assumed.

E1 (2026-09-09) concluded that no shipping client negotiates MCP 2026-07-28, so
`io.modelcontextprotocol/*` arrives empty and N4 buys nothing today. That was
measured against Claude Code, and it is still true of Claude Code.

It is NOT true of **fastmcp 4's own `Client`**, which negotiates `2026-07-28`
and writes all three reserved keys into every request's `_meta`. So the tier
this SDK wired "for the day clients move" is already live on that library, and
the day arrived through a dependency rather than through an agent.

Two consequences this file pins:

1. `runtime_meta` carries the reserved keys, so a consumer sees them now.
2. The key is written by the CLIENT LIBRARY, not the caller — a caller-supplied
   value in it is overwritten. That is a stronger property than the ladder
   claimed: on a new-spec transport the top tier cannot be spoofed by whoever
   is composing the tool call.

Skipped on any resolve that negotiates the old revision, which is what the
`fastmcp` 2.x/3.x legs of the matrix do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP

from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink

NEW_SPEC = "2026-07-28"
PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_KEY = "io.modelcontextprotocol/clientInfo"


async def _drive(events_path: Path, meta: dict[str, Any], declared: str) -> list[dict[str, Any]]:
    mcp: Any = FastMCP("new-spec")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"ok": True}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="ns",
            vendor_display_name="New Spec Vendor",
            consent_token="ct_ns",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        # A model, not a dict — fastmcp calls ``model_dump()`` on it.
        info = mcp_types.Implementation(name=declared, version="9.9.9")
        async with Client(mcp, client_info=info) as client:
            await client.call_tool("lookup", {"name": "a"}, meta=meta)
    finally:
        await handle.aclose()
    events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
    calls = [e for e in events if e["event_type"].startswith("tool_call")]
    assert calls, "no tool_call events — the assertions below would be vacuous"
    return calls


async def test_the_client_library_owns_the_reserved_key(tmp_path: Path) -> None:
    calls = await _drive(
        tmp_path / "e.jsonl",
        {CLIENT_INFO_KEY: {"name": "zed"}, "claudecode/toolUseId": "tu_1"},
        declared="some-gateway",
    )
    meta = calls[0].get("runtime_meta") or {}
    if meta.get(PROTOCOL_KEY) != NEW_SPEC:
        pytest.skip(f"this resolve negotiates {meta.get(PROTOCOL_KEY)!r}, not {NEW_SPEC}")

    # The reserved keys arrive — the traffic E1 said did not exist yet.
    assert CLIENT_INFO_KEY in meta
    assert "io.modelcontextprotocol/clientCapabilities" in meta

    # And the library wrote them: the planted `zed` is gone, replaced by what
    # the client declared for itself in `initialize`.
    assert meta[CLIENT_INFO_KEY]["name"] == "some-gateway", meta[CLIENT_INFO_KEY]
    assert {e["agent_runtime"] for e in calls} == {"some-gateway"}


async def test_a_caller_cannot_spoof_the_runtime_on_a_new_spec_client(tmp_path: Path) -> None:
    """The security-shaped half, stated separately because it is the useful one.

    On the old spec a caller composing a tool call could plant any name in this
    key and the top tier would report it. On a new-spec client it cannot: the
    library overwrites the key with its own declaration on every request.
    """
    calls = await _drive(
        tmp_path / "e.jsonl",
        {CLIENT_INFO_KEY: {"name": "totally-not-me"}},
        declared="honest-client",
    )
    meta = calls[0].get("runtime_meta") or {}
    if meta.get(PROTOCOL_KEY) != NEW_SPEC:
        pytest.skip(f"this resolve negotiates {meta.get(PROTOCOL_KEY)!r}, not {NEW_SPEC}")
    assert {e["agent_runtime"] for e in calls} == {"honest-client"}
