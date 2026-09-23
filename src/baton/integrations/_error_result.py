"""Detecting MCP's returned error flag, shared by both adapters.

SPEC §11.4.3. **A failed MCP tool call is a 200.** The protocol files it as a
successful JSON-RPC response whose ``CallToolResult`` body sets the error flag;
a JSON-RPC error means a protocol fault, not a tool failure. So classifying on
exceptions alone — which is what §6.1 used to say, and what both adapters did —
files real failures as successes.

⚠ **The flag has two spellings, and which one appears depends on the library
VERSION, not on which adapter is running.** Measured 2026-09-22 across eight
(library, version) cells; the table lives in SPEC §11.4.3. In short: official
``mcp`` 1.20 to 1.27.x publish ``isError``, official ``mcp`` 2.x publishes
``is_error`` (the ``mcp_types`` rewrite), standalone ``fastmcp`` 3.x/4.x
publish ``is_error``, and ``fastmcp`` 2.14.7 has no such field at all. On that
floor there is nothing to read and nothing to reclassify, which is correct
rather than a gap — ``is_error_result`` simply answers False.

Everything here is duck-typed, never ``isinstance``-checked, for the reason
``_is_mrtr_pause`` is: an import shim against one library's types would make
this inert on the other adapter and untestable on whichever venv is installed.
"""

from __future__ import annotations

from typing import Any

#: ``error_type`` for a returned error, as opposed to a raised exception whose
#: class name is used. Matches what ``baton-extmcp`` has emitted since 0.1.0 —
#: sensor parity is the point of the change that introduced this module.
TOOL_ERROR_TYPE = "tool_error"

# ⚠ No cap here, deliberately. Both callers truncate AFTER scrubbing, matching
# the raise path's `str(scrubber(str(exc)))[:2000]`. Cutting inside this helper
# would put the truncation BEFORE the scrubber for one of the two failure
# shapes and after it for the other — and a PII value straddling the boundary
# would reach the scrubber as a fragment its pattern cannot match, so the half
# that survives the cut ships unredacted. The order has to be the same for both
# shapes, and scrub-then-cut is the one that is safe.


def is_error_result(value: Any) -> bool:
    """True if ``value`` is a tool result carrying MCP's error flag.

    ⚠ **``is_error`` is probed BEFORE ``isError``, and the order is
    load-bearing rather than cosmetic.** ``fastmcp`` answers a camelCase
    attribute through a compatibility shim that emits a deprecation warning, so
    asking for ``isError`` first would warn on every error result any
    ``fastmcp`` server produces. Asking for ``is_error`` first short-circuits
    before the shim is ever reached, and an ``mcp`` 1.x ``CallToolResult`` —
    which has only the camelCase name — still falls through to it.

    ⚠ **A list-valued ``content`` is required, and that is the NEGATIVE
    control's rule.** Without it, any object that happens to carry an
    ``is_error`` attribute reads as a failed tool call. A vendor returning its
    own dict that merely spells the flag (``{"isError": True, ...}``) is
    returning domain data: neither library marks it, so neither may we.

    The whole predicate is guarded, because ``content`` may be a property on a
    lazily-proxied result that raises something other than ``AttributeError``,
    which ``getattr``'s default would not suppress. This runs on the vendor's
    tool-call path (SPEC §11.2: never block the call), so it fails to False.
    """
    try:
        if value is None:
            return False
        for name in ("is_error", "isError"):
            if hasattr(value, name):
                if not isinstance(getattr(value, "content", None), list):
                    return False
                return bool(getattr(value, name))
        return False
    except Exception:  # pragma: no cover - defensive; see docstring
        return False


def error_text(result: Any) -> str:
    """The human-readable reason inside an error result.

    Joins the ``content`` envelope's text parts, which is where a vendor puts
    the actual sentence ("You do not have sufficient access to delete this
    Project"). This is what a human reads in the Console, so the fallback is
    deliberately NOT ``str(result)`` — a pydantic repr, or worse a
    ``<ToolResult object at 0x…>`` whose address changes every run, would put
    noise where the reason belongs. An empty string says "no reason given",
    which is honest and which the Console already renders as such.
    """
    parts: list[str] = []
    try:
        content = getattr(result, "content", None)
        if isinstance(content, list):
            for part in content:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    except Exception:  # pragma: no cover - defensive; see is_error_result
        return ""
    return "\n".join(parts)


def envelope_to_jsonable(result: Any) -> Any:
    """The FULL result envelope as JSON-safe data — deliberately NOT unwrapped.

    ⚠ **This is the opposite of what ``tool_call_end`` does, on purpose.** That
    event unwraps to the developer's own return value, because that is the
    interesting half of a successful call. On a failure it is the wrong half:
    ``structured_content`` is typically ``None`` on an error result, the reason
    lives in ``content``, and the flag lives on the envelope. Unwrapping here
    is exactly the bug this module exists to fix — the official adapter's
    unwrapper dropped the envelope on ``mcp`` 2.x, which is why no stored 2.x
    row carries a flag under any spelling.

    The dump is **era-native**: no ``by_alias``, so the keys are whatever the
    installed library calls them. Normalizing the spelling here would rewrite
    the meaning of data already stored, and SPEC §11.4.3 makes era-native the
    consumer's contract instead.
    """
    if result is None:
        return None
    if hasattr(result, "model_dump"):
        try:
            return result.model_dump(mode="json")
        except Exception:
            pass
    if isinstance(result, (str, int, float, bool, list, dict)):
        return result
    return str(result)
