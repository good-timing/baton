"""The ``Baton-Candidate`` header over a real HTTP server (SPEC §2.6): only a
network request carries a header, so in-memory transports cannot show it."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from baton.integrations import standalone
from baton.sinks import FileSink
from tests._candidate_helpers import CHANGE, Collector, candidate_settings, document
from tests._event_helpers import read_events

BOOT_TIMEOUT_S = 15.0


@contextlib.contextmanager
def _running(mcp: Any) -> Iterator[str]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    loop = asyncio.new_event_loop()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        with contextlib.suppress(BaseException):
            loop.run_until_complete(
                mcp.run_async(transport="http", host="127.0.0.1", port=port, show_banner=False)
            )

    threading.Thread(target=serve, daemon=True).start()
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while True:
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
            break
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        for task in asyncio.all_tasks(loop):
            loop.call_soon_threadsafe(task.cancel)


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> tuple[Any, Path]:
    from baton.integrations import _shared

    settings = candidate_settings(Collector(changes))
    monkeypatch.setattr(_shared, "build_capture_settings", lambda *_: settings)
    mcp: Any = FastMCP("candidate-http")

    @mcp.tool
    def lookup(name: str) -> str:
        """Vendor description."""
        return name

    @mcp.tool
    def drop(name: str) -> str:
        return name

    events = tmp_path / "e.jsonl"
    standalone.install_baton(
        mcp,
        standalone.VendorConfig(
            vendor_id="srv",
            vendor_display_name="Server",
            consent_token="ct",
            tenant_id="ten_1",
            sink=FileSink(str(events)),
        ),
    )
    return mcp, events


async def _listed_until(url: str, headers: dict[str, str], text: str) -> dict[str, Any]:
    """The runner's warm-up: list tools until ``lookup`` reads ``text``."""
    deadline = time.monotonic() + 5
    while True:
        async with Client(StreamableHttpTransport(url, headers=headers)) as client:
            tools = {t.name: t for t in await client.list_tools()}
            if tools["lookup"].description == text:
                result = await client.call_tool(
                    "lookup", {"name": "a", "user_goal": "1: find a"}, raise_on_error=False
                )
                dropped = await client.call_tool(
                    "drop", {"name": "a", "user_goal": "1: drop a"}, raise_on_error=False
                )
                return {"tools": tools, "result": result, "dropped": dropped}
        assert time.monotonic() < deadline, f"never listed {text!r}"
        await asyncio.sleep(0.05)


def _hides_drop(description: str, **extra: Any) -> dict[str, Any]:
    hides = document(description, **extra)
    hides["overlays"]["ov_1"]["actions"].append({"target": {"tool": "drop"}, "remove": True})
    return hides


async def test_a_test_key_with_the_header_gets_the_change_and_tagged_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, events = _install(
        tmp_path, monkeypatch, {CHANGE: _hides_drop("The change's text.", candidate=CHANGE)}
    )
    with _running(mcp) as url:
        seen = await _listed_until(url, {"Baton-Candidate": CHANGE}, "The change's text.")
        plain = await _listed_until(url, {}, "Today's text.")
    assert "drop" not in seen["tools"]
    assert seen["dropped"].is_error
    assert not plain["dropped"].is_error
    tagged = {
        (e["event_type"], e["payload"]["tool_name"], e["capture"].get("candidate"))
        for e in read_events(events)
        if e["event_type"].startswith("tool_call_") and e["event_type"] != "tool_call_start"
    }
    assert tagged == {
        ("tool_call_end", "lookup", CHANGE),
        ("tool_call_error", "drop", CHANGE),
        ("tool_call_end", "lookup", None),
        ("tool_call_end", "drop", None),
    }


async def test_a_prod_key_with_the_header_gets_the_approved_overlays_tool_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, events = _install(tmp_path, monkeypatch, {CHANGE: 403})
    with _running(mcp) as url:
        await _listed_until(url, {"Baton-Candidate": CHANGE}, "Today's text.")
        seen = await _listed_until(url, {"Baton-Candidate": CHANGE}, "Today's text.")
    assert set(seen["tools"]) >= {"lookup", "drop"}
    events_seen = [e for e in read_events(events) if e["event_type"] == "tool_call_end"]
    assert events_seen
    assert all("candidate" not in (e.get("capture") or {}) for e in events_seen)
