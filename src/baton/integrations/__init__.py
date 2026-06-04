"""Baton integrations — protocol/runtime-specific entry points.

Each integration wraps a particular agent surface (MCP, future Managed Agents,
future A2A, etc.) and adapts it to the core Baton event-emitter substrate.
Optional dependencies are declared via pip extras in ``pyproject.toml``:

    pip install baton-sdk[mcp]              # MCP integration (FastMCP-based)
    pip install baton-sdk[managed-agents]   # future: Anthropic Managed Agents
    pip install baton-sdk[a2a]              # future: A2A protocol

Core SDK (``baton.Client``, emitter, events, scrub) lives at the top level
and does not depend on any specific integration.
"""
