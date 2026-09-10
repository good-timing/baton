"""Both MCP adapters must produce the SAME ``user_id`` from the same token.

The companion to ``test_agent_runtime_parity.py``, and it exists for the same
recorded reason: ``detect_agent_runtime`` lived under one adapter's package,
the other never called it, and every event that adapter emitted carried
``"unknown"`` through a rename, a release and a CI matrix — because each
adapter's own suite asserted only about itself. ``identity_adapter.py`` is
shared from the first commit specifically so that cannot recur, and this file
is what pins it.

It follows the same two rules as the runtime parity test:

1. **Assert the EXPECTED value, not merely that the two agree.** Two adapters
   broken identically — both returning ``None``, which is precisely the state
   before this change — pass an agreement-only check.
2. **Fail when nothing was checked.** A filter matching no events would
   otherwise turn a vacuous pass into a green tick.

The token is injected per adapter at its own ``_auth`` module, which is also
the assertion: two DIFFERENT auth seams (``mcp.server.auth.middleware`` and
``fastmcp.server.dependencies``) feeding one shared resolver must land on one
value. If a future change reads the token differently on one side, this is
where it shows up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import without_surface_snapshots

pytestmark = pytest.mark.functional

TENANT = "tenant-parity"
HMAC_KEY = b"parity-user-id-key"
CLAIMS = {"sub": "alice@acme.example", "iss": "https://idp.example"}


def _read_events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _official_token() -> Any:
    from mcp.server.auth.provider import AccessToken

    if "claims" not in AccessToken.model_fields:
        pytest.skip("mcp < 1.27 cannot carry claims; parity is asserted on 1.27+ only")
    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=CLAIMS)


def _standalone_token() -> Any:
    from fastmcp.server.dependencies import AccessToken

    return AccessToken(token="jwt", client_id="acme-app", scopes=[], claims=CLAIMS)


async def _run_official_path(
    events_path: Path, token: Any, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from baton.integrations.official import VendorConfig, _auth, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: token)
    mcp = FastMCP("parity-official-uid")

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
            tenant_id=TENANT,
            user_id_mode=mode,
            user_id_hmac_key=HMAC_KEY,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                "parity_annotate", {"user_goal": "look up", "signal_type": "failure"}
            )
    finally:
        await handle.aclose()


async def _run_standalone_path(
    events_path: Path, token: Any, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, _auth, install_baton
    from baton.sinks import FileSink

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: token)
    mcp: Any = FastMCP("parity-standalone-uid")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="parity",
            vendor_display_name="Parity Vendor",
            consent_token="ct_parity",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
            user_id_mode=mode,
            user_id_hmac_key=HMAC_KEY,
        ),
    )
    try:
        async with Client(mcp) as client:
            await client.call_tool("lookup", {"name": "alice"})
            await client.call_tool(
                "parity_annotate", {"user_goal": "look up", "signal_type": "failure"}
            )
    finally:
        await handle.aclose()


def _user_ids(path: Path) -> set[str | None]:
    events = without_surface_snapshots(_read_events(path))
    assert events, f"no events captured at {path} — the assertion would be vacuous"
    return {ev.get("user_id") for ev in events}


async def test_both_adapters_hash_one_principal_to_one_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expected value first, then agreement.

    The expected value is computed independently here rather than by comparing
    the two runs, because two adapters that both dropped the field agree
    perfectly.
    """
    from baton.identity import hash_user_id

    expected = hash_user_id(CLAIMS["sub"], tenant_id=TENANT, key=HMAC_KEY, issuer=CLAIMS["iss"])

    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "hashed", monkeypatch)

    official = _user_ids(official_path)
    standalone = _user_ids(standalone_path)

    assert official == {expected}, f"official adapter: {official}"
    assert standalone == {expected}, f"standalone adapter: {standalone}"
    assert official == standalone


async def test_neither_adapter_leaks_the_principal_in_hashed_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked against the raw files on both sides.

    One adapter leaking while the other does not is the shape this whole file
    exists to catch, and a residency leak is the worst version of it.
    """
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "hashed", monkeypatch)

    for path in (official_path, standalone_path):
        blob = path.read_text()
        assert CLAIMS["sub"] not in blob, f"raw principal found in {path.name}"
        assert CLAIMS["iss"] not in blob, f"issuer found in {path.name}"


async def test_both_adapters_agree_in_raw_mode_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw mode is a wire contract like any other — two sensors watching one
    client must not disagree about who it is, whichever mode is configured."""
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, _official_token(), "raw", monkeypatch)
    await _run_standalone_path(standalone_path, _standalone_token(), "raw", monkeypatch)

    assert _user_ids(official_path) == {CLAIMS["sub"]}
    assert _user_ids(standalone_path) == {CLAIMS["sub"]}


async def test_both_adapters_drop_the_field_when_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    official_path = tmp_path / "official.jsonl"
    standalone_path = tmp_path / "standalone.jsonl"
    await _run_official_path(official_path, None, "hashed", monkeypatch)
    await _run_standalone_path(standalone_path, None, "hashed", monkeypatch)

    assert _user_ids(official_path) == {None}
    assert _user_ids(standalone_path) == {None}
