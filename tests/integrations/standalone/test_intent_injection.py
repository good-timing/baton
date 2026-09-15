"""Tests for per-tool intent-param injection in the FastMCP middleware.

Mirrors baton-extmcp's vendor-neutral intent-injection matrix, adapted to the
in-process FastMCP Client harness: inject ``user_goal``/``expected_result`` on
``tools/list``, strip both on ``tools/call``, ride ``user_goal`` on
``tool_call_start.payload.call_intent``, and synthesise one proactive
annotation (carrying ``expected_result`` too, if present) from the session's
first injected intent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    OVERALL_TASK_PARAM_NAME,
    USER_GOAL_PARAM_NAME,
    build_expected_result_param_description,
    build_user_goal_param_description,
)
from baton.integrations.standalone import VendorConfig, install_baton
from baton.integrations.standalone.middleware import BatonMiddleware
from baton.sinks import FileSink, HttpSink, Sink
from tests._event_helpers import without_surface_snapshots


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def sink(httpserver: HTTPServer, captured: list[dict[str, Any]]) -> Sink:
    def handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
    s = HttpSink(url=httpserver.url_for(""), api_key="k")
    yield s
    await s.aclose()


def _build_mcp(sink: Sink, **mw_kwargs: Any) -> FastMCP:
    mcp = FastMCP("test-vendor")
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ten_test",
            vendor_id="ten_test",
            consent_token="ct_test",
            sink=sink,
            **mw_kwargs,
        )
    )
    return mcp


# =============================================================================
# tools/list injection
# =============================================================================


class TestListInjection:
    async def test_optional_mode_injects_param(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="optional")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        props = echo_tool.inputSchema["properties"]
        assert USER_GOAL_PARAM_NAME in props
        assert props[USER_GOAL_PARAM_NAME]["type"] == "string"
        # optional → NOT added to required
        assert USER_GOAL_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_required_mode_adds_to_required(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["required"]

    async def test_description_label_tracks_the_mode(self, sink: Sink) -> None:
        """The advertised label has to move with the schema.

        Under ``required`` the injector adds ``user_goal`` to the tool's
        ``required`` list, so a description still opening "OPTIONAL." would
        contradict the schema it ships inside — and the model reads both.
        Pinned in BOTH directions, because the failure this replaces was a
        constant that was true under one mode and silently false under the
        other: the ``optional`` leg is the control that proves the mode is
        what moves the label. ``expected_result`` is never escalated, so its
        label must NOT move — that is what separates a label that tracks the
        schema from one that tracks the mode.
        """

        for mode, expected_lead in (("optional", "OPTIONAL."), ("required", "REQUIRED.")):
            mcp = _build_mcp(sink, intent_param_mode=mode)

            @mcp.tool()
            def echo(text: str) -> str:
                return text

            async with Client(mcp) as client:
                tools = await client.list_tools()

            (echo_tool,) = tools
            props = echo_tool.inputSchema["properties"]
            goal_desc = props[USER_GOAL_PARAM_NAME]["description"]
            assert goal_desc.startswith(expected_lead), f"{mode} mode advertised {goal_desc[:12]!r}"
            assert goal_desc == build_user_goal_param_description(intent_param_mode=mode)
            # Only the label moved — the measured sentence after it is identical.
            assert (
                goal_desc[len(expected_lead) :]
                == build_user_goal_param_description()[len("OPTIONAL.") :]
            )
            # expected_result is never added to `required`, so it never flips.
            assert (
                props[EXPECTED_RESULT_PARAM_NAME]["description"]
                == build_expected_result_param_description()
            )
            assert props[EXPECTED_RESULT_PARAM_NAME]["description"].startswith("OPTIONAL.")

    async def test_off_mode_no_injection(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="off")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert USER_GOAL_PARAM_NAME not in echo_tool.inputSchema.get("properties", {})

    async def test_native_param_left_untouched(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A tool that already declares ``user_goal`` keeps its own — the
        injector records it ``native`` and the caller's value is forwarded, not
        stripped."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str, user_goal: str = "") -> str:
            # Vendor's own param — echo it back so the test can assert it arrived.
            return f"{text}|{user_goal}"

        async with Client(mcp) as client:
            result = await client.call_tool("echo", {"text": "x", "user_goal": "vendor-value"})

        await sink.flush()
        # forwarded to the vendor handler (not stripped)
        assert "vendor-value" in str(result.content[0].text)  # type: ignore[union-attr]
        # and never captured as call_intent (disposition = native)
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None

    async def test_expected_result_injected_optional_even_in_required_mode(
        self, sink: Sink
    ) -> None:
        """``required`` mode escalates only ``user_goal`` — ``expected_result``
        stays optional regardless (a bigger surface mutation than the signal
        warrants)."""
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert EXPECTED_RESULT_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert EXPECTED_RESULT_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_native_expected_result_left_untouched_independently(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A tool with its own ``expected_result`` param is forwarded untouched
        for that param, while ``user_goal`` injection still proceeds normally —
        dispositions are tracked per param, not per tool."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str, expected_result: str = "") -> str:
            return f"{text}|{expected_result}"

        async with Client(mcp) as client:
            tools = await client.list_tools()
            (echo_tool,) = tools
            assert USER_GOAL_PARAM_NAME in echo_tool.inputSchema["properties"]

            result = await client.call_tool(
                "echo",
                {
                    "text": "x",
                    "expected_result": "vendor-value",
                    USER_GOAL_PARAM_NAME: "why the user called",
                },
            )

        await sink.flush()
        # vendor's own expected_result forwarded, not stripped
        assert "vendor-value" in str(result.content[0].text)  # type: ignore[union-attr]
        # user_goal still stripped + captured normally
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "why the user called"


# =============================================================================
# tools/call strip + capture
# =============================================================================


class TestCallStripAndCapture:
    async def test_strips_intent_and_captures_call_intent(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        seen: dict[str, Any] = {}
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            # If user_goal leaked to the handler, FastMCP would have rejected
            # the call (unexpected kwarg) — reaching here proves it was stripped.
            seen["text"] = text
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo", {"text": "hello", USER_GOAL_PARAM_NAME: "user wants a greeting"}
            )

        await sink.flush()
        assert seen == {"text": "hello"}  # vendor handler never saw user_goal
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_intent"] == "user wants a greeting"
        assert start["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        # params captured == vendor-visible args, no user_goal
        assert start["payload"]["params"] == {"text": "hello"}

    async def test_no_intent_leaves_call_intent_null(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hi"})

        await sink.flush()
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None
        assert start["payload"].get("intent_source") is None


# =============================================================================
# proactive synthesis + dedup
# =============================================================================


class TestProactiveSynthesis:
    async def test_first_intent_emits_proactive_before_start(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo", {"text": "x", USER_GOAL_PARAM_NAME: "why the user called"}
            )

        await sink.flush()
        tool_events = without_surface_snapshots(captured)
        types = [ev["event_type"] for ev in tool_events]
        assert types[0] == "annotation", "proactive must be sequenced first"
        ann = tool_events[0]
        assert ann["payload"]["intent"] == "why the user called"
        assert ann["payload"]["intent_source"] == INTENT_SOURCE_PARAM
        assert ann["payload"]["tool_name"] == "echo"
        # sequence: annotation < start
        start = next(ev for ev in tool_events if ev["event_type"] == "tool_call_start")
        assert ann["sequence_number"] < start["sequence_number"]

    async def test_only_one_proactive_per_session(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "1", USER_GOAL_PARAM_NAME: "first why"})
            await client.call_tool("echo", {"text": "2", USER_GOAL_PARAM_NAME: "second why"})

        await sink.flush()
        annotations = [ev for ev in captured if ev["event_type"] == "annotation"]
        assert len(annotations) == 1, "only the session's first intent synthesises a proactive"
        # but the second call still rides its intent on the start event
        starts = [ev for ev in captured if ev["event_type"] == "tool_call_start"]
        assert starts[1]["payload"]["call_intent"] == "second why"

    async def test_expected_result_rides_proactive_and_every_start(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """``expected_result`` feeds the proactive annotation's
        ``expected_outcome`` AND rides every ``tool_call_start`` as
        ``call_expected`` (2026-08-10 — previously dropped after the session's
        first call). Never reaches the vendor handler."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {
                    "text": "x",
                    USER_GOAL_PARAM_NAME: "why the user called",
                    EXPECTED_RESULT_PARAM_NAME: "a successful echo",
                },
            )
            await client.call_tool(
                "echo", {"text": "y", EXPECTED_RESULT_PARAM_NAME: "a second success"}
            )

        await sink.flush()
        ann = next(ev for ev in captured if ev["event_type"] == "annotation")
        assert ann["payload"]["expected_outcome"] == "a successful echo"
        starts = [ev for ev in captured if ev["event_type"] == "tool_call_start"]
        assert starts[0]["payload"]["call_expected"] == "a successful echo"
        assert starts[1]["payload"]["call_expected"] == "a second success"
        for start in starts:
            assert "expected_result" not in start["payload"]["params"]


# =============================================================================
# overall_task param (task-label grouping key, 2026-08-10)
# =============================================================================


class TestOverallTaskParam:
    async def test_overall_task_injected_optional_even_in_required_mode(self, sink: Sink) -> None:
        mcp = _build_mcp(sink, intent_param_mode="required")

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            tools = await client.list_tools()

        (echo_tool,) = tools
        assert OVERALL_TASK_PARAM_NAME in echo_tool.inputSchema["properties"]
        assert OVERALL_TASK_PARAM_NAME not in echo_tool.inputSchema.get("required", [])

    async def test_overall_task_stripped_and_captured_as_call_workflow(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        seen: dict[str, Any] = {}
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            seen["text"] = text
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo", {"text": "hello", OVERALL_TASK_PARAM_NAME: "file q3 notes"}
            )

        await sink.flush()
        assert seen == {"text": "hello"}
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start["payload"]["call_workflow"] == "file q3 notes"
        assert start["payload"]["params"] == {"text": "hello"}

    async def test_overall_task_rides_synthesised_proactive_workflow(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {
                    "text": "x",
                    USER_GOAL_PARAM_NAME: "find the q3 notes page",
                    OVERALL_TASK_PARAM_NAME: "file q3 notes",
                },
            )

        await sink.flush()
        ann = next(ev for ev in captured if ev["event_type"] == "annotation")
        assert ann["payload"]["workflow"] == "file q3 notes"


# =============================================================================
# the default: user_goal advertised as required, never enforced (2026-09-15)
# =============================================================================


def _read_events(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _wire(result: Any) -> dict[str, Any]:
    """A ``CallToolResult`` as it crossed the wire. mcp 2.0 renamed the Python
    attr ``isError`` and kept the wire alias, so dump by alias."""
    return result.model_dump(by_alias=True, mode="json")


def _echo_server(path: str, **overrides: Any) -> tuple[FastMCP, Any, list[str]]:
    """A vendor server with one ``echo`` tool, wrapped through the real
    ``install_baton`` so ``VendorConfig``'s default is the one under test.
    ``seen`` is what the vendor's handler actually received."""
    seen: list[str] = []
    mcp = FastMCP("test-vendor")

    @mcp.tool()
    def echo(text: str) -> str:
        seen.append(text)
        return f"echo:{text}"

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="test-vendor",
            vendor_display_name="Test Vendor",
            consent_token="ct_test",
            sink=FileSink(path),
            **overrides,
        ),
    )
    return mcp, handle, seen


class TestRequiredByDefaultIsNeverEnforced:
    """``intent_param_mode`` defaults to ``required``, which ADVERTISES
    ``user_goal`` as required and must never refuse a call that omits it.

    Driven through fastmcp's in-memory ``Client``, a real MCP session, so the
    client, the server's call path and the middleware chain all see the
    ``tools/call``. The middleware advertises on a copy of the ``tools/list``
    response; this pins that nothing on the call path checks arguments against
    that copy. Measured not to on fastmcp 2.14.7, 3.4.2, 4.0.2 and 4.0.3
    (2026-09-15).
    """

    async def test_a_call_without_user_goal_is_served(self, tmp_path: Any) -> None:
        path = str(tmp_path / "events.jsonl")
        mcp, handle, seen = _echo_server(path)
        try:
            async with Client(mcp) as client:
                echo_tool = next(t for t in await client.list_tools() if t.name == "echo")
                schema = echo_tool.inputSchema
                without = _wire(await client.call_tool_mcp("echo", {"text": "a"}))
                with_goal = _wire(
                    await client.call_tool_mcp("echo", {"text": "b", USER_GOAL_PARAM_NAME: "why"})
                )
            await handle.flush()
        finally:
            await handle.aclose()

        assert USER_GOAL_PARAM_NAME in schema["required"]
        assert schema["properties"][USER_GOAL_PARAM_NAME]["description"].startswith("REQUIRED.")
        assert not without.get("isError"), without
        assert without["content"][0]["text"] == "echo:a"
        assert not with_goal.get("isError"), with_goal
        assert seen == ["a", "b"], "the vendor's handler ran for both calls"
        starts = [e for e in _read_events(path) if e["event_type"] == "tool_call_start"]
        assert [s["payload"].get("call_intent") for s in starts] == [None, "why"]

    async def test_a_native_user_goal_is_left_alone(self, tmp_path: Any) -> None:
        """A tool that declares its own ``user_goal`` keeps it: not added to
        ``required``, not re-described, and the caller's value reaches the
        handler instead of being captured."""
        path = str(tmp_path / "events.jsonl")
        mcp = FastMCP("test-vendor")

        @mcp.tool()
        def echo(text: str, user_goal: str = "") -> str:
            return f"{text}|{user_goal}"

        handle = install_baton(
            mcp,
            VendorConfig(
                vendor_id="test-vendor",
                vendor_display_name="Test Vendor",
                consent_token="ct_test",
                sink=FileSink(path),
            ),
        )
        try:
            async with Client(mcp) as client:
                echo_tool = next(t for t in await client.list_tools() if t.name == "echo")
                schema = echo_tool.inputSchema
                result = _wire(
                    await client.call_tool_mcp(
                        "echo", {"text": "x", USER_GOAL_PARAM_NAME: "vendor-value"}
                    )
                )
            await handle.flush()
        finally:
            await handle.aclose()

        assert USER_GOAL_PARAM_NAME not in schema.get("required", [])
        ours = {
            build_user_goal_param_description(intent_param_mode=m) for m in ("optional", "required")
        }
        assert schema["properties"][USER_GOAL_PARAM_NAME].get("description") not in ours
        assert result["content"][0]["text"] == "x|vendor-value"
        start = next(e for e in _read_events(path) if e["event_type"] == "tool_call_start")
        assert start["payload"].get("call_intent") is None

    async def test_the_default_does_not_move_the_surface_hash(self, tmp_path: Any) -> None:
        """The snapshot hashes the vendor-true surface, so moving the default
        must leave every existing server's ``surface_hash`` where it was. The
        mode is recorded beside the hash, in ``seam_augmentations``."""
        payloads: dict[str, dict[str, Any]] = {}
        for label, overrides in (("optional", {"intent_param_mode": "optional"}), ("default", {})):
            path = str(tmp_path / f"{label}.jsonl")
            mcp, handle, _ = _echo_server(path, **overrides)
            try:
                async with Client(mcp) as client:
                    await client.list_tools()
                await handle.flush()
            finally:
                await handle.aclose()
            payloads[label] = next(
                e for e in _read_events(path) if e["event_type"] == "surface_snapshot"
            )["payload"]

        assert payloads["default"]["surface_hash"] == payloads["optional"]["surface_hash"]
        assert payloads["default"]["seam_augmentations"]["intent_param"]["mode"] == "required"
        assert payloads["optional"]["seam_augmentations"]["intent_param"]["mode"] == "optional"
