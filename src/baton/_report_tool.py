"""The annotation tool as data, for a vendor offering it through its own API (SPEC §5.6)."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from baton.integrations._annotation_name import (
    tool_name_for_display_name,
    validate_annotation_tool_name,
)
from baton.integrations._annotation_payload import is_report, with_tool_name_required
from baton.integrations._llm_text import (
    USER_GOAL_PARAM_NAME,
    build_annotation_tool_description,
    build_refusal_text,
    build_server_instructions,
)

__all__ = ["ReportTool", "report_tool"]

_NULLABLE_TEXT: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}

_INPUT_SCHEMA: dict[str, Any] = with_tool_name_required(
    {
        "type": "object",
        "properties": {
            USER_GOAL_PARAM_NAME: {"type": "string"},
            "what_happened": _NULLABLE_TEXT,
            "tool_name": _NULLABLE_TEXT,
            "suggested_improvement": _NULLABLE_TEXT,
            "context": {
                "anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}],
                "default": None,
            },
        },
        "required": [USER_GOAL_PARAM_NAME],
    }
)

_OPTIONAL: tuple[tuple[str, str, type], ...] = (
    (USER_GOAL_PARAM_NAME, "intent", str),
    ("suggested_improvement", "suggested_improvement", str),
    ("context", "context", dict),
)


@dataclass(frozen=True)
class ReportTool:
    """The tool an MCP adapter registers under ``report_mode="on"``."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(repr=False)
    instructions: str = field(repr=False)
    """The report pointer and ``INTENT_INSTRUCTIONS``, for a vendor serving MCP itself."""
    refusal: str = field(repr=False)
    """What to answer when ``annotate_kwargs`` returns ``None``."""

    def annotate_kwargs(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """The ``annotate()`` arguments for this tool's ``arguments``, or ``None``
        when there is no ``what_happened``: answer ``refusal`` and send nothing."""
        what_happened = arguments.get("what_happened")
        if not isinstance(what_happened, str) or not is_report(what_happened):
            return None
        kwargs: dict[str, Any] = {"what_happened": what_happened}
        # Sent as written, "" included: SPEC §5.1.1 gives tool_name three states.
        if isinstance(tool_name := arguments.get("tool_name"), str):
            kwargs["tool_name"] = tool_name
        # An empty value is absent, as the adapters send it.
        for argument, kwarg, kind in _OPTIONAL:
            value = arguments.get(argument)
            if isinstance(value, kind) and value:
                kwargs[kwarg] = value
        return kwargs


def report_tool(*, vendor_display_name: str, name: str | None = None) -> ReportTool:
    """The annotation tool an adapter registers on a server named
    ``vendor_display_name``. ``name`` overrides the name made from it."""
    if name is None:
        name = tool_name_for_display_name(vendor_display_name)
        if name is None:
            raise ValueError(
                f"vendor_display_name={vendor_display_name!r} gives no usable tool name, "
                "or one too long for the instructions. Pass name="
            )
    validate_annotation_tool_name(name)
    return ReportTool(
        name=name,
        description=build_annotation_tool_description(vendor_display_name=vendor_display_name),
        input_schema=copy.deepcopy(_INPUT_SCHEMA),
        instructions=build_server_instructions(
            vendor_display_name=vendor_display_name, annotation_tool_name=name
        ),
        refusal=build_refusal_text(annotation_tool_name=name),
    )
