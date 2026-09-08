"""Baton integrations — protocol/runtime-specific entry points.

Each integration wraps a particular agent surface (MCP, future Managed Agents,
future A2A, etc.) and adapts it to the core Baton event-emitter substrate.
Optional dependencies are declared via pip extras in ``pyproject.toml``:

    pip install baton-sdk[mcp]              # official Anthropic ``mcp`` SDK
    pip install baton-sdk[fastmcp]          # standalone ``fastmcp`` library
    pip install baton-sdk[http]             # HttpSink (POST to a collector)

There are TWO MCP adapters because there are two different libraries, and
both name their server class ``FastMCP`` — pick by your import, not by the
name:

    from mcp.server.fastmcp import FastMCP   # mcp 1.x  -> baton.integrations.official
    from mcp.server.mcpserver import MCPServer  # mcp 2.x -> baton.integrations.official
    from fastmcp import FastMCP              # standalone -> baton.integrations.standalone

Passing a server to the wrong adapter is refused before anything is mutated,
with a message naming the right one. Simpler still, ``baton.install_baton``
picks for you by inspecting the server object.

The folder names and the extra names deliberately DIFFER: an extra names a
PyPI distribution (``mcp``, ``fastmcp``) and a folder names our adapter for
it. The folders used to carry the distribution names too, which is exactly
what made the mixup easy — the official SDK's server class is also called
``FastMCP``, so a class-name match sent people to the wrong one. The old
paths ``baton.integrations.mcp`` and ``baton.integrations.fastmcp`` survive
as silent aliases for one release.

Not yet built, no extra published: Anthropic Managed Agents, A2A.

Core SDK (``baton.Client``, emitter, events, scrub) lives at the top level
and does not depend on any specific integration.
"""
