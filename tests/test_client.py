"""Tests for the library API (``Client``, ``AsyncClient``, ``Trace``, ``AsyncTrace``).

Uses pytest-httpserver for a real in-process HTTP capture server (no mocks);
asserts on the events that actually land on the wire.

Covers phase 6 of ``docs/design-notes/library_api_engineering_plan.md``:

- Happy path: trace emits start + end with correct sequence numbers
- Exception path: trace emits start + error, re-raises
- observed() missing: UserWarning + tool_call_end with result=None
- observed() called twice: UserWarning + last wins
- annotate() emits standalone annotation event
- Proactive annotation (intent/expected on trace) emits in same session
- Params via trace(params=...) ship on start
- Params via with_params() after enter emits UserWarning
- Config loading: explicit kwargs win, env-var fallback works
- close() then trace() raises RuntimeError
- close() is idempotent
- agent_runtime defaults to "python-library"
- Sync + async parity
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from baton import AsyncClient, Client, SignalType


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def captured_events() -> list[dict[str, Any]]:
    """Mutable list that the capture handler appends parsed event JSON to."""
    return []


@pytest.fixture
def capture_server(
    httpserver: HTTPServer, captured_events: list[dict[str, Any]]
) -> HTTPServer:
    """HTTPServer that captures POST /v0/events bodies into ``captured_events``."""

    def _handler(req: Request) -> Response:
        captured_events.append(json.loads(req.data.decode("utf-8")))
        return Response("", status=204)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(_handler)
    return httpserver


@pytest.fixture
def sync_client(capture_server: HTTPServer) -> Iterator[Client]:
    client = Client(
        api_key="bk_test_xyz",
        ingest_url=capture_server.url_for(""),
        vendor_id="test-vendor",
        consent_token="ct-test",
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def async_client(capture_server: HTTPServer) -> AsyncClient:
    return AsyncClient(
        api_key="bk_test_xyz",
        ingest_url=capture_server.url_for(""),
        vendor_id="test-vendor",
        consent_token="ct-test",
    )


# =============================================================================
# SignalType enum
# =============================================================================


class TestSignalType:
    def test_all_eight_signal_types_present(self) -> None:
        values = {s.value for s in SignalType}
        assert values == {
            "failure",
            "retry_loop",
            "dead_end",
            "parameter_confusion",
            "slow_performance",
            "abandonment",
            "feature_gap",
            "other",
        }

    def test_str_enum_serializes_as_bare_string(self) -> None:
        assert SignalType.DEAD_END == "dead_end"
        assert str(SignalType.FAILURE) == "failure"


# =============================================================================
# Config loading
# =============================================================================


class TestConfigLoading:
    def test_explicit_kwargs_required(self, capture_server: HTTPServer) -> None:
        # api_key required
        with pytest.raises(ValueError, match="api_key"):
            Client(
                ingest_url=capture_server.url_for(""),
                vendor_id="v",
            )

    def test_env_var_fallback(
        self,
        capture_server: HTTPServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BATON_API_KEY", "from-env")
        monkeypatch.setenv("BATON_INGEST_URL", capture_server.url_for(""))
        monkeypatch.setenv("BATON_VENDOR_ID", "from-env-vendor")
        monkeypatch.setenv("BATON_CONSENT_TOKEN", "ct-from-env")
        client = Client()  # no explicit kwargs
        try:
            assert client._vendor_id == "from-env-vendor"
            assert client._consent_token == "ct-from-env"
        finally:
            client.close()

    def test_explicit_kwarg_wins_over_env(
        self,
        capture_server: HTTPServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BATON_VENDOR_ID", "env-vendor")
        client = Client(
            api_key="explicit",
            ingest_url=capture_server.url_for(""),
            vendor_id="explicit-vendor",
            consent_token="ct-test",
        )
        try:
            assert client._vendor_id == "explicit-vendor"
        finally:
            client.close()

    def test_consent_token_required(self, capture_server: HTTPServer) -> None:
        # Per SPEC §2.3 + §3.1, every event MUST carry a consent_token, so the
        # SDK MUST be constructed with one (explicit or via env). Missing both
        # raises at init — closes a gap surfaced during a library-API dogfood
        # spike (an early version silently accepted None, dropping consent).
        with pytest.raises(ValueError, match="consent_token"):
            Client(
                api_key="x",
                ingest_url=capture_server.url_for(""),
                vendor_id="v",
            )


# =============================================================================
# Sync Client — happy path
# =============================================================================


class TestSyncHappyPath:
    def test_trace_emits_start_and_end(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="my.tool") as trace:
            trace.observed({"ok": True})
        sync_client.flush()

        types = [e["event_type"] for e in captured_events]
        assert "tool_call_start" in types
        assert "tool_call_end" in types

    def test_start_carries_tool_name(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="my.tool") as trace:
            trace.observed("ok")
        sync_client.flush()

        start = next(e for e in captured_events if e["event_type"] == "tool_call_start")
        assert start["payload"]["tool_name"] == "my.tool"

    def test_end_carries_result_and_duration(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="my.tool") as trace:
            trace.observed({"answer": 42})
        sync_client.flush()

        end = next(e for e in captured_events if e["event_type"] == "tool_call_end")
        assert end["payload"]["result"] == {"answer": 42}
        assert end["payload"]["duration_ms"] is not None
        assert end["payload"]["duration_ms"] >= 0

    def test_params_via_trace_constructor_arg(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(
            tool_name="my.tool", params={"model": "gpt-4"}
        ) as trace:
            trace.observed("ok")
        sync_client.flush()

        start = next(e for e in captured_events if e["event_type"] == "tool_call_start")
        assert start["payload"]["params"] == {"model": "gpt-4"}

    def test_proactive_annotation_emits_when_intent_set(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(
            tool_name="my.tool",
            intent="find the answer",
            expected_outcome="a number",
            workflow="user-question",
        ) as trace:
            trace.observed("ok")
        sync_client.flush()

        # tool_call_start, annotation, tool_call_end — in that order
        types = [e["event_type"] for e in captured_events]
        assert types == ["tool_call_start", "annotation", "tool_call_end"]
        ann = captured_events[1]
        assert ann["payload"]["intent"] == "find the answer"
        assert ann["payload"]["expected_outcome"] == "a number"
        assert ann["payload"]["workflow"] == "user-question"

    def test_sequence_numbers_monotonic_per_session(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(
            tool_name="my.tool",
            intent="x",  # forces proactive annotation
        ) as trace:
            trace.observed("ok")
        sync_client.flush()

        seqs = [e["sequence_number"] for e in captured_events]
        assert seqs == [1, 2, 3]

    def test_default_agent_runtime_is_python_library(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="t") as trace:
            trace.observed("ok")
        sync_client.flush()

        assert all(e["agent_runtime"] == "python-library" for e in captured_events)


# =============================================================================
# Sync Client — exception path
# =============================================================================


class TestSyncExceptionPath:
    def test_exception_emits_error_event_and_reraises(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with pytest.raises(ValueError, match="simulated"):
            with sync_client.trace(tool_name="failing.tool"):
                raise ValueError("simulated failure")
        sync_client.flush()

        types = [e["event_type"] for e in captured_events]
        assert types == ["tool_call_start", "tool_call_error"]

        error = captured_events[1]
        assert error["payload"]["tool_name"] == "failing.tool"
        assert error["payload"]["error_type"] == "ValueError"
        assert "simulated failure" in error["payload"]["error_body"]

    def test_observed_with_error_emits_tool_call_error(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="t") as trace:
            trace.observed(error_type="HTTPError", error_body="503 backend down")
        sync_client.flush()

        error = next(e for e in captured_events if e["event_type"] == "tool_call_error")
        assert error["payload"]["error_type"] == "HTTPError"
        assert error["payload"]["error_body"] == "503 backend down"

    def test_observed_with_exception_object_derives_fields(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        # The RE-01 ergonomic fix: pass the exception object directly; trace
        # derives error_type + error_body without the caller doing
        # type(exc).__name__ / str(exc) themselves.
        class BadRequestError(ValueError):
            pass

        with sync_client.trace(tool_name="t") as trace:
            try:
                raise BadRequestError("Grammar must have a 'properties' field")
            except BadRequestError as exc:
                trace.observed(error=exc)
        sync_client.flush()

        error = next(e for e in captured_events if e["event_type"] == "tool_call_error")
        assert error["payload"]["error_type"] == "BadRequestError"
        assert error["payload"]["error_body"] == "Grammar must have a 'properties' field"

    def test_observed_error_kwarg_yields_to_explicit_type_body(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        # Explicit error_type / error_body should override the derived ones
        # — useful for re-classifying ("HTTPError" → "RateLimitExceeded") or
        # scrubbing the body before it's stored.
        with sync_client.trace(tool_name="t") as trace:
            try:
                raise ValueError("raw")
            except ValueError as exc:
                trace.observed(error=exc, error_type="ReclassifiedError", error_body="clean body")
        sync_client.flush()

        error = next(e for e in captured_events if e["event_type"] == "tool_call_error")
        assert error["payload"]["error_type"] == "ReclassifiedError"
        assert error["payload"]["error_body"] == "clean body"


# =============================================================================
# Sync Client — warning paths
# =============================================================================


class TestSyncWarnings:
    def test_observed_missing_warns(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with sync_client.trace(tool_name="t"):
                pass
            sync_client.flush()

        assert any(
            "exited without observed()" in str(w.message) for w in caught
        ), f"expected UserWarning about missing observed(), got: {[str(w.message) for w in caught]}"

        # end event still emitted with result=None
        end = next(e for e in captured_events if e["event_type"] == "tool_call_end")
        assert end["payload"]["result"] is None

    def test_observed_called_twice_warns(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with sync_client.trace(tool_name="t") as trace:
                trace.observed("first")
                trace.observed("second")  # should warn
            sync_client.flush()

        assert any("multiple times" in str(w.message) for w in caught)

        # last call wins
        end = next(e for e in captured_events if e["event_type"] == "tool_call_end")
        assert end["payload"]["result"] == "second"

    def test_with_params_after_enter_warns(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with sync_client.trace(tool_name="t") as trace:
                trace.with_params({"late": "params"})  # start already shipped
                trace.observed("ok")
            sync_client.flush()

        assert any("with_params() called after" in str(w.message) for w in caught)


# =============================================================================
# Sync Client — annotate
# =============================================================================


class TestSyncAnnotate:
    def test_standalone_annotate_emits_annotation_event(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        sync_client.annotate(
            signal_type=SignalType.DEAD_END,
            intent="find warmest mutual",
            suggested_improvement="add warmth signals",
            context={"target_user_id": "abc"},
        )
        sync_client.flush()

        assert len(captured_events) == 1
        ann = captured_events[0]
        assert ann["event_type"] == "annotation"
        assert ann["payload"]["signal_type"] == "dead_end"
        assert ann["payload"]["intent"] == "find warmest mutual"
        assert ann["payload"]["suggested_improvement"] == "add warmth signals"
        assert ann["payload"]["context"] == {"target_user_id": "abc"}

    def test_annotate_accepts_str_signal_type(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        sync_client.annotate(signal_type="feature_gap")
        sync_client.flush()
        assert captured_events[0]["payload"]["signal_type"] == "feature_gap"

    def test_annotate_rejects_typo_signal_type(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        # The RE-05 fix: silently shipping a non-standard signal_type
        # (typo like "dead-end" vs "dead_end") used to slip through and
        # bucket as "other" Console-side. Now raises ValueError immediately.
        with pytest.raises(ValueError, match="signal_type"):
            sync_client.annotate(signal_type="dead-end")
        # No event should have been emitted for the failed call.
        sync_client.flush()
        assert captured_events == []


# =============================================================================
# Sync Client — lifecycle
# =============================================================================


class TestSyncLifecycle:
    def test_close_then_trace_raises(self, capture_server: HTTPServer) -> None:
        client = Client(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        )
        client.close()
        with pytest.raises(RuntimeError, match="closed"):
            client.trace(tool_name="t")

    def test_close_then_annotate_raises(self, capture_server: HTTPServer) -> None:
        client = Client(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        )
        client.close()
        with pytest.raises(RuntimeError, match="closed"):
            client.annotate(signal_type=SignalType.OTHER)

    def test_close_idempotent(self, capture_server: HTTPServer) -> None:
        client = Client(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        )
        client.close()
        client.close()  # should not raise

    def test_context_manager(
        self,
        capture_server: HTTPServer,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with Client(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        ) as client:
            with client.trace(tool_name="t") as trace:
                trace.observed("ok")
        # Exit closes the client; events should have flushed
        assert len(captured_events) == 2  # start + end


# =============================================================================
# AsyncClient — parity smoke tests
# =============================================================================


class TestAsyncHappyPath:
    async def test_trace_emits_start_and_end(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        try:
            async with async_client.trace(tool_name="my.tool") as trace:
                trace.observed({"ok": True})
            await async_client.flush()
        finally:
            await async_client.aclose()

        types = [e["event_type"] for e in captured_events]
        assert "tool_call_start" in types
        assert "tool_call_end" in types

    async def test_exception_path(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        try:
            with pytest.raises(ValueError, match="boom"):
                async with async_client.trace(tool_name="t"):
                    raise ValueError("boom")
            await async_client.flush()
        finally:
            await async_client.aclose()

        error = next(e for e in captured_events if e["event_type"] == "tool_call_error")
        assert error["payload"]["error_type"] == "ValueError"

    async def test_observed_with_exception_object_derives_fields(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        # Async parity for the RE-01 ergonomic fix.
        try:
            async with async_client.trace(tool_name="t") as trace:
                try:
                    raise RuntimeError("upstream service unavailable")
                except RuntimeError as exc:
                    trace.observed(error=exc)
            await async_client.flush()
        finally:
            await async_client.aclose()

        error = next(e for e in captured_events if e["event_type"] == "tool_call_error")
        assert error["payload"]["error_type"] == "RuntimeError"
        assert error["payload"]["error_body"] == "upstream service unavailable"

    async def test_annotate(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        try:
            await async_client.annotate(signal_type=SignalType.FEATURE_GAP)
            await async_client.flush()
        finally:
            await async_client.aclose()

        assert captured_events[0]["event_type"] == "annotation"
        assert captured_events[0]["payload"]["signal_type"] == "feature_gap"

    async def test_default_agent_runtime_is_python_library(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        try:
            async with async_client.trace(tool_name="t") as trace:
                trace.observed("ok")
            await async_client.flush()
        finally:
            await async_client.aclose()
        assert all(e["agent_runtime"] == "python-library" for e in captured_events)

    async def test_async_context_manager(
        self,
        capture_server: HTTPServer,
        captured_events: list[dict[str, Any]],
    ) -> None:
        async with AsyncClient(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        ) as client:
            async with client.trace(tool_name="t") as trace:
                trace.observed("ok")
        assert len(captured_events) == 2  # start + end

    async def test_close_then_trace_raises(
        self,
        capture_server: HTTPServer,
    ) -> None:
        client = AsyncClient(
            api_key="x", ingest_url=capture_server.url_for(""), vendor_id="v", consent_token="ct-test"
        )
        await client.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            client.trace(tool_name="t")


# =============================================================================
# Env vars — verify they don't leak across tests
# =============================================================================


def test_no_env_var_leakage() -> None:
    """Sanity check the test runner isn't polluting BATON_* env vars."""
    for var in ["BATON_API_KEY", "BATON_INGEST_URL", "BATON_VENDOR_ID", "BATON_CONSENT_TOKEN"]:
        # Either unset, or this test fails loudly so we know test isolation broke
        assert os.environ.get(var) is None, f"BATON_* env var leaked: {var}"
