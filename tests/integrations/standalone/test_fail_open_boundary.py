"""Capture must never fail the vendor's tool call (SPEC §11.2, CHARTER).

Every test here reproduces a way that guarantee was actually broken, through
the PUBLIC API a vendor would use — not by calling the guard directly. That
distinction is the point: the unit test for the runtime detector's context
guard passed the whole time the escape below was live, because it fed the guard
a fake context that raised ``ValueError``. Only driving a real fastmcp server
raises what fastmcp really raises.
"""

from __future__ import annotations

import json
import logging
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


def test_a_real_detached_context_does_not_raise_through_the_detector() -> None:
    """The regression, against a REAL ``fastmcp.Context`` rather than a stub.

    ``fastmcp``'s ``Context.session`` raises **RuntimeError** when no MCP
    session is established; the detector's guard enumerated
    ``(AttributeError, ValueError, TypeError)`` — the OFFICIAL SDK's spelling —
    so it escaped ``detect_agent_runtime`` into ``BatonMiddleware``, which does
    not guard it, and failed the vendor's tool call.

    **A real Context is the whole point.** The detector already had a unit test
    for a raising context, and it passed the entire time this was broken,
    because it fed the guard a fake that raised the exception the guard already
    handled. Only the actual library object raises what the library actually
    raises — confirmed the same (``RuntimeError``) on fastmcp 2.14.7 and 4.0.2.

    Driven directly rather than through a server-side tool call: the entry
    point for invoking a tool without a session is ``call_tool`` on fastmcp 3+
    and ``_call_tool`` with a different signature on the 2.14 floor, so an
    end-to-end driver would have to skip the floor — and the floor is where a
    vendor most plausibly is.
    """
    from fastmcp import Context

    from baton.integrations.runtime_adapter import detect_agent_runtime

    ctx = Context(FastMCP("detached"))
    with pytest.raises(RuntimeError):
        _ = ctx.session  # the precondition: this really is the raising shape

    assert detect_agent_runtime({}, context=ctx) is None
    # One tier lost, not the whole ladder.
    assert detect_agent_runtime({"claudecode/toolUseId": "t"}, context=ctx) == "claude-code"


async def test_identity_failures_cannot_fail_the_call_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract, the identity seam. A token accessor that raises, and a
    token object whose attribute access raises, must both cost the FIELD and
    not the call."""
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
        mcp, handle = _install(events_path, principal_id_hmac_key=b"k", tenant_id="t")
        try:
            async with Client(mcp) as client:
                assert await client.call_tool("lookup", {"name": "alice"}) is not None
        finally:
            await handle.aclose()
        events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
        calls = [e for e in events if e["event_type"].startswith("tool_call")]
        assert calls, "the call must still have been captured"
        assert {e.get("principal") for e in calls} == {None}


async def test_a_str_hmac_key_does_not_break_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hmac.new`` refuses a ``str`` key, inside the call, in the one
    deployment shape a vendor cannot reach locally. The env var has always
    taken a string, so this is the natural mistake."""
    from dataclasses import dataclass

    from baton.integrations.standalone import _auth

    @dataclass
    class _Tok:
        claims: dict[str, Any] | None = None

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: _Tok({"sub": "alice"}))
    events_path = tmp_path / "e.jsonl"
    mcp, handle = _install(events_path, principal_id_hmac_key="a-string-secret", tenant_id="t")
    try:
        async with Client(mcp) as client:
            assert await client.call_tool("lookup", {"name": "alice"}) is not None
    finally:
        await handle.aclose()
    events = [json.loads(x) for x in events_path.read_text().splitlines() if x.strip()]
    principals = [e.get("principal") for e in events if e["event_type"].startswith("tool_call")]
    assert principals and all(
        p is not None and p["id"].startswith("h1:") and p["form"] == "hashed" for p in principals
    ), principals


async def test_the_missing_key_warning_fires_once_per_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One install, one warning — not one per emit path.

    The tool-call path and the annotation path each held their own warn-once
    set, so a vendor in hashed mode with no key was told twice.
    """
    from dataclasses import dataclass

    from baton.integrations.standalone import _auth

    @dataclass
    class _Tok:
        claims: dict[str, Any] | None = None

    monkeypatch.setattr(_auth, "get_access_token_or_none", lambda: _Tok({"sub": "alice"}))
    mcp, handle = _install(tmp_path / "e.jsonl", tenant_id="t")  # no key
    with caplog.at_level(logging.WARNING):
        try:
            async with Client(mcp) as client:
                await client.call_tool("lookup", {"name": "alice"})
                await client.call_tool(
                    handle.annotation_tool_name, {"user_goal": "g", "signal_type": "failure"}
                )
        finally:
            await handle.aclose()
    hits = [r for r in caplog.records if "HMAC key" in r.message]
    assert len(hits) == 1, f"expected one warning per install, got {len(hits)}"
