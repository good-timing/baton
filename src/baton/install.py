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
  a supported public API and is precisely what ``integrations.standalone`` needs.
- official ``mcp`` SDK — a resolvable tool registry, which is what
  ``integrations.official`` wraps (and what a bare low-level ``Server`` lacks, which
  is why that shape is refused rather than half-installed).

**The middleware seam decides; the registry only breaks a tie it never has.**
``add_middleware`` is exclusive to the standalone package and the ordering rests
on that, measured across all four shipped shapes rather than assumed:

===========================  ================  ================
server                       add_middleware    tool registry
===========================  ================  ================
official ``mcp`` 1.x         no                yes
official ``mcp`` 2.x         no                yes
standalone ``fastmcp`` 2.14  **yes**           **yes**
standalone ``fastmcp`` 4.x   yes               no
===========================  ================  ================

**Corrected 2026-09-08, after 0.7.0 shipped the wrong rule.** This module was
written to treat both-signals as an ambiguity and refuse, on the stated belief
that the seams were disjoint. They are not, and never were on ``fastmcp`` 2.x,
which keeps a ``_tool_manager`` alongside its middleware chain — so the new
entry point raised ``TypeError`` for every server on the declared ``>=2.14``
floor, and its message blamed a hypothetical upstream move for a fact that
predates the module. Only ``baton.install_baton`` was affected;
``baton.integrations.standalone.install_baton`` routes correctly and always did.

**Neither signal is still an error**, and that direction is unchanged: a bare
low-level ``Server`` must be refused rather than half-installed, because a
mis-route installs a capture that looks healthy and produces nothing
(→ ``broken and unbuilt must not look alike``).
**The off switch.** Setting ``BATON_DISABLED=1`` in the
environment makes this function install NOTHING and raise nothing: no
middleware, no wrapped tools, no annotation tool on the surface, no
instructions rewrite, no sink. The server behaves exactly as it would without
this call. See ``baton._optout``.

"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from baton._optout import capture_disabled
from baton.integrations._handle import disabled_handle

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
        from baton.integrations.official._registry import get_tool_manager
    except Exception:  # pragma: no cover - only when the extra is absent
        return False
    try:
        return get_tool_manager(server) is not None
    except Exception:
        return False


def install_baton(
    server: Any,
    config: VendorConfig | None = None,
    *,
    dsn: str | None = None,
) -> BatonHandle:
    """Install Baton into either MCP server implementation.

    Routes to ``baton.integrations.standalone`` for a standalone ``fastmcp``
    server and ``baton.integrations.official`` for the official SDK's, detecting on
    the seam each adapter needs. Raises ``TypeError`` — before mutating
    anything — only when the object is neither.

    ``dsn`` is the packed value from ``/account``, and the two-line install::

        from baton import install_baton

        install_baton(mcp, dsn="https://baton_pk_...@host/ten_.../echo-server")

    It is passed through untouched — the adapters resolve it, so one rule
    governs both and neither can drift into its own parse.
    """
    # ⚠ **Before the seam detection, because the detection's own miss RAISES.**
    # Both adapters carry this guard too — they are the shipped public entry
    # points and are called directly — but leaving it out here would keep one
    # throw path alive under a switch whose whole promise is that it cannot
    # break a boot: hand this function something it does not recognise with
    # BATON_DISABLED set, and it would still ``TypeError``.
    switch = capture_disabled()
    if switch is not None:
        # ``config.sink`` passed through for the same reason the adapters do
        # it: a sink the caller constructed is theirs to have closed, and this
        # door returns before either adapter is reached.
        return disabled_handle(switch, "install_baton", config.sink if config else None)

    is_fastmcp = _has_fastmcp_middleware_seam(server)

    # Checked FIRST and on its own. ``fastmcp`` 2.x carries a ``_tool_manager``
    # as well, so requiring the registry to be absent here refuses every server
    # on the declared floor — which is exactly what 0.7.0 shipped. The official
    # SDK has never exposed ``add_middleware`` on either major, so its presence
    # is decisive rather than merely suggestive.
    if is_fastmcp:
        from baton.integrations.standalone import install_baton as _install

        logger.debug("baton: routing to the standalone fastmcp adapter")
        return _install(server, config, dsn=dsn)

    if _has_official_tool_registry(server):
        from baton.integrations.official import install_baton as _install

        logger.debug("baton: routing to the official mcp SDK adapter")
        return _install(server, config, dsn=dsn)

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
