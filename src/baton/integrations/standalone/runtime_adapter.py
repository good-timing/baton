"""Per-runtime ``_meta`` heuristics — detect which agent runtime is calling
the vendor's MCP server.

Per SPEC §5.2: the SDK reads agent-supplied identifiers from MCP's ``_meta``
field on each tool call. Different runtimes populate this differently
(Claude Code adds ``claudecode/toolUseId``; Cursor only sets
``progressToken``; Claude Desktop sets nothing). This module distills the
spike-validated detection rules into one function.

Vendors / clients MAY override the heuristic by setting
``_meta.baton.agent_runtime`` explicitly — useful when shipping into a
known runtime (e.g., a Claude Code plugin that wants to assert its
identity).
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
    1. Explicit ``_meta.baton.agent_runtime`` (vendor/client override)
    2. Heuristic on key prefixes (e.g., ``claudecode/*`` → ``claude-code``)
    3. ``None`` if nothing matches; caller substitutes a default
    """
    meta_dict = meta_to_dict(meta)
    if not meta_dict:
        return None

    # Explicit override via _meta.baton.agent_runtime
    baton_meta = meta_dict.get("baton")
    if isinstance(baton_meta, dict):
        runtime = baton_meta.get("agent_runtime")
        if isinstance(runtime, str) and runtime:
            return runtime

    # Heuristic: namespace prefixes from runtime-specific _meta keys
    for key in meta_dict:
        if isinstance(key, str) and key.startswith("claudecode/"):
            return "claude-code"

    return None
