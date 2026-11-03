"""``baton.create_baton``: one Baton for a process that builds a new MCP server
per request. The servers share one sink, one sequence counter and one record of
the surfaces already sent; each keeps its own session."""

from __future__ import annotations

from typing import Any

import pytest

import baton
from tests._forced_reorder import CapturingSink
from tests._tools import list_tools, tool_schema

pytestmark = pytest.mark.functional

ADAPTERS = ["official", "standalone"]


class _CountingSink(CapturingSink):
    def __init__(self) -> None:
        super().__init__()
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


def _server(adapter: str) -> Any:
    if adapter == "official":
        from baton.integrations.official._compat import MCPServerClass

        server: Any = MCPServerClass("per-request", instructions="Vendor text.")
    else:
        from fastmcp import FastMCP

        server = FastMCP("per-request", instructions="Vendor text.")

    @server.tool()
    def lookup(name: str) -> str:
        return name

    return server


async def _call(adapter: str, server: Any) -> None:
    if adapter == "official":
        from tests._mcp_session import connected_session

        async with connected_session(server) as session:
            await session.call_tool("lookup", {"name": "a", "user_goal": "1: find a"})
    else:
        from fastmcp import Client

        async with Client(server) as client:
            await client.call_tool("lookup", {"name": "a", "user_goal": "1: find a"})


async def _tool_schemas(server: Any) -> list[dict[str, Any]]:
    return [tool_schema(t) for t in await list_tools(server)]


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_servers_built_per_request_share_one_sink_session_and_surface(adapter: str) -> None:
    sink = _CountingSink()
    shared = baton.create_baton(baton.VendorConfig(vendor_id="per-request", sink=sink))
    for _ in range(2):
        server = _server(adapter)
        shared.wrap(server)
        await _call(adapter, server)
    await shared.aclose()

    types = [e.event_type for e in sink.events]
    assert types.count("tool_call_start") == 2 and types.count("tool_call_end") == 2
    assert types.count("surface_snapshot") == 1
    # Each server is its own connection, so its own session; the counter is
    # shared and keyed by session, so no (session, number) pair repeats.
    calls = [e for e in sink.events if e.event_type != "surface_snapshot"]
    assert len({e.session_id for e in calls}) == 2
    pairs = [(e.session_id, e.sequence_number) for e in sink.events]
    assert len(pairs) == len(set(pairs))
    assert sink.closes == 1


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_with_the_off_switch_wrap_does_nothing(
    adapter: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_DISABLED", "1")
    shared = baton.create_baton()
    server = _server(adapter)
    shared.wrap(server)
    shared.wrap(object())
    assert all("user_goal" not in s.get("properties", {}) for s in await _tool_schemas(server))
    await shared.aclose()


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_with_no_dsn_wrap_checks_the_server_and_adds_nothing(
    adapter: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="baton"):
        shared = baton.create_baton()
    assert caplog.text.count("no DSN") == 1
    server = _server(adapter)
    shared.wrap(server)
    assert server.instructions == "Vendor text."
    assert all("user_goal" not in s.get("properties", {}) for s in await _tool_schemas(server))
    with pytest.raises(TypeError):
        shared.wrap(object())
    await shared.aclose()


@pytest.mark.parametrize(
    "config",
    [
        baton.VendorConfig(vendor_id="v", report_mode="bogus"),
        baton.VendorConfig(report_mode="bogus"),
        baton.VendorConfig(report_mode="on", annotation_tool_name="bad name!"),
    ],
    ids=["configured", "no dsn", "no dsn, bad annotation name"],
)
def test_a_setting_that_is_not_valid_fails_at_create_not_on_a_request(
    config: baton.VendorConfig,
) -> None:
    with pytest.raises(ValueError):
        baton.create_baton(config)


@pytest.mark.parametrize(
    "config",
    [
        baton.VendorConfig(vendor_id="v", report_mode="on", vendor_display_name="X" * 3000),
        baton.VendorConfig(report_mode="on", vendor_display_name="X" * 3000),
    ],
    ids=["configured", "no dsn"],
)
def test_instructions_over_the_cap_fail_at_create(config: baton.VendorConfig) -> None:
    with pytest.raises(ValueError):
        baton.create_baton(config)


def test_a_settings_warning_is_logged_once_not_once_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = baton.VendorConfig(vendor_id="v", annotation_tool_name="x_y", sink=_CountingSink())
    with caplog.at_level("WARNING", logger="baton"):
        shared = baton.create_baton(config)
        for _ in range(3):
            shared.wrap(_server("standalone"))
    assert caplog.text.count("has no effect with report_mode='off'") == 1


async def test_with_a_dsn_each_server_is_named_by_its_own_name(httpserver: Any) -> None:
    httpserver.expect_request("/v0/events/batch", method="POST").respond_with_data("", status=204)
    dsn = f"http://baton_pk_{'z' * 43}@{httpserver.host}:{httpserver.port}/ten_{'0' * 32}/svc"
    shared = baton.create_baton(baton.VendorConfig(dsn=dsn, report_mode="on"))
    servers = []
    for name in ("Acme Search", "Acme Files"):
        from fastmcp import FastMCP

        server = FastMCP(name)
        shared.wrap(server)
        servers.append(server)
    await shared.aclose()
    assert "Acme Search" in servers[0].instructions and "Acme Files" in servers[1].instructions


async def _tool_names(server: Any) -> set[str]:
    return {t.name for t in await list_tools(server)}


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_wrapped_server_gets_what_install_baton_gives_it(adapter: str) -> None:
    def config() -> baton.VendorConfig:
        return baton.VendorConfig(
            vendor_id="v", report_mode="on", vendor_display_name="Acme", sink=_CountingSink()
        )

    installed, wrapped = _server(adapter), _server(adapter)
    handle = baton.install_baton(installed, config())
    shared = baton.create_baton(config())
    shared.wrap(wrapped)
    assert await _tool_names(wrapped) == await _tool_names(installed)
    assert wrapped.instructions == installed.instructions
    await handle.aclose()
    await shared.aclose()


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_servers_own_session_never_enters_the_shared_counter(adapter: str) -> None:
    sink = _CountingSink()
    shared = baton.create_baton(baton.VendorConfig(vendor_id="v", sink=sink))
    for _ in range(3):
        server = _server(adapter)
        shared.wrap(server)
        await _call(adapter, server)
    assert shared._shared is not None
    assert shared._shared.counter._counters == {}
    assert len({e.session_id for e in sink.events if e.event_type == "tool_call_start"}) == 3


async def test_wrap_after_aclose_adds_nothing(caplog: pytest.LogCaptureFixture) -> None:
    shared = baton.create_baton(baton.VendorConfig(vendor_id="v", sink=_CountingSink()))
    await shared.aclose()
    server = _server("standalone")
    with caplog.at_level("WARNING", logger="baton"):
        shared.wrap(server)
        shared.wrap(_server("standalone"))
    assert caplog.text.count("after aclose") == 1
    assert all("user_goal" not in s.get("properties", {}) for s in await _tool_schemas(server))


def test_a_baton_is_made_by_create_baton() -> None:
    with pytest.raises(TypeError, match="create_baton"):
        baton.Baton(sink=_CountingSink())


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_an_install_that_fails_partway_still_serves_and_keeps_the_vendors_text(
    adapter: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib

    install = importlib.import_module(f"baton.integrations.{adapter}.install")

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("unknown layout")

    # The report tool is registered last, after Baton's instructions are written.
    monkeypatch.setattr(install, "register_annotation_tool", broken)
    config = baton.VendorConfig(
        vendor_id="v", report_mode="on", vendor_display_name="V", sink=_CountingSink()
    )
    shared = baton.create_baton(config)
    with caplog.at_level("DEBUG", logger="baton"):
        for _ in range(3):
            server = _server(adapter)
            shared.wrap(server)
            assert server.instructions == "Vendor text."
            await _call(adapter, server)
    failures = [r for r in caplog.records if "could not finish installing" in r.getMessage()]
    assert [r.levelname for r in failures] == ["ERROR", "DEBUG", "DEBUG"]
    await shared.aclose()


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_instructions_over_the_runtime_cap_warn_once(
    adapter: str, caplog: pytest.LogCaptureFixture
) -> None:
    shared = baton.create_baton(baton.VendorConfig(vendor_id="v", sink=_CountingSink()))
    with caplog.at_level("WARNING", logger="baton"):
        for _ in range(3):
            server = _server(adapter)
            server_instructions(server, "x" * 3000)
            shared.wrap(server)
    assert caplog.text.count("Claude Code cuts them") == 1


def server_instructions(server: Any, text: str) -> None:
    import importlib

    adapter = "standalone" if hasattr(server, "add_middleware") else "official"
    importlib.import_module(f"baton.integrations.{adapter}.install").set_instructions(server, text)


async def test_two_servers_number_one_client_session_in_order() -> None:
    from baton._state import SessionCounter
    from baton.integrations._shared import ServerCounter

    shared = SessionCounter()
    first, second = ServerCounter(shared, "own-1"), ServerCounter(shared, "own-2")
    assert [await first.next("client"), await second.next("client")] == [1, 2]
    assert [await first.next("own-1"), await second.next("own-2")] == [1, 1]
    assert [await first.next("client")] == [3]


def test_a_shared_warned_set_says_each_install_text_warning_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from baton.integrations._annotation_name import resolve_install_text

    config = baton.VendorConfig(vendor_id="v", annotation_tool_name="x_y")
    warned: set[str] = set()
    with caplog.at_level("WARNING", logger="baton"):
        for name in ("a", "b", "c"):
            from fastmcp import FastMCP

            resolve_install_text(FastMCP(name), config, display_name_given=False, warned=warned)
    assert caplog.text.count("has no effect with report_mode='off'") == 1
