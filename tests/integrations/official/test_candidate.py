"""Test changes by id on the official adapter (SPEC §2.6). Here so the
``mcp-matrix`` legs run it: the variable in memory, the header over a real
HTTP server, since only a network request carries a header."""

from __future__ import annotations

import contextlib
import inspect
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp import ClientSession
from mcp.shared._httpx_utils import create_mcp_http_client

from baton.integrations import official
from baton.integrations.official._compat import MCPServerClass
from baton.sinks import FileSink
from tests._candidate_helpers import CHANGE, Collector, candidate_settings, document, wait_for
from tests._event_helpers import read_events
from tests._mcp_session import connected_session

try:
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:  # mcp before the rename
    from mcp.client.streamable_http import (  # type: ignore[attr-defined,no-redef]
        streamablehttp_client as _http_client,
    )

GOAL = "1: find a"


def _changed() -> dict[str, Any]:
    changed = document("The change's text.", candidate=CHANGE, server_overlay="ov_s")
    changed["overlays"]["ov_1"]["actions"].append({"target": {"tool": "drop"}, "remove": True})
    changed["overlays"]["ov_s"] = {
        "actions": [{"target": {"instructions": True}, "append": "Changed."}]
    }
    return changed


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], variable: str | None
) -> tuple[Any, Any, Path]:
    from baton.integrations import _shared

    settings = candidate_settings(Collector(changes), variable)
    monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings)
    mcp = MCPServerClass("candidate-official", instructions="Vendor text.")

    @mcp.tool()
    def lookup(name: str) -> str:
        """Vendor description."""
        return name

    @mcp.tool()
    def drop(name: str) -> str:
        return name

    events = tmp_path / "e.jsonl"
    handle = official.install_baton(
        mcp,
        official.VendorConfig(
            vendor_id="srv",
            vendor_display_name="Server",
            consent_token="ct",
            tenant_id="ten_1",
            sink=FileSink(str(events)),
        ),
    )
    return mcp, handle, events


def _outcomes(events: Path) -> set[tuple[str, str, str | None]]:
    return {
        (e["event_type"], e["payload"]["tool_name"], (e.get("capture") or {}).get("candidate"))
        for e in read_events(events)
        if e["event_type"] in ("tool_call_end", "tool_call_error")
    }


async def test_the_variables_change_is_served_from_the_first_initialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, handle, events = _install(tmp_path, monkeypatch, {CHANGE: _changed()}, CHANGE)
    instructions = str(mcp.instructions)
    try:
        async with connected_session(mcp) as session:
            listed = {t.name: t for t in (await session.list_tools()).tools}
            await session.call_tool("lookup", {"name": "a", "user_goal": GOAL})
            await session.call_tool("drop", {"name": "a", "user_goal": GOAL})
    finally:
        await handle.aclose()
    assert "Changed." in instructions
    assert listed["lookup"].description == "The change's text."
    assert "drop" not in listed
    assert _outcomes(events) == {
        ("tool_call_end", "lookup", CHANGE),
        ("tool_call_error", "drop", CHANGE),
    }


@contextlib.contextmanager
def _running(mcp: Any) -> Iterator[str]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    server = uvicorn.Server(
        uvicorn.Config(mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started, 15)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(15)


def _connect(url: str, headers: dict[str, str]) -> Any:
    if "headers" in inspect.signature(_http_client).parameters:
        return _http_client(url, headers=headers)
    return _http_client(url, http_client=create_mcp_http_client(headers=headers))


async def _warmed_up(url: str, headers: dict[str, str], text: str) -> dict[str, Any]:
    """The runner's warm-up: list tools until ``lookup`` reads ``text``, then call."""
    deadline = time.monotonic() + 5
    while True:
        async with _connect(url, headers) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = {t.name: t for t in (await session.list_tools()).tools}
                if listed["lookup"].description == text:
                    await session.call_tool("lookup", {"name": "a", "user_goal": GOAL})
                    await session.call_tool("drop", {"name": "a", "user_goal": GOAL})
                    return listed
        assert time.monotonic() < deadline, f"never listed {text!r}"


async def test_a_test_key_with_the_header_gets_the_change_and_tagged_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, handle, events = _install(tmp_path, monkeypatch, {CHANGE: _changed()}, None)
    with _running(mcp) as url:
        listed = await _warmed_up(url, {"Baton-Candidate": CHANGE}, "The change's text.")
        await _warmed_up(url, {}, "Today's text.")
    await handle.aclose()
    assert "drop" not in listed
    assert _outcomes(events) == {
        ("tool_call_end", "lookup", CHANGE),
        ("tool_call_error", "drop", CHANGE),
        ("tool_call_end", "lookup", None),
        ("tool_call_end", "drop", None),
    }


async def test_a_prod_key_with_the_header_gets_the_approved_overlays_tool_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, handle, events = _install(tmp_path, monkeypatch, {CHANGE: 403}, None)
    with _running(mcp) as url:
        await _warmed_up(url, {"Baton-Candidate": CHANGE}, "Today's text.")
        listed = await _warmed_up(url, {"Baton-Candidate": CHANGE}, "Today's text.")
    await handle.aclose()
    assert {"lookup", "drop"} <= set(listed)
    assert {candidate for _, _, candidate in _outcomes(events)} == {None}
