"""Per-runtime ``_meta`` heuristics — detect which agent runtime is calling
the vendor's MCP server.

Shared by BOTH adapters. It used to live under ``standalone/``, which is why
the official adapter never called it and shipped ``agent_runtime: "unknown"``
unconditionally on every event since the package was split.

Per SPEC §5.2: the SDK reads agent-supplied identifiers from MCP's ``_meta``
field on each tool call. Different runtimes populate this differently
(Claude Code adds ``claudecode/toolUseId``; Cursor only sets
``progressToken``; Claude Desktop sets nothing). This module distills the
spike-validated detection rules into one function.

**There is no ``io.baton/agent_runtime`` override any more — removed
2026-09-09 with the rest of the ``io.baton/*`` keys.** It let a caller assert
its own runtime, but no client anywhere ever set it (checked across all eight
repos) and ``instructions.py`` never told one it existed, so the only discovery
path was reading the spec. Its cost was not hypothetical: **B5 happened because
that one key had two documented spellings and no users to notice the
contradiction.** If a real need appears — the strongest candidate is a gateway
asserting the true agent behind a ``clientInfo`` that names the gateway — bring
it back deliberately, with something that tells clients it exists.

⚠ **Call this on the RAW ``_meta``, before the vendor's scrubber runs.** The
default scrubber is an identity no-op, so detecting from the scrubbed dict
passes every test in this repo and fails only at a vendor whose scrubber
touches meta keys — which is the shape of bug this module was moved here to
stop, not one to reintroduce one layer down.
"""

from __future__ import annotations

from typing import Any


def meta_to_dict(meta: Any) -> dict[str, Any] | None:
    """Normalize an MCP ``_meta`` value to a plain dict.

    Accepts a dict, an MCP ``RequestParams.Meta`` model, or ``None``. Uses
    ``by_alias=True`` so namespaced keys like ``claudecode/toolUseId`` survive
    the dump (they are model extras whose JSON form is the alias).
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta
    if hasattr(meta, "model_dump"):
        return meta.model_dump(by_alias=True)  # type: ignore[no-any-return]
    return None


def detect_agent_runtime(meta: Any) -> str | None:
    """Return the detected agent runtime, or ``None`` if no signal.

    Accepts either a dict or an MCP ``RequestParams.Meta`` (it normalizes).

    Detection precedence:
    1. Heuristic on key prefixes (e.g., ``claudecode/*`` → ``claude-code``)
    2. ``None`` if nothing matches; caller substitutes a default

    Everything this returns is a constant the SDK controls, never client text —
    which is why nothing here is scrubbed or length-capped. That stops being
    true the moment a tier reads a client-supplied value (``clientInfo.name``
    is the one coming); such a tier owns its own scrub and cap.
    """
    meta_dict = meta_to_dict(meta)
    if not meta_dict:
        return None

    # Heuristic: namespace prefixes from runtime-specific _meta keys
    for key in meta_dict:
        if isinstance(key, str) and key.startswith("claudecode/"):
            return "claude-code"

    return None
