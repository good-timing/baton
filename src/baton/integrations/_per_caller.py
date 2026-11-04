"""The tool list and instructions under the Console's per-caller settings (SPEC §2.6)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from baton._capture_settings import CaptureSettings
from baton.integrations._annotation_name import InstallText
from baton.integrations._config import VendorConfig
from baton.integrations._identity_adapter import ResolvedCaller
from baton.integrations._llm_text import (
    INTENT_INSTRUCTIONS,
    USER_GOAL_PARAM_NAME,
    a_tool_declares_user_goal,
    build_user_goal_param_description,
    inject_goal_params,
    remove_subagent_clause,
    with_vendor_instructions,
)

if TYPE_CHECKING:
    from baton.integrations._shared import Shared

logger = logging.getLogger("baton")


def per_caller_surface(
    mcp: Any,
    config: VendorConfig,
    text: InstallText,
    shared: Shared,
    write_instructions: Callable[[Any, str], None],
) -> PerCallerSurface | None:
    """For an install whose instructions are already written; ``None`` with
    no Console settings."""
    if shared.capture_settings is None:
        return None
    surface = PerCallerSurface(
        mcp=mcp,
        settings=shared.capture_settings,
        intent_param_mode=config.intent_param_mode,
        annotation_tool_name=text.names.annotation_tool_name if text.names else None,
        instructions=text.instructions,
        write_instructions=write_instructions,
        warned=shared.warned,
    )
    # Now, not at the first request: ``initialize`` passes no seam of ours.
    surface.follow_settings()
    return surface


class PerCallerSurface:
    """Rewrites each ``tools/list`` result for its caller, and keeps the
    instructions' report pointer while some audience has reports on.

    Runs at install and then on the server's event loop, never the settings
    thread, so it does not race the other writers of the instructions."""

    def __init__(
        self,
        *,
        mcp: Any,
        settings: CaptureSettings,
        intent_param_mode: str,
        annotation_tool_name: str | None,
        instructions: str,
        write_instructions: Callable[[Any, str], None],
        warned: set[str],
    ) -> None:
        self._mcp = mcp
        self._settings = settings
        self._intent_param_mode = intent_param_mode
        self._annotation_tool_name = annotation_tool_name
        self._instructions = instructions
        self._write = write_instructions
        self._warned = warned
        self._pointer_on = True

    def follow_settings(self) -> None:
        """Called at install and on every request; writes only when the report
        pointer should change."""
        if self._annotation_tool_name is None:
            return
        try:
            document = self._settings.document
            pointer_on = document is None or document.reports_on_for_anyone("on")
            if pointer_on == self._pointer_on:
                return
            ours = self._instructions if pointer_on else INTENT_INSTRUCTIONS
            self._write(self._mcp, with_vendor_instructions(self._mcp, ours, self._warned))
            if a_tool_declares_user_goal(self._mcp):
                remove_subagent_clause(self._mcp, self._write)
            self._pointer_on = pointer_on
        except Exception:
            logger.exception("baton: could not update the server instructions for new settings")

    def tools_for(self, result: Any, caller: ResolvedCaller) -> Any:
        """``result`` with the caller's goal and reports settings; as served on any failure."""
        if caller.settings is None:
            return result
        goal = caller.intent_param_mode(self._intent_param_mode)
        hide_reports = self._annotation_tool_name is not None and caller.report_mode("on") == "off"
        if goal == self._intent_param_mode and not hide_reports:
            return result
        try:
            return _with_tools(result, lambda tools: self._rewrite(tools, goal, hide_reports))
        except Exception:
            logger.exception("baton: could not apply capture settings to the tool list")
            return result

    def _rewrite(self, tools: list[Any], goal: str, hide_reports: bool) -> list[Any]:
        served = build_user_goal_param_description(intent_param_mode=self._intent_param_mode)
        out = []
        for tool in tools:
            name = _field(tool, "name")
            if hide_reports and name == self._annotation_tool_name:
                continue
            schema = _field(tool, "inputSchema")
            if goal != self._intent_param_mode and _injected_goal(schema, served):
                tool = _replace(tool, "inputSchema", _with_goal_mode(schema, goal))
            out.append(tool)
        return out


def _injected_goal(schema: Any, served_description: str) -> bool:
    """Whether ``user_goal`` is Baton's; a tool's own ``user_goal`` is left as it is."""
    if not isinstance(schema, Mapping):
        return False
    props = schema.get("properties")
    param = props.get(USER_GOAL_PARAM_NAME) if isinstance(props, Mapping) else None
    return isinstance(param, Mapping) and param.get("description") == served_description


def _with_goal_mode(schema: Mapping[str, Any], goal: str) -> dict[str, Any]:
    props = {k: v for k, v in schema["properties"].items() if k != USER_GOAL_PARAM_NAME}
    rebuilt = {**schema, "properties": props}
    required = schema.get("required")
    if isinstance(required, list):
        rebuilt["required"] = [n for n in required if n != USER_GOAL_PARAM_NAME]
    inject_goal_params(rebuilt, intent_param_mode=goal)
    return rebuilt


# mcp 2.x names the schema field ``input_schema``; ``inputSchema`` is its alias.
_ATTRIBUTES = {"inputSchema": ("input_schema", "inputSchema")}


def _attribute(obj: Any, wire_name: str) -> str | None:
    fields = type(obj).model_fields
    return next((a for a in _ATTRIBUTES.get(wire_name, (wire_name,)) if a in fields), None)


def _field(obj: Any, wire_name: str) -> Any:
    attribute = _attribute(obj, wire_name)
    return None if attribute is None else getattr(obj, attribute)


def _replace(obj: Any, wire_name: str, value: Any) -> Any:
    return obj.model_copy(update={_attribute(obj, wire_name): value})


def _with_tools(result: Any, rewrite: Callable[[list[Any]], list[Any]]) -> Any:
    """``result`` of the same type, its tools rewritten. mcp 1.x wraps the list
    result in a ``ServerResult``; mcp 2.x does not."""
    root = getattr(result, "root", None)
    if root is not None:
        return result.model_copy(update={"root": _with_tools(root, rewrite)})
    tools = _field(result, "tools")
    if not isinstance(tools, list | tuple):
        return result
    return _replace(result, "tools", rewrite(list(tools)))
