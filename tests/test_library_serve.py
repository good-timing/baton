"""``serve()``: Baton around a vendor's own MCP dispatch (SPEC §5.6)."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from baton import AsyncClient, Client, Organization, Principal, report_tool
from baton._capture_settings import CaptureSettings, parse_document
from baton.events import Event
from baton.integrations._llm_text import INTENT_INSTRUCTIONS
from baton.sinks import Sink

DSN = "https://key@example.invalid/ten_0123abcd/srv_lib"
REPORT = report_tool(vendor_display_name="Sunny").name
GOAL = "1: book a slot"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "find_slots",
        "description": "Finds slots.",
        "inputSchema": {
            "type": "object",
            "properties": {"day": {"type": "string"}},
            "required": ["day"],
        },
    },
    {"name": "legacy_book", "description": "Old.", "inputSchema": {"type": "object"}},
]

EVERYONE = {"id": "everyone", "match": {}, "capture": {}}
HIDES_LEGACY = {
    "version": 3,
    "audiences": [{**EVERYONE, "overlay": "ov_1"}],
    "overlays": {"ov_1": {"actions": [{"target": {"tool": "legacy_book"}, "remove": True}]}},
}
RESULTS_OFF = {"version": 4, "audiences": [{**EVERYONE, "capture": {"results": "off"}}]}


class _CollectingSink(Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def write(self, event: Event) -> None:
        self.events.append(event)

    async def flush(self) -> None: ...

    async def aclose(self) -> None: ...

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.events if e.event_type == event_type]


class _Fetcher:
    def __init__(self, document: dict[str, Any] | None) -> None:
        self.document = None if document is None else parse_document(json.dumps(document).encode())

    def start(self) -> None: ...

    def stop(self) -> None: ...


class Vendor:
    """A hand-written MCP server's dispatch: message in, answer out."""

    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self.tools = TOOLS if tools is None else tools
        self.seen: list[Any] = []
        self.answers: list[Any] = []
        self.call_answer: Any = None
        self.raises: Exception | None = None

    def __call__(self, message: Any) -> Any:
        self.seen.append(message)
        if self.raises is not None:
            raise self.raises
        method = message.get("method") if isinstance(message, dict) else None
        reply_id = message.get("id") if isinstance(message, dict) else None
        if method == "initialize":
            result: Any = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "Sunny", "version": "1"},
                "capabilities": {"tools": {}},
                "instructions": "Vendor text.",
            }
        elif method == "tools/list":
            result = {"tools": copy.deepcopy(self.tools)}
        elif method == "tools/call" and self.call_answer is not None:
            answer = self.call_answer
            self.answers.append(answer)
            return answer
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "booked 42"}], "isError": False}
        else:
            result = {}
        answer = {"jsonrpc": "2.0", "id": reply_id, "result": result}
        self.answers.append(answer)
        return answer


class Harness:
    """One client, sync or async, served through one call shape."""

    def __init__(self, make: type[Client] | type[AsyncClient], **kwargs: Any) -> None:
        self.is_async = make is AsyncClient
        self.loop = asyncio.new_event_loop() if self.is_async else None
        self.client: Any = make(**kwargs)

    def serve(self, message: Any, vendor: Callable[[Any], Any], **kwargs: Any) -> Any:
        if self.loop is None:
            return self.client.serve(message, vendor, **kwargs)

        async def dispatch(m: Any) -> Any:
            return vendor(m)

        return self.loop.run_until_complete(self.client.serve(message, dispatch, **kwargs))

    def close(self) -> None:
        if self.loop is None:
            self.client.close()
            return
        self.loop.run_until_complete(self.client.aclose())
        self.loop.close()


CLIENTS = pytest.mark.parametrize("make", [Client, AsyncClient], ids=["sync", "async"])


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> _CollectingSink:
    collecting = _CollectingSink()
    monkeypatch.setattr("baton.client.HttpSink", lambda *_, **__: collecting)
    return collecting


@pytest.fixture
def harness(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def build(document: dict[str, Any] | None = None, **kwargs: Any) -> Harness:
        fetcher = _Fetcher(document)
        monkeypatch.setattr(
            "baton.client.console_settings",
            lambda _dsn, _tenant, vendor_id, _refresh: CaptureSettings(
                vendor_id=vendor_id,
                fetcher=fetcher,  # type: ignore[arg-type]
            ),
        )
        options = {"dsn": DSN, "settings": "console", "vendor_display_name": "Sunny", **kwargs}
        made.append(Harness(make, **options))
        return made[-1]

    yield build
    for h in made:
        h.close()


def _initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"clientInfo": {"name": "claude-code", "version": "2"}},
    }


def _list(cursor: str | None = None) -> dict[str, Any]:
    params = {"cursor": cursor} if cursor else {}
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _names(answer: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in answer["result"]["tools"]]


def _tool(answer: dict[str, Any], name: str) -> dict[str, Any]:
    (tool,) = [t for t in answer["result"]["tools"] if t["name"] == name]
    return tool


# --- what serve() adds --------------------------------------------------------


@CLIENTS
def test_initialize_puts_batons_text_below_the_vendors(harness: Any) -> None:
    h = harness()
    answer = h.serve(_initialize(), Vendor())
    instructions = answer["result"]["instructions"]
    assert instructions.startswith("Vendor text.\n\n")
    assert REPORT in instructions
    assert instructions.endswith(INTENT_INSTRUCTIONS)
    assert answer["result"]["serverInfo"] == {"name": "Sunny", "version": "1"}


@CLIENTS
def test_the_list_gets_user_goal_and_the_report_tool(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    answer = h.serve(_list(), Vendor())
    assert _names(answer) == ["find_slots", "legacy_book", REPORT]
    schema = _tool(answer, "find_slots")["inputSchema"]
    assert schema["properties"]["day"] == {"type": "string"}
    assert "user_goal" in schema["properties"]
    assert schema["required"] == ["day", "user_goal"]
    (snapshot,) = sink.of("surface_snapshot")
    assert [t["name"] for t in snapshot["payload"]["tools"]] == ["find_slots", "legacy_book"]
    seam = snapshot["payload"]["seam_augmentations"]
    assert seam["injected_tools"] == [REPORT]
    assert seam["server_overlay"] is None


@CLIENTS
def test_local_settings_add_user_goal_and_no_report_tool(
    harness: Any, sink: _CollectingSink
) -> None:
    h = harness(settings="local")
    answer = h.serve(_list(), Vendor())
    assert _names(answer) == ["find_slots", "legacy_book"]
    (snapshot,) = sink.of("surface_snapshot")
    assert "server_overlay" not in snapshot["payload"]["seam_augmentations"]


@CLIENTS
def test_a_paged_list_gets_the_report_tool_on_its_first_page(harness: Any) -> None:
    h = harness()

    def paged(message: Any) -> Any:
        answer = Vendor()(message)
        answer["result"]["nextCursor"] = "p2"
        return answer

    assert REPORT in _names(h.serve(_list(), paged))
    assert REPORT not in _names(h.serve(_list("p2"), Vendor()))


@CLIENTS
def test_a_call_loses_user_goal_and_is_traced(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    vendor = Vendor()
    h.serve(_list(), vendor)
    answer = h.serve(_call("find_slots", {"day": "mon", "user_goal": GOAL}), vendor)
    assert vendor.seen[-1]["params"]["arguments"] == {"day": "mon"}
    assert answer is vendor.answers[-1]
    (start,) = sink.of("tool_call_start")
    assert start["payload"]["call_intent"] == GOAL
    assert start["payload"]["params"] == {"day": "mon"}
    (end,) = sink.of("tool_call_end")
    assert "booked 42" in json.dumps(end["payload"]["result"])


@CLIENTS
def test_the_principal_and_headers_ride_the_call(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    principal = Principal(
        principal_id="u1", organization=Organization(organization_id="acme", labels=["vip"])
    )
    h.serve(
        _call("find_slots", {"day": "mon"}),
        Vendor(),
        principal=principal,
        headers={"user-agent": "claude-code/2"},
    )
    (start,) = sink.of("tool_call_start")
    assert start["principal"]["id"] == "u1"
    assert start["labels"] == ["vip"]
    assert start["client_observed"] is not None


@CLIENTS
def test_a_report_is_answered_by_baton(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    vendor = Vendor()
    answer = h.serve(
        _call(REPORT, {"user_goal": GOAL, "what_happened": "find_slots returned nothing"}), vendor
    )
    assert vendor.seen == []
    assert answer["id"] == 3
    assert answer["result"]["structuredContent"] == {"ok": True}
    (annotation,) = sink.of("annotation")
    assert annotation["payload"]["what_happened"] == "find_slots returned nothing"


@CLIENTS
def test_a_report_with_nothing_wrong_is_refused(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    answer = h.serve(_call(REPORT, {"user_goal": GOAL}), Vendor())
    assert answer["result"]["structuredContent"]["ok"] is False
    assert sink.of("annotation") == []


@CLIENTS
def test_a_hidden_tool_is_unknown_to_the_caller(harness: Any, sink: _CollectingSink) -> None:
    h = harness(HIDES_LEGACY)
    vendor = Vendor()
    assert "legacy_book" not in _names(h.serve(_list(), vendor))
    answer = h.serve(_call("legacy_book", {"user_goal": GOAL}), vendor)
    assert answer["result"] == {
        "content": [{"type": "text", "text": "Unknown tool: legacy_book"}],
        "isError": True,
    }
    assert [m["method"] for m in vendor.seen] == ["tools/list"]
    (error,) = sink.of("tool_call_error")
    assert error["payload"]["failure_kind"] == "tool_hidden"


@CLIENTS
def test_without_settings_every_call_reaches_dispatch(harness: Any) -> None:
    h = harness(settings="local")
    vendor = Vendor()
    h.serve(_call("legacy_book", {}), vendor)
    h.serve(_call(REPORT, {"what_happened": "x"}), vendor)
    assert len(vendor.seen) == 2


@CLIENTS
def test_results_off_withholds_results_and_errors(harness: Any, sink: _CollectingSink) -> None:
    h = harness(RESULTS_OFF)
    vendor = Vendor()
    h.serve(_call("find_slots", {"day": "mon"}), vendor)
    vendor.call_answer = {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"content": [{"type": "text", "text": "secret detail"}], "isError": True},
    }
    h.serve(_call("find_slots", {"day": "mon"}), vendor)
    vendor.raises = RuntimeError("secret raise")
    with pytest.raises(RuntimeError):
        h.serve(_call("find_slots", {"day": "mon"}), vendor)
    sent = json.dumps([e.model_dump(mode="json") for e in sink.events])
    assert "booked 42" not in sent
    assert "secret" not in sent
    assert [e["payload"]["result_capture"] for e in sink.of("tool_call_error")] == ["off", "off"]


@CLIENTS
def test_a_tool_whose_root_schema_is_composite_is_left_alone(harness: Any) -> None:
    composite = {
        "name": "pick",
        "inputSchema": {"oneOf": [{"type": "object"}, {"type": "object"}]},
    }
    h = harness()
    vendor = Vendor([composite])
    assert _tool(h.serve(_list(), vendor), "pick")["inputSchema"] == composite["inputSchema"]
    # It declares no user_goal, so the subagent sentence stays.
    assert h.serve(_initialize(), vendor)["result"]["instructions"].endswith(INTENT_INSTRUCTIONS)


@CLIENTS
def test_a_tool_with_its_own_user_goal_keeps_it(harness: Any) -> None:
    own = {
        "name": "own",
        "inputSchema": {"type": "object", "properties": {"user_goal": {"type": "string"}}},
    }
    h = harness()
    vendor = Vendor([own])
    h.serve(_list(), vendor)
    h.serve(_call("own", {"user_goal": "theirs"}), vendor)
    assert vendor.seen[-1]["params"]["arguments"] == {"user_goal": "theirs"}
    assert not h.serve(_initialize(), vendor)["result"]["instructions"].endswith(
        INTENT_INSTRUCTIONS
    )


@CLIENTS
def test_a_vendor_tool_with_the_report_name_wins(
    harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mine = {"name": REPORT, "description": "Mine.", "inputSchema": {"type": "object"}}
    h = harness()
    vendor = Vendor([mine])
    with caplog.at_level(logging.WARNING, logger="baton"):
        h.serve(_list(), vendor)
        answer = h.serve(_list(), vendor)
    assert _names(answer) == [REPORT]
    assert _tool(answer, REPORT)["description"] == "Mine."
    assert sum("report tool's name" in r.getMessage() for r in caplog.records) == 1
    h.serve(_call(REPORT, {"what_happened": "x"}), vendor)
    assert vendor.seen[-1]["params"]["name"] == REPORT
    assert REPORT not in h.serve(_initialize(), vendor)["result"]["instructions"]


@CLIENTS
def test_a_paged_list_sends_no_snapshot(harness: Any, sink: _CollectingSink) -> None:
    h = harness()

    def paged(message: Any) -> Any:
        answer = Vendor()(message)
        answer["result"]["nextCursor"] = "p2"
        return answer

    h.serve(_list(), paged)
    h.serve(_list("p2"), Vendor())
    assert sink.of("surface_snapshot") == []


@CLIENTS
def test_a_block_the_vendor_opened_is_kept(harness: Any, sink: _CollectingSink) -> None:
    h = harness(settings="local")
    with h.client.session(principal=Principal(principal_id="u9"), session_id="conv-1"):
        h.serve(_call("find_slots", {"day": "mon"}), Vendor())
    (start,) = sink.of("tool_call_start")
    assert start["principal"]["id"] == "u9"
    assert start["session_id"] == "conv-1"


@CLIENTS
def test_a_scrubber_that_raises_still_records_a_failure(
    harness: Any, sink: _CollectingSink
) -> None:
    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            raise ValueError("scrubber broke")
        return value

    h = harness(scrubber=scrub)
    vendor = Vendor()
    vendor.call_answer = {"jsonrpc": "2.0", "id": 3, "error": {"code": -1, "message": "bad"}}
    assert h.serve(_call("find_slots", {}), vendor) is vendor.call_answer
    assert sink.of("tool_call_end") == []
    (error,) = sink.of("tool_call_error")
    assert error["payload"]["error_type"] == "JSONRPCError"


@CLIENTS
def test_a_server_name_with_no_tool_name_falls_back_to_the_vendor_id(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "baton.client.console_settings",
        lambda _dsn, _tenant, vendor_id, _refresh: CaptureSettings(
            vendor_id=vendor_id,
            fetcher=_Fetcher(None),  # type: ignore[arg-type]
        ),
    )
    h = Harness(make, dsn="https://k@example.invalid/ten_0123abcd/fastmcp", settings="console")
    assert "fastmcp_annotate" in _names(h.serve(_list(), Vendor()))
    h.close()


# --- never break the vendor ---------------------------------------------------


@CLIENTS
def test_the_vendors_exception_comes_out_unchanged(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    vendor = Vendor()
    vendor.raises = KeyError("theirs")
    with pytest.raises(KeyError) as raised:
        h.serve(_call("find_slots", {"day": "mon"}), vendor)
    assert raised.value is vendor.raises
    assert raised.value.__context__ is None
    (error,) = sink.of("tool_call_error")
    assert error["payload"]["error_type"] == "KeyError"


@CLIENTS
def test_a_json_rpc_error_answer_comes_out_unchanged(harness: Any, sink: _CollectingSink) -> None:
    h = harness()
    vendor = Vendor()
    vendor.call_answer = {"jsonrpc": "2.0", "id": 3, "error": {"code": -32602, "message": "bad"}}
    assert h.serve(_call("find_slots", {}), vendor) is vendor.call_answer
    (error,) = sink.of("tool_call_error")
    assert error["payload"]["error_type"] == "JSONRPCError"


MALFORMED = [
    None,
    [],
    "tools/list",
    {"method": "tools/call", "params": {"name": "find_slots"}},
    {"id": 1, "method": "tools/call", "params": None},
    {"id": 1, "method": "tools/call", "params": {"name": 5}},
    {"id": 1, "method": "tools/call", "params": {"name": "x", "arguments": []}},
    {"id": 1, "method": "ping"},
]


@CLIENTS
@pytest.mark.parametrize("message", MALFORMED, ids=range(len(MALFORMED)))
def test_anything_else_goes_to_dispatch_as_it_came(
    harness: Any, sink: _CollectingSink, message: Any
) -> None:
    h = harness()
    vendor = Vendor()
    assert h.serve(message, vendor) is vendor.answers[-1]
    assert vendor.seen == [message] and vendor.seen[0] is message
    assert sink.events == []


@CLIENTS
def test_the_off_switch_makes_serve_a_plain_dispatch(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")
    h = Harness(make, settings="console")
    vendor = Vendor()
    message = _call("find_slots", {"user_goal": GOAL})
    assert h.serve(message, vendor) is vendor.answers[-1]
    assert vendor.seen[0] is message
    h.close()
    assert sink.events == []


def _boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("baton broke")


# Each of Baton's steps, and the methods whose answer it changes.
STEPS = {
    "baton.client.read_request": ("initialize", "tools/list", "tools/call"),
    "baton.client.session_for_call": ("initialize", "tools/list", "tools/call"),
    "baton._library_serve.Serving.initialized": ("initialize",),
    "baton._library_serve.Serving.listed": ("tools/list",),
    "baton._library_serve.Serving.call": ("tools/call",),
    "baton.client.record": ("tools/call",),
}


@CLIENTS
@pytest.mark.parametrize("step", STEPS)
def test_a_baton_failure_serves_the_vendors_own_answer(
    harness: Any, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    h = harness()
    monkeypatch.setattr(step, _boom)
    for message in (_initialize(), _list(), _call("find_slots", {"day": "mon"})):
        vendor = Vendor()
        answer = h.serve(message, vendor)
        assert len(vendor.seen) == 1
        if message["method"] in STEPS[step]:
            assert answer == Vendor()(copy.deepcopy(message))


@CLIENTS
def test_a_failure_before_dispatch_still_removes_user_goal(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness()
    monkeypatch.setattr("baton._library_serve.Serving.call", _boom)
    vendor = Vendor()
    message = _call("find_slots", {"day": "mon", "user_goal": GOAL})
    h.serve(message, vendor)
    # The list Baton served asked for user_goal, so the vendor never sees it.
    assert vendor.seen == [_call("find_slots", {"day": "mon"})]


@CLIENTS
def test_serve_never_waits_on_a_sink_that_hangs(make: Any) -> None:
    class Hangs(_CollectingSink):
        async def write(self, event: Event) -> None:
            await asyncio.sleep(3600)

    h = Harness(make, vendor_id="v", sink=Hangs(), report_mode="on")
    vendor = Vendor()
    started = time.monotonic()
    for _ in range(20):
        h.serve(_list(), vendor)
        h.serve(_call("find_slots", {"day": "mon", "user_goal": GOAL}), vendor)
    assert time.monotonic() - started < 2.0
    h.close()


@CLIENTS
def test_a_raised_message_is_cut_to_the_cap(harness: Any, sink: _CollectingSink) -> None:
    h = harness(settings="local", scrubber=lambda value: value)
    vendor = Vendor()
    vendor.raises = RuntimeError("x" * 5000)
    with pytest.raises(RuntimeError):
        h.serve(_call("find_slots", {}), vendor)
    (error,) = sink.of("tool_call_error")
    assert len(error["payload"]["error_body"]) == 2000


# --- tools=: what a process knows before it serves a list ---------------------

OWN_GOAL = {
    "name": "own",
    "inputSchema": {"type": "object", "properties": {"user_goal": {"type": "string"}}},
}
CLASHING = {"name": REPORT, "description": "Mine.", "inputSchema": {"type": "object"}}


@CLIENTS
@pytest.mark.parametrize("given", ["list", "function"])
def test_tools_keep_a_tools_own_user_goal_before_any_list(harness: Any, given: str) -> None:
    tools = [OWN_GOAL]
    h = harness(tools=tools if given == "list" else lambda: tools)
    vendor = Vendor(tools)
    h.serve(_call("own", {"user_goal": "theirs"}), vendor)
    assert vendor.seen[-1]["params"]["arguments"] == {"user_goal": "theirs"}


@CLIENTS
def test_tools_send_a_call_to_the_vendors_report_named_tool_to_it(harness: Any) -> None:
    h = harness(tools=[CLASHING])
    vendor = Vendor([CLASHING])
    h.serve(_call(REPORT, {"what_happened": "x"}), vendor)
    assert vendor.seen[-1]["params"]["name"] == REPORT


@CLIENTS
def test_tools_set_the_first_instructions_right(harness: Any) -> None:
    h = harness(tools=[OWN_GOAL])
    instructions = h.serve(_initialize(), Vendor([OWN_GOAL]))["result"]["instructions"]
    assert not instructions.endswith(INTENT_INSTRUCTIONS)


@CLIENTS
def test_a_tools_function_that_raises_falls_back_with_one_warning(
    harness: Any, caplog: pytest.LogCaptureFixture
) -> None:
    calls = []

    def broken() -> list[Any]:
        calls.append(1)
        raise RuntimeError("no list")

    h = harness(tools=broken)
    vendor = Vendor([OWN_GOAL])
    with caplog.at_level(logging.WARNING, logger="baton"):
        h.serve(_call("own", {"user_goal": "theirs"}), vendor)
        h.serve(_call("own", {"user_goal": "theirs"}), vendor)
    assert vendor.seen[-1]["params"]["arguments"] == {}
    assert calls == [1]
    assert sum("could not read tools=" in r.getMessage() for r in caplog.records) == 1


@CLIENTS
def test_a_served_list_still_teaches_after_tools(harness: Any) -> None:
    h = harness(tools=[TOOLS[0]])
    vendor = Vendor([OWN_GOAL])
    h.serve(_list(), vendor)
    h.serve(_call("own", {"user_goal": "theirs"}), vendor)
    assert vendor.seen[-1]["params"]["arguments"] == {"user_goal": "theirs"}


@CLIENTS
def test_the_off_switch_never_reads_tools(
    make: Any, sink: _CollectingSink, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")

    def refuse() -> list[Any]:
        raise AssertionError("read tools")

    h = Harness(make, settings="console", tools=refuse)
    h.serve(_call("own", {}), Vendor())
    h.close()


# --- old wiring left inside serve() --------------------------------------------


@pytest.fixture
def fresh_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("baton.client._DOUBLE_WIRING_LOGGED", set())


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@CLIENTS
def test_old_wiring_inside_serve_is_an_error_once_each(
    harness: Any, caplog: pytest.LogCaptureFixture, fresh_log: None
) -> None:
    h = harness()
    vendor = Vendor()

    def hand_wired(message: Any) -> Any:
        with h.client.session():
            h.client.trace(tool_name="find_slots")
            return vendor(message)

    with caplog.at_level(logging.ERROR, logger="baton"):
        for _ in range(3):
            h.serve(_call("find_slots", {"day": "mon"}), hand_wired)
    errors = _errors(caplog)
    assert len(errors) == 2
    assert "client.session() was called inside serve()" in errors[0]
    assert "client.trace() was called inside serve()" in errors[1]


@CLIENTS
def test_serve_on_its_own_logs_no_error(
    harness: Any, caplog: pytest.LogCaptureFixture, fresh_log: None
) -> None:
    h = harness()
    vendor = Vendor()
    with caplog.at_level(logging.ERROR, logger="baton"):
        h.serve(_initialize(), vendor)
        h.serve(_list(), vendor)
        h.serve(_call("find_slots", {"day": "mon"}), vendor)
        h.serve(_call(REPORT, {"what_happened": "x"}), vendor)
    assert _errors(caplog) == []


def test_old_wiring_outside_serve_is_fine(
    caplog: pytest.LogCaptureFixture, fresh_log: None
) -> None:
    client = Client(vendor_id="v", sink=_CollectingSink())
    with caplog.at_level(logging.ERROR, logger="baton"), client.session():
        client.list_tools(TOOLS)
        with client.trace(tool_name="find_slots") as trace:
            trace.observed({})
    client.close()
    assert _errors(caplog) == []
