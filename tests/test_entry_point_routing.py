"""``baton.install_baton`` routes to the right adapter, detecting on structure.

The bug this closes is not a crash — it is that both libraries name their server
class ``FastMCP``, take the same constructor argument, and expose an
``install_baton`` with an identical signature and an identical ``VendorConfig``.
The only thing separating a correct call from a wrong one is the import line.

So these tests assert WHICH adapter ran, not merely that install returned. A test
that only checked "events were emitted" would pass if every server routed to one
adapter, which is exactly the failure worth catching: the wrong adapter half-
installs and its capture looks healthy while producing nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

import baton
from baton.integrations._config import VendorConfig
from baton.sinks import StdoutSink


def _config() -> VendorConfig:
    return VendorConfig(
        vendor_id="routing-probe",
        vendor_display_name="Routing Probe",
        consent_token="ct_routing",
        sink=StdoutSink(),
    )


def test_exported_from_the_top_level() -> None:
    """The point of the entry point is that a caller never types an adapter path."""
    assert "install_baton" in baton.__all__
    assert "VendorConfig" in baton.__all__
    assert baton.VendorConfig is VendorConfig


def test_routes_standalone_fastmcp_to_the_middleware_adapter() -> None:
    from fastmcp import FastMCP

    from baton.integrations.fastmcp.middleware import BatonMiddleware

    server: FastMCP[Any] = FastMCP("routing-standalone")

    @server.tool
    def echo(text: str) -> str:
        return text

    baton.install_baton(server, _config())

    assert any(isinstance(m, BatonMiddleware) for m in server.middleware), (
        "standalone fastmcp server did not get the fastmcp adapter's middleware"
    )


async def test_routes_official_sdk_to_the_tool_wrap_adapter() -> None:
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP
    from baton.integrations.mcp._registry import get_tool_registry
    from baton.integrations.mcp._tool_wrap import _WRAPPED_SENTINEL

    server = FastMCP("routing-official")

    @server.tool()
    def echo(text: str) -> str:
        return text

    baton.install_baton(server, _config())

    # The sentinel lands on ``Tool.run`` — the adapter wraps the dispatcher,
    # not the vendor's function object.
    echo_tool = get_tool_registry(server)["echo"]
    assert getattr(echo_tool.run, _WRAPPED_SENTINEL, False), (
        "official SDK server's tool was not wrapped by the mcp adapter"
    )


def test_refuses_an_object_that_is_neither() -> None:
    class NotAServer:
        pass

    with pytest.raises(TypeError, match="does not recognise this object"):
        baton.install_baton(NotAServer(), _config())


def test_both_seams_routes_to_fastmcp_because_that_is_what_2_x_looks_like(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.7.0 refused this shape, and the shape is the declared floor.

    The module was written believing the two seams disjoint, so both-present
    raised ``TypeError`` naming an "upstream release moved one". Measured on the
    published 0.7.0 against a one-resolve ``fastmcp==2.14.7`` install: that
    server has ``add_middleware`` AND ``_tool_manager``, so every server on the
    ``>=2.14`` floor hit the refusal, and the message blamed upstream for a fact
    older than the module. ``add_middleware`` is the exclusive signal — the
    official SDK exposes it on neither major (measured: ``mcp`` 1.x ``FastMCP``
    and ``mcp`` 2.0.0 ``MCPServer``, both no) — so it decides.

    **Asserted on the routing DECISION, not on a completed install.** The first
    version of this test let the chosen adapter run and checked that a
    ``BatonMiddleware`` landed. It passed locally on mcp 1.x and failed on CI
    under mcp 2.x — because the fixture is an official-SDK object and the
    fastmcp adapter, correctly chosen, then died reaching for a ``_mcp_server``
    that 2.x does not have. That failure said nothing about routing, which is
    this module's only job: whether one adapter can install into a synthetic
    hybrid is the adapter's contract, not the router's. A REAL ``fastmcp`` 2.x
    server both routes and installs, and the ``fastmcp-matrix`` CI leg now runs
    this file against a pinned 2.14.7 to hold that end.
    """
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP

    server = FastMCP("routing-both-seams")
    server.add_middleware = lambda *_a, **_k: None  # type: ignore[attr-defined]

    from baton.install import _has_fastmcp_middleware_seam, _has_official_tool_registry

    assert _has_fastmcp_middleware_seam(server)
    assert _has_official_tool_registry(server), "fixture must carry BOTH seams"

    chosen: list[str] = []
    import baton.integrations.fastmcp as fastmcp_adapter
    import baton.integrations.mcp as official_adapter

    monkeypatch.setattr(
        fastmcp_adapter, "install_baton", lambda *_a, **_k: chosen.append("fastmcp")
    )
    monkeypatch.setattr(
        official_adapter, "install_baton", lambda *_a, **_k: chosen.append("official")
    )

    baton.install_baton(server, _config())

    assert chosen == ["fastmcp"], "both seams present must route, not refuse"


def test_importing_baton_does_not_require_the_adapters() -> None:
    """``install_baton`` is exported at the top level, so its imports must stay
    inside the call — otherwise ``import baton`` would need both optional
    extras and the library-API path would stop working without them."""
    import baton.install as install_mod

    src = install_mod.__doc__ or ""
    assert src, "module docstring lost"
    for attr in ("integrations",):
        assert not hasattr(install_mod, attr), (
            f"baton.install imported {attr} at module level; adapter imports must be deferred"
        )
