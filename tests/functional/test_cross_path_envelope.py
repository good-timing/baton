"""Cross-path functional correctness: the same event envelope shape,
sequencing, and signal_type validity holds across all three capture paths
(mcp adapter, fastmcp adapter, library API) — asserted against ONE shared
core (``envelope_assertions.py``) so a divergence in any one adapter's
emission (e.g. a forgotten field on the error path) fails loudly instead of
only being caught by a per-adapter test that happens to check for it.

Each path runs the same logical scenario: one proactive-intent call, one
plain successful call, one call that raises. Sink: ``FileSink`` → JSONL
read-back — the collector's on-the-wire JSON view (the same
``model_dump(mode="json")`` serialization ``HttpSink`` POSTs), not
in-process ``Event`` objects, so this proves each adapter's *serialized*
output is consistent, not just its in-memory model.

Path-specific facts (``agent_runtime`` value, whether ``surface_snapshot``
fires) stay as per-path assertions rather than forced into the shared core
— see ``tests/_event_helpers.py::without_surface_snapshots``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import without_surface_snapshots
from tests.functional.envelope_assertions import (
    assert_envelope_shape,
    assert_sequence_monotonic_per_session,
    assert_signal_types_valid,
)

pytestmark = pytest.mark.functional

EXPECTED_EVENT_TYPES = {"tool_call_start", "tool_call_end", "tool_call_error", "annotation"}


def _read_events(path: str) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


async def _run_mcp_path(events_path: str) -> None:
    from baton.integrations.mcp import VendorConfig, install_baton
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("cross-path-mcp")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="cross-path",
            vendor_display_name="Cross Path Vendor",
            consent_token="ct_cross_path",
            sink=FileSink(events_path),
        ),
    )
    try:
        await mcp.call_tool(
            "cross-path_annotate",
            {
                "user_goal": "look something up",
                "signal_type": "failure",
                "suggested_improvement": "return a typed not-found result",
            },
        )
        await mcp.call_tool("lookup", {"name": "alice"})
        try:
            await mcp.call_tool("boom", {})
        except Exception:
            pass
    finally:
        await handle.aclose()


async def _run_fastmcp_path(events_path: str) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.fastmcp import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp = FastMCP("cross-path-fastmcp")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    @mcp.tool()
    def boom() -> None:
        raise ValueError("simulated failure")

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="cross-path",
            vendor_display_name="Cross Path Vendor",
            consent_token="ct_cross_path",
            sink=FileSink(events_path),
        ),
    )
    try:
        async with Client(mcp) as client:
            # Reactive, not proactive: proactive_mode defaults to "off", and
            # this test deliberately runs the default config a real vendor gets.
            await client.call_tool(
                "cross-path_annotate",
                {
                    "user_goal": "look something up",
                    "signal_type": "failure",
                    "suggested_improvement": "return a typed not-found result",
                },
            )
            await client.call_tool("lookup", {"name": "alice"})
            try:
                await client.call_tool("boom", {})
            except Exception:
                pass
    finally:
        await handle.aclose()


def _run_library_path(events_path: str) -> None:
    from baton import Client
    from baton.sinks import FileSink

    client = Client(
        vendor_id="cross-path",
        consent_token="ct_cross_path",
        sink=FileSink(events_path),
    )
    try:
        with client.trace(
            tool_name="lookup",
            intent="look something up",
            expected_outcome="a match",
            params={"name": "alice"},
        ) as trace:
            trace.observed({"found": True, "name": "alice"})

        try:
            with client.trace(tool_name="boom"):
                raise ValueError("simulated failure")
        except ValueError:
            pass
    finally:
        client.close()


# =============================================================================
# mcp + fastmcp adapters — one shared test body, parametrized
# =============================================================================

_MCP_FAMILY_RUNNERS = {"mcp": _run_mcp_path, "fastmcp": _run_fastmcp_path}


@pytest.mark.parametrize("path_name", sorted(_MCP_FAMILY_RUNNERS))
async def test_mcp_family_envelope_invariants_hold(path_name: str, tmp_path: Path) -> None:
    events_path = str(tmp_path / "events.jsonl")
    await _MCP_FAMILY_RUNNERS[path_name](events_path)
    events = without_surface_snapshots(_read_events(events_path))

    assert_envelope_shape(events)
    assert_sequence_monotonic_per_session(events)
    assert_signal_types_valid(events)

    # Path-specific: MCP-family agent_runtime is never the library sentinel.
    assert all(e["agent_runtime"] != "python-library" for e in events), (
        f"{path_name}: expected a non-library agent_runtime, got "
        f"{ {e['agent_runtime'] for e in events} }"
    )
    assert {e["event_type"] for e in events} == EXPECTED_EVENT_TYPES


# =============================================================================
# library API — separate test (sync driver, distinct agent_runtime)
# =============================================================================


async def test_library_path_envelope_invariants_hold(tmp_path: Path) -> None:
    events_path = str(tmp_path / "events.jsonl")
    _run_library_path(events_path)
    events = without_surface_snapshots(_read_events(events_path))

    assert_envelope_shape(events)
    assert_sequence_monotonic_per_session(events)
    assert_signal_types_valid(events)

    assert all(e["agent_runtime"] == "python-library" for e in events)
    assert {e["event_type"] for e in events} == EXPECTED_EVENT_TYPES
