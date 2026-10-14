"""The official adapter's ``session_id_origin`` (SPEC §11.4), in the directory
the mcp version matrix runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.integrations.official._registry import get_tool_registry
from baton.sinks import FileSink
from tests._event_helpers import read_events
from tests._mcp_session import connected_session

from ._fake_context import _FakeContextV1, _FakeContextV2

_PER_CALL_TYPES = {"annotation", "tool_call_start", "tool_call_end", "tool_call_error"}


def _server(events_path: Path) -> tuple[Any, Any]:
    mcp = FastMCP("origin-official")

    @mcp.tool()
    def echo(msg: str) -> str:
        return msg

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="origin",
            vendor_display_name="Origin Vendor",
            consent_token="ct_origin",
            sink=FileSink(str(events_path)),
        ),
    )
    return mcp, handle


async def _run_both_tools(mcp: Any, context: Any) -> None:
    tools = get_tool_registry(mcp)
    await tools["echo"].run({"msg": "a", "user_goal": "say a"}, context=context)
    with pytest.raises(Exception, match=r"boom|simulated failure"):
        await tools["boom"].run({}, context=context)


def _per_call(events_path: Path) -> list[dict[str, Any]]:
    events = [ev for ev in read_events(events_path) if ev["event_type"] in _PER_CALL_TYPES]
    assert {ev["event_type"] for ev in events} == _PER_CALL_TYPES
    return events


@pytest.mark.parametrize("fake_context", [_FakeContextV1, _FakeContextV2], ids=["mcp1", "mcp2"])
async def test_an_id_read_from_the_header_is_observed(fake_context: Any, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    mcp, handle = _server(events_path)
    try:
        await _run_both_tools(mcp, fake_context({"mcp-session-id": "from-header"}))
    finally:
        await handle.aclose()

    assert {(ev["session_id"], ev["session_id_origin"]) for ev in _per_call(events_path)} == {
        ("from-header", "observed")
    }


@pytest.mark.parametrize(
    "context",
    [_FakeContextV1({"host": "example.invalid"}), _FakeContextV2({"host": "example.invalid"})],
    ids=["mcp1", "mcp2"],
)
async def test_an_http_call_with_no_session_header_is_minted(context: Any, tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    mcp, handle = _server(events_path)
    try:
        await _run_both_tools(mcp, context)
    finally:
        await handle.aclose()

    events = _per_call(events_path)
    assert {ev["transport_observed"] for ev in events} == {"http"}
    assert {(ev["session_id"], ev["session_id_origin"]) for ev in events} == {
        (handle.session_id, "minted")
    }


@pytest.mark.parametrize("fake_context", [_FakeContextV1, _FakeContextV2], ids=["mcp1", "mcp2"])
async def test_the_annotation_tool_lands_in_the_session_of_the_calls(
    fake_context: Any, tmp_path: Path
) -> None:
    events_path = tmp_path / "events.jsonl"
    mcp, handle = _server(events_path)
    context = fake_context({"mcp-session-id": "from-header"})
    try:
        await get_tool_registry(mcp)["echo"].run({"msg": "a"}, context=context)
        await get_tool_registry(mcp)[handle.annotation_tool_name].run(
            {"user_goal": "say a", "what_happened": "the call came back unusable"},
            context=context,
        )
    finally:
        await handle.aclose()

    events = [
        ev
        for ev in read_events(events_path)
        if ev["event_type"] in {"tool_call_start", "annotation"}
    ]
    assert {ev["event_type"] for ev in events} == {"tool_call_start", "annotation"}
    assert {(ev["session_id"], ev["session_id_origin"]) for ev in events} == {
        ("from-header", "observed")
    }


async def test_every_event_off_the_wire_says_minted(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    mcp, handle = _server(events_path)
    try:
        async with connected_session(mcp) as session:
            await session.list_tools()
            await session.call_tool("echo", {"msg": "a"})
            await session.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "say a", "what_happened": "the call came back unusable"},
            )
            await session.list_resources()
            await session.list_prompts()
    finally:
        await handle.aclose()

    events = read_events(events_path)
    seen = {ev["event_type"] for ev in events}
    expected = {
        "surface_snapshot",
        "tool_list_start",
        "tool_call_start",
        "annotation",
        "resource_list_start",
        "prompt_list_end",
    }
    assert expected <= seen, f"the run never sent {sorted(expected - seen)}"
    assert {(ev["session_id"], ev.get("session_id_origin")) for ev in events} == {
        (handle.session_id, "minted")
    }
