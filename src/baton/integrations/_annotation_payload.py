"""The ``annotation`` payload, projected once for both adapters.

Both ``_annotate`` handlers build the same ``AnnotationPayload`` from the same
agent-supplied free-text fields, so one copy keeps them scrubbed alike.

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
        what_happened=scrubber(what_happened) if is_report(what_happened) else None,
        tool_name=scrubber(tool_name) if tool_name else tool_name,
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
