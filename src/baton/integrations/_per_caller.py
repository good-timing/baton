"""The tool list and instructions under the Console's per-caller settings (SPEC §2.6)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from baton._capture_settings import CaptureSettings
from baton._overlays import Action, Overlay, changed_text, overlaid, warn_skipped
from baton._white_space import WHITE_SPACE
from baton.integrations._annotation_name import InstallText
from baton.integrations._config import VendorConfig
from baton.integrations._identity_adapter import ResolvedCaller
from baton.integrations._llm_text import (
    INTENT_INSTRUCTIONS,
    USER_GOAL_PARAM_NAME,
    a_tool_declares_user_goal,
    build_report_pointer,
    build_user_goal_param_description,
    inject_goal_params,
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
        pointer=(
            build_report_pointer(
                vendor_display_name=text.names.vendor_display_name,
                annotation_tool_name=text.names.annotation_tool_name,
            )
            if text.names
            else None
        ),
        write_instructions=write_instructions,
        warned=shared.warned,
    )
    # Now, not at the first request: ``initialize`` passes no seam of ours.
    surface.follow_settings()
    return surface


_UNREAD = object()


class PerCallerSurface:
    """Rewrites each ``tools/list`` result for its caller, and keeps the
    instructions' report pointer while some audience has reports on, with the
    server overlay applied.

    Runs at install and then on the server's event loop, never the settings
    thread, so it does not race the other writers of the instructions."""

    def __init__(
        self,
        *,
        mcp: Any,
        settings: CaptureSettings,
        intent_param_mode: str,
        annotation_tool_name: str | None,
        pointer: str | None,
        write_instructions: Callable[[Any, str], None],
        warned: set[str],
    ) -> None:
        self._mcp = mcp
        self._settings = settings
        self._intent_param_mode = intent_param_mode
        self._annotation_tool_name = annotation_tool_name
        self._pointer = pointer
        self._write = write_instructions
        self._warned = warned
        self._document: object = _UNREAD
        self._served: tuple[bool, str | None] | None = None
        self.server_overlay: Overlay | None = None

    def follow_settings(self) -> None:
        """Called at install and on every request; writes only when the report
        pointer or the server overlay should change."""
        try:
            document = self._settings.document
            if document is self._document:
                return
            overlay = document.server_overlay if document is not None else None
            pointer_on = self._pointer is not None and (
                document is None or document.reports_on_for_anyone("on")
            )
            served = (pointer_on, overlay.id if overlay is not None else None)
            if served != self._served:
                ours, subagent = self._instructions(pointer_on, overlay)
                self._write(
                    self._mcp,
                    with_vendor_instructions(self._mcp, ours, self._warned, subagent=subagent),
                )
                self._served, self.server_overlay = served, overlay
            self._document = document
        except Exception:
            logger.exception("baton: could not update the server instructions for new settings")

    def _instructions(self, pointer_on: bool, overlay: Overlay | None) -> tuple[str, str | None]:
        """Baton's paragraphs, and the subagent sentence they end with if any."""
        parts = list(overlay.instruction_appends()) if overlay is not None else []
        if pointer_on and self._pointer is not None:
            parts.append(overlaid(self._pointer, overlay, "instructions.report_pointer"))
        if not a_tool_declares_user_goal(self._mcp):
            parts.append(overlaid(INTENT_INSTRUCTIONS, overlay, "instructions.subagent"))
        # SPEC §2.6's append rule: each paragraph loses the white space at its end.
        kept = [part.rstrip(WHITE_SPACE) for part in parts]
        subagent = kept[-1] if kept and not a_tool_declares_user_goal(self._mcp) else None
        return "\n\n".join(part for part in kept if part), subagent or None

    def tools_for(self, result: Any, caller: ResolvedCaller) -> Any:
        """``result`` with the caller's goal, reports and overlay; as served on any failure."""
        if caller.settings is None:
            return result
        goal = caller.intent_param_mode(self._intent_param_mode)
        hide_reports = self._annotation_tool_name is not None and caller.report_mode("on") == "off"
        overlay = caller.overlay
        if goal == self._intent_param_mode and not hide_reports and overlay is None:
            return result
        try:
            return _with_tools(
                result, lambda tools: self._rewrite(tools, goal, hide_reports, overlay)
            )
        except Exception:
            logger.exception("baton: could not apply capture settings to the tool list")
            return result

    def _rewrite(
        self, tools: list[Any], goal: str, hide_reports: bool, overlay: Overlay | None
    ) -> list[Any]:
        served = build_user_goal_param_description(intent_param_mode=self._intent_param_mode)
        out = []
        injected = []
        for tool in tools:
            name = _field(tool, "name")
            if hide_reports and name == self._annotation_tool_name:
                continue
            schema = _field(tool, "inputSchema")
            ours = _injected_goal(schema, served)
            if goal != self._intent_param_mode and ours:
                tool = _replace(tool, "inputSchema", _with_goal_mode(schema, goal))
            out.append(tool)
            injected.append(ours)
        if overlay is None:
            return out
        return _with_overlay(out, injected, overlay, self._annotation_tool_name)


def _with_overlay(
    tools: list[Any], injected: list[bool], overlay: Overlay, report_tool: str | None
) -> list[Any]:
    """SPEC §2.6: the audience overlay's actions on the tool list, in order.
    ``injected`` says which tools carry Baton's ``user_goal``."""
    position = {_field(tool, "name"): i for i, tool in enumerate(tools)}
    hidden: set[int] = set()
    for action in overlay.actions:
        if action.tool is None:
            continue
        i = position.get(action.tool)
        if i is None:
            warn_skipped(overlay.id, action.index, f"no tool is named {action.tool!r}")
        elif action.tool == report_tool:
            warn_skipped(overlay.id, action.index, "the report tool takes agent_text targets only")
        elif action.param is None and action.change == "remove":
            hidden.add(i)
        elif action.param is None:
            text, _ = changed_text(_field(tools[i], "description"), (action,))
            tools[i] = _replace(tools[i], "description", text)
        elif (reason := _param_change(tools, i, action, injected[i])) is not None:
            warn_skipped(overlay.id, action.index, reason)
    if goal_actions := overlay.piece_actions("goal_param.description"):
        for i, ours in enumerate(injected):
            if ours:
                _describe_param(tools, i, USER_GOAL_PARAM_NAME, goal_actions)
    if (report_actions := overlay.piece_actions("report_tool.description")) and (
        report_tool in position
    ):
        i = position[report_tool]
        text, _ = changed_text(_field(tools[i], "description"), report_actions)
        tools[i] = _replace(tools[i], "description", text)
    return [tool for i, tool in enumerate(tools) if i not in hidden]


def _param_change(tools: list[Any], i: int, action: Action, injected: bool) -> str | None:
    """Apply one param action to ``tools[i]``; why it was skipped, else ``None``."""
    assert action.param is not None
    schema = _field(tools[i], "inputSchema")
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping) or action.param not in props:
        return f"{action.tool!r} has no param {action.param!r}"
    if injected and action.param == USER_GOAL_PARAM_NAME:
        return "Baton's user_goal takes the goal_param.description target"
    required = schema.get("required")
    required = list(required) if isinstance(required, list) else []
    if action.change == "remove":
        if action.param in required:
            return f"{action.tool!r} requires {action.param!r}"
        rebuilt = {
            **schema,
            "properties": {k: v for k, v in props.items() if k != action.param},
        }
        tools[i] = _replace(tools[i], "inputSchema", rebuilt)
    elif action.change == "required":
        if action.param not in required:
            tools[i] = _replace(
                tools[i], "inputSchema", {**schema, "required": [*required, action.param]}
            )
    else:
        _describe_param(tools, i, action.param, (action,))
    return None


def _describe_param(tools: list[Any], i: int, param: str, actions: tuple[Action, ...]) -> None:
    schema = _field(tools[i], "inputSchema")
    props = schema["properties"]
    described = props[param] if isinstance(props[param], Mapping) else {}
    text, _ = changed_text(described.get("description"), actions)
    rebuilt = {**schema, "properties": {**props, param: {**described, "description": text}}}
    tools[i] = _replace(tools[i], "inputSchema", rebuilt)


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
