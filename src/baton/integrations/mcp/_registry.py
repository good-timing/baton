"""Resolver for the official ``mcp.server.fastmcp`` tool registry.

Centralizes access to ``mcp._tool_manager._tools`` so the rest of the
adapter doesn't reach into private internals directly. Upstream PR #1951
(shipped in mcp 2.0: ``FastMCP`` → ``MCPServer``, ``mcp.server.fastmcp.*``
→ ``mcp.server.mcpserver.*``) preserved the internal struct
(``_tool_manager``, ``_tools`` dict, ``Tool.fn``/``Tool.is_async``)
byte-for-byte across the rename — so this file needs no change for 2.0;
only the class *import* moved (see ``_compat.py``).

Per spike + verification: the internal struct has been bit-stable across
mcp v1.10 → v2.0.0 (the only 2.0 breaks were the module/class rename and
the instructions backing ``_mcp_server`` → ``_lowlevel_server``, both
handled in ``_compat.py``). No churn to the tool registry itself.
"""

from __future__ import annotations

from typing import Any


def get_tool_registry(mcp: Any) -> dict[str, Any]:
    """Return the live ``name -> Tool`` dict from the FastMCP's tool manager.

    Mutating an entry's ``fn`` (or ``is_async``) affects the running server.
    Used by ``_tool_wrap`` to install Baton emission wrappers in place.
    """
    return mcp._tool_manager._tools  # type: ignore[no-any-return]


def get_tool_manager(mcp: Any) -> Any:
    """Return the ``ToolManager`` instance.

    Needed to monkey-patch ``add_tool`` so tools registered AFTER
    ``install_baton(...)`` are also wrapped.
    """
    return mcp._tool_manager
