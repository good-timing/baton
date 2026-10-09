"""Both MCP adapters send the tool list events (SPEC §11.4.5), and send the
same ones for the same client.

Each adapter's suite runs separately in CI, so only a test that drives both
with one input can see one of them drift.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from baton import Principal
from tests._event_helpers import TOOL_LIST_EVENT_TYPES, principal_of, read_events
from tests._mcp_session import DECLARED_VERSION

pytestmark = pytest.mark.functional

DECLARED_NAME = "listing-client"

Hook = Callable[[Any], Principal | None]
Driver = Callable[..., Awaitable[int | None]]
Action = Callable[[Any], Awaitable[None]]


SENT_META = {"progressToken": "p1", "io.example/trace": "abc"}


def _config(
    config_class: Any, events_path: Path, hook: Hook | None, mode: str, scrubber: Any = None
) -> Any:
    from baton.sinks import FileSink

    return config_class(
        **({} if scrubber is None else {"scrubber": scrubber}),
        vendor_id="parity",
        vendor_display_name="Parity Vendor",
        consent_token="ct_parity",
        sink=FileSink(str(events_path)),
        resolve_principal=hook,
        intent_param_mode=mode,
        proactive_mode="on" if mode == "off" else "off",
    )


async def _call_a_tool_twice_and_an_unknown_one(client: Any) -> None:
    for name in ("lookup", "lookup", "no-such-tool"):
        try:
            await client.call_tool(name, {"name": "alice"})
        except Exception:
            pass


async def _list_and_call_with_meta(client: Any) -> None:
    import mcp.types as mcp_types

    session = getattr(client, "session", client)
    params = mcp_types.PaginatedRequestParams.model_validate({"_meta": SENT_META})
    await session.list_tools(params=params)
    await session.call_tool("lookup", {"name": "alice"}, meta=SENT_META)


async def _list_official(
    events_path: Path,
    *,
    hook: Hook | None = None,
    mode: str = "required",
    break_listing: bool = False,
    scrubber: Any = None,
    installs: int = 1,
    instead: Action | None = None,
) -> int | None:
    """Connect a client through the official ``mcp`` adapter and list once, or
    run ``instead``. Returns how many tools the listing returned, or ``None``
    when it failed or did not run."""
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass
    from baton.integrations.official._registry import get_tool_manager
    from tests._mcp_session import connected_session

    mcp = MCPServerClass("parity-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handles = [
        install_baton(mcp, _config(VendorConfig, events_path, hook, mode, scrubber))
        for _ in range(installs)
    ]
    if break_listing:

        def refuse() -> Any:
            raise RuntimeError("the registry is down")

        get_tool_manager(mcp).list_tools = refuse
    try:
        async with connected_session(mcp, declared_name=DECLARED_NAME) as client:
            if instead is not None:
                await instead(client)
                return None
            try:
                return len((await client.list_tools()).tools)
            except Exception:
                return None
    finally:
        for handle in handles:
            await handle.aclose()


async def _list_standalone(
    events_path: Path,
    *,
    hook: Hook | None = None,
    mode: str = "required",
    break_listing: bool = False,
    scrubber: Any = None,
    installs: int = 1,
    instead: Action | None = None,
) -> int | None:
    """The same through the standalone ``fastmcp`` adapter."""
    import mcp.types as mcp_types
    from fastmcp import Client, FastMCP
    from fastmcp.server.middleware import Middleware

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp: Any = FastMCP("parity-standalone")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handles = [
        install_baton(mcp, _config(VendorConfig, events_path, hook, mode, scrubber))
        for _ in range(installs)
    ]
    if break_listing:

        class Refuse(Middleware):
            async def on_list_tools(self, context: Any, call_next: Any) -> Any:
                raise RuntimeError("the registry is down")

        mcp.add_middleware(Refuse())
    try:
        client_info = mcp_types.Implementation(name=DECLARED_NAME, version=DECLARED_VERSION)
        async with Client(mcp, client_info=client_info) as client:
            if instead is not None:
                await instead(client)
                return None
            try:
                return len(await client.list_tools())
            except Exception:
                return None
    finally:
        for handle in handles:
            await handle.aclose()


ADAPTERS = [
    pytest.param(_list_official, id="official"),
    pytest.param(_list_standalone, id="standalone"),
]


def _listing_events(events_path: Path) -> list[dict[str, Any]]:
    return [ev for ev in read_events(events_path) if ev["event_type"] in TOOL_LIST_EVENT_TYPES]


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_listing_sends_a_start_and_an_end(list_once: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    received = await list_once(events_path)

    start, end = _listing_events(events_path)
    assert (start["event_type"], end["event_type"]) == ("tool_list_start", "tool_list_end")
    assert start["payload"] == {}
    assert received == 2, "the vendor's tool and Baton's own"
    assert end["payload"]["count"] == received
    assert end["payload"]["duration_ms"] >= 0
    assert set(end["payload"]) == {"count", "duration_ms"}
    assert start["session_id"] == end["session_id"]
    assert start["sequence_number"] < end["sequence_number"]
    for ev in (start, end):
        assert ev["call_id"] is None
        assert ev["client_observed"]["info"] == {
            "name": DECLARED_NAME,
            "version": DECLARED_VERSION,
        }
        assert ev["transport_observed"] == "no-http-request"
        assert principal_of(ev) is None


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_listing_carries_the_principal_asked_once(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    asked: list[tuple[str | None, dict[str, Any]]] = []

    def hook(context: Any) -> Principal:
        asked.append((context.tool_name, context.arguments))
        return Principal(principal_id="user-7")

    await list_once(events_path, hook=hook)

    start, end = _listing_events(events_path)
    assert asked == [(None, {})]
    for ev in (start, end):
        assert principal_of(ev) == {
            "id": "user-7",
            "source": "asserted",
            "form": "raw",
            "display_name": None,
        }


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_hook_that_raises_costs_the_principal_not_the_listing(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    def hook(context: Any) -> Principal:
        raise RuntimeError("the directory is down")

    received = await list_once(events_path, hook=hook)

    assert received == 2
    start, end = _listing_events(events_path)
    assert (start["event_type"], end["event_type"]) == ("tool_list_start", "tool_list_end")
    assert principal_of(start) is None
    assert principal_of(end) is None


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_listing_is_sent_with_the_intent_params_off(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    received = await list_once(events_path, mode="off")

    start, end = _listing_events(events_path)
    assert (start["event_type"], end["event_type"]) == ("tool_list_start", "tool_list_end")
    assert end["payload"]["count"] == received


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_failed_listing_sends_an_error_with_the_principal(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    received = await list_once(
        events_path, hook=lambda context: Principal(principal_id="user-7"), break_listing=True
    )

    assert received is None
    start, error = _listing_events(events_path)
    assert (start["event_type"], error["event_type"]) == ("tool_list_start", "tool_list_error")
    assert error["payload"]["error_type"] == "RuntimeError"
    assert error["payload"]["error_body"] == "the registry is down"
    assert error["payload"]["duration_ms"] >= 0
    assert principal_of(error) == principal_of(start)
    assert principal_of(error) is not None


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_only_a_request_from_the_client_is_a_listing(
    list_once: Driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mcp 1.x lists its own tools while it serves a call to a tool it has not
    cached. That is not a client looking at the server."""
    from mcp import ClientSession

    events_path = tmp_path / "events.jsonl"
    requests_sent = 0
    send_list_request = ClientSession.list_tools

    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal requests_sent
        requests_sent += 1
        return await send_list_request(self, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "list_tools", counted)

    await list_once(events_path, instead=_call_a_tool_twice_and_an_unknown_one)

    starts = [ev for ev in _listing_events(events_path) if ev["event_type"] == "tool_list_start"]
    assert len(read_events(events_path)) > len(starts), "the calls were captured"
    assert len(starts) == requests_sent


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_listing_reads_the_requests_meta_as_a_call_does(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    seen_by_hook: dict[str | None, dict[str, Any]] = {}

    def hook(context: Any) -> None:
        seen_by_hook.setdefault(context.tool_name, dict(context.meta))

    await list_once(events_path, hook=hook, instead=_list_and_call_with_meta)

    events = read_events(events_path)
    (listed,) = [ev for ev in events if ev["event_type"] == "tool_list_start"][:1]
    (called,) = [ev for ev in events if ev["event_type"] == "tool_call_start"]
    assert listed["runtime_meta"]["io.example/trace"] == "abc"
    assert listed["runtime_meta"] == called["runtime_meta"]
    assert seen_by_hook[None] == seen_by_hook["lookup"]


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_installing_twice_still_sends_one_pair(list_once: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    await list_once(events_path, installs=2)

    assert [ev["event_type"] for ev in _listing_events(events_path)] == [
        "tool_list_start",
        "tool_list_end",
    ]


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_failed_listings_message_is_scrubbed(list_once: Driver, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"

    def scrubber(value: Any) -> Any:
        return value.replace("registry", "[R]") if isinstance(value, str) else value

    await list_once(events_path, break_listing=True, scrubber=scrubber)

    (_start, error) = _listing_events(events_path)
    assert error["payload"]["error_body"] == "the [R] is down"


@pytest.mark.parametrize("list_once", ADAPTERS)
async def test_a_scrubber_that_raises_does_not_cost_the_listing(
    list_once: Driver, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"

    def scrubber(value: Any) -> Any:
        raise RuntimeError("the scrubber is broken")

    received = await list_once(events_path, scrubber=scrubber)

    assert received == 2
    assert [ev["event_type"] for ev in _listing_events(events_path)] == [
        "tool_list_start",
        "tool_list_end",
    ]


@pytest.mark.parametrize(
    ("list_once", "adapter"),
    [
        pytest.param(_list_official, "official", id="official"),
        pytest.param(_list_standalone, "standalone", id="standalone"),
    ],
)
async def test_the_token_is_not_read_when_no_hook_is_set(
    list_once: Driver, adapter: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    auth = importlib.import_module(f"baton.integrations.{adapter}._auth")
    reads = 0

    def counted() -> None:
        nonlocal reads
        reads += 1

    monkeypatch.setattr(auth, "current_access_token", counted)
    events_path = tmp_path / "events.jsonl"

    await list_once(events_path)
    assert reads == 0
    assert len(_listing_events(events_path)) == 2

    await list_once(events_path, hook=lambda context: None)
    assert reads == 1
