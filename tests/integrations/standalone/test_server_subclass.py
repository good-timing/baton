"""``install_baton`` on a vendor's subclass of ``fastmcp.FastMCP``.

Detection is by shape, so a subclass installs like the base class. On fastmcp
3 and later, a ``call_tool`` override runs BEFORE the middleware chain: the
gate sees ``user_goal``, and a call it rejects emits no ``tool_call_*`` events.
fastmcp 2 has no public ``call_tool``, so an override there never runs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from baton.integrations._llm_text import USER_GOAL_PARAM_NAME
from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots

GOAL = "1: echo the text; success is the same text back"


class VendorServer(FastMCP):  # type: ignore[misc]
    pass


class GatedServer(FastMCP):  # type: ignore[misc]
    """Refuses calls whose text is "deny", the way a credit check would."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate_saw: list[dict[str, Any]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *args: Any, **kwargs: Any
    ) -> Any:
        self.gate_saw.append(dict(arguments or {}))
        if (arguments or {}).get("text") == "deny":
            raise ToolError("out of credits")
        return await super().call_tool(name, arguments, *args, **kwargs)


needs_public_call_tool = pytest.mark.skipif(
    not hasattr(FastMCP, "call_tool"),
    reason="this fastmcp has no public call_tool, so an override is never called",
)


@pytest.fixture
def events_path(tmp_path: Any) -> str:
    return str(tmp_path / "events.jsonl")


def _install(mcp: Any, events_path: str) -> tuple[Any, list[str]]:
    handler_saw: list[str] = []

    @mcp.tool
    def echo(text: str) -> str:
        handler_saw.append(text)
        return text

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(events_path),
        ),
    )
    return handle, handler_saw


def _tool_call_events(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    return [
        e for e in without_surface_snapshots(events) if e["event_type"].startswith("tool_call_")
    ]


async def _call(mcp: Any, text: str) -> tuple[list[str], bool]:
    async with Client(mcp) as client:
        listed = await client.list_tools()
        result = await client.call_tool(
            "echo", {"text": text, USER_GOAL_PARAM_NAME: GOAL}, raise_on_error=False
        )
    schema = listed[0].model_dump(by_alias=True)["inputSchema"]
    return list(schema["properties"]), bool(result.is_error)


class TestPlainSubclass:
    async def test_installs_injects_strips_and_captures(self, events_path: str) -> None:
        mcp = VendorServer("vendor")
        handle, handler_saw = _install(mcp, events_path)
        try:
            properties, is_error = await _call(mcp, "hi")
            await handle.flush()
        finally:
            await handle.aclose()

        assert USER_GOAL_PARAM_NAME in properties
        assert not is_error
        assert handler_saw == ["hi"]
        start, end = _tool_call_events(events_path)
        assert start["event_type"] == "tool_call_start"
        assert start["payload"]["tool_name"] == "echo"
        assert start["payload"]["params"] == {"text": "hi"}
        assert start["payload"]["call_intent"] == GOAL
        assert end["event_type"] == "tool_call_end"


@needs_public_call_tool
class TestSubclassOverridingCallTool:
    async def test_the_gate_runs_before_the_strip(self, events_path: str) -> None:
        mcp = GatedServer("vendor")
        handle, handler_saw = _install(mcp, events_path)
        try:
            _, is_error = await _call(mcp, "hi")
            await handle.flush()
        finally:
            await handle.aclose()

        assert not is_error
        # fastmcp calls call_tool again from inside the chain, so the gate can
        # run twice; only the first, outer call is ours to pin.
        assert mcp.gate_saw[0] == {"text": "hi", USER_GOAL_PARAM_NAME: GOAL}
        assert handler_saw == ["hi"]
        assert [e["event_type"] for e in _tool_call_events(events_path)] == [
            "tool_call_start",
            "tool_call_end",
        ]

    async def test_a_call_the_gate_rejects_emits_no_tool_call_events(
        self, events_path: str
    ) -> None:
        mcp = GatedServer("vendor")
        handle, handler_saw = _install(mcp, events_path)
        try:
            _, is_error = await _call(mcp, "deny")
            await handle.flush()
        finally:
            await handle.aclose()

        assert is_error
        assert handler_saw == []
        assert _tool_call_events(events_path) == []
