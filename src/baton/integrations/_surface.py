"""Shared ``surface_snapshot`` helpers — both adapters (``baton.integrations.standalone``,
``baton.integrations.official``) and the library's ``list_tools`` build the
vendor-true surface the same way; this module owns the canonical logic so they
cannot drift.

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
from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from baton.integrations._llm_text import (
    USER_GOAL_PARAM_NAME,
    instructions_suffix,
    required_param_names,
    served_agent_text,
)

if TYPE_CHECKING:
    from baton._overlays import Overlay
    from baton.integrations._annotation_name import AnnotationNames

SENT_SURFACES_CAP = 256


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


class SentSurfaces:
    """The last ``SENT_SURFACES_CAP`` surface hashes sent, least recent dropped
    first. Bounded because a tool list that differs per caller would otherwise
    grow it for the life of the process; a dropped hash is only sent again."""

    def __init__(self) -> None:
        self._hashes: OrderedDict[str, None] = OrderedDict()
        self._server_overlay: str | None = None

    def was_sent(self, digest: str, server_overlay: str | None = None) -> bool:
        """``server_overlay`` is not in ``surface_hash``, and a snapshot is sent
        again whenever it changes (SPEC §11.4.2)."""
        if server_overlay != self._server_overlay or digest not in self._hashes:
            return False
        self._hashes.move_to_end(digest)
        return True

    def mark(self, digest: str, server_overlay: str | None = None) -> None:
        if server_overlay != self._server_overlay:
            self._hashes.clear()
            self._server_overlay = server_overlay
        self._hashes[digest] = None
        if len(self._hashes) > SENT_SURFACES_CAP:
            self._hashes.popitem(last=False)

    def unmark(self, digest: str) -> None:
        self._hashes.pop(digest, None)


def build_server_meta(lowlevel_server: Any) -> dict[str, Any]:
    """Vendor-true ``server_info``/``capabilities``/``instructions``.

    Both adapters wrap the same official low-level ``mcp.server.lowlevel.
    server.Server`` (reachable via ``._mcp_server`` on the standalone
    ``fastmcp`` library and, pre-2.0, on the official SDK too — see
    ``integrations.official._compat.get_lowlevel_server`` for the 2.0 rename).
    ``create_initialization_options()`` reads current server state, so
    callers MUST invoke this before mutating instructions (the Baton
    suffix) — otherwise the snapshot captures Baton's own text instead of
    the vendor's, and the hash drifts with it.
    """
    opts = lowlevel_server.create_initialization_options()
    capabilities = opts.capabilities
    return {
        "server_info": {"name": opts.server_name, "version": opts.server_version},
        # by_alias: mcp 2.x names these fields snake_case with the camelCase wire
        # name as the alias (a no-op on 1.x). It fixes the key names only — the
        # two majors still report different fields, so hashes still differ.
        "capabilities": (
            capabilities.model_dump(mode="json", by_alias=True)
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
    required_names: list[str],
    agent_text: dict[str, Any],
) -> dict[str, Any]:
    """The as-served delta Baton added on top of the vendor-true surface —
    mirrors proxy's ``seam_augmentations`` so a consumer can render both
    layers.

    ``required_names`` is which of ``names`` the mode advertises as required.
    It is per surface, not per tool: a tool that declares one of the names
    itself is not escalated and is still covered by this list.
    """
    return {
        "injected_tools": sorted(injected_tool_names),
        "intent_param": {
            "names": sorted(intent_param_names),
            "required_names": sorted(required_names),
            "mode": intent_param_mode,
        },
        "instructions_suffix": instructions_suffix(agent_text),
        "agent_text": agent_text,
    }


def adapter_seam_augmentations(
    *,
    names: AnnotationNames | None,
    intent_param_mode: str,
    surface_tools: list[Any],
    applies_overlays: bool,
    server_overlay: Overlay | None = None,
) -> dict[str, Any]:
    """``build_seam_augmentations`` for an MCP adapter, which serves the
    instructions and, with Console settings, applies the server overlay."""
    seam = build_seam_augmentations(
        injected_tool_names=[names.annotation_tool_name] if names else [],
        intent_param_names=[USER_GOAL_PARAM_NAME],
        intent_param_mode=intent_param_mode,
        required_names=list(required_param_names(intent_param_mode=intent_param_mode)),
        agent_text=served_agent_text(
            names=names,
            intent_param_mode=intent_param_mode,
            surface_tools=surface_tools,
            server_overlay=server_overlay,
        ),
    )
    if applies_overlays:
        seam["server_overlay"] = server_overlay.id if server_overlay is not None else None
    return seam
