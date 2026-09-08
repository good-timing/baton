"""``tenant_id`` is the ACCOUNT and ``vendor_id`` is the SERVER, on every path.

The envelope has carried both fields since 0.2.8, and every producer filled both
from ``vendor_id`` — so the two were indistinguishable on the wire. Two symptoms
followed, both driven by hand on 2026-09-07: a server naming itself with its
workspace's opaque id (the LEAK), and two servers wrapped into one workspace
rendering as ONE whose label flips to whichever deployed last (the COLLAPSE).

These tests pin the property that ends both: given an account id, the account id
lands in ``tenant_id`` and the server id lands in ``vendor_id``, and **they are
not equal**. The inequality is the assertion that matters — a test that only
checked ``tenant_id == "ten_…"`` would still pass if some path quietly wrote the
account into both slots.

The ``vendor_id`` fallback is covered too, because it is a migration shim rather
than a supported configuration: it reproduces the collapse, so it must be
visible and deliberate rather than something a reader discovers in prod.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ACCOUNT = "ten_7cd4c8cf00000000000000000000abcd"
SERVER = "echo-server"


def _events(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _ids(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(e["tenant_id"], e["vendor_id"]) for e in events}


async def _run_fastmcp(events_path: Path, **config_kwargs: Any) -> list[dict[str, Any]]:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: FastMCP[Any] = FastMCP("split-probe")

    @mcp.tool
    def echo(text: str) -> str:
        return text

    sink = FileSink(str(events_path))
    install_baton(
        mcp,
        VendorConfig(
            vendor_id=SERVER,
            vendor_display_name="Echo Server",
            consent_token="ct_split",
            sink=sink,
            **config_kwargs,
        ),
    )
    async with Client(mcp) as client:
        await client.call_tool("echo", {"text": "hi"})
    await sink.aclose()
    return _events(events_path)


async def _run_mcp(events_path: Path, **config_kwargs: Any) -> list[dict[str, Any]]:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink

    mcp = FastMCP("split-probe")

    @mcp.tool()
    def echo(text: str) -> str:
        return text

    sink = FileSink(str(events_path))
    install_baton(
        mcp,
        VendorConfig(
            vendor_id=SERVER,
            vendor_display_name="Echo Server",
            consent_token="ct_split",
            sink=sink,
            **config_kwargs,
        ),
    )
    await mcp.call_tool("echo", {"text": "hi"})
    await sink.aclose()
    return _events(events_path)


@pytest.mark.parametrize("runner", [_run_fastmcp, _run_mcp], ids=["fastmcp", "mcp"])
async def test_explicit_tenant_id_is_not_the_vendor_id(runner: Any, tmp_path: Path) -> None:
    events = await runner(tmp_path / "e.jsonl", tenant_id=ACCOUNT)
    assert events, "no events emitted — the probe proved nothing"
    assert _ids(events) == {(ACCOUNT, SERVER)}


@pytest.mark.parametrize("runner", [_run_fastmcp, _run_mcp], ids=["fastmcp", "mcp"])
async def test_tenant_id_from_env(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_TENANT_ID", ACCOUNT)
    events = await runner(tmp_path / "e.jsonl")
    assert events
    assert _ids(events) == {(ACCOUNT, SERVER)}


@pytest.mark.parametrize("runner", [_run_fastmcp, _run_mcp], ids=["fastmcp", "mcp"])
async def test_absent_tenant_id_falls_back_to_vendor_id(
    runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim, pinned so its removal is a deliberate act rather than a surprise."""
    monkeypatch.delenv("BATON_TENANT_ID", raising=False)
    events = await runner(tmp_path / "e.jsonl")
    assert events
    assert _ids(events) == {(SERVER, SERVER)}


async def test_explicit_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_TENANT_ID", "ten_from_env")
    events = await _run_fastmcp(tmp_path / "e.jsonl", tenant_id=ACCOUNT)
    assert _ids(events) == {(ACCOUNT, SERVER)}


async def test_library_api_carries_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library path resolves through ``Client``, not ``VendorConfig`` — a
    separate ladder, and the one the console's own dogfood traffic runs on."""
    monkeypatch.delenv("BATON_TENANT_ID", raising=False)
    from baton import AsyncClient
    from baton.sinks import FileSink

    events_path = tmp_path / "e.jsonl"
    sink = FileSink(str(events_path))
    client = AsyncClient(sink=sink, vendor_id=SERVER, tenant_id=ACCOUNT, consent_token="ct_split")
    async with client.trace(tool_name="echo", params={"text": "hi"}) as trace:
        trace.observed("hi")
    await client.aclose()

    events = _events(events_path)
    assert events
    assert _ids(events) == {(ACCOUNT, SERVER)}


async def test_library_api_env_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from baton import AsyncClient
    from baton.sinks import FileSink

    monkeypatch.setenv("BATON_TENANT_ID", ACCOUNT)
    sink = FileSink(str(tmp_path / "env.jsonl"))
    c = AsyncClient(sink=sink, vendor_id=SERVER, consent_token="ct")
    assert (c._tenant_id, c._vendor_id) == (ACCOUNT, SERVER)
    await c.aclose()

    monkeypatch.delenv("BATON_TENANT_ID", raising=False)
    sink2 = FileSink(str(tmp_path / "fb.jsonl"))
    c2 = AsyncClient(sink=sink2, vendor_id=SERVER, consent_token="ct")
    assert (c2._tenant_id, c2._vendor_id) == (SERVER, SERVER)
    await c2.aclose()


def test_tenant_id_is_appended_so_positional_construction_still_binds() -> None:
    """``VendorConfig`` is a plain dataclass, so field ORDER is public API.

    0.7.0 inserted ``tenant_id`` third, ahead of ``consent_token`` and ``sink``.
    A caller writing ``VendorConfig("acme", "Acme", "ct", my_sink)`` — valid on
    0.6.1 — then bound ``tenant_id="ct"`` and ``consent_token=my_sink``, and
    nothing caught it: ``_validate_vendor_config`` only tests
    ``if not config.consent_token``, and a Sink instance is truthy, so a Sink
    object rode into the envelope's ``consent_token`` field. Silent, and
    described in the changelog as purely additive.

    Every in-repo call site uses keywords, which is exactly why the suite could
    not see it. This test is the missing one: it constructs positionally, the
    way a vendor's code does.
    """
    from baton.integrations._config import VendorConfig
    from baton.sinks import StdoutSink

    sink = StdoutSink()
    config = VendorConfig("acme", "Acme Corp", "ct-positional", sink)

    assert config.vendor_id == "acme"
    assert config.vendor_display_name == "Acme Corp"
    assert config.consent_token == "ct-positional"
    assert config.sink is sink
    # The new field takes no positional slot from anything that existed.
    assert config.tenant_id is None
