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
        # ⚠ `mcp`, not `claude-code`, since the ladder became declared-first.
        # Both drivers' clients DECLARE themselves (as the library, setting no
        # `client_info`) while this `_meta` merely carries a `claudecode/` key,
        # and a declaration outranks that inference now.
        #
        # A consequence worth stating: the heuristic is close to unreachable
        # end-to-end, because every real client declares something and Claude
        # Code declares `claude-code` anyway — so where the heuristic would
        # fire, the declaration already gives the same answer. It survives as a
        # backstop for a client that declares nothing at all, which no driver
        # here can produce; that path is pinned in the unit tests instead.
        {"claudecode/toolUseId": "tu_abc123"},
        "mcp",
        id="a-declaration-outranks-a-carried-claudecode-key",
    ),
    pytest.param(
        # The client override was REMOVED 2026-09-09. Parity matters as much
        # for a key that is ignored as for one that is read: if one adapter
        # kept honouring it, the same client would be reported two ways by two
        # sensors watching the same call.
        {"io.baton/agent_runtime": "acme-plugin"},
        # `mcp` (the driver client's declared name), NOT `acme-plugin`: the
        # removed override loses to the declared tier rather than to a
        # default, which is a sharper proof of inertness than "unknown" was.
        "mcp",
        id="io.baton-override-is-inert",
    ),
    pytest.param(
        # And it cannot SUPPRESS detection: the declared name still wins, and
        # the asserted `acme-plugin` appears nowhere.
        {"io.baton/agent_runtime": "acme-plugin", "claudecode/toolUseId": "tu_abc123"},
        "mcp",
        id="removed-override-does-not-suppress-detection",
    ),
    pytest.param(
        # Cursor's shape per SPEC §5.2: a progressToken and nothing else.
        # ⚠ This was "unknown" until 2026-09-09 and is now the DECLARED name.
        # Both drivers' clients identify as the `mcp` library, because neither
        # sets `client_info` — which is exactly what a client that declares
        # nothing about itself looks like on the wire, and it is still more
        # than "unknown" told us.
        {"progressToken": 7},
        "mcp",
        id="no-per-call-signal-falls-to-the-declared-name",
    ),
    pytest.param(
        # The pre-B5 nested form. Dead on both adapters, or the two wire
        # shapes B5 removed are back.
        {"baton": {"agent_runtime": "acme-plugin"}},
        "mcp",
        id="nested-baton-dict-is-dead",
    ),
]

# (case id, _meta, the name the CLIENT declares in initialize, expected)
#
# The tier that does the real work, and the reason B1-R exists: identity that
# does NOT depend on a vendor prefix appearing in a key name. Claude Desktop
# and Cursor were unattributable on both adapters before this.
DECLARED_CASES = [
    pytest.param({}, "claude-ai", "claude-ai", id="desktops-declared-name"),
    pytest.param({"progressToken": 7}, "cursor", "cursor", id="cursor-declared"),
    pytest.param(
        # The ordering case, and it was briefly written the other way round.
        # A DECLARATION outranks the prefix heuristic: `claudecode/*` says the
        # metadata originated from Claude Code, not that the caller is Claude
        # Code, and `baton-proxy` forwards `initialize` unchanged — so a server
        # behind it sees the agent's own `clientInfo` anyway.
        {"claudecode/toolUseId": "tu_1"},
        "some-other-client",
        "some-other-client",
        id="a-declaration-outranks-the-prefix-heuristic",
    ),
]

# ⚠ **The new-spec carrier CANNOT be a parity case, and trying made this file
# red on a fresh resolve.** A case here planted
# `io.modelcontextprotocol/clientInfo: {"name": "zed"}` in `_meta` and expected
# both adapters to report `zed`, on the reasoning that tier 1 outranks tier 2.
# The precedence is right and is pinned in
# `tests/test_runtime_adapter.py::test_the_per_request_key_outranks_the_connection`,
# where the meta can be forged. What cannot be forged is a REAL client's
# request: **fastmcp 4's `Client` negotiates `2026-07-28` and writes that
# reserved key itself on every request**, overwriting whatever the caller
# passed, so the standalone driver reported its own declared name and the case
# failed — while the official driver on mcp 2.2 does NOT inject it, so the
# planted value survived there and that half passed.
#
# The two drivers therefore disagree for a reason that has nothing to do with
# the code under test: the two CLIENT libraries behave differently on the same
# protocol revision. A parity case cannot have two right answers. The
# fastmcp-4 behaviour is pinned on its own in
# `tests/integrations/standalone/test_new_spec_reserved_keys.py`.
#
# Worth carrying forward: this is the first observed client that actually
# speaks the new spec and populates the reserved keys, which is the traffic
# E1 concluded does not exist yet and N4 was told to expect empty.


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


async def _run_official_path(
    events_path: Path, meta: dict[str, Any], declared: str | None = None
) -> None:
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
            mcp_types.Implementation(name=declared, version="9.9.9")
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


@pytest.mark.parametrize(("meta", "declared", "expected"), DECLARED_CASES)
async def test_both_adapters_read_the_clients_declared_identity(
    tmp_path: Path, meta: dict[str, Any], declared: str, expected: str
) -> None:
    """Parity on the DECLARED tier, driven by a real client that names itself.

    Asserted through both adapters for the same reason the `_meta` test is:
    the declaration is read from ``ctx.session.client_params``, whose attribute
    was RENAMED between mcp 1.x (``clientInfo``) and 2.x (``client_info``). An
    adapter reading one spelling reports the correct name on one major version
    and ``unknown`` on the other — and since the two adapters resolve different
    mcp versions in CI, a per-adapter test could stay green through exactly
    that split.
    """
    official_events = tmp_path / "official.jsonl"
    standalone_events = tmp_path / "standalone.jsonl"

    await _run_official_path(official_events, meta, declared)
    await _run_standalone_path(standalone_events, meta, declared)

    official = _runtimes(official_events, "official")
    standalone = _runtimes(standalone_events, "standalone")

    assert official == {expected}, (
        f"official adapter reported {official}, expected {{{expected!r}}} — "
        f"a {{'unknown'}} here means the declared tier is not wired in; the "
        f"client's own name is on the session"
    )
    assert standalone == {expected}, f"standalone adapter reported {standalone}"
