"""Shared rig for the scrubber fail-open suites.

Three things lived in two or three copies before this module existed, and each
copy was a place the next change had to be made again:

- **`SelectiveThrower`** — a repr-contains scrubber that raises. `_DeepThrower`
  was a second implementation of `tests/test_client_fail_open.py`'s class, which
  now lives here and is imported by all three suites.
- **`FLAG_IS_EXPRESSIBLE`** — whether the installed `fastmcp` can express a
  returned error flag at all. The 2.14.7 FLOOR cannot, so a fixture using it
  raises `TypeError` there rather than failing an assertion. The positive pin
  (`test_iserror_reclassify.py::test_floor_has_no_flag_to_read`) only ever
  guarded the copy beside it, so a second copy could drift silently through
  exactly the floor bump that pin exists for.
- **`error_call_tool_result`** — the snake-then-camel `CallToolResult` dance for
  the `mcp` majors.

⚠ The throwers here are SELECTIVE on purpose. One that throws on everything also
throws inside the `safe_emit` build thunks, which drop the event by design — so
the only surviving assertion is "the call worked" and every site becomes
indistinguishable. Throwing on one value keeps the rest of the pipeline, which
lets each leg assert that ITS field degraded. (The throw-EVERYTHING matrix is a
separate, still-unbuilt thing: see `fail_open_capture_boundary.md`.)
"""

from __future__ import annotations

from typing import Any

SENTINEL = "BATON_SCRUBBER_BOOM"

try:  # fastmcp 3.x / 4.x
    from fastmcp.tools import ToolResult as _ToolResult
except ImportError:  # 2.14.7 — the floor exports it only from the leaf module
    from fastmcp.tools.tool import ToolResult as _ToolResult

FLAG_IS_EXPRESSIBLE = "is_error" in getattr(_ToolResult, "model_fields", {}) or hasattr(
    _ToolResult, "is_error"
)


class SelectiveThrower:
    """Identity, except on values whose repr contains ``trip_on``.

    ``repr`` rather than the value itself so one thrower covers both a bare
    string (what the RETURN projection scrubs) and a serialised envelope (what
    the END projection scrubs).

    ``tripped_on`` records WHAT fired. ``tripped`` alone cannot tell the two
    result projections apart — both scrub a value whose repr carries the same
    reason — so a RETURN-leg probe passes unchanged when only the END
    projection ran, which is what happens wherever the error flag is
    unreadable. The tripped value's TYPE is the discriminator.
    """

    def __init__(self, trip_on: str) -> None:
        self.trip_on = trip_on
        self.tripped = 0
        self.tripped_on: Any = None

    def __call__(self, value: Any) -> Any:
        if self.trip_on in repr(value):
            self.tripped += 1
            self.tripped_on = value
            raise RuntimeError(SENTINEL)
        return value


def error_result(text: str) -> Any:
    """A ``fastmcp`` ``ToolResult`` carrying the error flag.

    Guard with ``FLAG_IS_EXPRESSIBLE`` — on the 2.14.7 floor this raises.
    """
    import mcp.types as mcp_types

    return _ToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        is_error=True,
    )


def error_call_tool_result(text: str) -> Any:
    """An ``mcp`` ``CallToolResult`` with the error flag, on either major.

    Snake-first, matching the production detection order and for the same
    reason: it is the spelling the current generation uses. Hardcoding one
    couples a test to one major and the `mcp-matrix` spans both.
    """
    import mcp.types as mcp_types

    content = [mcp_types.TextContent(type="text", text=text)]
    try:
        return mcp_types.CallToolResult(content=content, is_error=True)
    except Exception:
        return mcp_types.CallToolResult(content=content, isError=True)
