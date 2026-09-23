"""A returned result carrying MCP's error flag must file as a FAILURE.

The standalone-``fastmcp`` twin of
``tests/integrations/official/test_iserror_reclassify.py``. SPEC §11.4.3.

⚠ **The floor cannot express the shape under test, and this file says so out
loud rather than skipping.** Probed 2026-09-22: ``fastmcp`` 2.14.7's
``ToolResult.__init__()`` has no ``is_error`` keyword at all, so on that leg
there is no flag to misread and nothing to reclassify — which is correct
behaviour, not a gap. ``test_floor_has_no_flag_to_read`` asserts that
absence positively, so the day the floor grows the field this file reddens
instead of quietly continuing to skip.

⚠ **The raise path is asserted here too, though it already worked.** It was
only ever ASSUMED to work on this adapter — ``fastmcp`` converts an exception
into an error result somewhere, and if that conversion sat INSIDE
``call_next`` the middleware would see a result rather than an exception and
this whole file would be testing the wrong seam. Measured: it sits outside.
An assertion keeps it that way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

try:  # fastmcp 3.x / 4.x
    from fastmcp.tools import ToolResult
except ImportError:  # 2.14.7 — the floor exports it only from the leaf module
    from fastmcp.tools.tool import ToolResult

import mcp.types as mcp_types

from baton.integrations.standalone.middleware import BatonMiddleware
from baton.scrub import identity_scrub
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots

REASON = "you do not have sufficient access to delete this Project"

FLAG_IS_EXPRESSIBLE = "is_error" in getattr(ToolResult, "model_fields", {}) or hasattr(
    ToolResult, "is_error"
)


def _flag_of(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    for name in ("is_error", "isError"):
        if name in result:
            return result[name]
    return None


async def _drive(events_path: Path) -> list[dict[str, Any]]:
    mcp = FastMCP("iserror")

    @mcp.tool
    def soft_fail(x: str) -> Any:
        """Returns an error result WITHOUT raising — the shape under test."""
        return ToolResult(content=[mcp_types.TextContent(type="text", text=REASON)], is_error=True)

    @mcp.tool
    def hard_fail(x: str) -> dict[str, Any]:
        raise ValueError("boom")

    sink = FileSink(str(events_path))
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ie",
            vendor_id="ie",
            consent_token="ct_ie",
            sink=sink,
            scrubber=identity_scrub,
        )
    )
    names = ("soft_fail", "hard_fail") if FLAG_IS_EXPRESSIBLE else ("hard_fail",)
    async with Client(mcp) as client:
        for name in names:
            # fastmcp raises client-side on a failed call, by either shape.
            # The event is what this file is about, not the client's return.
            try:
                await client.call_tool(name, {"x": name})
            except Exception:
                pass
    await sink.aclose()

    with open(events_path) as f:
        return without_surface_snapshots(json.loads(line) for line in f if line.strip())


def _terminal(events: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    matching = [
        ev
        for ev in events
        if ev["event_type"] in ("tool_call_end", "tool_call_error")
        and (ev.get("payload") or {}).get("tool_name") == tool
    ]
    assert len(matching) == 1, f"{tool}: expected 1 terminal event, got {len(matching)}"
    return matching[0]


def _error_payload(events: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    """The payload of ``tool``'s terminal event, ASSERTING it is the error one.

    ⚠ Every body assertion goes through here rather than through ``_terminal``,
    and that is not tidiness. On fastmcp 3.x/4.x the pre-fix ``tool_call_end``
    ALREADY carries ``is_error`` inside ``result`` — that is the spelling gap
    this work exists to close — so a body assertion taken off whichever
    terminal event happened to be emitted passes before the fix, for the wrong
    reason. It was observed passing that way. The same trap sits on the
    ``mcp`` 1.x legs of the official twin.
    """
    event = _terminal(events, tool)
    assert event["event_type"] == "tool_call_error", (
        f"{tool}: filed as {event['event_type']} — a returned error flag is a FAILURE"
    )
    return event["payload"]


@pytest.fixture
async def events(tmp_path: Path) -> list[dict[str, Any]]:
    return await _drive(tmp_path / "events.jsonl")


def test_floor_has_no_flag_to_read() -> None:
    """Pins the version split itself. On 2.14.7 the field does not exist, so
    the adapter has nothing to classify on and correctly emits an end event;
    on 3.x/4.x it does. Asserted rather than assumed, because the whole
    reclassification rests on which side of this line a version falls."""
    import importlib.metadata as md

    major = int(md.version("fastmcp").split(".")[0])
    assert FLAG_IS_EXPRESSIBLE is (major >= 3), (
        f"fastmcp {md.version('fastmcp')}: expressibility of `is_error` moved — "
        "SPEC §11.4.3's version table needs re-measuring"
    )


@pytest.mark.skipif(not FLAG_IS_EXPRESSIBLE, reason="fastmcp floor has no is_error field")
async def test_returned_error_flag_files_as_error(events: list[dict[str, Any]]) -> None:
    assert _terminal(events, "soft_fail")["event_type"] == "tool_call_error"


@pytest.mark.skipif(not FLAG_IS_EXPRESSIBLE, reason="fastmcp floor has no is_error field")
async def test_returned_error_keeps_the_body(events: list[dict[str, Any]]) -> None:
    payload = _error_payload(events, "soft_fail")
    assert payload["result"] is not None, "the envelope was dropped on reclassification"
    assert _flag_of(payload["result"]) is True


@pytest.mark.skipif(not FLAG_IS_EXPRESSIBLE, reason="fastmcp floor has no is_error field")
async def test_returned_error_body_carries_the_reason(events: list[dict[str, Any]]) -> None:
    payload = _error_payload(events, "soft_fail")
    assert REASON in payload["error_body"]
    assert "object at 0x" not in payload["error_body"]


@pytest.mark.skipif(not FLAG_IS_EXPRESSIBLE, reason="fastmcp floor has no is_error field")
async def test_returned_error_type_is_tool_error(events: list[dict[str, Any]]) -> None:
    assert _error_payload(events, "soft_fail")["error_type"] == "tool_error"


async def test_raise_shape_is_unchanged(events: list[dict[str, Any]]) -> None:
    """Runs on EVERY leg, floor included — see the module docstring. It is the
    only coverage the floor has, and it is the assertion that proves this
    adapter sits outside fastmcp's exception-to-flag conversion."""
    payload = _terminal(events, "hard_fail")["payload"]
    assert payload["error_type"] == "ToolError"
    assert "boom" in payload["error_body"]
    assert payload.get("result") is None
