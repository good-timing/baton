"""One spelling of "the vendor asked us not to capture results" (SPEC §11.4).

`VendorConfig.result_capture_mode` is how a vendor declares it; `result_capture`
is what reaches the wire. This module is the only place the two meet, so the
three capture surfaces cannot disagree about what withholding means.

**Why the helpers take the RAW result and own the scrubber call.** SPEC §7:
under `"off"` the scrubber MUST NOT be invoked on `result`. A helper taking an
already-scrubbed value would be handed one that had *already* run the vendor's
code — the guard would sit after the thing it is guarding, which is the shape
`feedback_a_guard_after_the_write_undoes_the_write` names. So `scrubber` and
the jsonable-izer come in as callables and are called only on the capturing
path.

**This module serves TWO of the three surfaces**, and the vocabulary it
shares with the third lives in `baton._result_capture`. `standalone/middleware.py`
and `official/_tool_wrap.py` both hold the result at payload-construction time,
so the decision fits in the payload kwargs. `client.py` — the library API path
— does not: it scrubs and stores the result inside `observed()`, long before a
payload exists, so its gate lives there and it uses `withholding()` only. Three
surfaces, three shapes; a single helper forced onto all of them would put the
library path's guard in the wrong place.

**What is NOT here: `error_body` on the RAISE leg.** The rule is keyed on
PROVENANCE, not on a field name (SPEC §11.2.6, §11.4.3). A raised exception's
message is the vendor's own code speaking about a call that returned nothing,
so it is kept under `"off"` and needs no helper — the raise legs are unchanged
and are meant to read that way.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from baton._result_capture import WITHHELD, withholding
from baton.integrations._error_result import envelope_to_jsonable, error_text

__all__ = ["end_result_fields", "returned_error_fields"]


def end_result_fields(
    *,
    mode: str,
    scrubber: Callable[[Any], Any],
    to_jsonable: Callable[[Any], Any],
    result: Any,
) -> dict[str, Any]:
    """Payload kwargs carrying the result on `tool_call_end`, or its absence.

    Under `"off"` neither `to_jsonable` nor `scrubber` runs and no `result` key
    is produced at all: the body never enters the payload, so it never reaches
    the bounded buffer, a `FileSink` on disk, or an exception message on the
    way there.
    """
    if withholding(mode):
        return {"result_capture": WITHHELD}
    return {"result": scrubber(to_jsonable(result))}


def returned_error_fields(
    *,
    mode: str,
    scrubber: Callable[[Any], Any],
    result: Any,
) -> dict[str, Any]:
    """Payload kwargs for the RETURN failure shape (SPEC §11.4.3).

    Both members here are result-derived — `error_body` is unwrapped from the
    result's `content` text parts, and `result` is the full envelope — so
    `"off"` withholds both.

    ⚠ `error_body` is emitted as `""` rather than dropped: it is in
    `ToolCallErrorPayload`'s `required` set, and widening that array is a
    conformance change every producer would have to follow. An empty
    `error_body` is genuinely ambiguous with "the failure carried no message",
    and `result_capture` is what tells the two apart — a second reason the
    marker is not optional.
    """
    if withholding(mode):
        return {"error_body": "", "result_capture": WITHHELD}
    return {
        "error_body": str(scrubber(error_text(result)))[:2000],
        # The ENVELOPE, not the unwrapped developer return: the error flag and
        # the reason both live on it, and unwrapping is what dropped them.
        "result": scrubber(envelope_to_jsonable(result)),
    }
