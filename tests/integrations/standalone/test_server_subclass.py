"""``baton.install_baton`` on a vendor's subclass of ``fastmcp.FastMCP``.

On fastmcp 3 and later, a ``call_tool`` override runs twice: first BEFORE the
middleware chain, where the gate sees ``user_goal``, then again from inside it
with the stripped arguments. A call the gate rejects on the first run is not
captured. fastmcp 2 has no public ``call_tool``, so the wire never reaches an
override there. Capturing rejected calls would change these tests on purpose.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from baton import VendorConfig, install_baton
from baton.integrations._llm_text import USER_GOAL_PARAM_NAME
from baton.sinks import FileSink
from tests._event_helpers import read_events, without_surface_snapshots

GOAL = "1: echo the text; success is the same text back"
HAS_PUBLIC_CALL_TOOL = hasattr(FastMCP, "call_tool")


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
        seen = dict(arguments or {})
        self.gate_saw.append(seen)
        if seen.get("text") == "deny":
            raise ToolError("out of credits")
        return await super().call_tool(name, arguments, *args, **kwargs)


class Run(NamedTuple):
    properties: list[str]
    is_error: bool
    handler_saw: list[str]
    tool_call_events: list[dict[str, Any]]


async def _run(mcp: Any, tmp_path: Any, text: str) -> Run:
    """Install, then list and call ``echo`` once over a client session."""
    handler_saw: list[str] = []

    @mcp.tool
    def echo(text: str) -> str:
        handler_saw.append(text)
        return text

    events_path = tmp_path / "events.jsonl"
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        async with Client(mcp) as client:
            listed = await client.list_tools()
            result = await client.call_tool(
                "echo", {"text": text, USER_GOAL_PARAM_NAME: GOAL}, raise_on_error=False
            )
        await handle.flush()
    finally:
        await handle.aclose()

    return Run(
        properties=list(
            next(t for t in listed if t.name == "echo").model_dump(by_alias=True)["inputSchema"][
                "properties"
            ]
        ),
        is_error=bool(result.is_error),
        handler_saw=handler_saw,
        tool_call_events=[
            e
            for e in without_surface_snapshots(read_events(events_path))
            if e["event_type"].startswith("tool_call_")
        ],
    )


def _types(run: Run) -> list[str]:
    return [e["event_type"] for e in run.tool_call_events]


class TestPlainSubclass:
    async def test_installs_injects_strips_and_captures(self, tmp_path: Any) -> None:
        run = await _run(VendorServer("vendor"), tmp_path, "hi")

        assert USER_GOAL_PARAM_NAME in run.properties
        assert not run.is_error
        assert run.handler_saw == ["hi"]
        start, end = run.tool_call_events
        assert start["event_type"] == "tool_call_start"
        assert start["payload"]["tool_name"] == "echo"
        assert start["payload"]["params"] == {"text": "hi"}
        assert start["payload"]["call_intent"] == GOAL
        assert end["event_type"] == "tool_call_end"
        assert end["call_id"] == start["call_id"]


class TestSubclassOverridingCallTool:
    async def test_the_gate_runs_before_the_strip(self, tmp_path: Any) -> None:
        mcp = GatedServer("vendor")
        run = await _run(mcp, tmp_path, "hi")

        assert not run.is_error
        assert run.handler_saw == ["hi"]
        assert _types(run) == ["tool_call_start", "tool_call_end"]
        if HAS_PUBLIC_CALL_TOOL:
            assert mcp.gate_saw == [{"text": "hi", USER_GOAL_PARAM_NAME: GOAL}, {"text": "hi"}]
        else:
            assert mcp.gate_saw == []

    async def test_a_call_the_gate_rejects_is_not_captured(self, tmp_path: Any) -> None:
        mcp = GatedServer("vendor")
        run = await _run(mcp, tmp_path, "deny")

        if HAS_PUBLIC_CALL_TOOL:
            assert mcp.gate_saw == [{"text": "deny", USER_GOAL_PARAM_NAME: GOAL}]
            assert run.is_error
            assert run.handler_saw == []
            assert run.tool_call_events == []
        else:
            assert mcp.gate_saw == []
            assert not run.is_error
            assert run.handler_saw == ["deny"]
            assert _types(run) == ["tool_call_start", "tool_call_end"]
