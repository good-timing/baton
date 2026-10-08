"""Both MCP adapters must report the same ``client_observed`` for the same
client, and neither may name the client in ``agent_runtime``.

Each adapter's suite runs separately in CI, so only a test that drives both
with one input can see one of them drift.

Two rules this file follows on purpose:

1. It asserts the expected value, not merely that the two agree: two adapters
   broken identically pass an agreement-only check.
2. It fails when nothing was checked: a filter that matches no events would
   otherwise be a vacuous pass.

Both paths drive a real client over an in-memory transport, because ``_meta``
and the handshake only exist on the wire.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import read_events, without_surface_snapshots
from tests._mcp_session import DECLARED_VERSION

pytestmark = pytest.mark.functional

# What each driver's client library declares when the test sets no
# ``client_info``. The two libraries' default versions differ, so undeclared
# cases compare the name only.
LIBRARY_NAME = "mcp"

UNDECLARED_CASES = [
    pytest.param({"claudecode/toolUseId": "tu_abc123"}, id="a-carried-claudecode-key"),
    pytest.param({"progressToken": 7}, id="no-per-call-signal"),
    pytest.param(
        {
            "io.baton/agent_runtime": "acme-plugin",
            "baton": {"agent_runtime": "acme-plugin"},
            "claudecode/toolUseId": "tu_abc123",
        },
        id="a-caller-asserting-its-own-runtime",
    ),
]

# (the _meta a client sends, the name it declares in initialize)
DECLARED_CASES = [
    pytest.param({}, "claude-ai", id="desktop"),
    pytest.param({"progressToken": 7}, "cursor", id="cursor"),
    pytest.param(
        {"claudecode/toolUseId": "tu_1"}, "some-other-client", id="beside-a-claudecode-key"
    ),
]

# The per-request ``io.modelcontextprotocol/clientInfo`` carrier cannot be a
# parity case: fastmcp 4's ``Client`` overwrites that key with its own
# declaration on every request and the official driver's client does not, so a
# planted value has two right answers. Its precedence is pinned in
# ``tests/test_client_observed.py`` and fastmcp's behaviour in
# ``tests/integrations/standalone/test_new_spec_reserved_keys.py``.


async def _run_official_path(
    events_path: Path, meta: dict[str, Any], declared: str | None = None
) -> None:
    """Official ``mcp`` SDK adapter, driven over its in-memory client session."""
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    mcp = FastMCP("parity-official")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        async with connected_session(mcp, declared_name=declared) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()


async def _run_standalone_path(
    events_path: Path, meta: dict[str, Any], declared: str | None = None
) -> None:
    """Standalone ``fastmcp`` adapter, driven over its in-process ``Client``."""
    import mcp.types as mcp_types
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("parity-standalone")

    @mcp.tool()
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
        ),
    )
    try:
        client_info = (
            mcp_types.Implementation(name=declared, version=DECLARED_VERSION)
            if declared is not None
            else None
        )
        async with Client(mcp, client_info=client_info) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool(
                handle.annotation_tool_name,
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()


def _caller_events(events_path: Path, path_name: str) -> list[dict[str, Any]]:
    events = without_surface_snapshots(read_events(events_path))
    types = {ev["event_type"] for ev in events}
    assert {"tool_call_start", "tool_call_end", "annotation"} <= types, (
        f"{path_name}: the driver captured only {sorted(types)}, so the "
        f"assertions below would not cover every emit site"
    )
    return events


def _carried_meta_keys(events: list[dict[str, Any]]) -> set[str]:
    (start,) = [ev for ev in events if ev["event_type"] == "tool_call_start"]
    return set(start.get("runtime_meta") or {})


@pytest.mark.parametrize("meta", UNDECLARED_CASES)
async def test_neither_adapter_names_the_client_in_agent_runtime(
    tmp_path: Path, meta: dict[str, Any]
) -> None:
    official_events = tmp_path / "official.jsonl"
    standalone_events = tmp_path / "standalone.jsonl"

    await _run_official_path(official_events, meta)
    await _run_standalone_path(standalone_events, meta)

    official = _caller_events(official_events, "official")
    standalone = _caller_events(standalone_events, "standalone")

    for path_name, events in (("official", official), ("standalone", standalone)):
        assert {ev["agent_runtime"] for ev in events} == {"unknown"}, path_name
        names = {(ev["client_observed"] or {}).get("info", {}).get("name") for ev in events}
        assert names == {LIBRARY_NAME}, f"{path_name}: {names}"
        # The consumer recognises Claude Code from this key, so it must arrive.
        claudecode_keys = {key for key in meta if key.startswith("claudecode/")}
        assert claudecode_keys <= _carried_meta_keys(events), path_name


@pytest.mark.parametrize(("meta", "declared"), DECLARED_CASES)
async def test_both_adapters_report_the_same_declared_client(
    tmp_path: Path, meta: dict[str, Any], declared: str
) -> None:
    """The handshake attribute is ``clientInfo`` on mcp 1.x and ``client_info``
    on 2.x, and the two adapters resolve different mcp versions in CI."""
    official_events = tmp_path / "official.jsonl"
    standalone_events = tmp_path / "standalone.jsonl"

    await _run_official_path(official_events, meta, declared)
    await _run_standalone_path(standalone_events, meta, declared)

    official = _caller_events(official_events, "official")
    standalone = _caller_events(standalone_events, "standalone")

    expected = {"info": {"name": declared, "version": DECLARED_VERSION}}
    for path_name, events in (("official", official), ("standalone", standalone)):
        for ev in events:
            assert ev["client_observed"] == expected, f"{path_name} {ev['event_type']}"
            assert ev["agent_runtime"] == "unknown", f"{path_name} {ev['event_type']}"
