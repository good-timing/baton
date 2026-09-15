"""Coordinate rounding on the STANDALONE adapter, at both of its ``runtime_meta``
sites: the middleware's tool-call events and the annotation tool. Plus the scope
half: a vendor tool's own ``latitude`` is captured at full precision.

The official adapter's twin, with the spy that pins detection reading the raw
meta, is in ``tests/integrations/official/test_agent_runtime.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import Client, FastMCP

from baton.integrations.standalone import VendorConfig, install_baton
from baton.scrub import identity_scrub
from baton.sinks import FileSink
from tests._chatgpt_meta import CHATGPT_MAC_META

# Minus the 2026-07-28 version key, for the official twin's reason: an mcp 2.x
# server refuses an enveloped request on a handshake-era connection, which is
# what the older fastmcp legs open. fastmcp 4's client writes its own.
_CHATGPT_META = {
    k: v for k, v in CHATGPT_MAC_META.items() if k != "io.modelcontextprotocol/protocolVersion"
}
_PRECISE_LATITUDE = "37.79535123456789"
_ROUNDED_LOCATION = {
    "city": "San Carlos",
    "region": "California",
    "country": "US",
    "latitude": "37.8",
    "timezone": "America/Los_Angeles",
    "longitude": "-122.4",
}
_META_SITES = ("tool_call_start", "tool_call_end", "annotation")


async def _drive(events_path: Path, **config: Any) -> dict[str, dict[str, Any]]:
    """A vendor tool that takes and returns a ``latitude``, then the annotation
    tool, both carrying ChatGPT-shaped ``_meta``. Returns the events by type."""
    mcp: Any = FastMCP("coords-standalone")

    @mcp.tool()
    def forecast(latitude: str) -> dict[str, Any]:
        return {"latitude": latitude}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="coords",
            vendor_display_name="Coords Vendor",
            consent_token="ct_coords",
            sink=FileSink(str(events_path)),
            **config,
        ),
    )
    try:
        async with Client(mcp) as client:
            await client.call_tool("forecast", {"latitude": _PRECISE_LATITUDE}, meta=_CHATGPT_META)
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "check the forecast", "signal_type": "failure"},
                meta=_CHATGPT_META,
            )
    finally:
        await handle.aclose()
    events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
    by_type = {ev["event_type"]: ev for ev in events}
    assert set(_META_SITES) <= set(by_type), sorted(by_type)
    return by_type


async def test_runtime_meta_carries_rounded_coordinates_at_both_sites(tmp_path: Path) -> None:
    by_type = await _drive(tmp_path / "e.jsonl")
    for event_type in _META_SITES:
        location = by_type[event_type]["runtime_meta"]["openai/userLocation"]
        assert location == _ROUNDED_LOCATION, event_type


async def test_a_tools_own_coordinates_are_captured_at_full_precision(tmp_path: Path) -> None:
    """The rounding is for what the CLIENT's ``_meta`` reveals about the person,
    not the vendor's data."""
    by_type = await _drive(tmp_path / "e.jsonl")
    assert by_type["tool_call_start"]["payload"]["params"] == {"latitude": _PRECISE_LATITUDE}
    assert _PRECISE_LATITUDE in json.dumps(by_type["tool_call_end"]["payload"]["result"])


async def test_a_vendor_scrubber_does_not_opt_out_of_the_rounding(tmp_path: Path) -> None:
    """The rounding runs before ``VendorConfig(scrubber=...)``, so even the
    explicit opt-out, ``identity_scrub``, still gets it."""
    by_type = await _drive(tmp_path / "e.jsonl", scrubber=identity_scrub)
    for event_type in _META_SITES:
        location = by_type[event_type]["runtime_meta"]["openai/userLocation"]
        assert location == _ROUNDED_LOCATION, event_type
