"""What the OFFICIAL mcp SDK adapter reports about the calling client, across
the mcp matrix.

Here rather than in ``tests/functional/``: ``mcp-matrix`` runs only
``tests/integrations/official/``, with no ``fastmcp`` installed, and the
handshake attribute this reads was renamed between mcp 1.x and 2.x. The
functional parity test asserts the two adapters agree; this one asserts the
official adapter is right on every supported mcp version.

No ``from __future__ import annotations``, like ``annotation.py``: the bare
``Context`` annotation must stay a live class for ``Tool.from_function``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from baton.integrations.client_observed import CLIENT_INFO_META_KEY
from baton.integrations.official import VendorConfig, _tool_wrap, annotation, install_baton
from baton.integrations.official._compat import MCPServerClass as FastMCP
from baton.scrub import identity_scrub
from baton.sinks import FileSink
from tests._chatgpt_meta import CHATGPT_MAC_META
from tests._event_helpers import CALLER_EVENT_TYPES, read_events, without_surface_snapshots
from tests._mcp_session import DECLARED_VERSION, connected_session
from tests.integrations.official.test_iserror_reclassify import _error_result


async def _drive_all(
    events_path: Path,
    meta: dict[str, Any] | None,
    *,
    declared: str | None = None,
    **config: Any,
) -> list[dict[str, Any]]:
    """A working call, a raising call, a call returning an error result and an
    annotation, each of which has its own emit site, over a real in-memory
    session, which is the only place ``_meta`` and the handshake exist."""
    mcp = FastMCP("observed-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def broken() -> str:
        raise ValueError("vendor bug")

    @mcp.tool()
    def refused() -> Any:
        return _error_result("not allowed")

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
        async with connected_session(mcp, declared_name=declared) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool("broken", {}, meta=meta)
            await client.call_tool("refused", {}, meta=meta)
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()
    events = read_events(events_path)
    types = {ev["event_type"] for ev in events}
    assert CALLER_EVENT_TYPES <= types, f"the driver captured only {sorted(types)}"
    errors = [ev for ev in events if ev["event_type"] == "tool_call_error"]
    assert len(errors) == 2, "the raising call and the refused one each end in an error"
    return events


async def _drive(
    events_path: Path, meta: dict[str, Any] | None, **kwargs: Any
) -> list[dict[str, Any]]:
    return without_surface_snapshots(await _drive_all(events_path, meta, **kwargs))


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({"claudecode/toolUseId": "tu_1"}, id="a-carried-claudecode-key"),
        pytest.param({"progressToken": 7}, id="no-per-call-signal"),
        pytest.param(None, id="no-meta-at-all"),
        pytest.param(
            {"io.baton/agent_runtime": "acme", "baton": {"agent_runtime": "acme"}},
            id="a-caller-asserting-its-own-runtime",
        ),
    ],
)
async def test_nothing_a_caller_sends_names_the_client(
    tmp_path: Path, meta: dict[str, Any] | None
) -> None:
    events = await _drive(tmp_path / "e.jsonl", meta, declared="claude-ai")
    expected = {"info": {"name": "claude-ai", "version": DECLARED_VERSION}}
    for ev in events:
        assert ev["agent_runtime"] == "unknown", ev["event_type"]
        assert ev["client_observed"] == expected, ev["event_type"]


def _where(ev: dict[str, Any]) -> str:
    return f"{ev['event_type']} {ev['payload'].get('tool_name', '')}"


async def test_every_caller_event_carries_the_declared_client(tmp_path: Path) -> None:
    """Every event, so a build where one emit site was missed names it. The
    annotation agreeing with the call it describes is the point."""
    events = await _drive(tmp_path / "e.jsonl", None, declared="claude-ai")
    expected = {"info": {"name": "claude-ai", "version": DECLARED_VERSION}}
    for ev in events:
        assert ev["client_observed"] == expected, _where(ev)


async def test_every_caller_event_carries_the_registered_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-memory session has no HTTP request, so the read is stood in for.
    The annotation tool holds its own reference to it, hence two patches."""
    sent = {"User-Agent": "agent-s3cret/1.0", "Authorization": "Bearer s3cret"}
    reads = {"tool": 0, "annotation": 0}

    def reading(path: str) -> Any:
        def read(_ctx: Any) -> dict[str, str]:
            reads[path] += 1
            return sent

        return read

    monkeypatch.setattr(_tool_wrap, "_extract_headers_from_context", reading("tool"))
    monkeypatch.setattr(annotation, "_extract_headers_from_context", reading("annotation"))

    events = await _drive(
        tmp_path / "e.jsonl",
        None,
        scrubber=lambda v: v.replace("s3cret", "[R]") if isinstance(v, str) else v,
        resolve_principal=lambda _ctx: None,
    )

    for ev in events:
        assert ev["client_observed"]["headers"] == {"user-agent": "agent-[R]/1.0"}, _where(ev)
    assert reads == {"tool": 3, "annotation": 1}, "one read per call, on each path"


async def test_the_surface_snapshot_carries_no_client(tmp_path: Path) -> None:
    events = await _drive_all(tmp_path / "e.jsonl", None, declared="claude-ai")
    snapshots = [ev for ev in events if ev["event_type"] == "surface_snapshot"]
    assert snapshots, "no surface_snapshot captured"
    assert {ev.get("client_observed") for ev in snapshots} == {None}
    assert {ev["agent_runtime"] for ev in snapshots} == {"unknown"}
    # The negative control: the same run did observe a client.
    callers = without_surface_snapshots(events)
    assert all(ev["client_observed"] for ev in callers)


async def test_a_claudecode_key_does_not_name_the_client(tmp_path: Path) -> None:
    """The consumer recognises Claude Code from the key in ``runtime_meta``; the
    SDK neither names it nor writes it into ``info``."""
    events = await _drive(tmp_path / "e.jsonl", {"claudecode/toolUseId": "tu_1"})
    for ev in events:
        assert ev["agent_runtime"] == "unknown", ev["event_type"]
        assert "claude" not in json.dumps(ev["client_observed"]), ev["event_type"]
    calls = [ev for ev in events if ev["event_type"].startswith("tool_call")]
    assert {ev["runtime_meta"]["claudecode/toolUseId"] for ev in calls} == {"tu_1"}


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
        # attribute directly would pass on 1.x and AttributeError on 2.0.
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
    """The annotation tool reads ``_meta`` for the client's declaration but
    does not emit it as ``runtime_meta`` while its ``session_id`` is the
    fallback.

    ``_meta`` can carry ``io.baton/session_id`` and ``traceparent``. Emitting
    those beside an envelope ``session_id`` that ignored them would let two
    consumers file the same event under two sessions. Delete this test only
    together with making the tool resolve a real session id.
    """
    events = await _drive(
        tmp_path / "e.jsonl",
        {"io.baton/session_id": "app-handle", CLIENT_INFO_META_KEY: {"name": "zed"}},
    )
    annotation = next(ev for ev in events if ev["event_type"] == "annotation")
    assert annotation["client_observed"] == {"info": {"name": "zed"}}, (
        "the meta is still read for the declaration; only its emission is withheld"
    )
    assert not annotation.get("runtime_meta"), (
        f"annotation carries runtime_meta {annotation.get('runtime_meta')!r} while its "
        f"session_id is {annotation['session_id']!r}, which ignored the meta's own handle"
    )


# ChatGPT-shaped meta minus the one key that makes it a 2026-07-28 envelope:
# mcp 2.x refuses an enveloped request on a handshake-era connection, which is
# what ``connected_session`` opens.
_CHATGPT_META = {
    k: v for k, v in CHATGPT_MAC_META.items() if k != "io.modelcontextprotocol/protocolVersion"
}
_PRECISE_LATITUDE = "37.79535123456789"


async def test_the_request_declaration_is_observed_beside_rounded_coordinates(
    tmp_path: Path,
) -> None:
    """ChatGPT-shaped ``_meta`` over a real session: the declaration it carries
    wins over the handshake, and ``runtime_meta`` has rounded coordinates."""
    events = await _drive(tmp_path / "e.jsonl", _CHATGPT_META, declared="some-gateway")

    for ev in events:
        assert ev["client_observed"] == {"info": {"name": "openai-mcp", "version": "1.0.0"}}, ev[
            "event_type"
        ]
        assert ev["agent_runtime"] == "unknown", ev["event_type"]

    calls = [ev for ev in events if ev["event_type"] in {"tool_call_start", "tool_call_end"}]
    for ev in calls:
        assert ev["runtime_meta"]["openai/userLocation"] == {
            "city": "San Carlos",
            "region": "California",
            "country": "US",
            "latitude": "37.8",
            "timezone": "America/Los_Angeles",
            "longitude": "-122.4",
        }, ev["event_type"]


async def test_a_scrubber_that_removes_meta_keys_cannot_hide_the_declaration(
    tmp_path: Path,
) -> None:
    """The declaration is read from the raw ``_meta``; the vendor's scrubber is
    applied to its values, not used to decide whether it exists."""

    def drops_the_declaration_key(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k != CLIENT_INFO_META_KEY}
        return value

    events = await _drive(
        tmp_path / "e.jsonl",
        {CLIENT_INFO_META_KEY: {"name": "zed", "version": "0.9"}},
        scrubber=drops_the_declaration_key,
    )
    calls = [ev for ev in events if ev["event_type"].startswith("tool_call")]
    for ev in calls:
        assert CLIENT_INFO_META_KEY not in (ev["runtime_meta"] or {}), ev["event_type"]
    for ev in events:
        assert ev["client_observed"] == {"info": {"name": "zed", "version": "0.9"}}, ev[
            "event_type"
        ]


async def test_the_vendor_scrubber_is_applied_to_the_declaration(tmp_path: Path) -> None:
    def redacts_the_marker(value: Any) -> Any:
        return value.replace("s3cret", "[R]") if isinstance(value, str) else value

    events = await _drive(
        tmp_path / "e.jsonl", None, declared="client-s3cret", scrubber=redacts_the_marker
    )
    assert {ev["client_observed"]["info"]["name"] for ev in events} == {"client-[R]"}


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
    by_type = {ev["event_type"]: ev for ev in read_events(events_path)}
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
