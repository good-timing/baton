"""Tests for the library API (``Client``, ``AsyncClient``, ``Trace``, ``AsyncTrace``).

Uses pytest-httpserver for a real in-process HTTP capture server (no mocks);
asserts on the events that actually land on the wire.

Covers the library API surface (Client / AsyncClient / Trace / AsyncTrace):

- Happy path: trace emits start + end with correct sequence numbers
- Exception path: trace emits start + error, re-raises
- observed() missing: UserWarning + tool_call_end with result=None
- observed() called twice: UserWarning + last wins
- annotate() emits standalone annotation event
- Intent on trace rides the start event, with no note
- Params via trace(params=...) ship on start
- Params via with_params() after enter emits UserWarning
- Config loading: explicit kwargs win, env-var fallback works
- close() then trace() raises RuntimeError
- close() is idempotent
- agent_runtime defaults to "unknown"
- Sync + async parity
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import baton.client as client_module
from baton import AsyncClient, Client
from baton.client import _SyncBridge
from baton.events import DEFAULT_CONSENT_TOKEN
from baton.sinks import HttpSink, Sink

# =============================================================================
# Fixtures
# =============================================================================


class _SlowCloseSink(Sink):
    """Counts closes, and yields inside one so a second close can overlap it."""

    def __init__(self) -> None:
        self.closes = 0
        self.finished = False

    async def write(self, event: Any) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def aclose(self) -> None:
        self.closes += 1
        await asyncio.sleep(0.05)
        self.finished = True


@pytest.fixture
def captured_events() -> list[dict[str, Any]]:
    """Mutable list that the capture handler appends parsed event JSON to."""
    return []


@pytest.fixture
def capture_server(httpserver: HTTPServer, captured_events: list[dict[str, Any]]) -> HTTPServer:
    """HTTPServer that captures POST /v0/events bodies into ``captured_events``."""

    def _handler(req: Request) -> Response:
        captured_events.append(json.loads(req.data.decode("utf-8")))
        return Response("", status=204)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(_handler)
    return httpserver


@pytest.fixture
def sync_client(capture_server: HTTPServer) -> Iterator[Client]:
    client = Client(
        vendor_id="test-vendor",
        consent_token="ct-test",
        sink=HttpSink(url=capture_server.url_for(""), api_key="bk_test_xyz"),
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def async_client(capture_server: HTTPServer) -> AsyncClient:
    return AsyncClient(
        vendor_id="test-vendor",
        consent_token="ct-test",
        sink=HttpSink(url=capture_server.url_for(""), api_key="bk_test_xyz"),
    )


# =============================================================================
# Config loading
# =============================================================================


class TestConfigLoading:
    def test_vendor_id_required(self, capture_server: HTTPServer) -> None:
        with pytest.raises(ValueError, match="vendor_id"):
            Client(
                consent_token="ct",
                sink=HttpSink(url=capture_server.url_for(""), api_key="k"),
            )

    def test_env_var_fallback(
        self,
        capture_server: HTTPServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BATON_VENDOR_ID", "from-env-vendor")
        monkeypatch.setenv("BATON_CONSENT_TOKEN", "ct-from-env")
        client = Client(sink=HttpSink(url=capture_server.url_for(""), api_key="k"))
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
            vendor_id="explicit-vendor",
            consent_token="ct-test",
            sink=HttpSink(url=capture_server.url_for(""), api_key="k"),
        )
        try:
            assert client._vendor_id == "explicit-vendor"
        finally:
            client.close()

    def test_consent_token_defaults_when_omitted(self, capture_server: HTTPServer) -> None:
        """Omitting it takes the SDK's default rather than raising.

        This REPLACES a test asserting the opposite. Per SPEC §2.3 + §3.1 every
        event MUST carry a consent_token, and every event still does — what
        changed is who supplies it. The customer used to thread a constant
        through an environment variable to a field that reads to nobody; the
        SDK states it once instead, and the wire is byte-identical.
        """
        client = Client(
            vendor_id="v",
            sink=HttpSink(url=capture_server.url_for(""), api_key="k"),
        )
        try:
            assert client._consent_token == DEFAULT_CONSENT_TOKEN
        finally:
            client.close()

    def test_consent_token_explicitly_emptied_still_raises(
        self, capture_server: HTTPServer
    ) -> None:
        """``""`` is a mistake, not a request for the default.

        The distinction is the whole reason the default is not simply "any
        falsy value is fine": a vendor who wires an empty variable into this
        field has a broken config, and an event carrying an empty consent_token
        MUST be rejected by the consumer — which is a failure they would meet
        at the collector instead of at init.
        """
        with pytest.raises(ValueError, match="consent_token"):
            Client(
                vendor_id="v",
                consent_token="",
                sink=HttpSink(url=capture_server.url_for(""), api_key="k"),
            )

    def test_consent_token_env_still_beats_the_default(
        self, capture_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``BATON_CONSENT_TOKEN`` keeps working — installs already set it."""
        monkeypatch.setenv("BATON_CONSENT_TOKEN", "ct-from-env")
        client = Client(
            vendor_id="v",
            sink=HttpSink(url=capture_server.url_for(""), api_key="k"),
        )
        try:
            assert client._consent_token == "ct-from-env"
        finally:
            client.close()


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
        with sync_client.trace(tool_name="my.tool", params={"model": "gpt-4"}) as trace:
            trace.observed("ok")
        sync_client.flush()

        start = next(e for e in captured_events if e["event_type"] == "tool_call_start")
        assert start["payload"]["params"] == {"model": "gpt-4"}

    def test_intent_rides_the_start_event_and_sends_no_note(
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

        assert [e["event_type"] for e in captured_events] == ["tool_call_start", "tool_call_end"]
        payload = captured_events[0]["payload"]
        assert payload["call_intent"] == "find the answer"
        assert payload["call_expected"] == "a number"
        assert payload["call_workflow"] == "user-question"
        assert payload["intent_source"] == "library_arg"

    def test_a_trace_without_intent_has_no_intent_source(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="my.tool") as trace:
            trace.observed("ok")
        sync_client.flush()

        assert captured_events[0]["payload"]["intent_source"] is None

    def test_sequence_numbers_increase_within_a_session(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.session():
            with sync_client.trace(tool_name="my.tool") as trace:
                trace.observed("ok")
            sync_client.annotate(what_happened="thin result", tool_name="my.tool")
        sync_client.flush()

        seqs = [e["sequence_number"] for e in captured_events]
        assert len(seqs) == 3
        assert seqs == sorted(set(seqs))

    def test_default_agent_runtime_is_unknown(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="t") as trace:
            trace.observed("ok")
        sync_client.flush()

        assert all(e["agent_runtime"] == "unknown" for e in captured_events)


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

        assert any("exited without observed()" in str(w.message) for w in caught), (
            f"expected UserWarning about missing observed(), got: {[str(w.message) for w in caught]}"
        )

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
# Trace re-entry — workplan §N12
# =============================================================================


class TestTraceReEntry:
    """Entering a Trace starts a CLEAN call; reusing one object is legal.

    The contract, decided 2026-09-10: ``__enter__`` resets everything the SDK
    DERIVES for one execution (``call_id``, the start sequence number, the
    observation state) and preserves everything the vendor CONFIGURED
    (``tool_name``, params, intent, ``session_id``). "Configure once, enter N
    times" therefore keeps working — a retry loop re-running one call with the
    same inputs is the case that makes params-carryover the right answer rather
    than a sibling of the bug below.

    What it fixes: a Trace reused for a second call, with no ``observed()`` on
    that call, used to emit the FIRST call's result body as the second call's
    outcome. Silently — the "exited without observed()" warning tests
    ``is _UNSET`` and the field still held the old value, so the defect
    suppressed its own alarm. Nothing downstream could catch it either: every
    console surface groups on tool name and params, never on the result body,
    so the ids and the totals stayed right while the content was another
    call's — and that content is read back as EVIDENCE by the Insights and
    LLM-explain paths, where a wrong body becomes a wrong sentence shown to a
    customer about their own traffic.
    """

    def test_second_entry_does_not_inherit_the_first_result(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The defect itself: the stale body, and the warning it silenced."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            trace = sync_client.trace(tool_name="t")
            with trace:
                trace.observed({"call": "FIRST"})
            with trace:
                pass  # no observed() on the second call
            sync_client.flush()

        ends = [e for e in captured_events if e["event_type"] == "tool_call_end"]
        assert len(ends) == 2, f"expected two end events, got {len(ends)}"
        assert ends[0]["payload"]["result"] == {"call": "FIRST"}
        assert ends[1]["payload"]["result"] is None, (
            "a re-entered Trace reported the PREVIOUS call's result as this "
            f"call's outcome: {ends[1]['payload']['result']}"
        )
        # The other half of the fix, and the one that matters more: the alarm
        # works again. Before the reset this warning could not fire, because
        # the field it tests was holding the stale value.
        assert any("exited without observed()" in str(w.message) for w in caught), (
            "the second entry emitted result=None and said nothing about it: "
            f"{[str(w.message) for w in caught]}"
        )

    def test_observed_once_per_entry_does_not_warn(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """One ``observed()`` per entry must not read as a duplicate.

        ⚠ This pins the ``_observed_result`` reset's tail, NOT
        ``_observed_warned`` — the duplicate check tests the result slot
        first, so with that slot cleared the warned-flag is never consulted
        on this path. Mutating ``_observed_warned`` leaves this test green;
        ``test_duplicate_observed_warns_again_on_the_second_entry`` below is
        what actually covers it. Recorded because the first version of this
        docstring claimed the wrong field, and a mutation run said so.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            trace = sync_client.trace(tool_name="t")
            with trace:
                trace.observed({"i": 0})
            with trace:
                trace.observed({"i": 1})
            sync_client.flush()

        assert not any("multiple times" in str(w.message) for w in caught), (
            f"one observed() per entry must not warn: {[str(w.message) for w in caught]}"
        )
        ends = [
            e["payload"]["result"] for e in captured_events if e["event_type"] == "tool_call_end"
        ]
        assert ends == [{"i": 0}, {"i": 1}], f"each entry must report its OWN result, got {ends}"

    def test_second_entry_does_not_inherit_the_first_error(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The error slot is the worse half: it changes the EVENT TYPE.

        A stale result body files a wrong outcome under the right event. A
        stale ``_observed_error`` makes the second call emit
        ``tool_call_error`` — the first call's failure reported as a second
        failure that never happened, inflating exactly the counts the console
        exists to surface. Found by mutating the reset line and watching
        nothing red.
        """
        trace = sync_client.trace(tool_name="t")
        with trace:
            trace.observed(error=ValueError("first call blew up"))
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            with trace:
                pass  # no observed() on the second call
        sync_client.flush()

        types = [
            e["event_type"] for e in captured_events if e["event_type"].startswith("tool_call_")
        ]
        assert types == [
            "tool_call_start",
            "tool_call_error",
            "tool_call_start",
            "tool_call_end",
        ], f"the second entry re-reported the first call's failure: {types}"
        end = next(e for e in captured_events if e["event_type"] == "tool_call_end")
        assert end["payload"]["result"] is None

    def test_duplicate_observed_warns_again_on_the_second_entry(
        self,
        sync_client: Client,
    ) -> None:
        """``_observed_warned`` is per-execution, and this is the only path there.

        The flag exists so one duplicate does not warn twice. Left unreset it
        also swallowed the SECOND call's duplicate — the warning fires once
        per Trace OBJECT rather than once per call, so a vendor with a real
        double-``observed()`` bug in a reused trace hears about it once and
        never again.
        """
        trace = sync_client.trace(tool_name="t")
        with warnings.catch_warnings(record=True) as first:
            warnings.simplefilter("always")
            with trace:
                trace.observed("a")
                trace.observed("b")
        assert any("multiple times" in str(w.message) for w in first)

        with warnings.catch_warnings(record=True) as second:
            warnings.simplefilter("always")
            with trace:
                trace.observed("c")
                trace.observed("d")
        sync_client.flush()
        assert any("multiple times" in str(w.message) for w in second), (
            "the second entry's duplicate observed() was silent — the warned "
            f"flag survived the call it belonged to: {[str(w.message) for w in second]}"
        )

    def test_with_params_between_entries_warns_the_TRUE_thing(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """Late params get a sentence that is true, in both ways of being late.

        This guard was wrong in each direction on the same day, which is why
        both halves are pinned here. It first said "params will not reach the
        emitted event" BETWEEN two entries — false, the next start carries
        them. Clearing the start sequence number on exit made that statement
        impossible and silently removed the OTHER one with it: a vendor who
        set params after a finished call and never re-entered got no warning
        while the params went nowhere (see the test below). So the branch that
        fires here is a different message, true for both readings — the params
        did not reach the completed call, and apply only on a re-entry.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            trace = sync_client.trace(tool_name="t", params={"call": "FIRST"})
            with trace:
                trace.observed("a")
            trace.with_params({"call": "SECOND"})
            with trace:
                trace.observed("b")
            sync_client.flush()

        messages = [str(w.message) for w in caught]
        assert not any("will not reach the emitted event" in m for m in messages), (
            f"the FALSE sentence is back — these params did ship: {messages}"
        )
        assert any("called after the call finished" in m for m in messages), (
            f"late params must still say so, accurately: {messages}"
        )
        starts = [
            e["payload"]["params"] for e in captured_events if e["event_type"] == "tool_call_start"
        ]
        assert starts == [{"call": "FIRST"}, {"call": "SECOND"}], (
            f"each entry must ship the params it was given, got {starts}"
        )

    def test_with_params_after_a_finished_trace_still_warns(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The true warning that the first cut of this fix deleted.

        Found by ``/code-review`` and reproduced both ways before it was
        accepted: on the tree before the fix this case warned truthfully, and
        after the first cut it was silent while the params still shipped
        nowhere. The two cases — params bound for a next entry, params bound
        for nothing — are indistinguishable at CALL time, so the message has
        to be true of both rather than the branch guessing which one it is.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            trace = sync_client.trace(tool_name="t")
            with trace:
                trace.observed("a")
            trace.with_params({"q": "never ships"})  # and never re-entered
            sync_client.flush()

        assert any("called after the call finished" in str(w.message) for w in caught), (
            f"params that reach no event at all must say so: {[str(w.message) for w in caught]}"
        )
        starts = [
            e["payload"]["params"] for e in captured_events if e["event_type"] == "tool_call_start"
        ]
        assert starts == [{}], f"nothing should have shipped these params, got {starts}"

    def test_with_params_before_the_first_entry_is_silent(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The documented path must stay quiet — the flag is what protects it.

        ``with_params()`` before entering is the ordinary usage every example
        in this package shows. It is "late" by neither measure, and a guard
        keyed only on "is there a start sequence number" would have been right
        here by accident; ``_entered_before`` is what makes it right on
        purpose.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            trace = sync_client.trace(tool_name="t")
            trace.with_params({"call": "FIRST"})
            with trace:
                trace.observed("a")
            sync_client.flush()

        assert not any("with_params()" in str(w.message) for w in caught), (
            f"the documented usage must not warn: {[str(w.message) for w in caught]}"
        )
        starts = [
            e["payload"]["params"] for e in captured_events if e["event_type"] == "tool_call_start"
        ]
        assert starts == [{"call": "FIRST"}]

    def test_vendor_configured_state_survives_re_entry(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The deliberate other half: what entry must NOT reset.

        Params are INPUT the vendor configured, not output the SDK derived, so
        re-entering without re-supplying them repeats the call as configured —
        a retry loop is exactly this shape. Recorded as a decision so the
        carryover is not read later as a sibling of §N12 and "fixed".
        """
        trace = sync_client.trace(tool_name="cfg.tool", params={"shared": True})
        with trace:
            trace.observed("a")
        with trace:
            trace.observed("b")
        sync_client.flush()

        starts = [e for e in captured_events if e["event_type"] == "tool_call_start"]
        assert [e["payload"]["params"] for e in starts] == [{"shared": True}, {"shared": True}]
        assert [e["payload"]["tool_name"] for e in starts] == ["cfg.tool", "cfg.tool"]
        assert len({e["session_id"] for e in starts}) == 1, "session_id is configured, not derived"

    async def test_async_twin_does_not_inherit_the_first_result(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """Driven, not asserted by symmetry with the sync edit.

        B1 is this repo's standing lesson: one adapter was fixed and the other
        assumed, and the assumption shipped ``agent_runtime: "unknown"`` for
        two releases. ``AsyncTrace`` is a separate class with its own copy of
        every field patched above.
        """
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                trace = async_client.trace(tool_name="t")
                async with trace:
                    trace.observed({"call": "FIRST"})
                async with trace:
                    pass
                await async_client.flush()
        finally:
            await async_client.aclose()

        ends = [e for e in captured_events if e["event_type"] == "tool_call_end"]
        assert len(ends) == 2, f"expected two end events, got {len(ends)}"
        assert ends[1]["payload"]["result"] is None, (
            "AsyncTrace re-entry reported the previous call's result: "
            f"{ends[1]['payload']['result']}"
        )
        assert any("exited without observed()" in str(w.message) for w in caught)

    async def test_async_twin_error_slot_and_warned_flag_and_start_seq(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        """The three async sites the sync tests structurally cannot reach.

        ``AsyncTrace`` holds its own copy of every field, so each of these
        needs driving here or the mutation run says "green" about a line no
        test executes. One test rather than three: they share a client and an
        event stream, and splitting them would triple the fixture cost for no
        extra discrimination — each assertion below fails alone.
        """
        try:
            trace = async_client.trace(tool_name="t", params={"call": "FIRST"})

            # (a) the error slot — a stale one changes the second call's EVENT TYPE
            async with trace:
                trace.observed(error=ValueError("first call blew up"))
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                async with trace:
                    pass

            # (b) the warned flag — the second call's duplicate must still warn
            with warnings.catch_warnings(record=True) as dup:
                warnings.simplefilter("always")
                async with trace:
                    trace.observed("a")
                    trace.observed("b")
                async with trace:
                    trace.observed("c")
                    trace.observed("d")
            assert len([w for w in dup if "multiple times" in str(w.message)]) == 2, (
                "each entry's duplicate observed() must warn on its own: "
                f"{[str(w.message) for w in dup]}"
            )

            # (c) the start sequence number — cleared on exit, so with_params
            #     between entries stops claiming the params were dropped and
            #     says the true thing instead
            with warnings.catch_warnings(record=True) as params_warn:
                warnings.simplefilter("always")
                trace.with_params({"call": "SECOND"})
                async with trace:
                    trace.observed("e")
            messages = [str(w.message) for w in params_warn]
            assert not any("will not reach the emitted event" in m for m in messages), (
                f"the FALSE sentence is back — these params did ship: {messages}"
            )
            assert any("called after the call finished" in m for m in messages), (
                f"late params must still say so, accurately: {messages}"
            )

            await async_client.flush()
        finally:
            await async_client.aclose()

        types = [
            e["event_type"] for e in captured_events if e["event_type"].startswith("tool_call_")
        ]
        assert types[:4] == [
            "tool_call_start",
            "tool_call_error",
            "tool_call_start",
            "tool_call_end",
        ], f"the second entry re-reported the first call's failure: {types[:4]}"
        starts = [
            e["payload"]["params"] for e in captured_events if e["event_type"] == "tool_call_start"
        ]
        assert starts[-1] == {"call": "SECOND"}, (
            f"last start did not carry the new params: {starts}"
        )


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
            what_happened="asked for warm intros; got a flat list",
            tool_name="find_mutuals",
            intent="find warmest mutual",
            suggested_improvement="add warmth signals",
            context={"target_user_id": "abc"},
        )
        sync_client.flush()

        assert len(captured_events) == 1
        ann = captured_events[0]
        assert ann["event_type"] == "annotation"
        assert ann["payload"]["what_happened"] == "asked for warm intros; got a flat list"
        assert ann["payload"]["tool_name"] == "find_mutuals"
        assert ann["payload"]["signal_type"] is None
        assert ann["payload"]["intent"] == "find warmest mutual"
        assert ann["payload"]["suggested_improvement"] == "add warmth signals"
        assert ann["payload"]["context"] == {"target_user_id": "abc"}

    def test_annotate_keeps_no_tool_apart_from_not_stated(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        sync_client.annotate(what_happened="no tool can do a bulk update", tool_name="")
        sync_client.annotate(what_happened="it failed")
        sync_client.flush()
        assert [e["payload"]["tool_name"] for e in captured_events] == ["", None]

    def test_a_trace_report_carries_the_tool_it_names(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with sync_client.trace(tool_name="search") as trace:
            trace.annotate(what_happened="it failed", tool_name="none")
            trace.observed("ok")
        sync_client.flush()
        (report,) = [e for e in captured_events if e["event_type"] == "annotation"]
        assert report["payload"]["tool_name"] == "none"

    def test_annotate_with_blank_what_happened_is_a_note(
        self,
        sync_client: Client,
        captured_events: list[dict[str, Any]],
    ) -> None:
        sync_client.annotate(what_happened="   ", intent="look something up")
        sync_client.flush()
        assert captured_events[0]["payload"]["what_happened"] is None


# =============================================================================
# Sync Client — lifecycle
# =============================================================================


class TestSyncLifecycle:
    def test_close_then_trace_and_annotate_send_nothing(self, capture_server: HTTPServer) -> None:
        client = Client(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        )
        client.close()
        with client.trace(tool_name="t") as trace:
            trace.observed("ok")
        client.annotate(what_happened="it failed")
        assert capture_server.log == []

    def test_close_from_two_threads_closes_the_sink_once(self) -> None:
        sink = _SlowCloseSink()
        client = Client(sink=sink, vendor_id="v")
        threads = [threading.Thread(target=client.close) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sink.closes == 1

    def test_an_emit_racing_a_close_on_another_thread_is_dropped(self) -> None:
        client = Client(sink=_SlowCloseSink(), vendor_id="v")
        trace = client.trace(tool_name="t")
        trace.__enter__()
        client._bridge.stop()  # type: ignore[union-attr]  # close() won the race
        trace.observed("ok")
        assert not trace.__exit__(None, None, None)
        client.flush()

    def test_a_close_reentered_on_the_closing_thread_returns(self) -> None:
        client = Client(sink=_SlowCloseSink(), vendor_id="v")
        # As if this thread were inside close() when a signal handler called it.
        with client._close_lock:
            client._closed = True
            client._closing_thread = threading.get_ident()
        client.close()
        assert not client._close_done.is_set()

    def test_a_second_close_waits_for_the_first(self) -> None:
        sink = _SlowCloseSink()
        client = Client(sink=sink, vendor_id="v")
        first = threading.Thread(target=client.close)
        first.start()
        while sink.closes == 0:
            time.sleep(0.001)
        client.close()
        assert sink.finished
        first.join()

    def test_close_idempotent(self, capture_server: HTTPServer) -> None:
        client = Client(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        )
        client.close()
        client.close()  # should not raise

    def test_context_manager(
        self,
        capture_server: HTTPServer,
        captured_events: list[dict[str, Any]],
    ) -> None:
        with Client(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
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
            await async_client.annotate(what_happened="no tool does this", tool_name="")
            await async_client.flush()
        finally:
            await async_client.aclose()

        assert captured_events[0]["event_type"] == "annotation"
        assert captured_events[0]["payload"]["what_happened"] == "no tool does this"
        assert captured_events[0]["payload"]["tool_name"] == ""
        assert captured_events[0]["payload"]["tool_name"] == ""

    async def test_default_agent_runtime_is_unknown(
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
        assert all(e["agent_runtime"] == "unknown" for e in captured_events)

    async def test_an_async_trace_report_carries_the_tool_it_names(
        self,
        async_client: AsyncClient,
        captured_events: list[dict[str, Any]],
    ) -> None:
        try:
            async with async_client.trace(tool_name="search") as trace:
                await trace.annotate(what_happened="it failed", tool_name="none")
                trace.observed("ok")
            await async_client.flush()
        finally:
            await async_client.aclose()
        (report,) = [e for e in captured_events if e["event_type"] == "annotation"]
        assert report["payload"]["tool_name"] == "none"

    async def test_async_context_manager(
        self,
        capture_server: HTTPServer,
        captured_events: list[dict[str, Any]],
    ) -> None:
        async with AsyncClient(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        ) as client:
            async with client.trace(tool_name="t") as trace:
                trace.observed("ok")
        assert len(captured_events) == 2  # start + end

    async def test_concurrent_aclose_closes_the_sink_once(self) -> None:
        sink = _SlowCloseSink()
        client = AsyncClient(sink=sink, vendor_id="v")
        await asyncio.gather(client.aclose(), client.aclose())
        assert sink.closes == 1

    async def test_only_the_first_aclose_raises_the_close_error(self) -> None:
        class _FailingCloseSink(_SlowCloseSink):
            async def aclose(self) -> None:
                await super().aclose()
                raise OSError("collector gone")

        client = AsyncClient(sink=_FailingCloseSink(), vendor_id="v")
        with pytest.raises(OSError):
            await client.aclose()
        await client.aclose()

    async def test_a_second_aclose_waits_for_the_first(self) -> None:
        sink = _SlowCloseSink()
        client = AsyncClient(sink=sink, vendor_id="v")
        first = asyncio.ensure_future(client.aclose())
        await asyncio.sleep(0)
        await client.aclose()
        assert sink.finished
        await first

    async def test_close_then_trace_sends_nothing(
        self,
        capture_server: HTTPServer,
    ) -> None:
        client = AsyncClient(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        )
        await client.aclose()
        async with client.trace(tool_name="t") as trace:
            trace.observed("ok")
        assert capture_server.log == []


# =============================================================================
# Env vars — verify they don't leak across tests
# =============================================================================


def test_no_env_var_leakage() -> None:
    """Sanity check the test runner isn't polluting BATON_* env vars."""
    for var in ["BATON_API_KEY", "BATON_INGEST_URL", "BATON_VENDOR_ID", "BATON_CONSENT_TOKEN"]:
        # Either unset, or this test fails loudly so we know test isolation broke
        assert os.environ.get(var) is None, f"BATON_* env var leaked: {var}"


# =============================================================================
# The sync bridge never holds a vendor thread for good (SPEC §11.2)
# =============================================================================


class _RecordingSink(Sink):
    """Records writes; each write and close can be made slow or failing."""

    def __init__(self, write_delay: float = 0.0, close_error: Exception | None = None) -> None:
        self.events: list[Any] = []
        self.write_delay = write_delay
        self.close_error = close_error

    async def write(self, event: Any) -> None:
        await asyncio.sleep(self.write_delay)
        self.events.append(event)

    async def flush(self) -> None:
        await asyncio.sleep(self.write_delay)

    async def aclose(self) -> None:
        if self.close_error is not None:
            raise self.close_error


def _send_one(client: Client) -> None:
    with client.trace(tool_name="t") as trace:
        trace.observed("ok")


class TestBridgeNeverHangs:
    @pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
    def test_work_on_a_loop_that_stopped_without_closing_returns_promptly(self) -> None:
        bridge = _SyncBridge.__new__(_SyncBridge)
        bridge._loop = asyncio.new_event_loop()  # accepts work, never runs it
        bridge._thread = threading.Thread(target=lambda: None)
        bridge._thread.start()
        bridge._thread.join()

        async def send() -> str:
            return "sent"

        started = time.monotonic()
        with pytest.raises(RuntimeError):
            bridge.run(send())
        assert time.monotonic() - started < 1.0
        bridge._loop.close()

    def test_a_write_in_flight_when_close_starts_is_delivered(self) -> None:
        sink = _RecordingSink(write_delay=0.3)
        client = Client(sink=sink, vendor_id="v")
        sender = threading.Thread(target=_send_one, args=(client,))
        sender.start()
        time.sleep(0.05)
        client.close()
        sender.join(timeout=3.0)
        assert not sender.is_alive()
        # The start was mid-write when close began; the end came after close.
        assert [e.event_type for e in sink.events] == ["tool_call_start"]

    def test_a_write_that_never_finishes_holds_neither_close_nor_its_sender(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_EMIT_WAIT_SECONDS", 0.1)
        monkeypatch.setattr(client_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.3)
        monkeypatch.setattr(client_module, "_DRAIN_SECONDS", 0.1)
        client = Client(sink=_RecordingSink(write_delay=30), vendor_id="v")
        sender = threading.Thread(target=_send_one, args=(client,))
        sender.start()
        time.sleep(0.05)
        started = time.monotonic()
        client.close()
        sender.join(timeout=3.0)
        assert time.monotonic() - started < 2.0
        assert not sender.is_alive()

    def test_a_slow_sink_costs_one_wait_and_still_gets_every_event(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(client_module, "_EMIT_WAIT_SECONDS", 0.1)
        sink = _RecordingSink(write_delay=0.3)
        client = Client(sink=sink, vendor_id="v")
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="baton.client"):
            for _ in range(3):
                _send_one(client)
        # One 0.1 s wait, not one per event (six events).
        assert time.monotonic() - started < 0.4
        assert sum("queued" in r.getMessage() for r in caplog.records) == 1
        client.close()
        assert len(sink.events) == 6

    def test_close_on_a_sink_that_blocks_the_loop_thread_keeps_its_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.3)

        class _BlockingCloseSink(_RecordingSink):
            async def aclose(self) -> None:
                time.sleep(2)  # blocks the loop thread itself

        client = Client(sink=_BlockingCloseSink(), vendor_id="v")
        started = time.monotonic()
        client.close()
        assert time.monotonic() - started < 1.5

    def test_snapshots_do_not_wait_while_the_sink_is_stalled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_EMIT_WAIT_SECONDS", 0.1)
        client = Client(sink=_RecordingSink(write_delay=0.2), vendor_id="v")
        _send_one(client)  # one 0.1 s wait; the sink is now stalled
        started = time.monotonic()
        client.list_tools([{"name": "fetch", "inputSchema": {"type": "object"}}])
        assert time.monotonic() - started < 0.1
        client.close()

    def test_a_snapshot_write_that_fails_late_is_sent_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "_EMIT_WAIT_SECONDS", 0.05)

        class _FailsOnceLateSink(_RecordingSink):
            async def write(self, event: Any) -> None:
                if not self.events:
                    self.events.append(None)
                    await asyncio.sleep(0.15)
                    raise OSError("collector gone")
                self.events.append(event)

        sink = _FailsOnceLateSink()
        client = Client(sink=sink, vendor_id="v")
        tools = [{"name": "fetch", "inputSchema": {"type": "object"}}]
        client.list_tools(tools)  # the caller stops waiting before the write fails
        time.sleep(0.3)
        client.list_tools(tools)
        client.close()
        assert [e.event_type for e in sink.events if e is not None] == ["surface_snapshot"]

    def test_close_and_flush_never_raise_a_sink_error(self) -> None:
        client = Client(sink=_RecordingSink(close_error=OSError("collector gone")), vendor_id="v")
        client.flush()
        client.close()

    def test_flush_on_a_stuck_sink_returns_within_its_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.2)
        client = Client(sink=_RecordingSink(write_delay=30), vendor_id="v")
        started = time.monotonic()
        client.flush()
        assert time.monotonic() - started < 1.0
        client._bridge.stop()  # type: ignore[union-attr]

    def test_close_called_while_this_thread_holds_the_close_lock_returns(self) -> None:
        client = Client(sink=_SlowCloseSink(), vendor_id="v")

        def signal_lands_inside_close() -> None:
            with client._close_lock:
                client._closing_thread = threading.get_ident()
                client.close()

        handler = threading.Thread(target=signal_lands_inside_close, daemon=True)
        handler.start()
        handler.join(timeout=2.0)
        assert not handler.is_alive()
