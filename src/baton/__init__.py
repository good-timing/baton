"""Baton SDK — structured signal capture for agent-mediated tool use.

Thin event emitter; the Console worker assembles signals, applies policy, and
dispatches. See ``docs/SPEC.md`` (the wire protocol) and ``docs/CHARTER.md``
(load-bearing decisions). The capture / interpretation / egress separation is
documented in SPEC §11.

Core (always installed):

- ``SPEC_VERSION``, ``__version__`` — version markers embedded in every event
- ``Client``, ``AsyncClient``, ``SignalType`` — library API for Skill-instrumented
  agent code (per ``docs/SKILLS_LIBRARY_API_DRAFT.md``)

Integrations (optional, opt-in via pip extras):

- ``baton.integrations.mcp`` — wraps a vendor's FastMCP server. Install with
  ``pip install baton-sdk[mcp]``. Exposes ``install_baton``, ``VendorConfig``,
  ``BatonHandle``.
- Future: ``baton.integrations.managed_agents``, ``baton.integrations.a2a``.

Pre-1.0 — public API not yet stable; breaking changes flagged in SPEC §13.
"""

from __future__ import annotations

__version__ = "0.1.0"
"""SDK version. Embedded in every emitted event's ``sdk_version`` field."""

SPEC_VERSION = "0.2"
"""Baton spec version this SDK implements. Embedded in every emitted event's
``spec_version`` field."""

__all__ = [
    "SPEC_VERSION",
    "AsyncClient",
    "AsyncTrace",
    "Client",
    "SignalType",
    "Trace",
    "__version__",
]


# Import the library API at module bottom so __version__ + SPEC_VERSION above
# are available to events.py (which imports them via `from baton import ...`).
# Trace + AsyncTrace are re-exported so typed callers can write
# ``def f(t: baton.Trace) -> ...`` without reaching into ``baton.client``.
from baton.client import AsyncClient, AsyncTrace, Client, SignalType, Trace  # noqa: E402
