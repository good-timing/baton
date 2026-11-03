"""Reading a server's tools across fastmcp and mcp versions."""

from __future__ import annotations

from typing import Any


async def list_tools(server: Any) -> list[Any]:
    """fastmcp 2.x lists with ``get_tools``, a mapping; later versions with ``list_tools``."""
    list_tools = getattr(server, "list_tools", None)
    if list_tools is not None:
        return list(await list_tools())
    return list((await server.get_tools()).values())


def tool_schema(tool: Any) -> dict[str, Any]:
    """``input_schema`` (mcp 2.x; fastmcp 4 deprecates ``inputSchema``), ``inputSchema``
    or ``parameters`` (fastmcp 2.x and 3.x)."""
    for name in ("input_schema", "inputSchema", "parameters"):
        if isinstance(getattr(tool, name, None), dict):
            return dict(getattr(tool, name))
    raise AssertionError(f"no schema on {type(tool).__name__}")
