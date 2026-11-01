"""A result the server rejects against the tool's output schema files as a failure.

On ``mcp`` 1.x (under fastmcp 2.x and 3.x) the low-level server checks
``structuredContent`` against ``outputSchema`` after fastmcp's middleware chain
has returned, and sends the client an error. ``mcp`` 2.x has no such check, so
under fastmcp 4.x the server sends a success and only a validating client
catches it.
"""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP

try:  # fastmcp 3.x / 4.x
    from fastmcp.tools import ToolResult
except ImportError:  # 2.14.7
    from fastmcp.tools.tool import ToolResult

from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink
from tests._event_helpers import read_events, without_surface_snapshots

SERVER_CHECKS_OUTPUT = int(version("mcp").split(".")[0]) < 2
REJECTED_TOOLS = ("wrong_value", "no_structured_content")


async def _drive(events_path: Path, **config: Any) -> list[dict[str, Any]]:
    mcp = FastMCP("schema")

    @mcp.tool
    def wrong_value(x: str) -> dict[str, int]:
        return {"a": "oops"}  # type: ignore[dict-item]

    @mcp.tool
    def no_structured_content(x: str) -> dict[str, int]:
        return ToolResult(content=[mcp_types.TextContent(type="text", text="oops")])  # type: ignore[return-value]

    @mcp.tool
    def matches(x: str) -> dict[str, int]:
        return {"a": 1}

    install_baton(
        mcp,
        VendorConfig(
            vendor_id="os",
            vendor_display_name="Schema Vendor",
            consent_token="ct_os",
            sink=FileSink(str(events_path)),
            **config,
        ),
    )
    async with Client(mcp) as client:
        for name in (*REJECTED_TOOLS, "matches"):
            try:
                await client.call_tool_mcp(name, {"x": "1"})
            except Exception:
                pass  # mcp 2.x's client raises on the invalid result

    return without_surface_snapshots(read_events(events_path))


def _pair(events: list[dict[str, Any]], tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    mine = [ev for ev in events if (ev.get("payload") or {}).get("tool_name") == tool]
    assert len(mine) == 2, f"{tool}: expected one start and one terminal event, got {mine}"
    start, terminal = mine
    assert start["event_type"] == "tool_call_start"
    assert terminal["call_id"] == start["call_id"]
    return start, terminal


@pytest.mark.parametrize("tool", REJECTED_TOOLS)
async def test_a_rejected_result_files_as_the_servers_answer(tmp_path: Path, tool: str) -> None:
    _, terminal = _pair(await _drive(tmp_path / "events.jsonl"), tool)
    if not SERVER_CHECKS_OUTPUT:
        assert terminal["event_type"] == "tool_call_end"
        return
    assert terminal["event_type"] == "tool_call_error"
    assert terminal["payload"]["failure_kind"] == "output_schema_mismatch"
    assert terminal["payload"]["error_type"] == "tool_error"
    assert "Output validation error" in terminal["payload"]["error_body"]


async def test_a_matching_result_is_still_a_success(tmp_path: Path) -> None:
    _, terminal = _pair(await _drive(tmp_path / "events.jsonl"), "matches")
    assert terminal["event_type"] == "tool_call_end"
    assert terminal["payload"]["result"] is not None


@pytest.mark.skipif(not SERVER_CHECKS_OUTPUT, reason="mcp 2.x sends a success")
async def test_the_kind_survives_withholding_and_the_message_does_not(tmp_path: Path) -> None:
    events = await _drive(tmp_path / "events.jsonl", result_capture_mode="off")
    _, terminal = _pair(events, "wrong_value")
    assert terminal["payload"]["failure_kind"] == "output_schema_mismatch"
    assert "oops" not in json.dumps(terminal)


async def _drive_outer_middleware(
    events_path: Path, middleware: Any, *, client_raises: bool
) -> dict[str, Any]:
    mcp = FastMCP("outer")

    @mcp.tool
    def matches(x: str) -> dict[str, int]:
        return {"a": 1}

    install_baton(
        mcp,
        VendorConfig(
            vendor_id="os",
            vendor_display_name="Schema Vendor",
            consent_token="ct_os",
            sink=FileSink(str(events_path)),
        ),
    )
    mcp.middleware.insert(0, middleware)
    async with Client(mcp) as client:
        if client_raises:
            with pytest.raises(Exception):  # noqa: B017 - the class differs by fastmcp major
                await client.call_tool_mcp("matches", {"x": "1"})
        else:
            result = await client.call_tool_mcp("matches", {"x": "1"})
            assert getattr(result, "is_error", None) or getattr(result, "isError", None)
    _, terminal = _pair(without_surface_snapshots(read_events(events_path)), "matches")
    return terminal


async def test_an_outer_middlewares_raise_after_the_tool_is_a_failure(tmp_path: Path) -> None:
    """On mcp 1.x the server turns the raise into an error result, which the
    outer seam cannot tell from a schema rejection. On mcp 2.x the raise
    escapes the server, and is recorded as a raise."""
    from fastmcp.server.middleware import Middleware

    class FailsAfterTheTool(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            await call_next(context)
            raise ValueError("quota exceeded")

    terminal = await _drive_outer_middleware(
        tmp_path / "events.jsonl", FailsAfterTheTool(), client_raises=not SERVER_CHECKS_OUTPUT
    )
    assert terminal["event_type"] == "tool_call_error"
    if SERVER_CHECKS_OUTPUT:
        assert terminal["payload"]["failure_kind"] == "output_schema_mismatch"
    else:
        assert terminal["payload"]["failure_kind"] is None
        assert terminal["payload"]["error_type"] == "ValueError"


@pytest.mark.skipif(SERVER_CHECKS_OUTPUT, reason="covered by the raise test on mcp 1.x")
async def test_an_error_result_where_no_schema_check_runs_is_not_named(tmp_path: Path) -> None:
    from fastmcp.server.middleware import Middleware

    class ReturnsAnErrorAfterTheTool(Middleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            await call_next(context)
            text = mcp_types.TextContent(type="text", text="quota exceeded")
            return ToolResult(content=[text], is_error=True)  # type: ignore[call-arg]

    terminal = await _drive_outer_middleware(
        tmp_path / "events.jsonl", ReturnsAnErrorAfterTheTool(), client_raises=False
    )
    assert terminal["event_type"] == "tool_call_error"
    assert terminal["payload"]["failure_kind"] is None
