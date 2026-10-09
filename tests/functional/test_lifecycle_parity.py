"""Both MCP adapters send the twelve resource and prompt events (SPEC §11.4.4),
and send the same ones for the same client."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from baton import Principal
from tests._event_helpers import LIFECYCLE_EVENT_TYPES, principal_of, read_events
from tests._mcp_session import DECLARED_VERSION

pytestmark = pytest.mark.functional

DOC_URI = "file:///docs/SECRET-plan.txt"
BROKEN_URI = "file:///broken.txt"
SENT_META = {"io.example/trace": "abc"}

Hook = Callable[[Any], Principal | None]
Action = Callable[[Any], Awaitable[None]]
Driver = Callable[..., Awaitable[None]]


def _config(config_class: Any, events_path: Path, hook: Hook | None, scrubber: Any) -> Any:
    from baton.sinks import FileSink

    return config_class(
        **({} if scrubber is None else {"scrubber": scrubber}),
        vendor_id="parity",
        vendor_display_name="Parity Vendor",
        consent_token="ct_parity",
        sink=FileSink(str(events_path)),
        resolve_principal=hook,
    )


def _register(mcp: Any) -> None:
    @mcp.resource(DOC_URI)
    def doc() -> str:
        return "the plan"

    @mcp.resource(BROKEN_URI)
    def broken() -> str:
        raise RuntimeError("the disk is gone")

    @mcp.prompt()
    def summarize(topic: str) -> str:
        return f"Summarize {topic}"

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}


_METHODS = {
    "resources/list": "ListResourcesRequest",
    "resources/read": "ReadResourceRequest",
    "prompts/list": "ListPromptsRequest",
    "prompts/get": "GetPromptRequest",
}


def _break_every_handler(low_level: Any, failure: BaseException) -> None:
    """Make the server's own handler for each of the four requests raise."""
    import mcp.types as mcp_types

    handlers = getattr(low_level, "request_handlers", None)
    for method, request_type in _METHODS.items():
        if handlers is None:

            async def refuse_params(_context: Any, _params: Any) -> Any:
                raise failure

            params_type = low_level.get_request_handler(method).params_type
            low_level.add_request_handler(method, params_type, refuse_params)
        else:

            async def refuse_request(_request: Any) -> Any:
                raise failure

            handlers[getattr(mcp_types, request_type)] = refuse_request


async def _drive_official(
    events_path: Path,
    action: Action,
    *,
    hook: Hook | None = None,
    scrubber: Any = None,
    broken: BaseException | None = None,
    installed: bool = True,
) -> None:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass, get_lowlevel_server
    from tests._mcp_session import connected_session

    mcp = MCPServerClass("parity-official")
    _register(mcp)
    if broken is not None:
        _break_every_handler(get_lowlevel_server(mcp), broken)
    handle = (
        install_baton(mcp, _config(VendorConfig, events_path, hook, scrubber))
        if installed
        else None
    )
    try:
        async with connected_session(mcp, declared_name="lifecycle-client") as session:
            await action(session)
    finally:
        if handle is not None:
            await handle.aclose()


async def _drive_standalone(
    events_path: Path,
    action: Action,
    *,
    hook: Hook | None = None,
    scrubber: Any = None,
    broken: BaseException | None = None,
    installed: bool = True,
) -> None:
    import mcp.types as mcp_types
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp: Any = FastMCP("parity-standalone")
    _register(mcp)
    if broken is not None:
        _break_every_handler(mcp._mcp_server, broken)
    handle = (
        install_baton(mcp, _config(VendorConfig, events_path, hook, scrubber))
        if installed
        else None
    )
    try:
        client_info = mcp_types.Implementation(name="lifecycle-client", version=DECLARED_VERSION)
        async with Client(mcp, client_info=client_info) as client:
            await action(client.session)
    finally:
        if handle is not None:
            await handle.aclose()


ADAPTERS = [
    pytest.param(_drive_official, id="official"),
    pytest.param(_drive_standalone, id="standalone"),
]


async def _all_four(session: Any) -> None:
    await session.list_resources()
    await session.read_resource(DOC_URI)
    await session.list_prompts()
    await session.get_prompt("summarize", {"topic": "the quarter"})


async def _read_the_broken_one(session: Any) -> None:
    with pytest.raises(Exception, match=r"\S"):
        await session.read_resource(BROKEN_URI)


def _who(ev: dict[str, Any]) -> str | None:
    principal = principal_of(ev)
    return None if principal is None else principal["id"]


def _lifecycle_events(events_path: Path) -> list[dict[str, Any]]:
    return [ev for ev in read_events(events_path) if ev["event_type"] in LIFECYCLE_EVENT_TYPES]


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_each_request_sends_a_start_and_an_end(drive: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    counts: dict[str, int] = {}

    async def all_four_counted(session: Any) -> None:
        counts["resources"] = len((await session.list_resources()).resources)
        await session.read_resource(DOC_URI)
        counts["prompts"] = len((await session.list_prompts()).prompts)
        await session.get_prompt("summarize", {"topic": "the quarter"})

    await drive(events_path, all_four_counted)

    events = _lifecycle_events(events_path)
    assert [ev["event_type"] for ev in events] == [
        "resource_list_start",
        "resource_list_end",
        "resource_read_start",
        "resource_read_end",
        "prompt_list_start",
        "prompt_list_end",
        "prompt_get_start",
        "prompt_get_end",
    ]
    payloads = [ev["payload"] for ev in events]
    durations = [p.pop("duration_ms") for p in payloads[1::2]]
    assert all(isinstance(d, int) for d in durations)
    assert payloads == [
        {},
        {"count": counts["resources"]},
        {"uri": DOC_URI, "params": {"uri": DOC_URI}},
        {"uri": DOC_URI},
        {},
        {"count": counts["prompts"]},
        {"name": "summarize", "params": {"topic": "the quarter"}},
        {"name": "summarize"},
    ]
    assert counts == {"resources": 2, "prompts": 1}
    assert [ev.get("call_id") for ev in events] == [None] * 8
    assert len({ev["session_id"] for ev in events}) == 1
    numbers = [ev["sequence_number"] for ev in events]
    assert numbers == sorted(numbers) and len(set(numbers)) == 8


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_every_event_carries_the_principal_the_hook_returned(
    drive: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    asked: list[tuple[Any, Any]] = []

    def hook(context: Any) -> Principal:
        asked.append((context.tool_name, dict(context.arguments)))
        return Principal(principal_id="user-7")

    await drive(events_path, _all_four, hook=hook)

    assert asked == [(None, {})] * 4
    events = _lifecycle_events(events_path)
    assert len(events) == 8
    assert [_who(ev) for ev in events] == ["user-7"] * 8


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_a_failed_read_sends_an_error_and_still_fails(drive: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    await drive(
        events_path, _read_the_broken_one, hook=lambda _context: Principal(principal_id="user-7")
    )

    start, error = _lifecycle_events(events_path)
    assert (start["event_type"], error["event_type"]) == (
        "resource_read_start",
        "resource_read_error",
    )
    assert error["payload"]["uri"] == BROKEN_URI
    assert error["payload"]["error_type"].endswith("Error")
    assert BROKEN_URI in error["payload"]["error_body"]
    assert isinstance(error["payload"]["duration_ms"], int)
    assert set(error["payload"]) == {"uri", "error_type", "error_body", "duration_ms"}
    assert [_who(start), _who(error)] == ["user-7", "user-7"]


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_a_hook_that_raises_costs_the_principal_not_the_request(
    drive: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    def hook(_context: Any) -> Principal:
        raise RuntimeError("the vendor's hook is broken")

    await drive(events_path, _all_four, hook=hook)

    events = _lifecycle_events(events_path)
    assert len(events) == 8
    assert [_who(ev) for ev in events] == [None] * 8


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_the_scrubber_sees_the_subject_and_the_params_together(
    drive: Driver, tmp_path: Path
) -> None:
    """A scrubber that redacts by key must find ``uri`` beside ``params``,
    which holds it too. Scrubbing one and not the other sends the value."""
    events_path = tmp_path / "events.jsonl"

    def by_value(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("SECRET", "[gone]")
        if isinstance(value, dict):
            return {key: by_value(item) for key, item in value.items()}
        return value

    def by_key(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: "[uri]" if key == "uri" else by_key(item) for key, item in value.items()}
        return value

    def dropping_the_key(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: dropping_the_key(item) for key, item in value.items() if key != "uri"}
        return value

    await drive(events_path, _all_four, scrubber=by_value)
    by_value_events = _lifecycle_events(events_path)
    events_path.unlink()
    await drive(events_path, _all_four, scrubber=by_key)
    by_key_events = _lifecycle_events(events_path)
    events_path.unlink()
    await drive(events_path, _all_four, scrubber=dropping_the_key)
    dropped_events = _lifecycle_events(events_path)

    assert len(by_value_events) == len(by_key_events) == 8
    assert "SECRET" not in json.dumps([ev["payload"] for ev in by_value_events])
    assert by_value_events[2]["payload"]["uri"] == DOC_URI.replace("SECRET", "[gone]")
    assert DOC_URI not in json.dumps([ev["payload"] for ev in by_key_events])
    assert by_key_events[2]["payload"] == {"uri": "[uri]", "params": {"uri": "[uri]"}}
    assert by_key_events[3]["payload"]["uri"] == "[uri]"
    assert DOC_URI not in json.dumps(dropped_events)
    assert [ev["event_type"] for ev in dropped_events if "read" in ev["event_type"]] == []


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_the_requests_meta_is_on_the_envelope_and_not_in_params(
    drive: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    async def read_with_meta(session: Any) -> None:
        import mcp.types as mcp_types

        request = mcp_types.ReadResourceRequest(
            params=mcp_types.ReadResourceRequestParams.model_validate(
                {"uri": DOC_URI, "_meta": SENT_META}
            )
        )
        send = session.send_request
        try:
            await send(mcp_types.ClientRequest(request), mcp_types.ReadResourceResult)
        except (AttributeError, TypeError):
            await send(request, mcp_types.ReadResourceResult)

    await drive(events_path, read_with_meta)

    start, end = _lifecycle_events(events_path)
    assert start["payload"] == {"uri": DOC_URI, "params": {"uri": DOC_URI}}
    assert start["runtime_meta"]["io.example/trace"] == "abc"
    assert end["runtime_meta"]["io.example/trace"] == "abc"


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_a_tool_call_sends_none_of_them(drive: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    async def call_a_tool(session: Any) -> None:
        await session.call_tool("lookup", {"name": "alice"})

    await drive(events_path, call_a_tool)

    assert read_events(events_path)
    assert _lifecycle_events(events_path) == []


def _install(adapter: str, events_path: Path, result_capture_mode: str) -> Any:
    from baton.sinks import FileSink

    if adapter == "official":
        from baton.integrations.official import VendorConfig, install_baton
        from baton.integrations.official._compat import MCPServerClass as Server
    elif adapter == "official through baton.install_baton":
        from baton import VendorConfig, install_baton
        from baton.integrations.official._compat import MCPServerClass as Server
    else:
        from fastmcp import FastMCP as Server

        from baton.integrations.standalone import VendorConfig, install_baton
    return install_baton(
        Server("parity"),
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
            result_capture_mode=result_capture_mode,
        ),
    )


@pytest.mark.parametrize(
    "adapter", ["official", "official through baton.install_baton", "standalone"]
)
async def test_withholding_results_warns_that_a_failed_requests_message_is_kept(
    adapter: str, tmp_path: Path
) -> None:
    """SPEC §11.4.4: the four error events carry the exception's text under
    every capture mode, and a producer with a withholding mode says so when the
    mode is set."""
    with pytest.warns(UserWarning) as caught:
        handle = _install(adapter, tmp_path / "events.jsonl", "off")
    await handle.aclose()

    (warning,) = [w for w in caught if "result_capture_mode" in str(w.message)]
    assert warning.filename == __file__
    for leg in (
        "resource_list_error",
        "resource_read_error",
        "prompt_list_error",
        "prompt_get_error",
    ):
        assert leg in str(warning.message)


@pytest.mark.parametrize("adapter", ["official", "standalone"])
async def test_capturing_results_does_not_warn(adapter: str, tmp_path: Path) -> None:
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        handle = _install(adapter, tmp_path / "events.jsonl", "full")
    await handle.aclose()

    assert [w for w in caught if "result_capture_mode" in str(w.message)] == []


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise ValueError("this exception cannot be printed")


async def _each_request_fails(session: Any, seen: list[str]) -> None:
    for request in (
        lambda: session.list_resources(),
        lambda: session.read_resource(DOC_URI),
        lambda: session.list_prompts(),
        lambda: session.get_prompt("summarize", {"topic": "the quarter"}),
    ):
        try:
            await request()
        except Exception as failed:
            seen.append(f"{type(failed).__name__}: {failed}")
        else:
            seen.append("answered")


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_a_failed_request_reaches_the_client_as_it_does_without_baton(
    drive: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    failure = RuntimeError("the backend is down")
    without: list[str] = []
    with_baton: list[str] = []

    await drive(
        events_path, lambda s: _each_request_fails(s, without), broken=failure, installed=False
    )
    await drive(events_path, lambda s: _each_request_fails(s, with_baton), broken=failure)

    assert "answered" not in without
    assert with_baton == without


@pytest.mark.parametrize("drive", ADAPTERS)
async def test_each_failed_request_sends_a_start_and_an_error(
    drive: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    await drive(
        events_path,
        lambda s: _each_request_fails(s, []),
        broken=RuntimeError("the backend is down"),
    )

    events = _lifecycle_events(events_path)
    assert [ev["event_type"] for ev in events] == [
        "resource_list_start",
        "resource_list_error",
        "resource_read_start",
        "resource_read_error",
        "prompt_list_start",
        "prompt_list_error",
        "prompt_get_start",
        "prompt_get_error",
    ]
    for error in events[1::2]:
        assert error["payload"]["error_type"] == "RuntimeError"
        assert error["payload"]["error_body"] == "the backend is down"
    assert events[7]["payload"]["name"] == "summarize"


def test_a_result_that_cannot_be_counted_counts_as_none() -> None:
    from types import SimpleNamespace

    from baton.integrations._lifecycle import _count

    assert _count(SimpleNamespace(resources=[1, 2]), "resources") == 2
    assert _count(SimpleNamespace(root=SimpleNamespace(resources=[1])), "resources") == 1
    assert _count({"resources": [1, 2, 3]}, "resources") == 3
    assert _count({"resources": None}, "resources") == 0
    assert _count(object(), "resources") == 0


async def test_the_handlers_own_exception_is_the_one_that_propagates(tmp_path: Path) -> None:
    """Driven below the library, which turns every exception into one error
    response and so hides which exception the handler let out."""
    from types import SimpleNamespace

    from baton._state import SessionCounter
    from baton.integrations._lifecycle import CapturedRequest, install_lifecycle_capture
    from baton.sinks import FileSink

    async def refuse(_context: Any, _params: Any) -> Any:
        raise _Unprintable

    wrapped: dict[str, Any] = {}
    server = SimpleNamespace(
        get_request_handler=lambda method: SimpleNamespace(handler=refuse, params_type=None),
        add_request_handler=lambda method, _params_type, handler: wrapped.update({method: handler}),
    )

    async def read_request(_context: Any) -> CapturedRequest:
        return CapturedRequest(
            meta=None, headers=None, handshake_context=None, transport_observed=None, session_id="s"
        )

    sink = FileSink(str(tmp_path / "events.jsonl"))
    install_lifecycle_capture(
        lambda: server,
        tenant_id="ten_1",
        vendor_id="acme",
        consent_token="ct",
        sink=sink,
        counter=SessionCounter(),
        scrubber=lambda value: value,
        resolve_principal_hook=None,
        read_request=read_request,
        read_access_token=lambda: None,
    )

    assert set(wrapped) == {"tools/list", *_METHODS}
    for handler in wrapped.values():
        with pytest.raises(_Unprintable):
            await handler(None, None)


@pytest.mark.parametrize("adapter", ["official", "standalone"])
def test_a_warning_turned_into_an_error_leaves_the_server_untouched(
    adapter: str, tmp_path: Path
) -> None:
    import warnings

    from baton.sinks import FileSink

    if adapter == "official":
        from baton.integrations.official import VendorConfig, install_baton
        from baton.integrations.official._compat import MCPServerClass as Server
        from baton.integrations.official._compat import get_lowlevel_server
    else:
        from fastmcp import FastMCP as Server

        from baton.integrations.standalone import VendorConfig, install_baton

        def get_lowlevel_server(mcp: Any) -> Any:
            return mcp._mcp_server

    def handlers(mcp: Any) -> list[Any]:
        low_level = get_lowlevel_server(mcp)
        table = getattr(low_level, "request_handlers", None)
        if table is not None:
            return list(table.values())
        return [low_level.get_request_handler(method).handler for method in _METHODS]

    mcp = Server("parity")
    before = handlers(mcp)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(UserWarning, match="result_capture_mode"):
            install_baton(
                mcp,
                VendorConfig(
                    vendor_id="parity",
                    vendor_display_name="Parity Vendor",
                    consent_token="ct_parity",
                    sink=FileSink(str(tmp_path / "events.jsonl")),
                    result_capture_mode="off",
                ),
            )

    assert handlers(mcp) == before
