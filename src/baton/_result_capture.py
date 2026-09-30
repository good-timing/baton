"""The result-capture mode's vocabulary, shared by every path that emits.

Top-level and dependency-free on purpose. Both the MCP adapters and the library
API (`baton.client`) have to agree on what withholding means, and `client.py`
imports only `baton._*` modules — never `baton.integrations` — so the constants
live here and the payload-shaping helpers that need MCP result types live in
`baton.integrations._result_capture` alongside them.

Splitting it this way rather than importing across the boundary: a deferred or
reversed import would hide a cycle rather than remove one
(`feedback_deferred_import_hides_cycle`), and the vocabulary genuinely has no
MCP in it.
"""

from __future__ import annotations

__all__ = ["RESULT_CAPTURE_MODES", "WITHHELD", "validate_mode", "withholding"]

WITHHELD = "off"
"""The one registered non-default value, on the wire and in the config alike.

SPEC §11.4 registers `"off"` and reserves a second value for the content
ladder's partial rung. That reservation is why the field is a string rather
than a boolean, and why nothing tests `!= "off"` to mean "capture".
"""

RESULT_CAPTURE_MODES: frozenset[str] = frozenset({"full", WITHHELD})
"""What a vendor may set. `"full"` never reaches the wire — SPEC §11.4 says
there is no `"full"` member, absence is what means captured."""


def withholding(mode: str) -> bool:
    """Whether `mode` withholds result-derived data.

    A POSITIVE test, deliberately. The partial rung, when it lands, is a third
    mode that also has no full body to emit, so `mode != "full"` would have to
    be revisited at every call site that day. Config validation rejects
    unregistered values at construction, so an unknown string never arrives
    here to be read as "capture everything".
    """
    return mode == WITHHELD


def validate_mode(mode: str, *, field: str) -> str:
    """Refuse an unregistered mode, naming the field the caller set.

    Every seam that accepts the mode calls this, and there are four: two public
    (`VendorConfig`, `Client`/`AsyncClient`) and two adapter-internal
    (`BatonMiddleware`, `install_wraps`), which this repo's own tests construct
    directly. Guarding only the public pair would leave the invariant with a
    door in it — and the failure through that door is silent in the one
    direction that cannot be undone: `withholding("OFF")` is `False`, so a
    vendor who meant to withhold captures every body and sees no error.

    ⚠ NOT called under the opt-out switch. `baton._optout` promises a disabled
    Baton never throws, and nothing is emitted then, so the mode cannot be
    wrong in any way that reaches a consumer.
    """
    if mode not in RESULT_CAPTURE_MODES:
        raise ValueError(
            f"{field} {mode!r} must be one of {sorted(RESULT_CAPTURE_MODES)} — "
            f"'full' captures tool results, 'off' withholds everything derived "
            f"from them and marks each event result_capture='off'. Refused here "
            f"rather than at emit: an unregistered value reads as 'not off' and "
            f"would capture bodies you believed were switched off."
        )
    return mode
