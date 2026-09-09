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

Vendors / clients MAY override the heuristic by setting
``_meta["io.baton/agent_runtime"]`` explicitly — useful when shipping into a
known runtime (e.g., a Claude Code plugin that wants to assert its identity).

⚠ **Call this on the RAW ``_meta``, before the vendor's scrubber runs.** The
default scrubber is an identity no-op, so detecting from the scrubbed dict
passes every test in this repo and fails only at a vendor whose scrubber
touches meta keys — which is the shape of bug this module was moved here to
stop, not one to reintroduce one layer down.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: The vendor/client override key. Reverse-DNS per the MCP ``_meta``
#: convention and per SPEC §5.2's key table, which is the form a vendor
#: reading the spec will send.
#:
#: This replaced a nested ``_meta["baton"]["agent_runtime"]`` dict, which no
#: MCP convention produces and which SPEC §5.2 contradicted itself about — the
#: key table said ``io.baton/agent_runtime`` while a prose line at the end of
#: the same section said ``_meta.baton.*``. The code followed the prose, so a
#: vendor asserting its runtime the way the table documents was silently
#: ignored. The prose line is the one that was wrong; it now names this key.
#:
#: Safe to change outright rather than accept both: nothing SENDS the nested
#: form. Checked across all eight repos — the only senders were this repo's
#: own middleware test and baton-proxy's, which reads its own copy of this
#: heuristic and is unaffected. There are no customers, and ``instructions.py``
#: never told a client to set it. Accepting both would have enshrined two wire
#: shapes for one assertion as compatibility for an audience of zero.
AGENT_RUNTIME_META_KEY = "io.baton/agent_runtime"

#: Cap on the override VALUE. The key is read from untrusted client input and
#: its value is copied onto every event of the call, so an unbounded string is
#: copied into every ``HttpSink`` payload too. 128 is far above any real runtime
#: name (``claude-code`` is 11) and far below anything worth shipping. Same
#: posture as ``error_body``, the other untrusted string on this wire, which is
#: both scrubbed and truncated at 2000.
AGENT_RUNTIME_MAX_LEN = 128


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


def detect_agent_runtime(meta: Any, scrubber: Callable[[Any], Any] | None = None) -> str | None:
    """Return the detected agent runtime, or ``None`` if no signal.

    Accepts either a dict or an MCP ``RequestParams.Meta`` (it normalizes).

    Detection precedence:
    1. Explicit ``_meta["io.baton/agent_runtime"]`` (vendor/client override)
    2. Heuristic on key prefixes (e.g., ``claudecode/*`` → ``claude-code``)
    3. ``None`` if nothing matches; caller substitutes a default

    ``scrubber`` is applied to the OVERRIDE VALUE only — never to the detection
    input, and never to a value this function derived itself. The distinction is
    the point: the raw ``_meta`` must reach the key scan (a vendor scrubber that
    touches meta keys would otherwise turn detection off), but the override's
    value is an arbitrary client-supplied string that lands verbatim on
    ``agent_runtime`` for every event of the call. A vendor whose scrubber
    redacts identifiers reasonably expects it to cover that field, and without
    this a client could put an email or a user id there and have it ship raw.
    Scrubbing the heuristic's own ``"claude-code"`` would be the opposite
    mistake — mangling a constant we control.
    """
    meta_dict = meta_to_dict(meta)
    if not meta_dict:
        return None

    # Explicit override via _meta["io.baton/agent_runtime"]
    override = meta_dict.get(AGENT_RUNTIME_META_KEY)
    if isinstance(override, str) and override:
        if scrubber is not None:
            override = str(scrubber(override))
        # Re-checked after scrubbing: a scrubber may return an empty string,
        # and an empty override must fall through to the heuristic rather than
        # becoming the reported runtime.
        if override:
            return override[:AGENT_RUNTIME_MAX_LEN]

    # Heuristic: namespace prefixes from runtime-specific _meta keys
    for key in meta_dict:
        if isinstance(key, str) and key.startswith("claudecode/"):
            return "claude-code"

    return None
