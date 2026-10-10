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
    what_happened: str | None,
    tool_name: str | None,
) -> AnnotationPayload:
    """``AnnotationPayload`` with every free-text member PII-scrubbed.

    ``tool_name`` is sent as given, so its three states survive: a name,
    ``"none"`` or ``""`` for "the agent said no tool exists", and ``None``
    for nothing stated (SPEC §5.1.1).
    """
    return AnnotationPayload(
        intent=scrubber(user_goal) if user_goal else None,
        expected_outcome=scrubber(expected_result) if expected_result else None,
        what_happened=scrubber(what_happened) if is_report(what_happened) else None,
        tool_name=scrubber(tool_name) if tool_name else tool_name,
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


def is_report(what_happened: str | None) -> bool:
    """SPEC §11.4: an annotation is a report when its account is filled."""
    return bool(what_happened and what_happened.strip())


def with_tool_name_required(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of the annotation tool's input schema listing ``tool_name`` as
    required (SPEC §5.1.1). Advertised only: a call that omits it is served."""
    required = schema.get("required")
    names = list(required) if isinstance(required, list) else []
    if "tool_name" not in names:
        names.append("tool_name")
    return {**schema, "required": names}
