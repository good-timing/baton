"""The result-capture mode: its vocabulary, and the success leg's projection.

`VendorConfig.result_capture_mode` / `Client(result_capture_mode=...)` is how a
vendor declares it; `result_capture` is what reaches the wire (SPEC §11.4). The
rationale for the mode — why a string and not a bool, why a declaration and not
a scrub rule, what `"off"` keeps and what it costs — lives in SPEC §11.4 and in
`VendorConfig.result_capture_mode`'s own docstring, and is not restated here.

Top-level and dependency-free, because `baton.client` imports only `baton._*`
modules and needs this vocabulary too. The RETURN-failure projection lives in
`baton.integrations._error_result` instead, next to the `error_text` and
`envelope_to_jsonable` it is built from.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

__all__ = [
    "WITHHELD",
    "ResultCaptureMode",
    "ResultFields",
    "end_result_fields",
    "validate_mode",
    "withholding",
]

WITHHELD = "off"
"""The one registered non-default value, on the wire and in the config alike.

SPEC §11.4 registers `"off"` and reserves a second value for the content
ladder's partial rung — which is why nothing here tests `!= "off"` to mean
"capture", and why the member is a string rather than a boolean.
"""

ResultCaptureMode = str
"""What a vendor may set: `"full"` or `WITHHELD`.

An alias rather than a `Literal`, matching `intent_param_mode`, which is a
plain `str` validated once at the config door.
⚠ If the partial rung lands and a third value has to be threaded, narrowing
this to a `Literal` and letting mypy-strict enforce it at the adapter-internal
seams is the stronger move than re-validating at each one.
"""

_MODES: frozenset[str] = frozenset({"full", WITHHELD})


def withholding(mode: str) -> bool:
    """Whether `mode` withholds result-derived data.

    A POSITIVE test, deliberately. The partial rung, when it lands, is a third
    mode that also has no full body to emit, so `mode != "full"` would have to
    be revisited at every call site that day.
    """
    return mode == WITHHELD


def validate_mode(mode: str, *, field: str) -> ResultCaptureMode:
    """Refuse an unregistered mode, naming the field the caller set.

    Refused HERE rather than at emit: an unregistered value reads as "not off"
    to every downstream test, so a typo captures everything and nothing says
    so. A door that throws is the only place the vendor finds out.

    Called at the two DOORS a vendor can reach — `VendorConfig` (via
    `resolve_config`) and `Client` / `AsyncClient` (via
    `_resolve_client_config`) — and nowhere else. The adapter-internal seams
    that take the mode onward are not re-checked, matching the two sibling
    modes.

    ⚠ **NOTHING holds the chain between — stated plainly because two earlier
    versions of this docstring claimed something that does.** It said
    mypy-strict covered it: `ResultCaptureMode` is a bare `str` alias (line 37),
    so mypy accepts any string in the slot, including one meant for
    `intent_param_mode`. It then said the seams are
    private: `BatonMiddleware` imports fine from
    `baton.integrations.standalone.middleware`, a path with no underscore in
    it, and `integrations/fastmcp.py` re-exports it deliberately, naming it
    "the obvious candidate" for an external caller. Only `install_wraps` is
    genuinely private.

    So the reachable hole, unguarded: a vendor importing `BatonMiddleware`
    directly and passing `result_capture_mode="Off"` gets no error,
    `withholding()` answers False, and every result body is captured. The
    behaviour is deliberate and pinned by
    `test_the_mode_is_validated_at_the_DOORS_and_not_re_checked_inside`; what
    is NOT true is that anything prevents it. Narrowing the alias to a
    `Literal` (see this module's ⚠ above) is what would.

    ⚠ It is the ENABLED resolver that calls this, never the disabled one.
    `baton._optout` promises a disabled Baton never throws, and both doors
    honour that by taking a different branch rather than by re-testing a flag.
    """
    if mode not in _MODES:
        raise ValueError(f"{field} {mode!r} must be one of {sorted(_MODES)}.")
    return mode


class ResultFields(NamedTuple):
    """The two `tool_call_end` payload members the mode decides.

    A named tuple rather than a kwargs dict: these go into a pydantic model on
    the per-call path, and `**dict[str, Any]` would switch off mypy-strict's
    field-name and field-type checking at exactly the three payload
    constructions most worth checking.

    ⚠ `result=None` and omitting `result` are the SAME WIRE, which is why the
    projection can return a value rather than a key set: events serialize with
    a plain `model_dump(mode="json")` and no `exclude_none`, so a defaulted
    member reaches the wire as an explicit null either way. SPEC §11.4 states
    the consumer half of this — absent and null are equivalent, and a consumer
    must read the marker's VALUE, never test for its key.
    """

    result: Any = None
    result_capture: str | None = None


def end_result_fields(
    *,
    mode: str,
    scrubber: Callable[[Any], Any],
    to_jsonable: Callable[[Any], Any],
    result: Any,
) -> ResultFields:
    """The result on `tool_call_end`, or the declaration that it was withheld.

    Takes the RAW result and OWNS the scrubber call, so `"off"` short-circuits
    it. SPEC §7 says the scrubber MUST NOT be invoked on a withheld result; a
    projection handed an already-scrubbed value would be a guard standing after
    the thing it guards, and the body would already have been through the
    vendor's code.
    """
    if withholding(mode):
        return ResultFields(result_capture=WITHHELD)
    return ResultFields(result=scrubber(to_jsonable(result)))
