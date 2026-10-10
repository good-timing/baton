"""What the STANDALONE fastmcp adapter reports about the calling client.

The official adapter's twin is ``tests/integrations/official/test_observed_client.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink
from tests._event_helpers import CALLER_EVENT_TYPES, read_events, without_surface_snapshots
from tests.integrations.standalone.test_concurrent_sessions import _running_server
from tests.integrations.standalone.test_iserror_reclassify import FLAG_IS_EXPRESSIBLE, ToolResult


async def test_the_surface_snapshot_carries_no_client_and_caller_events_do(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "e.jsonl"
    mcp: Any = FastMCP("observed-standalone")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool
    def broken() -> str:
        raise ValueError("vendor bug")

    @mcp.tool
    def refused() -> Any:
        """An error result without raising, which ends at its own emit site."""
        return ToolResult(
            content=[mcp_types.TextContent(type="text", text="not allowed")], is_error=True
        )

    failing = ("broken", "refused") if FLAG_IS_EXPRESSIBLE else ("broken",)

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="oc",
            vendor_display_name="Observed Vendor",
            consent_token="ct_oc",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        declared = mcp_types.Implementation(name="claude-ai", version="1.2.3")
        async with Client(mcp, client_info=declared) as client:
            await client.list_tools()
            await client.call_tool("lookup", {"name": "alice"})
            for name in failing:
                with pytest.raises(Exception):  # noqa: B017 fastmcp wraps the tool's error
                    await client.call_tool(name, {})
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "what_happened": "the call came back unusable"},
            )
    finally:
        await handle.aclose()

    events = read_events(events_path)
    assert {ev["event_type"] for ev in events} == (
        CALLER_EVENT_TYPES | {"surface_snapshot", "tool_list_start", "tool_list_end"}
    )
    errors = [ev for ev in events if ev["event_type"] == "tool_call_error"]
    assert len(errors) == len(failing)
    for ev in events:
        where = f"{ev['event_type']} {ev['payload'].get('tool_name', '')}"
        if ev["event_type"] == "surface_snapshot":
            assert ev["client_observed"] is None, where
        else:
            assert ev["client_observed"] == {"info": {"name": "claude-ai", "version": "1.2.3"}}, (
                where
            )
        assert ev["agent_runtime"] == "unknown", where


async def test_a_user_agent_sent_over_http_is_observed_and_scrubbed() -> None:
    """Over a real socket, because the in-process client has no HTTP request."""
    credential = "Bearer s3cret-token"

    def redacts_the_marker(value: Any) -> Any:
        return value.replace("MARK", "[R]") if isinstance(value, str) else value

    with _running_server("http", scrubber=redacts_the_marker) as (sink, url):
        # Two callers: the fixture's tool returns only once both are inside it.
        async def call_as(caller: str) -> None:
            transport = StreamableHttpTransport(
                url,
                headers={
                    "User-Agent": "agent-MARK/1.0",
                    "X-Anthropic-Client": "desktop-MARK",
                    "Authorization": credential,
                },
            )
            async with Client(transport) as client:
                await client.call_tool("work", {"caller": caller})
                (annotate,) = [t.name for t in await client.list_tools() if t.name != "work"]
                await client.call_tool(
                    annotate, {"user_goal": "g", "what_happened": "the call came back unusable"}
                )

        await asyncio.gather(call_as("client-a"), call_as("client-b"))

    dumped = [ev.model_dump(mode="json") for ev in sink.events]
    callers = without_surface_snapshots(dumped)
    assert {ev["event_type"] for ev in callers} == {
        "tool_call_start",
        "tool_call_end",
        "annotation",
        "tool_list_start",
        "tool_list_end",
    }
    for ev in callers:
        assert ev["client_observed"]["headers"] == {
            "user-agent": "agent-[R]/1.0",
            "x-anthropic-client": "desktop-[R]",
        }, ev["event_type"]
    assert "s3cret" not in json.dumps(dumped)
