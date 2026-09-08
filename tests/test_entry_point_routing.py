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


def test_refuses_an_ambiguous_object_rather_than_guessing() -> None:
    """Both seams present means an upstream release moved one. Guessing here
    installs a capture that looks healthy and emits nothing, so it must fail
    loudly and name the two explicit entry points instead."""
    from baton.integrations.mcp._compat import MCPServerClass as FastMCP

    server = FastMCP("routing-ambiguous")
    # Give the official server a fastmcp-shaped seam it does not really have.
    server.add_middleware = lambda *_a, **_k: None  # type: ignore[attr-defined]

    with pytest.raises(TypeError, match="cannot tell which adapter"):
        baton.install_baton(server, _config())


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
