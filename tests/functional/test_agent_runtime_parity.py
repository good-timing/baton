"""Both MCP adapters must report the SAME ``agent_runtime`` for the same
``_meta``.

This test exists because its absence shipped. ``detect_agent_runtime`` lived
under ``integrations/standalone/``, the official adapter never called it, and
so every event that adapter has ever emitted carried
``agent_runtime: "unknown"`` — through a package rename, a release, and a CI
matrix that runs each adapter's suite separately. No per-adapter test could
catch it: the standalone suite asserted detection worked (it did) and the
official suite asserted nothing about the field at all. Only a test that
drives BOTH paths with one input can.

Two rules this file follows on purpose:

1. **It asserts the EXPECTED value, not merely that the two agree.** Two
   adapters broken identically — which is exactly the state before this
   change, both defaulting to ``"unknown"`` — pass an agreement-only check.
   Agreement is asserted too, because the pair is what regresses.
2. **It fails when nothing was checked.** A filter that silently matches no
   events would otherwise turn a vacuous pass into a green tick, the way
   V2's first extractor did when it tested ``isinstance(result, str)``
   against a nested dict and reported zero mispairs.

Both paths drive a REAL client over a real in-memory transport rather than
calling the tool object directly, because ``_meta`` only exists on the wire:
``mcp.call_tool(...)`` and ``BatonMiddleware`` invoked by hand both carry
none, so a direct-call version of this test would assert ``"unknown"`` on
both sides and pass while proving nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import without_surface_snapshots

pytestmark = pytest.mark.functional


# (case id, the _meta a client sends, the agent_runtime BOTH adapters must report)
RUNTIME_CASES = [
    pytest.param(
        {"claudecode/toolUseId": "tu_abc123"},
        "claude-code",
        id="claudecode-prefix-heuristic",
    ),
    pytest.param(
        # The client override was REMOVED 2026-09-09. Parity matters as much
        # for a key that is ignored as for one that is read: if one adapter
        # kept honouring it, the same client would be reported two ways by two
        # sensors watching the same call.
        {"io.baton/agent_runtime": "acme-plugin"},
        "unknown",
        id="io.baton-override-is-inert",
    ),
    pytest.param(
        # And it cannot SUPPRESS a heuristic that would otherwise match.
        {"io.baton/agent_runtime": "acme-plugin", "claudecode/toolUseId": "tu_abc123"},
        "claude-code",
        id="removed-override-does-not-suppress-the-heuristic",
    ),
    pytest.param(
        # Cursor's shape per SPEC §5.2: a progressToken and nothing else.
        {"progressToken": 7},
        "unknown",
        id="no-signal-falls-back",
    ),
    pytest.param(
        # The pre-B5 nested form. Dead on both adapters, or the two wire
        # shapes B5 removed are back.
        {"baton": {"agent_runtime": "acme-plugin"}},
        "unknown",
        id="nested-baton-dict-is-dead",
    ),
]


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


async def _run_official_path(events_path: Path, meta: dict[str, Any]) -> None:
    """Official ``mcp`` SDK adapter, driven over its in-memory client session.

    ``ClientSession.call_tool`` takes a keyword-only ``meta``, which is what
    puts a real ``_meta`` on ``params`` — the same field the wrapper reads.
    """
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
        # ``connected_session`` rather than mcp's own convenience helper:
        # that helper was REMOVED in mcp 2.0, and `core` resolves whatever
        # ``mcp>=1.20,<3`` gives it — so using it would make this test's
        # portability depend on a resolution accident.
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool(
                "parity_annotate",
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()


async def _run_standalone_path(events_path: Path, meta: dict[str, Any]) -> None:
    """Standalone ``fastmcp`` adapter, driven over its in-process ``Client``."""
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
        async with Client(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"}, meta=meta)
            await client.call_tool(
                "parity_annotate",
                {"user_goal": "look something up", "signal_type": "failure"},
                meta=meta,
            )
    finally:
        await handle.aclose()


def _runtimes(events_path: Path, path_name: str) -> set[str]:
    """The ``agent_runtime`` values on every event that should carry a detected
    one, with a loud failure when that set is empty.

    ``surface_snapshot`` is excluded deliberately, not incidentally: it
    describes the SERVER, is captured outside any tool call (the standalone
    adapter fires it from ``on_list_tools``, which has no per-call ``_meta``
    at all), and both adapters emit the install-time default on it.
    """
    events = without_surface_snapshots(_read_events(events_path))
    assert events, (
        f"{path_name}: no non-surface events captured, so every assertion "
        f"below would be vacuously true — the driver is broken, not the SDK"
    )
    return {ev["agent_runtime"] for ev in events}


@pytest.mark.parametrize(("meta", "expected"), RUNTIME_CASES)
async def test_both_adapters_report_the_same_agent_runtime(
    tmp_path: Path, meta: dict[str, Any], expected: str
) -> None:
    official_events = tmp_path / "official.jsonl"
    standalone_events = tmp_path / "standalone.jsonl"

    await _run_official_path(official_events, meta)
    await _run_standalone_path(standalone_events, meta)

    official = _runtimes(official_events, "official")
    standalone = _runtimes(standalone_events, "standalone")

    # One value per path — a path reporting two different runtimes for one
    # client's calls means some emit site was missed, which is how the
    # official adapter's annotation tool differed from its own tool calls.
    assert official == {expected}, (
        f"official adapter reported {official}, expected {{{expected!r}}} — "
        f"if this is {{'unknown'}} the detection is not wired into every emit site"
    )
    assert standalone == {expected}, f"standalone adapter reported {standalone}"
    assert official == standalone
