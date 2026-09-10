"""Multi-round tool calls (MRTR, SEP-2322) on the standalone adapter — N10b.

A tool may pause mid-call to ask the client for input, returning an
``InputRequiredResult``; the client answers and calls again, and that second
round completes the SAME logical call. The official adapter has handled this
since `f509b20`. This adapter did not handle it at all, so one paused-and-
resumed call arrived as **two complete calls** — the first carrying the ask
itself as its result body. Measured end-to-end on fastmcp 4.0.2 before the fix.

⚠ fastmcp frames an ask as a legitimate RESULT rather than a pause, because at
the protocol level each round is a complete request/response. That framing is
right about the wire and wrong about the vendor's call, and adopting it here
would leave the two adapters disagreeing about what a tool call IS. See the
comment at the suppression site.

The two rounds carry DIFFERENT ``call_id``s and therefore do not pair at tier
1 — the documented MRTR limitation, identical on both adapters, not something
this change introduces or fixes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.integrations.standalone.middleware import (
    BatonMiddleware,
    _is_mrtr_continuation,
    _is_mrtr_pause,
)
from baton.sinks import HttpSink, Sink
from tests._event_helpers import without_surface_snapshots

# Capability gate, not a version string: MRTR exists where the request context
# can carry the client's answers. fastmcp 2.x/3.x have neither property, and
# the detector tests below are what cover those versions.
MRTR_AVAILABLE = hasattr(Context, "input_responses")
requires_mrtr = pytest.mark.skipif(
    not MRTR_AVAILABLE, reason="fastmcp predates MRTR (SEP-2322); no ask can be raised"
)


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def sink(
    httpserver: HTTPServer,
    captured: list[dict[str, Any]],
) -> Sink:
    def handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
    s = HttpSink(url=httpserver.url_for(""), api_key="k")
    yield s
    await s.aclose()


def _build_mcp(sink: Sink) -> FastMCP:
    mcp = FastMCP("test-vendor")
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ten_test",
            vendor_id="ten_test",
            consent_token="ct_test",
            sink=sink,
        )
    )
    return mcp


def _add_guarded_tool(mcp: FastMCP) -> None:
    """A tool that asks once, then completes on the round that carries answers."""
    import mcp.types as mcp_types

    @mcp.tool()
    async def guarded(topic: str, ctx: Context) -> str:
        if ctx.input_responses is None and ctx.request_state is None:
            return mcp_types.InputRequiredResult(  # type: ignore[return-value]
                input_requests={
                    "q1": mcp_types.ElicitRequest(
                        method="elicitation/create",
                        params=mcp_types.ElicitRequestFormParams(
                            mode="form",
                            message="Confirm?",
                            requestedSchema={"type": "object", "properties": {}},
                        ),
                    )
                },
                request_state="asked-once",
            )
        return f"done: {topic}"


async def _accept(message: Any, response_type: Any, params: Any, context: Any) -> Any:
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="accept", content={})


@requires_mrtr
class TestOneLogicalCallEmitsOnePair:
    async def test_two_round_exchange_emits_exactly_one_start_and_one_end(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)
        _add_guarded_tool(mcp)

        async with Client(mcp, elicitation_handler=_accept) as client:
            result = await client.call_tool("guarded", {"topic": "x"})

        assert "done: x" in result.content[0].text, "the exchange did not complete"
        await sink.flush()

        types = [ev["event_type"] for ev in without_surface_snapshots(captured)]
        assert types.count("tool_call_start") == 1, f"one call, one start — got {types}"
        assert types.count("tool_call_end") == 1, f"one call, one end — got {types}"
        assert "tool_call_error" not in types

    async def test_the_end_carries_the_answer_not_the_ask(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)
        _add_guarded_tool(mcp)

        async with Client(mcp, elicitation_handler=_accept) as client:
            await client.call_tool("guarded", {"topic": "x"})

        await sink.flush()
        end = next(ev for ev in captured if ev["event_type"] == "tool_call_end")
        body = end["payload"]["result"]

        texts = [b.get("text", "") for b in body.get("content", []) if isinstance(b, dict)]
        assert texts, f"nothing was checked — no content blocks in {body!r}"
        assert any("done: x" in t for t in texts), f"expected the answer, got {body!r}"
        # Substring over the SERIALISED body, not ``in body`` — that tests dict
        # KEYS, and a serialised ToolResult never has an ``input_required`` key,
        # so the guard could not fail. Caught in review.
        assert "input_required" not in json.dumps(body), (
            f"the ask leaked into the result body: {body!r}"
        )


@requires_mrtr
class TestContinuationDoesNotReplayPerCallWork:
    async def test_injected_intent_yields_one_proactive_annotation(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """A continuation RESENDS the original arguments, injected params and
        all. The proactive annotation dedups per session rather than per round,
        so this holds by construction — pinned because "by construction" is
        what the suppressed start would otherwise have been asserting for it.
        """
        mcp = _build_mcp(sink)
        _add_guarded_tool(mcp)

        async with Client(mcp, elicitation_handler=_accept) as client:
            await client.call_tool("guarded", {"topic": "x", "user_goal": "check the intent path"})

        await sink.flush()
        types = [ev["event_type"] for ev in captured]
        assert types.count("annotation") == 1, f"one call, one proactive — got {types}"


@requires_mrtr
class TestPausedRoundEmitsNoEnd:
    async def test_ask_alone_emits_a_start_and_no_end(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """No elicitation handler ⇒ the client cannot answer, so exactly one
        server-side round runs: the ask. It must not be reported as an outcome.
        """
        mcp = _build_mcp(sink)
        _add_guarded_tool(mcp)

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 — client-side; the leg is what matters
                await client.call_tool("guarded", {"topic": "x"})

        await sink.flush()
        types = [ev["event_type"] for ev in without_surface_snapshots(captured)]
        assert types.count("tool_call_start") == 1, f"the round did not run: {types}"
        assert "tool_call_end" not in types, f"the ask was reported as a result: {types}"
        assert "tool_call_error" not in types, f"a pause is not an error: {types}"


class TestContinuationDetector:
    """Runs on EVERY fastmcp version — including the ones with no MRTR, where
    the point is that the detector reports False and nothing changes."""

    class _Ctx:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    class _MwCtx:
        def __init__(self, fastmcp_context: Any) -> None:
            self.fastmcp_context = fastmcp_context

    def test_input_responses_marks_a_continuation(self) -> None:
        ctx = self._MwCtx(self._Ctx(input_responses={"q1": "answered"}, request_state=None))
        assert _is_mrtr_continuation(ctx) is True  # type: ignore[arg-type]

    def test_request_state_alone_marks_a_continuation(self) -> None:
        ctx = self._MwCtx(self._Ctx(input_responses=None, request_state="asked-once"))
        assert _is_mrtr_continuation(ctx) is True  # type: ignore[arg-type]

    def test_neither_is_a_fresh_call(self) -> None:
        ctx = self._MwCtx(self._Ctx(input_responses=None, request_state=None))
        assert _is_mrtr_continuation(ctx) is False  # type: ignore[arg-type]

    def test_a_real_context_outside_a_request_is_not_a_continuation(self) -> None:
        """The REAL library object, not a stand-in — this is the version-shaped
        case. On 2.x/3.x neither property exists at all; on 4.x both exist and
        read ``None`` off a context with no live request. Measured on both, and
        it is why the fix is inert below fastmcp 4.
        """
        ctx = self._MwCtx(Context(FastMCP("probe")))
        assert _is_mrtr_continuation(ctx) is False  # type: ignore[arg-type]

    def test_missing_fastmcp_context_is_not_a_continuation(self) -> None:
        assert _is_mrtr_continuation(self._MwCtx(None)) is False  # type: ignore[arg-type]

    def test_a_raising_property_fails_open(self) -> None:
        """No shipped version raises here — measured, both properties return
        ``None`` off a live-less context. The guard exists because which error
        a library raises when there is no request is a GUESS this repo has
        already had wrong (``8b4356d``), and a capture-path read must never
        fail the vendor's call.
        """

        class Exploding:
            @property
            def input_responses(self) -> Any:
                raise RuntimeError("no active request")

        assert _is_mrtr_continuation(self._MwCtx(Exploding())) is False  # type: ignore[arg-type]


class TestPauseDetector:
    def test_wrapped_ask_is_a_pause(self) -> None:
        class Wrapped:
            input_required = object()

        assert _is_mrtr_pause(Wrapped()) is True

    def test_a_normal_result_is_not_a_pause(self) -> None:
        try:
            from fastmcp.tools import ToolResult
        except ImportError:  # fastmcp 2.14.7
            from fastmcp.tools.tool import ToolResult

        assert _is_mrtr_pause(ToolResult(content="ok")) is False

    def test_none_is_not_a_pause(self) -> None:
        assert _is_mrtr_pause(None) is False
