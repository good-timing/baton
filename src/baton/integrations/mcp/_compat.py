"""Compat shims for the official mcp SDK's 1.x → 2.0 rename.

mcp 2.0 (upstream PR #1951) renamed the server class and moved one private
attribute:

- ``mcp.server.fastmcp.FastMCP`` → ``mcp.server.mcpserver.MCPServer``
- the server-instructions backing ``_mcp_server`` → ``_lowlevel_server``

Everything else the adapter reaches into is preserved **byte-for-byte** across
the rename (verified on mcp 2.0.0): ``_tool_manager._tools``, ``Tool.run`` /
``parameters`` / ``fn`` / ``is_async`` / ``fn_metadata``, ``ToolManager.add_tool``
/ ``list_tools``, and the ``tool()`` decorator's ``name`` / ``description``
kwargs. So the injection + wrap layers (`_tool_wrap`, `_registry`) — which are
already duck-typed on ``Any`` — need no changes; only the import site and the
instructions setter route through here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baton.integrations.mcp._registry import get_tool_manager

if TYPE_CHECKING:
    # The server class is chosen at RUNTIME from whichever mcp major is
    # installed, so its static type differs per environment — and one mypy run
    # can only see one of them. Annotating either concrete class makes the
    # other environment's run wrong, which is what the previous
    # ``type: ignore`` here did: correct under 1.x, and under 2.x three errors
    # (a missing ``FastMCP`` attribute, the suppression itself reported
    # unused, and an untyped ``tool()`` decorator downstream). Since CI's
    # unpinned ``mcp>=1.20,<3`` resolves to whatever is newest, the pin was
    # guaranteed to rot on the next major.
    #
    # ``Any`` is the honest annotation, not a suppression: everything the
    # adapter reaches for on this object is the private internals listed in
    # the module docstring, already duck-typed on ``Any`` in ``_tool_wrap``
    # and ``_registry``. The version matrix — not mypy — is what proves those
    # internals still exist, and `mcp-matrix` runs the tests on 1.20 / 1.25 /
    # 1.27 / 2.0.
    MCPServerClass = Any
else:
    try:  # mcp <2
        from mcp.server.fastmcp import FastMCP as MCPServerClass
    except ImportError:  # mcp >=2.0 renamed the module + class
        from mcp.server.mcpserver import MCPServer as MCPServerClass

__all__ = [
    "MCPServerClass",
    "get_lowlevel_server",
    "require_high_level_server",
    "set_server_instructions",
]


def _looks_like_standalone_fastmcp(mcp: Any) -> bool:
    """True if this is the *standalone* ``fastmcp`` library's server rather
    than the official SDK's.

    Sniffed by module path, not by importing ``fastmcp`` — it is an optional
    extra and this adapter must never import it. Worth a dedicated branch
    because the mixup is easy to make and invisible once made: both libraries
    name the class ``FastMCP``, and the two ``install_baton`` entry points take
    the same arguments, so the only thing that differs is which import line the vendor
    copied.
    """
    return type(mcp).__module__.split(".")[0] == "fastmcp"


def require_high_level_server(mcp: Any) -> None:
    """Refuse anything that isn't a high-level server, BEFORE install mutates it.

    ``install_baton`` needs FastMCP/MCPServer internals in two places, and the
    two fail very differently. The low-level-server lookup is best-effort
    (surface-snapshot capture degrades to an empty ``server_meta``), so on a
    bare ``Server`` its careful error was only ever a logged traceback. Then
    ``set_server_instructions`` *succeeds* — on a bare ``Server``
    ``instructions`` is a plain settable attribute, not the read-only property
    it is on FastMCP/MCPServer, so the public-setter fast path returns before
    the compat helper is consulted. Install finally died in ``install_wraps``
    on a raw ``'Server' object has no attribute '_tool_manager'``, by which
    point the server was already advertising an annotation tool that never got
    registered. That is a half-install, not a refusal.

    So the refusal happens here instead, on the seam install genuinely cannot
    do without: the tool registry. Probed through ``_registry`` rather than by
    reaching for ``_tool_manager`` directly, because that module is the single
    swap point for the attribute (see its docstring) — a second hardcoded copy
    here would survive an upstream rename and start refusing every legitimate
    server.

    Duck-typed rather than ``isinstance``: everything downstream is duck-typed
    on ``Any``, and a vendor proxy that exposes the internals should install.

    ``TypeError``, not ``AttributeError``: this is a wrong-shaped argument, and
    it must not be swallowed by the best-effort ``except AttributeError``
    around the surface-snapshot capture.
    """
    try:
        manager = get_tool_manager(mcp)
    except AttributeError:
        manager = None
    if manager is not None:
        return

    if _looks_like_standalone_fastmcp(mcp):
        raise TypeError(
            "baton: this is the standalone ``fastmcp`` library's FastMCP, but "
            "``baton.integrations.mcp.install_baton`` adapts the official mcp "
            "SDK's server — different library, different hook mechanism "
            "(middleware vs. tool-handler wrapping). Use "
            "``baton.integrations.fastmcp.install_baton`` instead; same "
            "signature, same VendorConfig."
        )
    raise TypeError(
        "baton: install_baton needs the official mcp SDK's high-level server, "
        "and this object has no tool registry. install_baton takes FastMCP "
        "(mcp 1.x) or MCPServer (mcp 2.x) and reaches down into its internals. "
        "A bare ``mcp.server.Server`` — the low-level API the reference servers "
        "(git, time, fetch) are written against — has none of them and is not "
        "supported yet. If this IS a FastMCP/MCPServer, then it is a version "
        "problem: pin mcp>=1.20,<3."
    )


def get_lowlevel_server(mcp: Any) -> Any:
    """Return the official low-level ``mcp.server.lowlevel.server.Server``
    backing this ``FastMCP``/``MCPServer`` instance, across the mcp 1.x/2.0
    attribute rename (``_mcp_server`` → ``_lowlevel_server``).

    Fails loud on an unknown layout, same rationale as
    ``set_server_instructions`` below — callers (surface-snapshot capture,
    instructions writes) depend on finding the real backing object.
    """
    backing = getattr(mcp, "_mcp_server", None)
    if backing is None:
        backing = getattr(mcp, "_lowlevel_server", None)
    if backing is None:
        raise AttributeError(
            "baton: this object has no low-level server backing (tried "
            "``_mcp_server`` and ``_lowlevel_server``). On the official SDK's "
            "FastMCP/MCPServer that backing always exists, so if that is what "
            "you built with, this is a version problem: pin mcp>=1.20,<3. "
            "Otherwise it is the shape: install_baton takes the high-level "
            "server and reaches down into it, and a bare ``mcp.server.Server`` "
            "— the low-level API the reference servers (git, time, fetch) are "
            "written against — has no such backing and is not supported yet."
        )
    return backing


def set_server_instructions(mcp: Any, instructions: str) -> None:
    """Write server instructions across the mcp 1.x/2.0 backing rename.

    ``instructions`` is a **read-only** property on both versions; the writable
    backing is ``_mcp_server`` (mcp 1.x) or ``_lowlevel_server`` (mcp 2.0). We
    still try the public setter first in case a future release makes it
    writable. Fails loud on an unknown layout — server instructions are
    load-bearing on instruction-aware runtimes, so silently dropping them is
    worse than a clear error the vendor can pin around.
    """
    try:
        mcp.instructions = instructions
        return
    except AttributeError:
        pass
    get_lowlevel_server(mcp).instructions = instructions
