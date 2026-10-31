"""The library API's session block and envelope (SPEC §5.6)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Any

import pytest

from baton import INTENT_PARAMS, AsyncClient, Client, Organization, Principal
from baton.events import Event
from baton.integrations._llm_text import (
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


def test_a_block_sends_the_organization_beside_the_principal_or_alone() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    acme = Organization(organization_id="org_acme", display_name="Acme")
    with client.session(principal=Principal(principal_id="user-7", organization=acme)):
        _traced(client)
    with client.session(principal=acme):
        _traced(client)
    with client.session(principal=Principal(principal_id=" ", organization=acme)):
        _traced(client)
    client.close()

    acme_wire = {"id": "org_acme", "source": "asserted", "form": "raw", "display_name": "Acme"}
    events = _wire(sink)
    assert [(e["principal"] and e["principal"]["id"], e["organization"]) for e in events] == [
        ("user-7", acme_wire),
        ("user-7", acme_wire),
        (None, acme_wire),
        (None, acme_wire),
        (None, acme_wire),
        (None, acme_wire),
    ]


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
    with client.session(
        client_info={"name": "x"}, headers={"user-agent": "y"}, meta={"threadId": "t-1"}
    ):
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
    monkeypatch.setattr("baton._state.time.time_ns", lambda: 1_700_000_000_000_000_000)
    sink = _CollectingSink()
    client = _client(sink)
    with client.session():
        _traced(client)
        _traced(client)
    client.close()

    numbers = [e.sequence_number for e in sink.events]
    assert numbers == list(range(numbers[0], numbers[0] + 4))


def test_intent_params_carry_the_text_an_agent_reads() -> None:
    (param,) = INTENT_PARAMS
    assert (param.name, param.trace_kwarg) == ("user_goal", "intent")
    assert build_user_goal_param_description(intent_param_mode="optional") == (
        "OPTIONAL. " + param.description
    )


def test_a_closed_client_drops_events_and_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.trace(tool_name="fetch") as trace:
        trace.observed("ok")
    client.close()
    sent = len(sink.events)

    with caplog.at_level(logging.WARNING, logger="baton.client"):
        trace.annotate(what_happened="late", tool_name="fetch")
        _traced(client)
        served = client.list_tools([{"name": "fetch", "inputSchema": {"type": "object"}}])

    assert len(sink.events) == sent
    assert [t["name"] for t in served] == ["fetch"]
    assert [r.getMessage() for r in caplog.records] == [
        "baton: Client is closed; further events are dropped."
    ]


def test_a_closed_async_client_drops_events_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def run() -> int:
        sink = _CollectingSink()
        client = AsyncClient(vendor_id="v1", tenant_id="ten_abc", sink=sink)
        await client.aclose()
        with caplog.at_level(logging.WARNING, logger="baton.client"):
            async with client.trace(tool_name="fetch") as trace:
                trace.observed("ok")
            await trace.annotate(what_happened="late", tool_name="fetch")
            await client.list_tools([{"name": "fetch", "inputSchema": {"type": "object"}}])
        return len(sink.events)

    assert asyncio.run(run()) == 0
    assert [r.getMessage() for r in caplog.records] == [
        "baton: AsyncClient is closed; further events are dropped."
    ]


def test_blank_intent_is_no_intent() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    _traced(client, intent="  ")
    client.close()

    assert sink.events[0].payload.intent_source is None  # type: ignore[union-attr]


def test_two_clients_in_one_process_never_repeat_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("baton._state.time.time_ns", lambda: 1_700_000_000_000_000_000)
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
        for name in ("session_id", "principal", "client_info", "headers", "turn_id", "meta"):
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


def test_the_requests_meta_rides_scrubbed_with_the_vendors_turn_id() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    meta = {
        "threadId": "thr-1",
        "note": "mail jane@example.com",
        "baton/turn_id": "from-the-client",
        "location": {"latitude": 59.91273},
    }
    with client.session(turn_id="run-1", meta=meta):
        _traced(client)
    client.close()

    runtime_meta = sink.events[0].runtime_meta
    assert runtime_meta is not None
    assert runtime_meta["threadId"] == "thr-1"
    assert "jane@example.com" not in runtime_meta["note"]
    assert runtime_meta["location"] == {"latitude": 59.9}
    assert runtime_meta["baton/turn_id"] == "run-1"
    assert meta["baton/turn_id"] == "from-the-client"


def test_a_clients_turn_id_never_rides_without_the_vendors() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with client.session(meta={"baton/turn_id": "from-the-client"}):
        _traced(client)
    with client.session(meta={"baton/turn_id": "c", "threadId": "thr-1"}):
        _traced(client)
    client.close()

    assert [e.runtime_meta for e in sink.events] == [None, None] + [{"threadId": "thr-1"}] * 2


def test_client_info_in_meta_is_preferred_and_client_info_is_the_fallback() -> None:
    sink = _CollectingSink()
    client = _client(sink)
    declared = {"io.modelcontextprotocol/clientInfo": {"name": "codex", "version": "0.9"}}
    with client.session(meta=declared, client_info={"name": "handshake", "version": "1"}):
        _traced(client)
    with client.session(meta={"threadId": "t"}, client_info={"name": "handshake"}):
        _traced(client)
    client.close()

    infos = [e.client_observed.info if e.client_observed else None for e in sink.events]
    assert [i.name if i else None for i in infos] == ["codex", "codex", "handshake", "handshake"]


def test_meta_that_is_not_a_dict_is_dropped_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    sink = _CollectingSink()
    client = _client(sink)
    with caplog.at_level(logging.WARNING, logger="baton"):
        with client.session(turn_id="run-1", meta=["not", "a", "dict"]):  # type: ignore[arg-type]
            _traced(client)
    client.close()

    assert sink.events[0].runtime_meta == {"baton/turn_id": "run-1"}
    assert "meta=" in caplog.text


def test_a_scrubber_that_returns_no_dict_for_meta_costs_only_the_meta() -> None:
    def scrub(value: Any) -> Any:
        return "[gone]" if isinstance(value, dict) and "threadId" in value else value

    sink = _CollectingSink()
    client = Client(vendor_id="v1", tenant_id="ten_abc", sink=sink, scrubber=scrub)
    with client.session(turn_id="run-1", meta={"threadId": "thr-1"}):
        _traced(client)
    client.close()

    assert sink.events[0].runtime_meta == {"baton/turn_id": "run-1"}


def test_any_mapping_is_accepted_as_meta() -> None:
    from types import MappingProxyType

    sink = _CollectingSink()
    client = _client(sink)
    with client.session(meta=MappingProxyType({"threadId": "thr-1"})):
        _traced(client)
    client.close()

    assert sink.events[0].runtime_meta == {"threadId": "thr-1"}


def test_headers_are_scrubbed_once_per_block() -> None:
    seen: list[Any] = []

    def scrub(value: Any) -> Any:
        seen.append(value)
        return value

    client = Client(vendor_id="v1", tenant_id="ten_abc", sink=_CollectingSink(), scrubber=scrub)
    with client.session(
        meta={"threadId": "t"}, client_info={"name": "app"}, headers={"user-agent": "agent/1"}
    ):
        pass
    client.close()
    assert seen.count("agent/1") == 1


def test_a_client_info_that_is_a_mapping_but_not_a_dict_is_read() -> None:
    from types import MappingProxyType

    sink = _CollectingSink()
    client = _client(sink)
    declared = MappingProxyType({"name": "cursor", "version": "1.2"})
    with client.session(
        meta={"io.modelcontextprotocol/clientInfo": declared}, client_info={"name": "app"}
    ):
        _traced(client)
    client.close()

    observed = sink.events[0].client_observed
    assert observed is not None and observed.info is not None
    assert observed.info.name == "cursor"
