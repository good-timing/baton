"""A connected in-memory client session for the OFFICIAL mcp SDK, portable
across the whole supported band (mcp 1.20 → 2.0).

Why this exists rather than ``mcp.shared.memory.create_connected_server_and_client_session``:
that convenience helper was REMOVED in mcp 2.0. Only
``create_client_server_memory_streams`` survives the rename, and it exists with
an identical signature on every version we support — so the session is
assembled from it here instead.

Why a real session at all, when every other test in ``tests/integrations/official/``
drives tools through ``mcp.call_tool(...)``: ``_meta`` only exists on the wire.
``call_tool`` carries none, so a test built on it reads ``agent_runtime:
"unknown"`` on a correct build and cannot tell detection working from detection
missing — which is precisely the failure mode that shipped.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from baton.integrations.official._compat import get_lowlevel_server


@asynccontextmanager
async def connected_session(mcp: Any) -> AsyncGenerator[ClientSession, None]:
    """Yield an initialized ``ClientSession`` wired to ``mcp`` over memory streams.

    ``mcp`` is the high-level server (``FastMCP`` on 1.x, ``MCPServer`` on 2.0);
    the low-level backing is resolved through ``_compat`` because that attribute
    was renamed in the same release.
    """
    server = get_lowlevel_server(mcp)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async with anyio.create_task_group() as tg:

            async def _run_server() -> None:
                await server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=False,
                )

            tg.start_soon(_run_server)
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()
