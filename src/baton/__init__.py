"""Baton SDK — structured signal capture for agent-mediated tool use.

Thin event capture surface with pluggable sinks. See ``docs/SPEC.md`` (the
wire protocol) and ``docs/CHARTER.md`` (load-bearing decisions). The
capture / interpretation / egress separation is documented in SPEC §11.

Core (always installed):

- ``__version__`` — embedded in every emitted event's ``sdk_version`` field
- ``Client``, ``AsyncClient``, ``SignalType`` — library API for Skill-instrumented
  agent code (see the "Library API" section in ``README.md``)
- ``Principal`` — the return type of a ``VendorConfig.resolve_user`` hook.
  Exported because that hook cannot be written without constructing one, unlike
  ``resolve_session_id``, which returns a plain ``str``.

Integrations (optional, opt-in via pip extras):

Two MCP adapters ship, one per library. Both libraries call their server class
``FastMCP``, so choose by the import in your server, not by the class name:

- ``baton.integrations.official`` — the official Anthropic ``mcp`` package
  (``mcp.server.fastmcp.FastMCP`` on 1.x, ``mcp.server.mcpserver.MCPServer``
  on 2.x). ``pip install baton-sdk[mcp]``.
- ``baton.integrations.standalone`` — the standalone ``fastmcp`` library, a
  different project. ``pip install baton-sdk[fastmcp]``.

Both expose ``install_baton``, ``VendorConfig``, ``BatonHandle``, and both
refuse the other library's server up front, naming the right adapter.

Pre-1.0 — public API not yet stable; breaking changes flagged in SPEC §13.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Embedded in every emitted event's ``sdk_version`` field. Derived from
# pyproject.toml via importlib.metadata — single source of truth, no
# hand-bumped constant to drift against the package version. The
# "0.0.0+local" fallback only applies when running from a checkout
# without `pip install -e .` present; CI/pip/sdist installs all carry
# the resolved dist-info version.
__version__: str
try:
    __version__ = _pkg_version("baton-sdk")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = [
    "AsyncClient",
    "AsyncTrace",
    "Client",
    "Principal",
    "SignalType",
    "Trace",
    "VendorConfig",
    "__version__",
    "install_baton",
]


# Import the library API at module bottom so __version__ above is available
# to events.py (which imports it via ``from baton import __version__``).
# Trace + AsyncTrace are re-exported so typed callers can write
# ``def f(t: baton.Trace) -> ...`` without reaching into ``baton.client``.
from baton.client import AsyncClient, AsyncTrace, Client, SignalType, Trace
from baton.identity import Principal

# One entry point for both adapters, detecting on structure — see
# ``baton.install``. Imported last: it pulls in ``integrations`` lazily inside
# the call, so neither optional extra is required to import ``baton``.
from baton.install import install_baton
from baton.integrations._config import VendorConfig
