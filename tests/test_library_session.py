"""The library API's session block and envelope (SPEC §5.6)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

import pytest

from baton import INTENT_PARAMS, AsyncClient, Client, Principal
from baton.events import Event
from baton.integrations._llm_text import (
    build_expected_result_param_description,
    build_overall_task_param_description,
    build_user_goal_param_description,
)
from baton.sinks import Sink


class _CollectingSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...


def _client(sink: _CollectingSink) -> Client:
    return Client(vendor_id="v1", tenant_id="ten_abc", sink=sink)


def _traced(client: Client, **kwargs: Any) -> None:
    with client.trace(tool_name="fetch", **kwargs) as trace:
        trace.observed("ok")


def _wire(sink: _CollectingSink) -> list[dict[str, Any]]:
    return [event.model_dump(mode="json") for event in sink.events]


def test_every_event_in_a_block_carries_its_session_principal_and_client() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(
        session_id="conv-1",
        principal=Principal(principal_id="user-7"),
        client_info={"name": "claude-code", "version": "2.1"},
        headers={"User-Agent": "agent/1.0", "Authorization": "Bearer secret"},
    ):
        _traced(client)
        client.annotate(what_happened="thin result", tool_name="fetch")
    client.close()

    events = _wire(sink)
    assert [e["event_type"] for e in events] == ["tool_call_start", "tool_call_end", "annotation"]
    for event in events:
        assert event["session_id"] == "conv-1"
        assert event["session_id_origin"] == "observed"
        assert event["transport_observed"] == "library"
        assert event["principal"] == {
            "id": "user-7",
            "source": "asserted",
            "form": "raw",
            "display_name": None,
        }
        assert event["client_observed"] == {
            "info": {"name": "claude-code", "version": "2.1"},
            "headers": {"user-agent": "agent/1.0"},
        }


def test_a_block_without_a_session_id_mints_one_for_all_its_calls() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session() as block:
        _traced(client)
        _traced(client)
    client.close()

    assert {e.session_id for e in sink.events} == {block.session_id}
    assert {e.session_id_origin for e in sink.events} == {"minted"}


def test_a_trace_outside_any_block_gets_a_session_of_its_own() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    _traced(client)
    _traced(client)
    client.close()

    assert len({e.session_id for e in sink.events}) == 2
    assert {e.session_id_origin for e in sink.events} == {"minted"}
    assert {e.principal for e in sink.events} == {None}


def test_a_trace_report_keeps_the_trace_session_and_its_origin() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.trace(tool_name="fetch") as trace:
        trace.observed("ok")
    trace.annotate(what_happened="thin result", tool_name="fetch")
    client.close()

    assert len({e.session_id for e in sink.events}) == 1
    assert {e.session_id_origin for e in sink.events} == {"minted"}


def test_an_inner_block_replaces_the_outer_until_it_closes() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(session_id="outer"):
        with client.session(session_id="inner"):
            _traced(client)
        _traced(client)
    client.close()

    assert [e.session_id for e in sink.events] == ["inner", "inner", "outer", "outer"]


def test_one_clients_block_never_reaches_another_clients_events() -> None:
    sink_a, sink_b = _CollectingSink(), _CollectingSink()
    client_a, client_b = _client(sink_a), _client(sink_b)
    with client_a.session(session_id="conv-a"):
        _traced(client_b)
    client_a.close()
    client_b.close()

    assert {e.session_id_origin for e in sink_b.events} == {"minted"}


def test_bad_values_are_dropped_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with caplog.at_level(logging.WARNING, logger="baton"):
        with client.session(
            session_id="  ",
            principal={"principal_id": "u"},  # type: ignore[arg-type]
            turn_id=7,  # type: ignore[arg-type]
        ):
            _traced(client)
    client.close()

    start = sink.events[0]
    assert start.session_id_origin == "minted"
    assert start.principal is None
    assert start.runtime_meta is None
    assert "not a baton.Principal" in caplog.text
    assert "turn_id=7" in caplog.text


def test_a_disabled_client_opens_a_block_without_scrubbing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")

    def _never(value: Any) -> Any:
        raise AssertionError("the scrubber ran on a disabled client")

    client = Client(vendor_id="v1", sink=_CollectingSink(), scrubber=_never)
    with client.session(client_info={"name": "x"}, headers={"user-agent": "y"}):
        _traced(client)
    client.close()


async def test_concurrent_tasks_keep_their_own_blocks() -> None:
    sink = _CollectingSink()
    client = AsyncClient(vendor_id="v1", tenant_id="ten_abc", sink=sink)

    async def handle(conversation: str) -> None:
        with client.session(session_id=conversation):
            for _ in range(3):
                async with client.trace(tool_name="fetch") as trace:
                    await asyncio.sleep(0)
                    trace.observed("ok")

    await asyncio.gather(handle("conv-a"), handle("conv-b"))
    await client.aclose()

    by_session: dict[str, int] = {}
    for event in sink.events:
        by_session[event.session_id] = by_session.get(event.session_id, 0) + 1
    assert by_session == {"conv-a": 6, "conv-b": 6}


def test_sequence_numbers_increase_when_the_clock_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("baton._library_session.time.time_ns", lambda: 1_700_000_000_000_000_000)
    sink = _CollectingSink()
    client = _client(sink)
    with client.session():
        _traced(client)
        _traced(client)
    client.close()

    numbers = [e.sequence_number for e in sink.events]
    assert numbers == list(range(numbers[0], numbers[0] + 4))


def test_intent_params_carry_the_text_an_agent_reads() -> None:
    builders = (
        build_user_goal_param_description,
        build_expected_result_param_description,
        build_overall_task_param_description,
    )
    assert [p.name for p in INTENT_PARAMS] == ["user_goal", "expected_result", "overall_task"]
    assert [p.trace_kwarg for p in INTENT_PARAMS] == ["intent", "expected_outcome", "workflow"]
    for param, build in zip(INTENT_PARAMS, builders, strict=True):
        assert build(intent_param_mode="optional") == "OPTIONAL. " + param.description


def test_a_trace_report_after_close_raises_the_closed_error() -> None:
    client = _client(_CollectingSink())
    with client.trace(tool_name="fetch") as trace:
        trace.observed("ok")
    client.close()

    with pytest.raises(RuntimeError, match="Client is closed"):
        trace.annotate(what_happened="late", tool_name="fetch")


def test_blank_intent_is_no_intent() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    _traced(client, intent="  ")
    client.close()

    assert sink.events[0].payload.intent_source is None  # type: ignore[union-attr]


def test_two_clients_in_one_process_never_repeat_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("baton._library_session.time.time_ns", lambda: 1_700_000_000_000_000_000)
    sink_a, sink_b = _CollectingSink(), _CollectingSink()
    client_a, client_b = _client(sink_a), _client(sink_b)
    with client_a.session(session_id="shared"):
        _traced(client_a)
    with client_b.session(session_id="shared"):
        _traced(client_b)
    client_a.close()
    client_b.close()

    numbers = [e.sequence_number for e in sink_a.events + sink_b.events]
    assert len(set(numbers)) == len(numbers)


async def test_a_block_closed_in_another_context_does_not_raise() -> None:
    client = AsyncClient(vendor_id="v1", tenant_id="ten_abc", sink=_CollectingSink())
    block = client.session(session_id="conv-1")
    block.__enter__()

    async def close_elsewhere() -> None:
        block.__exit__(None, None, None)

    await asyncio.get_running_loop().create_task(close_elsewhere(), context=contextvars.Context())
    await client.aclose()


def test_session_and_turn_ids_are_sent_as_passed_not_scrubbed() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(session_id="jane@example.com", turn_id="jane@example.com:run-1"):
        _traced(client)
    client.close()

    assert {e.session_id for e in sink.events} == {"jane@example.com"}
    assert {e.runtime_meta["baton/turn_id"] for e in sink.events if e.runtime_meta} == {
        "jane@example.com:run-1"
    }


def test_a_padded_session_or_turn_id_is_stripped() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(session_id=" conv-1 ", turn_id=" run-1 "):
        _traced(client)
    client.close()

    assert {e.session_id for e in sink.events} == {"conv-1"}
    assert [e.runtime_meta for e in sink.events] == [{"baton/turn_id": "run-1"}] * 2


def test_trace_and_annotate_take_no_session_of_their_own() -> None:
    """Only ``session()`` sets the session, principal and client."""
    sync = _client(_CollectingSink())
    async_ = AsyncClient(vendor_id="v1", tenant_id="ten_abc", sink=_CollectingSink())
    for door in (sync.trace, sync.annotate, async_.trace, async_.annotate):
        for name in ("session_id", "principal", "client_info", "headers", "turn_id"):
            with pytest.raises(TypeError):
                door(tool_name="fetch", **{name: "x"})
    sync.close()


def test_annotate_outside_a_block_gets_a_session_of_its_own() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(session_id="conv-1"):
        pass
    client.annotate(what_happened="thin", tool_name="fetch")
    client.annotate(what_happened="thin again", tool_name="fetch")
    client.close()

    assert len({e.session_id for e in sink.events}) == 2
    assert {e.session_id_origin for e in sink.events} == {"minted"}


async def test_every_event_in_a_block_carries_its_turn_id() -> None:
    sync_sink, async_sink = _CollectingSink(), _CollectingSink()
    sync, async_ = _client(sync_sink), AsyncClient(vendor_id="v1", sink=async_sink)
    with sync.session(session_id="run-1", turn_id="run-1"):
        with sync.trace(tool_name="fetch") as trace:
            trace.observed("ok")
        trace.annotate(what_happened="thin", tool_name="fetch")
        sync.annotate(what_happened="thin again", tool_name="fetch")
    with async_.session(turn_id="run-1"):
        async with async_.trace(tool_name="fetch") as atrace:
            atrace.observed("ok")
    sync.close()
    await async_.aclose()

    events = _wire(sync_sink) + _wire(async_sink)
    assert [e["runtime_meta"] for e in events] == [{"baton/turn_id": "run-1"}] * 6


def test_a_block_without_a_turn_id_and_a_call_outside_any_block_send_no_runtime_meta() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(session_id="run-1"):
        _traced(client)
    _traced(client)
    client.close()

    assert {e["runtime_meta"] for e in _wire(sink)} == {None}
