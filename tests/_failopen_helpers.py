"""Shared rig for the scrubber fail-open suites.

Three things lived in two or three copies before this module existed, and each
copy was a place the next change had to be made again: a repr-contains throwing
scrubber, the `fastmcp` floor's error-flag predicate, and the error-result
builders for the two `mcp` majors.

⚠ **NOTHING here may import `fastmcp` at module scope.** `tests/integrations/
official/` is run by the `mcp-matrix` CI job against `[mcp,test]`, a tree that
has no `fastmcp` and that the job asserts is fastmcp-free — so a module-scope
import here turns the whole directory's collection into one error, not one
failed test. Measured: `0 items collected` on every matrix leg. The constraint is
documented in `test_observe_transport.py` and `test_install.py` for the same
reason; this module is imported by BOTH adapters' suites, so it is the one place
that must respect it unconditionally. `fastmcp` imports go inside the functions
that need it.

⚠ The throwers here are SELECTIVE on purpose. One that throws on everything also
throws inside the `safe_emit` build thunks, which drop the event by design — so
the only surviving assertion is "the call worked" and every site becomes
indistinguishable. Throwing on one value keeps the rest of the pipeline, which
lets each leg assert that ITS field degraded. ``ThrowAll`` is the opposite tool
for the opposite question: the ``test_*fail_open_matrix.py`` files use it to ask
only "did the vendor's caller see any difference", on every surface and leg.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "FLAG_IS_EXPRESSIBLE",
    "SENTINEL",
    "SelectiveThrower",
    "ThrowAll",
    "error_call_tool_result",
    "error_result",
]

SENTINEL = "BATON_SCRUBBER_BOOM"


def _flag_is_expressible() -> bool:
    """Whether the installed `fastmcp` can express a returned error flag.

    2.14.7 is the `fastmcp-matrix` FLOOR and its `ToolResult.__init__` takes no
    `is_error` at all, so a fixture using it raises `TypeError` there rather
    than failing an assertion. `False` when `fastmcp` is absent entirely, which
    is the `mcp-matrix` tree — there is nothing to express it with.

    `test_iserror_reclassify.py::test_floor_has_no_flag_to_read` pins the
    absence positively, so the day the floor grows the field that test reddens
    rather than skipping forever. It only ever guarded the copy beside it, which
    is why this predicate has exactly one home now.
    """
    try:
        try:  # fastmcp 3.x / 4.x
            from fastmcp.tools import ToolResult
        except ImportError:  # 2.14.7 — the floor exports it from the leaf module
            from fastmcp.tools.tool import ToolResult
    except ImportError:  # no fastmcp at all — the mcp-matrix tree
        return False
    return "is_error" in getattr(ToolResult, "model_fields", {}) or hasattr(ToolResult, "is_error")


FLAG_IS_EXPRESSIBLE = _flag_is_expressible()


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


class ThrowAll:
    """Raises on every value it is handed, and remembers each one."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def saw(self, marker: str) -> bool:
        """Whether a value carrying ``marker`` reached the scrubber. A bare
        call count is satisfied by the params scrub alone on every leg."""
        return any(marker in repr(value) for value in self.seen)

    def __call__(self, value: Any) -> Any:
        self.seen.append(value)
        raise RuntimeError(SENTINEL)


def error_result(text: str) -> Any:
    """A ``fastmcp`` ``ToolResult`` carrying the error flag.

    Guard callers with ``FLAG_IS_EXPRESSIBLE`` — on the 2.14.7 floor the
    keyword does not exist. ``fastmcp`` is imported HERE, not at module scope;
    see this module's docstring.
    """
    import mcp.types as mcp_types

    try:  # fastmcp 3.x / 4.x
        from fastmcp.tools import ToolResult
    except ImportError:  # 2.14.7
        from fastmcp.tools.tool import ToolResult

    return ToolResult(
        content=[mcp_types.TextContent(type="text", text=text)],
        is_error=True,
    )


def error_call_tool_result(text: str) -> Any:
    """An ``mcp`` ``CallToolResult`` with the error flag, on either major.

    ⚠ **Branches on which name is a DECLARED FIELD, not on a ``TypeError``.**
    A try/except here is dead code that silently ships a broken fixture:
    ``mcp.types.Result`` sets ``extra="allow"``, so on 1.x
    ``CallToolResult(is_error=True)`` SUCCEEDS — parking ``is_error`` as a
    pydantic extra while the real ``isError`` field stays ``False``. Measured on
    1.27.2: ``model_dump()`` carries both ``isError: False`` and a junk
    ``is_error: True``, a shape no server produces, and the camelCase branch — the
    only one a real 1.x server can hit — never ran.
    """
    import mcp.types as mcp_types

    content = [mcp_types.TextContent(type="text", text=text)]
    if "is_error" in mcp_types.CallToolResult.model_fields:
        return mcp_types.CallToolResult(content=content, is_error=True)
    return mcp_types.CallToolResult(content=content, isError=True)
