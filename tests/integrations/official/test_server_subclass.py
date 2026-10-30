"""``install_baton`` on a vendor's subclass of the server class, over the wire.

Detection is by shape, so a subclass installs like the base class. A subclass
that overrides ``call_tool`` with its own gate runs that gate BEFORE Baton's
wrapped handler: the gate sees ``user_goal``, and a call it rejects never
reaches the wrapper, so it emits no ``tool_call_*`` events.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from baton.integrations._llm_text import USER_GOAL_PARAM_NAME
from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots
from tests._mcp_session import connected_session

GOAL = "1: echo the text; success is the same text back"


class VendorServer(MCPServerClass):  # type: ignore[misc,valid-type]
    pass


class GatedServer(MCPServerClass):  # type: ignore[misc,valid-type]
    """Refuses calls whose text is "deny", the way a credit check would."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate_saw: list[dict[str, Any]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        self.gate_saw.append(dict(arguments))
        if arguments.get("text") == "deny":
            raise PermissionError("out of credits")
        return await super().call_tool(name, arguments, *args, **kwargs)


@pytest.fixture
def events_path(tmp_path: Any) -> str:
    return str(tmp_path / "events.jsonl")


def _install(mcp: Any, events_path: str) -> tuple[Any, list[str]]:
    handler_saw: list[str] = []

    @mcp.tool()
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


def _is_error(result: Any) -> bool:
    return bool(result.model_dump(by_alias=True)["isError"])


async def _call(mcp: Any, text: str) -> tuple[list[str], bool]:
    async with connected_session(mcp) as session:
        listed = (await session.list_tools()).tools
        schema = listed[0].model_dump(by_alias=True)["inputSchema"]
        result = await session.call_tool("echo", {"text": text, USER_GOAL_PARAM_NAME: GOAL})
    return list(schema["properties"]), _is_error(result)


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
        assert mcp.gate_saw == [{"text": "hi", USER_GOAL_PARAM_NAME: GOAL}]
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
