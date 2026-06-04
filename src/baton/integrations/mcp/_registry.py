"""Resolver for the official ``mcp.server.fastmcp`` tool registry.

Centralizes access to ``mcp._tool_manager._tools`` so the rest of the
adapter doesn't reach into private internals directly. When upstream PR
#1951 lands (``FastMCP`` → ``MCPServer``, ``mcp.server.fastmcp.*`` →
``mcp.server.mcpserver.*``), only this file changes — the internal struct
(``_tool_manager``, ``_tools`` dict, ``Tool.fn``/``Tool.is_async``) is
preserved byte-for-byte across the rename.

Per spike: the internal struct has been bit-stable across mcp v1.10 →
v1.27 (one year, six tagged releases checked). No churn.
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
