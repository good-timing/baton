"""One entry point that routes to the right adapter, detecting on STRUCTURE.

**Both libraries name their server class ``FastMCP``.** The standalone
``fastmcp`` package and the official ``mcp`` SDK each export that name, they take
the same constructor argument, and ``install_baton`` has the same signature and
the same ``VendorConfig`` on both adapters — so the only thing distinguishing a
correct call from a wrong one is which module the caller imported from. That has
already cost two runtime guards, three wrong docstrings and one misread diagram,
and each guard exists because someone had already made the mistake.

``baton.install_baton`` removes the choice: hand it either server and it finds
the adapter. The per-adapter entry points keep working unchanged — they are the
shipped public contract, and this is additive.

**Detection is on the SEAM each adapter installs into, never on the module
path.** A module-path test would be shorter and is what the existing guards fall
back to, but it answers "where does this class live" when the question is "can
this adapter attach to it" — and those come apart on subclasses, on re-exports,
and on any upstream reorganisation. The seams:

- standalone ``fastmcp`` — a callable ``add_middleware``; the middleware chain is
  a supported public API and is precisely what ``integrations.fastmcp`` needs.
- official ``mcp`` SDK — a resolvable tool registry, which is what
  ``integrations.mcp`` wraps (and what a bare low-level ``Server`` lacks, which
  is why that shape is refused rather than half-installed).

**Both signals, or neither, is an error rather than a guess.** If a future
upstream release gives the official server a middleware chain, this raises and
names the two explicit entry points instead of silently routing to whichever
branch happened to be tested first — a mis-route installs a capture that looks
healthy and produces nothing
(→ ``broken and unbuilt must not look alike``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baton.integrations._config import VendorConfig
    from baton.integrations._handle import BatonHandle

logger = logging.getLogger(__name__)

__all__ = ["install_baton"]


def _has_fastmcp_middleware_seam(server: Any) -> bool:
    """Standalone ``fastmcp``'s public middleware chain."""
    return callable(getattr(server, "add_middleware", None))


def _has_official_tool_registry(server: Any) -> bool:
    """The official SDK's tool registry, resolved through the same helper the
    adapter itself uses — so detection cannot drift from installation.

    Import-tolerant: ``mcp`` is an optional extra, and its absence means "not
    that shape here", never an error at import time.
    """
    try:
        from baton.integrations.mcp._registry import get_tool_manager
    except Exception:  # pragma: no cover - only when the extra is absent
        return False
    try:
        return get_tool_manager(server) is not None
    except Exception:
        return False


def install_baton(server: Any, config: VendorConfig) -> BatonHandle:
    """Install Baton into either MCP server implementation.

    Routes to ``baton.integrations.fastmcp`` for a standalone ``fastmcp``
    server and ``baton.integrations.mcp`` for the official SDK's, detecting on
    the seam each adapter needs. Raises ``TypeError`` — before mutating
    anything — when the object is neither, or ambiguously both.
    """
    is_fastmcp = _has_fastmcp_middleware_seam(server)
    is_official = _has_official_tool_registry(server)

    if is_fastmcp and is_official:
        raise TypeError(
            "baton.install_baton cannot tell which adapter this server needs: it "
            "exposes BOTH a fastmcp-style ``add_middleware`` and an official-SDK "
            "tool registry. Rather than guess, call the adapter you mean directly "
            "— ``baton.integrations.fastmcp.install_baton`` for a server built "
            "from the standalone ``fastmcp`` package, or "
            "``baton.integrations.mcp.install_baton`` for one built from the "
            "official ``mcp`` SDK. Please report this: the two seams are supposed "
            "to be disjoint, so this means an upstream release moved one."
        )

    if is_fastmcp:
        from baton.integrations.fastmcp import install_baton as _install

        logger.debug("baton: routing to the standalone fastmcp adapter")
        return _install(server, config)

    if is_official:
        from baton.integrations.mcp import install_baton as _install

        logger.debug("baton: routing to the official mcp SDK adapter")
        return _install(server, config)

    raise TypeError(
        "baton.install_baton does not recognise this object as an MCP server it "
        "can install into. It has no ``add_middleware`` (the standalone "
        "``fastmcp`` seam) and no resolvable tool registry (the official ``mcp`` "
        "SDK seam). A bare low-level ``mcp.server.lowlevel.Server`` lands here: "
        "it has no tool registry to wrap, so it needs its request handlers "
        "wrapped instead — not supported yet. If this IS a FastMCP or MCPServer, "
        "the installed ``mcp``/``fastmcp`` may be outside the supported range; "
        f"got {type(server).__module__}.{type(server).__qualname__}."
    )
