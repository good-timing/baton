"""A returned result carrying MCP's error flag must file as a FAILURE.

SPEC §11.4.3. MCP files a failed ``tools/call`` as a **200** whose body sets
the error flag; a JSON-RPC error means a protocol fault. An adapter that
classifies on exceptions alone therefore files real failures as successes —
which is what this adapter did, and what §6.1's own "on exception" wording
told it to do.

⚠ **Driven through ``connected_session``, not ``mcp.call_tool``.** The shape
this adapter receives depends on the ``convert_result`` the lowlevel server
actually passes, and a test that calls ``Tool.run`` with a guessed value
measures the guess. Probed 2026-09-22: it is ``True`` on every supported
version.

⚠ **The flag's spelling is version-dependent** — official ``mcp`` 1.20 to 1.27.x
publish ``isError``, 2.x publishes ``is_error`` (the ``mcp_types`` rewrite).
``_error_result`` below builds the result by whichever name the installed
version accepts, so this file is one test across the whole ``mcp-matrix``
band rather than a 1.x test that silently vacuously-passes on 2.x.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
import pytest

from baton.integrations.official import VendorConfig, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.sinks import FileSink
from tests._event_helpers import without_surface_snapshots
from tests._mcp_session import connected_session

REASON = "you do not have sufficient access to delete this Project"


def _error_result(text: str) -> Any:
    """A ``CallToolResult`` with the error flag set, on either mcp major.

    Tried snake-first, matching the production detection order and for the
    same reason: it is the spelling the current generation uses.
    """
    content = [mcp_types.TextContent(type="text", text=text)]
    try:
        return mcp_types.CallToolResult(content=content, is_error=True)
    except Exception:
        return mcp_types.CallToolResult(content=content, isError=True)


def _flag_of(result: Any) -> Any:
    """The error flag off a dumped result body, under either spelling."""
    if not isinstance(result, dict):
        return None
    for name in ("is_error", "isError"):
        if name in result:
            return result[name]
    return None


async def _drive(events_path: Path) -> list[dict[str, Any]]:
    """One call per failure shape, plus the negative control."""
    mcp = FastMCP("iserror")

    @mcp.tool()
    def soft_fail(x: str) -> Any:
        """Returns an error result WITHOUT raising — the shape under test."""
        return _error_result(REASON)

    @mcp.tool()
    def hard_fail(x: str) -> dict[str, Any]:
        """Raises — the shape that already worked, and must keep working."""
        raise ValueError("boom")

    @mcp.tool()
    def looks_like_an_error(x: str) -> dict[str, Any]:
        """The NEGATIVE CONTROL. A vendor's own dict that merely SPELLS an
        error flag is not one: neither library marks it, and treating it as a
        failure would invent one out of the vendor's domain data."""
        return {"isError": True, "content": [{"type": "text", "text": "not really"}]}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="ie",
            vendor_display_name="IsError Vendor",
            consent_token="ct_ie",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        async with connected_session(mcp) as client:
            for name in ("soft_fail", "hard_fail", "looks_like_an_error"):
                await client.call_tool(name, {"x": name})
    finally:
        await handle.aclose()

    with open(events_path) as f:
        return without_surface_snapshots(json.loads(line) for line in f if line.strip())


def _terminal(events: list[dict[str, Any]], tool: str) -> dict[str, Any]:
    """The end-or-error event for one tool. Exactly one must exist — a shape
    that emitted BOTH would satisfy a `next(...)` scan while double-counting
    the call downstream."""
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
    and that is not tidiness. On ``mcp`` 1.x the pre-fix ``tool_call_end``
    ALREADY carries ``isError`` inside ``result`` — by accident, because a
    returned ``CallToolResult`` is not the 2-tuple ``_result_to_jsonable``
    unwraps, so it falls through to a full ``model_dump``. So a body assertion
    taken off whichever terminal event happened to be emitted would pass on
    the 1.20/1.25/1.27 matrix legs before the fix, for the wrong reason, and
    redden only on 2.x. The fastmcp twin was observed doing exactly that.
    """
    event = _terminal(events, tool)
    assert event["event_type"] == "tool_call_error", (
        f"{tool}: filed as {event['event_type']} — a returned error flag is a FAILURE"
    )
    return event["payload"]


@pytest.fixture
async def events(tmp_path: Path) -> list[dict[str, Any]]:
    return await _drive(tmp_path / "events.jsonl")


async def test_returned_error_flag_files_as_error(events: list[dict[str, Any]]) -> None:
    """The whole point: a 200 carrying the flag is a failure, not a success."""
    assert _terminal(events, "soft_fail")["event_type"] == "tool_call_error"


async def test_returned_error_keeps_the_body(events: list[dict[str, Any]]) -> None:
    """⚠ The reason ``result`` was added to the payload at all. Reclassifying
    without it would move a structured body into a flat truncated string, so
    the fix would cost more than it bought."""
    payload = _error_payload(events, "soft_fail")
    assert payload["result"] is not None, "the envelope was dropped on reclassification"
    assert _flag_of(payload["result"]) is True, (
        "result must carry the ENVELOPE, not the unwrapped developer return — "
        "the flag is what makes it readable"
    )


async def test_returned_error_body_carries_the_reason(events: list[dict[str, Any]]) -> None:
    """``error_body`` is what a human reads in the Console, so it must hold
    the vendor's own sentence rather than a repr of the result object."""
    payload = _error_payload(events, "soft_fail")
    assert REASON in payload["error_body"]
    assert "object at 0x" not in payload["error_body"]


async def test_returned_error_type_is_tool_error(events: list[dict[str, Any]]) -> None:
    """``"tool_error"`` is what ``baton-extmcp`` has emitted since 0.1.0;
    parity across sensors is the point of the change."""
    assert _error_payload(events, "soft_fail")["error_type"] == "tool_error"


def test_error_text_does_not_truncate_before_the_scrubber() -> None:
    """⚠ SCRUB happens BEFORE the cut, on both failure shapes.

    The raise path has always been ``str(scrubber(str(exc)))[:2000]``. When the
    returned path was added, its helper cut at 2000 internally and the caller
    scrubbed afterwards — so for that shape alone the truncation ran FIRST, and
    a PII value straddling the 2000th character reached the scrubber as a
    fragment no pattern matches, shipping the surviving half unredacted.

    Nothing else here could see it: every other test uses short bodies, so the
    cut never fired. This asserts the helper returns the text WHOLE and leaves
    truncation to the callers, which is the only order that is safe.
    """
    from baton.integrations._error_result import error_text

    long_reason = "x" * 2500

    class _Part:
        text = long_reason

    class _Result:
        def __init__(self) -> None:
            self.content = [_Part()]

    assert len(error_text(_Result())) == 2500


async def test_raise_shape_is_unchanged(events: list[dict[str, Any]]) -> None:
    """The shape that already worked keeps working, and keeps NOT carrying a
    result — there is no result object when a handler raises, so a populated
    one here would be fabricated."""
    payload = _terminal(events, "hard_fail")["payload"]
    assert payload["error_type"] == "ValueError"
    assert "boom" in payload["error_body"]
    assert payload.get("result") is None


async def test_a_vendor_dict_that_spells_the_flag_is_not_an_error(
    events: list[dict[str, Any]],
) -> None:
    """NEGATIVE CONTROL, paired with the positive case above so neither can
    pass vacuously. A vendor returning its own dict is returning domain data;
    neither library marks it, and nothing downstream may."""
    assert _terminal(events, "looks_like_an_error")["event_type"] == "tool_call_end"
