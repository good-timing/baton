"""``baton.install_baton`` on a vendor's subclass of the server class, over the wire,
and the outer ``tools/call`` seam that runs above the subclass's own ``call_tool``.

The outer seam strips ``user_goal`` before a ``call_tool`` override sees it, and
a call the override refuses is captured with ``failure_kind: "refused"``. The
inner capture still files every call that reaches the tool, so each call yields
one pair.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import pytest

from baton import VendorConfig, install_baton
from baton.integrations import _lifecycle
from baton.integrations._llm_text import USER_GOAL_PARAM_NAME
from baton.integrations.official._compat import MCPServerClass
from baton.sinks import FileSink, Sink
from tests._event_helpers import read_events, without_surface_snapshots
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


class _RaisingSink(Sink):
    async def write(self, event: Any) -> None:
        raise OSError("disk full")

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class Run(NamedTuple):
    results: list[Any]
    handler_saw: list[dict[str, Any]]
    tool_call_events: list[dict[str, Any]]
    properties: list[str]


async def _run(
    mcp: Any,
    tmp_path: Any,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    installs: int = 1,
    sink: Sink | None = None,
    **config: Any,
) -> Run:
    """Register ``echo`` and ``boom``, install, then make ``calls`` over a client
    session. A call is ``(tool, arguments)``; ``GOAL`` is added to each."""
    handler_saw: list[dict[str, Any]] = []

    @mcp.tool()
    def echo(text: str) -> str:
        handler_saw.append({"text": text})
        return text

    @mcp.tool()
    def boom(text: str) -> str:
        raise ValueError("tool broke")

    events_path = tmp_path / "events.jsonl"
    handles = [
        install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=sink or FileSink(str(events_path)),
                **config,
            ),
        )
        for _ in range(installs)
    ]
    results = []
    try:
        async with connected_session(mcp) as session:
            listed = (await session.list_tools()).tools
            for tool, arguments in calls:
                result = await session.call_tool(tool, {**arguments, USER_GOAL_PARAM_NAME: GOAL})
                results.append(result.model_dump(by_alias=True))
        for handle in handles:
            await handle.flush()
    finally:
        for handle in handles:
            await handle.aclose()

    events = read_events(events_path) if events_path.exists() else []
    return Run(
        results=results,
        handler_saw=handler_saw,
        tool_call_events=[
            e for e in without_surface_snapshots(events) if e["event_type"].startswith("tool_call_")
        ],
        properties=list(
            next(t for t in listed if t.name == "echo").model_dump(by_alias=True)["inputSchema"][
                "properties"
            ]
        ),
    )


def _types(run: Run) -> list[str]:
    return [e["event_type"] for e in run.tool_call_events]


class TestPlainSubclass:
    async def test_installs_injects_strips_and_captures(self, tmp_path: Any) -> None:
        run = await _run(VendorServer("vendor"), tmp_path, [("echo", {"text": "hi"})])

        assert USER_GOAL_PARAM_NAME in run.properties
        assert not run.results[0]["isError"]
        assert run.handler_saw == [{"text": "hi"}]
        start, end = run.tool_call_events
        assert start["event_type"] == "tool_call_start"
        assert start["payload"]["tool_name"] == "echo"
        assert start["payload"]["params"] == {"text": "hi"}
        assert start["payload"]["call_intent"] == GOAL
        assert end["event_type"] == "tool_call_end"
        assert end["call_id"] == start["call_id"]


class TestSubclassOverridingCallTool:
    async def test_the_gate_never_sees_user_goal(self, tmp_path: Any) -> None:
        mcp = GatedServer("vendor")
        run = await _run(mcp, tmp_path, [("echo", {"text": "hi"})])

        assert mcp.gate_saw == [{"text": "hi"}]
        assert run.handler_saw == [{"text": "hi"}]
        assert _types(run) == ["tool_call_start", "tool_call_end"]
        assert run.tool_call_events[0]["payload"]["call_intent"] == GOAL

    async def test_a_call_the_gate_refuses_is_captured(self, tmp_path: Any) -> None:
        mcp = GatedServer("vendor")
        run = await _run(mcp, tmp_path, [("echo", {"text": "deny"})])

        assert mcp.gate_saw == [{"text": "deny"}]
        assert run.results[0]["isError"]
        assert run.handler_saw == []
        start, error = run.tool_call_events
        assert start["event_type"] == "tool_call_start"
        assert start["payload"]["params"] == {"text": "deny"}
        assert start["payload"]["call_intent"] == GOAL
        assert error["event_type"] == "tool_call_error"
        assert error["call_id"] == start["call_id"]
        assert error["payload"]["error_type"] == "tool_error"
        assert error["payload"]["failure_kind"] == "refused"
        assert "out of credits" in error["payload"]["error_body"]

    async def test_a_refusals_message_is_withheld_under_off(self, tmp_path: Any) -> None:
        run = await _run(
            GatedServer("vendor"),
            tmp_path,
            [("echo", {"text": "deny"})],
            result_capture_mode="off",
        )

        error = run.tool_call_events[1]["payload"]
        assert error["failure_kind"] == "refused"
        assert error["error_body"] == ""
        assert error["result"] is None
        assert error["result_capture"] == "off"

    async def test_two_installs_capture_a_refusal_once(self, tmp_path: Any) -> None:
        run = await _run(GatedServer("vendor"), tmp_path, [("echo", {"text": "deny"})], installs=2)

        assert _types(run) == ["tool_call_start", "tool_call_error"]


class TestOuterSeam:
    async def test_a_tool_that_raises_keeps_its_exception_class_in_one_pair(
        self, tmp_path: Any
    ) -> None:
        run = await _run(VendorServer("vendor"), tmp_path, [("boom", {"text": "x"})])

        start, error = run.tool_call_events
        assert error["call_id"] == start["call_id"]
        assert error["payload"]["error_type"] == "ValueError"
        assert error["payload"]["failure_kind"] is None

    async def test_an_unknown_tool_is_captured_as_unknown_tool(self, tmp_path: Any) -> None:
        run = await _run(VendorServer("vendor"), tmp_path, [("nope", {})])

        start, error = run.tool_call_events
        assert start["payload"]["tool_name"] == "nope"
        assert start["payload"]["params"] == {}
        assert start["payload"]["call_intent"] == GOAL
        assert error["payload"]["failure_kind"] == "unknown_tool"

    async def test_an_unknown_tools_message_stays_under_off(self, tmp_path: Any) -> None:
        run = await _run(
            VendorServer("vendor"), tmp_path, [("nope", {})], result_capture_mode="off"
        )

        error = run.tool_call_events[1]["payload"]
        assert error["failure_kind"] == "unknown_tool"
        assert "nope" in error["error_body"]

    async def test_a_tools_own_user_goal_reaches_it(self, tmp_path: Any) -> None:
        mcp = GatedServer("vendor")
        received: list[str] = []

        @mcp.tool()
        def approve(doc: str, user_goal: str = "") -> str:
            received.append(user_goal)
            return doc

        await _run(mcp, tmp_path, [("approve", {"doc": "d"})])

        assert received == [GOAL]
        assert mcp.gate_saw == [{"doc": "d", USER_GOAL_PARAM_NAME: GOAL}]

    async def test_a_rejected_argument_on_the_annotation_tool_files_no_tool_call(
        self, tmp_path: Any
    ) -> None:
        run = await _run(
            VendorServer("vendor"),
            tmp_path,
            [("test-vendor_annotate", {"what_happened": 3})],
            report_mode="on",
            annotation_tool_name="test-vendor_annotate",
        )

        assert run.results[0]["isError"]
        assert run.tool_call_events == []


class RoutingServer(MCPServerClass):  # type: ignore[misc,valid-type]
    """Serves ``alias`` as ``echo``, and any ``dyn_*`` tool its registry lacks."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        if name.startswith("dyn_"):
            raise RuntimeError(f"customer record 4111 for {name}")
        return await super().call_tool(
            "echo" if name == "alias" else name, arguments, *args, **kwargs
        )


class TestServerWithItsOwnCallTool:
    async def test_a_routed_call_is_one_pair_with_its_goal(self, tmp_path: Any) -> None:
        run = await _run(RoutingServer("vendor"), tmp_path, [("alias", {"text": "hi"})])

        assert run.handler_saw == [{"text": "hi"}]
        start, end = run.tool_call_events
        assert start["payload"]["call_intent"] == GOAL
        assert end["event_type"] == "tool_call_end"

    async def test_a_tool_outside_the_registry_is_refused_not_unknown(self, tmp_path: Any) -> None:
        run = await _run(
            RoutingServer("vendor"),
            tmp_path,
            [("dyn_lookup", {"text": "x"})],
            result_capture_mode="off",
        )

        start, error = run.tool_call_events
        assert start["payload"]["params"] == {"text": "x"}
        assert start["payload"]["call_intent"] == GOAL
        assert error["payload"]["failure_kind"] == "refused"
        assert error["payload"]["error_body"] == ""
        assert error["payload"]["result_capture"] == "off"


class TestFailOpen:
    async def test_a_raising_sink_changes_no_result(self, tmp_path: Any) -> None:
        calls = [("echo", {"text": "hi"}), ("echo", {"text": "deny"}), ("nope", {})]
        bare = GatedServer("vendor")
        with_baton = GatedServer("vendor")

        expected = await _run_without_baton(bare, calls)
        run = await _run(with_baton, tmp_path / "b", calls, sink=_RaisingSink())

        assert run.results == expected
        assert [g for g in with_baton.gate_saw if "text" in g] == [{"text": "hi"}, {"text": "deny"}]

    async def test_a_failing_strip_falls_back_to_the_inner_capture(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("strip broke")

        monkeypatch.setattr(_lifecycle, "pop_goal_params", broken)
        run = await _run(VendorServer("vendor"), tmp_path, [("echo", {"text": "hi"})])

        assert not run.results[0]["isError"]
        assert run.handler_saw == [{"text": "hi"}]
        assert run.tool_call_events[0]["payload"]["call_intent"] == GOAL

    async def test_a_failing_refusal_capture_changes_no_result(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken() -> str:
            raise RuntimeError("id broke")

        monkeypatch.setattr(_lifecycle, "uuid7", broken)
        mcp = GatedServer("vendor")
        run = await _run(mcp, tmp_path, [("echo", {"text": "deny"})])

        assert run.results[0]["isError"]
        assert "out of credits" in run.results[0]["content"][0]["text"]
        assert mcp.gate_saw == [{"text": "deny"}]
        assert run.tool_call_events == []


async def _run_without_baton(mcp: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    @mcp.tool()
    def echo(text: str) -> str:
        return text

    results = []
    async with connected_session(mcp) as session:
        await session.list_tools()
        for tool, arguments in calls:
            results.append((await session.call_tool(tool, arguments)).model_dump(by_alias=True))
    return results
