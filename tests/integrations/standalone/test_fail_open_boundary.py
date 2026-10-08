"""Capture must never fail the vendor's tool call (SPEC §11.2, CHARTER).

Each test drives the real library object or the public API a vendor would use,
not a stub: a stub raises what its author expected, and only fastmcp raises
what fastmcp really raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from baton.integrations.standalone import VendorConfig, install_baton
from baton.sinks import FileSink


def _install(events_path: Path, **cfg: Any) -> tuple[Any, Any]:
    mcp: Any = FastMCP("fail-open")

    @mcp.tool
    def lookup(name: str) -> dict[str, Any]:
        return {"found": True, "name": name}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="fo",
            vendor_display_name="Fail Open Vendor",
            consent_token="ct_fo",
            sink=FileSink(str(events_path)),
            **cfg,
        ),
    )
    return mcp, handle


def test_a_real_detached_context_costs_only_the_declaration() -> None:
    """``fastmcp``'s ``Context.session`` raises ``RuntimeError`` when no MCP
    session is established. Driven directly rather than through a server-side
    tool call, whose entry point differs between fastmcp 2.14 and 3+."""
    from fastmcp import Context

    from baton.integrations.client_observed import observe_client

    ctx = Context(FastMCP("detached"))
    with pytest.raises(RuntimeError):
        _ = ctx.session

    assert observe_client({}, context=ctx) is None
    observed = observe_client({}, context=ctx, headers={"user-agent": "agent/1.0"})
    assert observed is not None
    assert observed.model_dump(mode="json") == {"headers": {"user-agent": "agent/1.0"}}


async def test_identity_failures_cannot_fail_the_call_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract, the identity seam. A token accessor that raises, and a
    token object whose attribute access raises, must both cost the FIELD and
    not the call. The OAuth hook is configured because the accessor runs only
    when a hook is — without one this would pass having exercised nothing."""
    from baton import principal_from_oauth_sub
    from baton.integrations.standalone import _auth

    class _Exploding:
        @property
        def claims(self) -> dict[str, Any]:
            raise RuntimeError("verifier blew up")

    for getter in (
        lambda: (_ for _ in ()).throw(TypeError("Expected fastmcp AccessToken, got ...")),
        lambda: _Exploding(),
    ):
        events_path = tmp_path / f"e{id(getter)}.jsonl"
        monkeypatch.setattr(_auth, "get_access_token_or_none", getter)
        mcp, handle = _install(
            events_path,
            tenant_id="t",
            resolve_principal=principal_from_oauth_sub,
        )
        try:
            async with Client(mcp) as client:
                assert await client.call_tool("lookup", {"name": "alice"}) is not None
        finally:
            await handle.aclose()
        events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
        calls = [e for e in events if e["event_type"].startswith("tool_call")]
        assert calls, "the call must still have been captured"
        assert {e.get("principal") for e in calls} == {None}
