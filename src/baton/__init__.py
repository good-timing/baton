"""Baton SDK — structured signal capture for agent-mediated tool use.

Thin event capture surface with pluggable sinks. See ``docs/SPEC.md`` (the
wire protocol) and ``docs/CHARTER.md`` (load-bearing decisions). The
capture / interpretation / egress separation is documented in SPEC §11.

Core (always installed):

- ``__version__`` — embedded in every emitted event's ``sdk_version`` field
- ``Client``, ``AsyncClient``, ``SignalType`` — library API for Skill-instrumented
  agent code (see the "Library API" section in ``README.md``)

Integrations (optional, opt-in via pip extras):

- ``baton.integrations.fastmcp`` — wraps a vendor's standalone
  ``fastmcp.FastMCP`` server. Install with ``pip install baton-sdk[fastmcp]``.
  Exposes ``install_baton``, ``VendorConfig``, ``BatonHandle``.
  (The official ``mcp.server.fastmcp.FastMCP`` adapter at
  ``baton.integrations.mcp`` arrives in the next release.)

Pre-1.0 — public API not yet stable; breaking changes flagged in SPEC §13.
"""

from __future__ import annotations

__version__ = "0.2.4"
"""SDK version. Embedded in every emitted event's ``sdk_version`` field."""

__all__ = [
    "AsyncClient",
    "AsyncTrace",
    "BatonExtension",
    "BatonHandle",
    "Client",
    "SignalType",
    "Trace",
    "__version__",
]


# Import the library API at module bottom so __version__ above is available
# to events.py (which imports it via ``from baton import __version__``).
# Trace + AsyncTrace are re-exported so typed callers can write
# ``def f(t: baton.Trace) -> ...`` without reaching into ``baton.client``.
from baton.client import AsyncClient, AsyncTrace, Client, SignalType, Trace  # noqa: E402
from baton.extension import BatonExtension, BatonHandle  # noqa: E402
