"""Shared ``surface_snapshot`` helpers — both adapters (``baton.integrations.fastmcp``,
``baton.integrations.mcp``) build the vendor-true surface the same way; this
module owns the canonical logic so they cannot drift.

Mirrors baton-proxy's ``MessageProcessor._capture_surface`` (``proxy.py``):
snapshot ``server_info``/``capabilities``/``instructions`` + the full ``tools``
list, hash it (canonical JSON, sorted keys), and only emit on a hash change.
The hash is the identity change specs are authored against (proxy's own
``base_surface_hash`` comment) — it MUST reflect the vendor's real surface,
never anything Baton adds, or toggling e.g. ``intent_param_mode`` would
invalidate every recipe pinned to it. That's why ``build_server_meta`` MUST be
called before either adapter mutates server instructions, and why callers
exclude Baton's own injected tool(s) from ``tools`` before hashing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def surface_hash(surface: Mapping[str, Any]) -> str:
    """Content hash of the vendor-true surface, canonical-JSON keyed.

    Identical algorithm to baton-proxy's ``_surface_hash`` — must be stable
    across process restarts and key ordering, so the same server always
    dedupes to the same hash within one adapter's install. NOT guaranteed
    identical to a proxy-observed hash of the "same" surface: each producer
    serializes ``tools`` from a different shape (fastmcp's ``to_mcp_tool()``
    dump, this adapter's hand-built ``{name, description, inputSchema}``,
    proxy's raw wire JSON), so a vendor migrating between producers should
    expect a fresh row in the Console's ``vendor_surfaces`` table, not a
    continuation of the old hash's identity.
    """
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_server_meta(lowlevel_server: Any) -> dict[str, Any]:
    """Vendor-true ``server_info``/``capabilities``/``instructions``.

    Both adapters wrap the same official low-level ``mcp.server.lowlevel.
    server.Server`` (reachable via ``._mcp_server`` on the standalone
    ``fastmcp`` library and, pre-2.0, on the official SDK too — see
    ``integrations.mcp._compat.get_lowlevel_server`` for the 2.0 rename).
    ``create_initialization_options()`` reads current server state, so
    callers MUST invoke this before mutating instructions (the Baton
    suffix) — otherwise the snapshot captures Baton's own text instead of
    the vendor's, and the hash drifts with it.
    """
    opts = lowlevel_server.create_initialization_options()
    capabilities = opts.capabilities
    return {
        "server_info": {"name": opts.server_name, "version": opts.server_version},
        "capabilities": (
            capabilities.model_dump(mode="json")
            if hasattr(capabilities, "model_dump")
            else capabilities
        ),
        "instructions": opts.instructions,
    }


def assemble_surface(server_meta: Mapping[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``{server_info, capabilities, instructions, tools}`` shape both
    adapters hash — owned here so it can't drift between them (previously
    hand-built identically in both ``fastmcp/middleware.py`` and
    ``mcp/_tool_wrap.py``). ``tools`` is the caller's responsibility: already
    vendor-true (pre-injection) and, for hash stability across pure
    reordering, already sorted by name.
    """
    return {
        "server_info": server_meta.get("server_info"),
        "capabilities": server_meta.get("capabilities"),
        "instructions": server_meta.get("instructions"),
        "tools": tools,
    }


def build_seam_augmentations(
    *,
    injected_tool_names: list[str],
    intent_param_names: list[str],
    intent_param_mode: str,
) -> dict[str, Any]:
    """The as-served delta Baton added on top of the vendor-true surface —
    mirrors proxy's ``seam_augmentations`` so a consumer can render both
    layers. Always records ``instructions_suffix: True``: unlike proxy
    (where the suffix is optional), the SDK's ``build_server_instructions``
    unconditionally documents the annotation tool whenever ``install_baton``
    runs.
    """
    return {
        "injected_tools": sorted(injected_tool_names),
        "intent_param": (
            {"names": sorted(intent_param_names), "mode": intent_param_mode}
            if intent_param_mode != "off"
            else None
        ),
        "instructions_suffix": True,
    }
