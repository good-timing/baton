"""The ``annotation`` payload, projected once for both adapters.

Both ``_annotate`` handlers build the identical ``AnnotationPayload`` from the
identical five agent-supplied free-text fields, and the duplication has already
cost once: ``workflow`` reached the sink UNSCRUBBED on both surfaces (SPEC
§11.2(2) lists annotation text as covered), needed the same one-line fix twice,
and needed a twin test in each adapter's suite — testing one would have left the
fix half proven on a path the vendor believes is scrubbed. The next field added
or renamed would diverge the same way, silently.

⚠ **Every scrubber application here is meant to run inside a ``safe_emit``
build thunk**, which is why this returns a payload rather than taking an
already-scrubbed one. A caller that resolves it eagerly puts the vendor's
scrubber back in its own frame, outside the guard, which is the defect
``safe_emit`` exists for. The two call sites pass this function's result from
inside their thunk for that reason.

Scoped to the PAYLOAD only. The two ``_annotate`` bodies differ materially on
session-id and ``_meta`` resolution and stay separate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from baton.events import AnnotationPayload

__all__ = ["build_annotation_payload"]


def build_annotation_payload(
    scrubber: Callable[[Any], Any],
    *,
    user_goal: str | None,
    expected_result: str | None,
    overall_task: str | None,
    suggested_improvement: str | None,
    context: dict[str, Any] | None,
    signal_type: str | None,
) -> AnnotationPayload:
    """``AnnotationPayload`` with all five free-text members PII-scrubbed.

    ``signal_type`` is a closed vocabulary the agent picks from, not free text,
    so it is not scrubbed — the same reason ``error_type`` is not (§11.4.3).
    """
    return AnnotationPayload(
        intent=scrubber(user_goal) if user_goal else None,
        expected_outcome=scrubber(expected_result) if expected_result else None,
        signal_type=signal_type,
        # Agent-facing param `overall_task` -> wire key `workflow`, the same
        # split the injected params use (`overall_task` -> `call_workflow`):
        # renaming the param must not move the key the console groups on.
        #
        # ⚠ SCRUBBED, which it was not until 2026-10-03. The rename that
        # produced this key (`d0a2630`) carried the expression across unchanged
        # and nothing named the omission. Besides the leak, the tool-call path
        # DOES scrub the same semantic field (`middleware.py`'s `scrubbed_task`
        # -> `call_workflow`), so any scrubber that rewrites rather than passes
        # through emitted two different values for the one grouping key the
        # comment above says must stay joinable — splitting the group across
        # the annotation and tool-call surfaces.
        workflow=scrubber(overall_task) if overall_task else None,
        suggested_improvement=(scrubber(suggested_improvement) if suggested_improvement else None),
        context=scrubber(context) if context else None,
    )
