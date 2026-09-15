"""Runtime detection on the OFFICIAL mcp SDK adapter, across the mcp matrix.

Deliberately here rather than in ``tests/functional/`` even though the
cross-adapter parity test covers the same field. ``mcp-matrix`` runs
``tests/integrations/official/`` against mcp 1.20.0 / 1.25.0 / 1.27.2 / 2.0.0,
and it CANNOT be widened to the functional directory: that job force-installs
an mcp version over fastmcp's transitive pin, which only works because nothing
it runs imports the standalone library (see the job's own comment). So the
version-sensitive halves of this change need a home the matrix already runs.

What is version-sensitive here:

- ``Context`` moved with its module on mcp 2.0
  (``mcp.server.fastmcp`` → ``mcp.server.mcpserver``), so the annotation tool's
  ``ctx`` kwarg resolves through ``_compat.ContextClass``.
- ``Tool.from_function`` introspects kwarg annotations to find the Context
  parameter. It is annotated BARE (``Context``, not ``Context[Any, Any, Any]``)
  because a parameterized generic crashes that introspection's ``issubclass``
  call on older versions — and this module, like ``annotation.py`` itself,
  omits ``from __future__ import annotations`` so the annotation stays a live
  class rather than a string.

The functional parity test asserts the two adapters AGREE; this one asserts
the official adapter is right on every mcp version we support.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from baton.integrations.official import VendorConfig, _tool_wrap, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.scrub import identity_scrub
from baton.sinks import FileSink
from tests._chatgpt_meta import CHATGPT_MAC_META
from tests._event_helpers import without_surface_snapshots
from tests._mcp_session import connected_session


def _read(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


async def _drive(events_path: Path, meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One tool call + one annotation call over a real in-memory session.

    A real session, not ``mcp.call_tool(...)``: the latter carries no ``_meta``
    at all, so a test built on it would read ``"unknown"`` on a correct build
    and could never tell detection from its absence.
    """
    mcp = FastMCP("runtime-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="rt",
            vendor_display_name="Runtime Vendor",
            consent_token="ct_rt",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()
    events = without_surface_snapshots(_read(events_path))
    assert events, "no events captured — assertions below would be vacuous"
    return events


# ⚠ Every "unknown" here became `mcp` on 2026-09-09. The driver's client sets
# no `client_info`, so it declares the LIBRARY name — and the SDK now reads a
# client's declared identity off the session, which fires for every client
# rather than only for one that happens to send a `claudecode/` key. "unknown"
# survives only where there is no session to read from at all.
@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        pytest.param({"claudecode/toolUseId": "tu_1"}, "mcp", id="declaration-beats-the-key"),
        pytest.param({"io.baton/agent_runtime": "acme"}, "mcp", id="override-is-inert"),
        pytest.param({"progressToken": 7}, "mcp", id="no-per-call-signal"),
        pytest.param(None, "mcp", id="no-meta-at-all"),
    ],
)
async def test_agent_runtime_is_detected_on_every_event(
    tmp_path: Path, meta: dict[str, Any] | None, expected: str
) -> None:
    events = await _drive(tmp_path / "e.jsonl", meta)
    reported = {ev["agent_runtime"] for ev in events}
    assert reported == {expected}, (
        f"expected every event to report {expected!r}, got {reported} across "
        f"{[ev['event_type'] for ev in events]}"
    )


async def test_the_annotation_tool_agrees_with_the_tool_calls(tmp_path: Path) -> None:
    """The annotation event and the tool-call events must report the same
    runtime.

    Its own regression: the annotation tool took no Context, so it emitted the
    install-time default while the calls around it were detected — an
    annotation and the failure it describes disagreeing about who was calling.
    Asserted per event_type rather than as a set, so a build where only one of
    the two paths works names which one.
    """
    events = await _drive(tmp_path / "e.jsonl", {"claudecode/toolUseId": "tu_1"})
    by_type = {ev["event_type"]: ev["agent_runtime"] for ev in events}
    assert "annotation" in by_type, f"no annotation event captured, got {sorted(by_type)}"
    assert "tool_call_start" in by_type, f"no tool_call_start captured, got {sorted(by_type)}"
    for event_type, runtime in by_type.items():
        assert runtime == "mcp", f"{event_type} reported {runtime!r}"


async def test_the_context_kwarg_stays_out_of_the_public_tool_schema(tmp_path: Path) -> None:
    """``ctx`` must not become a parameter the agent is asked to fill.

    The SDK detects the Context kwarg and excludes it, but that detection is
    exactly what the mcp version affects, and a regression here is invisible at
    runtime: the tool keeps working while every agent sees a spurious required
    argument. ``context`` — the annotation's own payload field — must still be
    there, which also pins that the two names did not get confused.
    """
    mcp = FastMCP("schema-official")
    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="rt",
            vendor_display_name="Runtime Vendor",
            consent_token="ct_rt",
            sink=FileSink(str(tmp_path / "e.jsonl")),
        ),
    )
    try:
        tools = await mcp.list_tools()
        annotate = next(t for t in tools if t.name == handle.annotation_tool_name)
        # ``by_alias`` because mcp 2.0 renamed the field ``inputSchema`` ->
        # ``input_schema`` and kept the old name as the wire ALIAS. Reading the
        # attribute directly would pass on 1.x and AttributeError on 2.0 — the
        # same rename that produced a real wire-format bug once already.
        properties = set(
            annotate.model_dump(by_alias=True).get("inputSchema", {}).get("properties", {})
        )
        assert "ctx" not in properties, (
            f"the MCP Context kwarg leaked into the agent-facing schema: {sorted(properties)}"
        )
        assert "context" in properties, (
            f"the annotation's own `context` payload field is missing: {sorted(properties)}"
        )
        assert "user_goal" in properties
    finally:
        await handle.aclose()


async def test_the_annotation_event_carries_no_session_bearing_meta(tmp_path: Path) -> None:
    """This tool reads ``_meta`` for the runtime but must not EMIT it as
    ``runtime_meta`` while its ``session_id`` is still the fallback.

    ``_meta`` can carry ``io.baton/session_id`` and ``traceparent`` — rungs 1-2
    of SPEC §3.4. Emitting those beside an envelope ``session_id`` that ignored
    them puts a session identifier on an event whose own field disagrees with
    it, so a consumer correlating via ``runtime_meta`` per §11.5 and one reading
    the envelope file the SAME event under two sessions. The current gap only
    LOSES a join; that would manufacture a wrong one. Delete this test only
    together with making the tool climb the ladder.
    """
    events = await _drive(
        tmp_path / "e.jsonl",
        {"io.baton/session_id": "app-handle", "claudecode/toolUseId": "tu_1"},
    )
    annotation = next(ev for ev in events if ev["event_type"] == "annotation")
    assert annotation["agent_runtime"] == "mcp", (
        "the meta is still READ for the runtime — only its emission is withheld"
    )
    assert not annotation.get("runtime_meta"), (
        f"annotation carries runtime_meta {annotation.get('runtime_meta')!r} while its "
        f"session_id is {annotation['session_id']!r}, which ignored the meta's own handle"
    )


# ChatGPT-shaped meta minus the one key that makes it a 2026-07-28 envelope:
# mcp 2.x refuses an enveloped request on a handshake-era connection, which is
# what ``connected_session`` opens (ChatGPT's own requests ride a 2026-07-28
# connection). The declared ``clientInfo`` stays, so tier 1 answers from it.
_CHATGPT_META = {
    k: v for k, v in CHATGPT_MAC_META.items() if k != "io.modelcontextprotocol/protocolVersion"
}
_PRECISE_LATITUDE = "37.79535123456789"


async def test_runtime_meta_carries_rounded_coordinates_while_detection_reads_the_raw_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ChatGPT-shaped ``_meta`` over a real session. The emitted
    ``runtime_meta`` carries rounded coordinates, and ``detect_agent_runtime``
    still receives the unrounded meta.

    The spy is what makes the second half checkable. Every key the runtime
    ladder reads reaches ``runtime_meta`` unchanged, so the reported runtime
    alone cannot tell a detect before the rounding from one after it. The
    coordinates can: they are the only thing that changes here.
    """
    seen: list[tuple[dict[str, Any], str | None]] = []
    real_detect = _tool_wrap.detect_agent_runtime

    def spy(meta: Any, **kwargs: Any) -> str | None:
        runtime = real_detect(meta, **kwargs)
        seen.append((copy.deepcopy(meta), runtime))
        return runtime

    monkeypatch.setattr(_tool_wrap, "detect_agent_runtime", spy)
    events = await _drive(tmp_path / "e.jsonl", _CHATGPT_META)

    assert seen, "the tool wrapper never called detect_agent_runtime"
    for meta, _ in seen:
        location = meta["openai/userLocation"]
        assert (location["latitude"], location["longitude"]) == ("37.79535", "-122.39366"), (
            f"detection was handed scrubbed meta: {location!r}"
        )
    detected = {runtime for _, runtime in seen} - {None}

    calls = [ev for ev in events if ev["event_type"] in {"tool_call_start", "tool_call_end"}]
    assert {ev["event_type"] for ev in calls} == {"tool_call_start", "tool_call_end"}
    for ev in calls:
        assert ev["runtime_meta"]["openai/userLocation"] == {
            "city": "San Carlos",
            "region": "California",
            "country": "US",
            "latitude": "37.8",
            "timezone": "America/Los_Angeles",
            "longitude": "-122.4",
        }, ev["event_type"]
        assert ev["agent_runtime"] in detected, (
            f"{ev['event_type']} reported {ev['agent_runtime']!r}; detection returned {detected}"
        )
        assert ev["agent_runtime"] == "openai-mcp", ev["event_type"]


async def _capture_a_located_call(events_path: Path, **config: Any) -> dict[str, dict[str, Any]]:
    """One call of a vendor tool that itself takes and returns a ``latitude``,
    carrying ChatGPT-shaped ``_meta``. Returns the call's events by type."""
    mcp = FastMCP("coords-official")

    @mcp.tool()
    def forecast(latitude: str) -> dict[str, Any]:
        return {"latitude": latitude}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="rt",
            vendor_display_name="Runtime Vendor",
            consent_token="ct_rt",
            sink=FileSink(str(events_path)),
            **config,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("forecast", {"latitude": _PRECISE_LATITUDE}, meta=_CHATGPT_META)
    finally:
        await handle.aclose()
    by_type = {ev["event_type"]: ev for ev in _read(events_path)}
    assert {"tool_call_start", "tool_call_end"} <= set(by_type), sorted(by_type)
    return by_type


async def test_a_tools_own_coordinates_are_captured_at_full_precision(tmp_path: Path) -> None:
    """The rounding is for what the CLIENT's ``_meta`` reveals about the person,
    not the vendor's data: the same event rounds its ``runtime_meta`` and keeps
    the tool's ``latitude`` param and result exactly as sent."""
    by_type = await _capture_a_located_call(tmp_path / "e.jsonl")
    start, end = by_type["tool_call_start"], by_type["tool_call_end"]
    assert start["payload"]["params"] == {"latitude": _PRECISE_LATITUDE}
    assert _PRECISE_LATITUDE in json.dumps(end["payload"]["result"])
    assert start["runtime_meta"]["openai/userLocation"]["latitude"] == "37.8"


async def test_a_vendor_scrubber_does_not_opt_out_of_the_rounding(tmp_path: Path) -> None:
    """The rounding runs before ``VendorConfig(scrubber=...)``, so even the
    explicit opt-out, ``identity_scrub``, still gets it."""
    by_type = await _capture_a_located_call(tmp_path / "e.jsonl", scrubber=identity_scrub)
    for event_type in ("tool_call_start", "tool_call_end"):
        location = by_type[event_type]["runtime_meta"]["openai/userLocation"]
        assert (location["latitude"], location["longitude"]) == ("37.8", "-122.4"), event_type
