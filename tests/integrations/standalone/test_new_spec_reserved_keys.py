"""What a new-spec client puts on the wire.

fastmcp 4's own ``Client`` negotiates MCP ``2026-07-28`` and writes the
reserved ``io.modelcontextprotocol/*`` keys into every request's ``_meta``.
This file pins two consequences:

1. ``runtime_meta`` carries the reserved keys.
2. The key is written by the client library, not the caller, so a
   caller-supplied value in it is overwritten and ``client_observed.info``
   reports what the client declared for itself.

Skipped on any resolve that negotiates the old revision, which is what the
``fastmcp`` 2.x/3.x legs of the matrix do.
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
DECLARED_VERSION = "9.9.9"
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
        # A model, not a dict: fastmcp calls ``model_dump()`` on it.
        info = mcp_types.Implementation(name=declared, version=DECLARED_VERSION)
        async with Client(mcp, client_info=info) as client:
            await client.call_tool("lookup", {"name": "a"}, meta=meta)
    finally:
        await handle.aclose()
    events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
    calls = [e for e in events if e["event_type"].startswith("tool_call")]
    assert calls, "no tool_call events — the assertions below would be vacuous"
    return calls


def _assert_observed(calls: list[dict[str, Any]], declared: str) -> None:
    for ev in calls:
        assert ev["client_observed"] == {"info": {"name": declared, "version": DECLARED_VERSION}}, (
            ev["event_type"]
        )
        assert ev["agent_runtime"] == "unknown", ev["event_type"]


async def test_the_client_library_owns_the_reserved_key(tmp_path: Path) -> None:
    calls = await _drive(
        tmp_path / "e.jsonl",
        {CLIENT_INFO_KEY: {"name": "zed"}, "claudecode/toolUseId": "tu_1"},
        declared="some-gateway",
    )
    meta = calls[0].get("runtime_meta") or {}
    if meta.get(PROTOCOL_KEY) != NEW_SPEC:
        pytest.skip(f"this resolve negotiates {meta.get(PROTOCOL_KEY)!r}, not {NEW_SPEC}")

    assert CLIENT_INFO_KEY in meta
    assert "io.modelcontextprotocol/clientCapabilities" in meta

    # The planted `zed` is gone, replaced by the client's own declaration.
    assert meta[CLIENT_INFO_KEY]["name"] == "some-gateway", meta[CLIENT_INFO_KEY]
    _assert_observed(calls, "some-gateway")


async def test_a_caller_cannot_spoof_the_declaration_on_a_new_spec_client(tmp_path: Path) -> None:
    calls = await _drive(
        tmp_path / "e.jsonl",
        {CLIENT_INFO_KEY: {"name": "totally-not-me", "version": "0.0.1"}},
        declared="honest-client",
    )
    meta = calls[0].get("runtime_meta") or {}
    if meta.get(PROTOCOL_KEY) != NEW_SPEC:
        pytest.skip(f"this resolve negotiates {meta.get(PROTOCOL_KEY)!r}, not {NEW_SPEC}")
    _assert_observed(calls, "honest-client")
