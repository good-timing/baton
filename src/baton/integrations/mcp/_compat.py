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

from typing import Any

try:  # mcp <2
    from mcp.server.fastmcp import FastMCP as MCPServerClass
except ImportError:  # mcp >=2.0 renamed the module + class
    # Version-conditional: this module only exists on mcp>=2.0, so the stub is
    # absent whenever type-checking runs against a 1.x env, and the rebind is a
    # deliberate fallback, not a real redefinition.
    from mcp.server.mcpserver import (  # type: ignore[import-not-found,no-redef]
        MCPServer as MCPServerClass,
    )

__all__ = ["MCPServerClass", "get_lowlevel_server", "set_server_instructions"]


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
            "baton: cannot locate the low-level server backing on this mcp "
            "version (tried ``_mcp_server`` and ``_lowlevel_server``). Pin a "
            "supported mcp release."
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
