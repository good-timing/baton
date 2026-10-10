"""Tests for BatonMiddleware per SPEC §11.2 + §11.5.

Strategy: use FastMCP's in-process Client to drive real tool calls through
a real BatonMiddleware-equipped FastMCP server. The middleware writes events
to an HttpSink pointing at pytest-httpserver; tests inspect what landed there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.http import set_http_request
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from baton.events import Event
from baton.integrations.standalone import VendorConfig, install_baton
from baton.integrations.standalone._session import extract_headers
from baton.integrations.standalone.middleware import BatonMiddleware
from baton.sinks import FileSink, HttpSink, Sink
from tests._asgi import fake_http_request
from tests._event_helpers import read_events, without_surface_snapshots


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    """Per-test list that collects ingested event JSON bodies."""
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


#: Shared with every other ASGI-scope site in the suite; see ``tests/_asgi``
#: for why the lowercasing lives there and not at each call.
_fake_http_request = fake_http_request


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
# Success path — tool_call_start + tool_call_end
# =============================================================================


class TestSuccessfulToolCall:
    async def test_emits_start_and_end(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "hello"})

        await sink.flush()
        types = [ev["event_type"] for ev in without_surface_snapshots(captured)]
        assert types == ["tool_call_start", "tool_call_end"]

    async def test_tool_name_and_params_in_start_event(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def add(a: int, b: int) -> int:
            return a + b

        async with Client(mcp) as client:
            await client.call_tool("add", {"a": 3, "b": 4})

        await sink.flush()
        start_event = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        assert start_event["payload"]["tool_name"] == "add"
        assert start_event["payload"]["params"] == {"a": 3, "b": 4}

    async def test_result_in_end_event(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def double(n: int) -> int:
            return n * 2

        async with Client(mcp) as client:
            await client.call_tool("double", {"n": 7})

        await sink.flush()
        end_event = next(ev for ev in captured if ev["event_type"] == "tool_call_end")
        assert end_event["payload"]["tool_name"] == "double"
        # Result is whatever FastMCP serializes — at minimum non-None
        assert end_event["payload"]["result"] is not None
        assert isinstance(end_event["payload"]["duration_ms"], int)
        assert end_event["payload"]["duration_ms"] >= 0


# =============================================================================
# Failure path — tool_call_start + tool_call_error
# =============================================================================


class TestFailedToolCall:
    async def test_emits_start_and_error(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def boom() -> None:
            raise RuntimeError("kaboom")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp wraps; we only care that something raised
                await client.call_tool("boom", {})

        await sink.flush()
        types = [ev["event_type"] for ev in captured]
        assert "tool_call_start" in types
        assert "tool_call_error" in types
        assert "tool_call_end" not in types  # error path skips end event

    async def test_error_payload_fields(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def boom() -> None:
            raise ValueError("specific message")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp wraps; we only care that something raised
                await client.call_tool("boom", {})

        await sink.flush()
        error_event = next(ev for ev in captured if ev["event_type"] == "tool_call_error")
        # error_type captures the exception class name (FastMCP may wrap; either
        # the original or the wrapper class name is acceptable).
        assert error_event["payload"]["error_type"]
        assert "specific message" in error_event["payload"]["error_body"]
        assert isinstance(error_event["payload"]["duration_ms"], int)


class _RaisingSink(Sink):
    """Sink that always raises on write — for fail-open testing."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc if exc is not None else RuntimeError("sink dead")
        self.write_attempts = 0

    async def write(self, event: Event) -> None:
        self.write_attempts += 1
        raise self._exc

    async def flush(self) -> None:
        return

    async def aclose(self) -> None:
        return


class TestSinkFailureDoesNotBreakToolCall:
    """SPEC §11.2: fail-open at the capture boundary. A sink-side failure
    (closed sink, transport error, etc.) MUST NOT propagate through the
    middleware and break the vendor's tool call."""

    async def test_raising_sink_does_not_block_successful_tool(self) -> None:
        raising = _RaisingSink()
        mcp = FastMCP("test-vendor")
        mcp.add_middleware(
            BatonMiddleware(tenant_id="t", vendor_id="t", consent_token="c", sink=raising)
        )

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            result = await client.call_tool("echo", {"text": "hello"})

        # Tool ran and returned even though every sink.write raised.
        assert raising.write_attempts >= 2  # start + end
        assert result is not None

    async def test_raising_sink_preserves_vendor_exception_on_failure(self) -> None:
        raising = _RaisingSink()
        mcp = FastMCP("test-vendor")
        mcp.add_middleware(
            BatonMiddleware(tenant_id="t", vendor_id="t", consent_token="c", sink=raising)
        )

        @mcp.tool()
        def boom() -> None:
            raise ValueError("vendor error")

        async with Client(mcp) as client:
            with pytest.raises(Exception):  # noqa: B017 fastmcp may wrap
                await client.call_tool("boom", {})

        # start + error emit both attempted; both raised; neither hid the
        # vendor's ValueError from the caller.
        assert raising.write_attempts >= 2


class _FlakyOnceSink(Sink):
    """Raises on the first write, then succeeds on every write after —
    simulates a transient sink failure recovering."""

    def __init__(self) -> None:
        self.write_attempts = 0
        self.written: list[Event] = []

    async def write(self, event: Event) -> None:
        self.write_attempts += 1
        if self.write_attempts == 1:
            raise RuntimeError("transient sink failure")
        self.written.append(event)

    async def flush(self) -> None:
        return

    async def aclose(self) -> None:
        return


class TestSurfaceSnapshotSinkFailureRetries:
    """A transient sink failure on surface_snapshot must not permanently
    drop that surface — unlike a genuine dedup skip (unchanged surface,
    already delivered), a write failure must retry on the next tools/list."""

    async def test_transient_failure_retries_on_next_tools_list(self) -> None:
        flaky = _FlakyOnceSink()
        mcp = _build_mcp(flaky)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.list_tools()  # write #1: raises, not recorded
            await client.list_tools()  # write #2: same digest, retried, succeeds

        assert flaky.write_attempts == 2
        snapshots = [e for e in flaky.written if e.event_type == "surface_snapshot"]
        assert len(snapshots) == 1


# =============================================================================
# Sequence numbers
# =============================================================================


class TestSequenceNumbers:
    async def test_monotonic_per_session(self, sink: Sink, captured: list[dict[str, Any]]) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "1"})
            await client.call_tool("echo", {"text": "2"})
            await client.call_tool("echo", {"text": "3"})

        await sink.flush()
        # 3 tool calls x 2 events each = 6 tool events, all in one session.
        tool_events = without_surface_snapshots(captured)
        seqs = [ev["sequence_number"] for ev in tool_events]
        # Same session → must be strictly increasing
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs), "sequence numbers must be unique"
        # The session's counter starts at 1, but the first number is not
        # necessarily a tool event's: an in-process/stdio surface_snapshot now
        # shares this session rather than sitting on one of its own. It used to
        # land elsewhere only because tool calls resolved via fastmcp's
        # Context.session_id while the snapshot used the process-wide fallback
        # — an artefact of the two disagreeing, not a designed separation. They
        # agree now, so assert over every event in the session.
        all_seqs = [ev["sequence_number"] for ev in captured]
        assert sorted(all_seqs) == list(range(1, len(all_seqs) + 1)), (
            "the session's sequence numbers are 1..n with no gaps or repeats"
        )
        assert len({ev["session_id"] for ev in captured}) == 1, (
            "every event of this session, snapshot included, shares its session_id"
        )

    async def test_start_seq_less_than_end_seq(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        start = next(ev for ev in captured if ev["event_type"] == "tool_call_start")
        end = next(ev for ev in captured if ev["event_type"] == "tool_call_end")
        assert start["sequence_number"] < end["sequence_number"]


# =============================================================================
# Tenant + envelope fields
# =============================================================================


class TestEnvelopeFields:
    async def test_tenant_id_set_correctly(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["tenant_id"] == "ten_test"

    async def test_session_id_same_across_events_within_call(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        session_ids = {ev["session_id"] for ev in without_surface_snapshots(captured)}
        assert len(session_ids) == 1, "start + end of same tool call must share session_id"

    async def test_spec_and_sdk_versions_present(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool("echo", {"text": "x"})

        await sink.flush()
        for ev in captured:
            assert ev["sdk_version"].startswith("0.")

    async def test_nothing_a_caller_or_vendor_passes_changes_agent_runtime(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(TypeError, match="default_agent_runtime"):
            BatonMiddleware(  # type: ignore[call-arg]
                tenant_id="t",
                vendor_id="v",
                consent_token="ct",
                sink=sink,
                default_agent_runtime="claude-code",
            )

        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        declared = mcp_types.Implementation(name="claude-ai", version="1.2.3")
        async with Client(mcp, client_info=declared) as client:
            await client.list_tools()
            await client.call_tool(
                "echo",
                {"text": "x"},
                meta={
                    "io.baton/agent_runtime": "my-custom-runtime",
                    "baton": {"agent_runtime": "my-custom-runtime"},
                    "claudecode/toolUseId": "tu_1",
                },
            )

        await sink.flush()
        types = {ev["event_type"] for ev in captured}
        assert {"surface_snapshot", "tool_call_start", "tool_call_end"} <= types, sorted(types)
        assert {ev["agent_runtime"] for ev in captured} == {"unknown"}

    async def test_a_claudecode_key_reaches_runtime_meta_and_names_nobody(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """FastMCP 3.x strips ``_meta`` from the middleware's
        ``CallToolRequestParams``, so it is read from
        ``fastmcp_context.request_context.meta``. The consumer recognises
        Claude Code from this key; the SDK does not write it into ``info``."""
        mcp = _build_mcp(sink)

        @mcp.tool()
        def echo(text: str) -> str:
            return text

        async with Client(mcp) as client:
            await client.call_tool(
                "echo",
                {"text": "x"},
                meta={"claudecode/toolUseId": "tool-use-abc-123"},
            )

        await sink.flush()
        tool_events = without_surface_snapshots(captured)
        assert tool_events, "no events captured"
        for ev in tool_events:
            assert ev["runtime_meta"]["claudecode/toolUseId"] == "tool-use-abc-123"
            assert ev["agent_runtime"] == "unknown"
            # The in-process client declares the library's own name.
            assert ev["client_observed"]["info"]["name"] == "mcp"


# =============================================================================
# extract_headers — the shared header read
# =============================================================================


class TestExtractHeaders:
    """``extract_headers`` feeds SPEC §3.4 rung 4 and the ``resolve_principal``
    hook's context. It used to feed rung 0 as well; that rung
    (``VendorConfig.resolve_session_id``) was REMOVED 2026-09-12 and its six
    tests went with it. These two stay because the header read is a separate
    thing that outlived the hook."""

    async def test_extract_headers_reads_a_real_http_request(self) -> None:
        """``extract_headers`` (called by rung 4) wraps FastMCP's
        ``get_http_headers()`` — exercised here against a real Starlette
        ``Request`` via ``set_http_request``, not mocked. (The full
        middleware dispatch can't be driven through this path in-process:
        FastMCP's in-process ``Client`` spawns the server-side call in a task
        created before any ``with set_http_request(...):`` block in the
        caller, so the contextvar set there never reaches it — a testing-
        harness limitation of in-process transport, not of the header
        extraction itself.)"""
        request = _fake_http_request({"x-vendor-session": "real-header-value"})
        with set_http_request(request):
            headers = extract_headers()

        assert headers is not None
        assert headers.get("x-vendor-session") == "real-header-value"

    async def test_extract_headers_none_outside_a_live_request(self) -> None:
        assert extract_headers() is None

    async def test_what_this_adapter_hands_a_hook_folds_case(self) -> None:
        """Register A8, at the seam a vendor actually touches.

        ``extract_headers`` itself returns the case-SENSITIVE dict ASGI built —
        the fold happens once, in ``SessionResolutionContext``. So the assertion
        is on the composition, because that is what reaches a hook: this
        adapter's real extractor, in the real context object.

        The canonical spelling is what a vendor writes, because it is what the
        header is called in every document describing it — never the lowercased
        form the wire happens to carry.
        """
        from baton.integrations._config import SessionResolutionContext

        with set_http_request(_fake_http_request({"X-Forwarded-User": "employee-4417"})):
            headers = extract_headers()

        assert headers is not None
        ctx = SessionResolutionContext(headers=headers, meta=None, tool_name="lookup", arguments={})
        assert ctx.headers is not None
        assert ctx.headers["X-Forwarded-User"] == "employee-4417"
        assert ctx.headers.get("X-FORWARDED-USER") == "employee-4417"
        assert "X-Forwarded-User" in ctx.headers
        # The lowercased spelling keeps working — the change is additive.
        assert ctx.headers["x-forwarded-user"] == "employee-4417"


@pytest.mark.parametrize("path", ["tool call", "annotation"])
async def test_headers_are_read_once_per_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """``client_observed``, the identity hook and the session ladder share one
    read per request, and each is handed its result."""
    from baton.integrations.standalone import _session, annotation, install, middleware

    reads = 0
    seen_by_hook: list[Any] = []

    def counting() -> Any:
        nonlocal reads
        reads += 1
        return {"mcp-session-id": "from-header", "user-agent": "agent/1.0"}

    for module in (_session, annotation, install, middleware):
        monkeypatch.setattr(module, "extract_headers", counting)

    mcp: Any = FastMCP("one-read")

    @mcp.tool
    def lookup() -> str:
        return "ok"

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="v",
            vendor_display_name="V",
            consent_token="ct",
            sink=FileSink(str(tmp_path / "e.jsonl")),
            resolve_principal=lambda ctx: seen_by_hook.append(ctx.headers.get("user-agent")),
        ),
    )
    try:
        async with Client(mcp) as client:
            if path == "tool call":
                await client.call_tool("lookup", {})
            else:
                await client.call_tool(
                    handle.annotation_tool_name,
                    {"user_goal": "g", "what_happened": "the call came back unusable"},
                )
    finally:
        await handle.aclose()

    events = without_surface_snapshots(read_events(tmp_path / "e.jsonl"))
    assert events
    requests = 1 + sum(ev["event_type"] == "tool_list_start" for ev in events)
    assert reads == requests
    assert seen_by_hook == ["agent/1.0"] * requests
    for ev in events:
        assert ev["session_id"] == "from-header", ev["event_type"]
        assert ev["client_observed"]["headers"] == {"user-agent": "agent/1.0"}, ev["event_type"]
