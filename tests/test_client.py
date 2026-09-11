"""Tests for the library API (``Client``, ``AsyncClient``, ``Trace``, ``AsyncTrace``).

Uses pytest-httpserver for a real in-process HTTP capture server (no mocks);
asserts on the events that actually land on the wire.

Covers the library API surface (Client / AsyncClient / Trace / AsyncTrace):

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
from baton.events import DEFAULT_CONSENT_TOKEN
from baton.sinks import HttpSink

# =============================================================================
# Fixtures
# =============================================================================


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
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        )
        client.close()
        with pytest.raises(RuntimeError, match="closed"):
            client.trace(tool_name="t")

    def test_close_then_annotate_raises(self, capture_server: HTTPServer) -> None:
        client = Client(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        )
        client.close()
        with pytest.raises(RuntimeError, match="closed"):
            client.annotate(signal_type=SignalType.OTHER)

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
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
        ) as client:
            async with client.trace(tool_name="t") as trace:
                trace.observed("ok")
        assert len(captured_events) == 2  # start + end

    async def test_close_then_trace_raises(
        self,
        capture_server: HTTPServer,
    ) -> None:
        client = AsyncClient(
            sink=HttpSink(url=capture_server.url_for(""), api_key="x"),
            vendor_id="v",
            consent_token="ct-test",
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
