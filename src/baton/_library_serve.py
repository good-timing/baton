"""``serve()``: Baton around a vendor's own MCP dispatch function (SPEC §5.6).

Each step here is pure and may raise; the client's ``serve()`` guards each one,
so a failure serves the vendor's own answer."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from baton._capture_settings import CaptureSettings
from baton._library_surface import INTENT_PARAM_MODE, Surface, serve_tools
from baton._report_tool import ReportTool
from baton._result_capture import WITHHELD, withholding
from baton.integrations._annotation_name import AnnotationNames
from baton.integrations._error_result import (
    TOOL_ERROR_TYPE,
    ErrorResultFields,
    is_error_result,
    returned_error_fields,
)
from baton.integrations._identity_adapter import ResolvedCaller
from baton.integrations._llm_text import (
    Goals,
    build_refusal_text,
    build_report_pointer,
    join_instructions,
    pop_goal_params,
)
from baton.integrations._per_caller import baton_paragraphs, tools_for_caller
from baton.integrations._surface import adapter_seam_augmentations

logger = logging.getLogger(__name__)

INITIALIZE = "initialize"
TOOL_LIST = "tools/list"
TOOL_CALL = "tools/call"
# The JSON-RPC error answer's error_type, as mcp names that type.
JSONRPC_ERROR_TYPE = "JSONRPCError"
_REMEMBERED_TOOLS = 4096


@dataclass(frozen=True)
class Request:
    method: str
    params: Mapping[str, Any]

    @property
    def meta(self) -> Any:
        return self.params.get("_meta")

    @property
    def client_info(self) -> Any:
        return self.params.get("clientInfo") if self.method == INITIALIZE else None


def read_request(message: Any) -> Request | None:
    """The request ``serve()`` acts on; ``None`` sends ``message`` to dispatch as it is."""
    if not isinstance(message, Mapping) or message.get("id") is None:
        return None
    method = message.get("method")
    params = message.get("params", {})
    if method not in (INITIALIZE, TOOL_LIST, TOOL_CALL) or not isinstance(params, Mapping):
        return None
    return Request(method, params)


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict[str, Any]
    """Without Baton's params."""
    goals: Goals
    message: Any
    """What dispatch gets."""
    answer: dict[str, Any] | None = None
    """Baton's own answer, when dispatch is not called."""
    report: dict[str, Any] | None = None
    """``annotate()``'s arguments, for an accepted report."""
    hidden: bool = False


@dataclass(frozen=True)
class Listed:
    answer: Any
    surface: Surface | None
    seam: dict[str, Any]
    server_overlay: str | None


class Serving:
    """What one client's ``serve()`` remembers between requests: the vendor's
    ``initialize`` values and which tools declare ``user_goal`` themselves."""

    def __init__(
        self,
        *,
        names: AnnotationNames | None,
        report: ReportTool | None,
        settings: CaptureSettings | None,
    ) -> None:
        self._names = names
        self._report = report
        self._settings = settings
        self._lock = threading.Lock()
        self._server_meta: dict[str, Any] = {
            "server_info": None,
            "capabilities": None,
            "instructions": None,
        }
        self._dispositions: dict[str, dict[str, str]] = {}
        self._native_goal = False
        self._vendor_has_report_name = False

    def initialized(self, answer: Any) -> Any:
        result = _result(answer)
        if result is None:
            return answer
        vendor = result.get("instructions")
        with self._lock:
            self._server_meta = {
                "server_info": result.get("serverInfo"),
                "capabilities": result.get("capabilities"),
                "instructions": vendor,
            }
            native_goal = self._native_goal
        document = self._settings.document if self._settings is not None else None
        overlay = document.server_overlay if document is not None else None
        pointer = None
        if self._names is not None and (document is None or document.reports_on_for_anyone("on")):
            pointer = build_report_pointer(
                vendor_display_name=self._names.vendor_display_name,
                annotation_tool_name=self._names.annotation_tool_name,
            )
        ours, _ = baton_paragraphs(pointer, overlay, subagent_line=not native_goal)
        return {**answer, "result": {**result, "instructions": join_instructions(vendor, ours)}}

    def listed(self, answer: Any, caller: ResolvedCaller) -> Listed | None:
        result = _result(answer)
        tools = result.get("tools") if result is not None else None
        if result is None or not isinstance(tools, list):
            return None
        with self._lock:
            meta = dict(self._server_meta)
        served = serve_tools(tools, **meta)
        report = self._report
        clash = report is not None and any(
            isinstance(tool, Mapping) and tool.get("name") == report.name for tool in tools
        )
        with self._lock:
            if len(self._dispositions) + len(served.dispositions) > _REMEMBERED_TOOLS:
                self._dispositions.clear()
            self._dispositions.update(served.dispositions)
            self._native_goal = self._native_goal or any(
                d.get("user_goal") == "native" for d in served.dispositions.values()
            )
            first_clash = clash and not self._vendor_has_report_name
            self._vendor_has_report_name = self._vendor_has_report_name or clash
            ours = report is not None and not self._vendor_has_report_name
        if first_clash:
            logger.warning(
                "baton: your tool %r has the report tool's name, so Baton serves yours "
                "and no report tool. Set vendor_display_name to give Baton's another name.",
                report.name if report is not None else None,
            )
        out = list(served.tools)
        # Added once, on the last page of a paged list.
        if ours and report is not None and result.get("nextCursor") is None:
            out.append(
                {
                    "name": report.name,
                    "description": report.description,
                    "inputSchema": report.input_schema,
                }
            )
        document = self._settings.document if self._settings is not None else None
        overlay = document.server_overlay if document is not None else None
        listed = tools_for_caller(
            {**result, "tools": out},
            caller,
            intent_param_mode=INTENT_PARAM_MODE,
            annotation_tool_name=report.name if ours and report is not None else None,
        )
        seam = adapter_seam_augmentations(
            names=self._names if ours else None,
            intent_param_mode=INTENT_PARAM_MODE,
            surface_tools=served.surface[0]["tools"] if served.surface is not None else [],
            applies_overlays=self._settings is not None,
            server_overlay=overlay,
        )
        return Listed(
            {**answer, "result": listed},
            served.surface,
            seam,
            overlay.id if overlay is not None else None,
        )

    def call(
        self, request: Request, message: Mapping[str, Any], caller: ResolvedCaller
    ) -> Call | None:
        """The plan for one ``tools/call``; ``None`` sends it to dispatch untraced."""
        name = request.params.get("name")
        arguments = request.params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return None
        with self._lock:
            dispositions = self._dispositions.get(name)
            ours = self._report is not None and not self._vendor_has_report_name
        report = self._report if ours else None
        if report is not None and name == report.name:
            if caller.report_mode("on") == "off":
                return self._hidden(name, dict(arguments), (None, None, None), message)
            kwargs = report.annotate_kwargs(arguments)
            body: dict[str, Any] = (
                {"ok": True}
                if kwargs is not None
                else {
                    "ok": False,
                    "error": build_refusal_text(annotation_tool_name=name, overlay=caller.overlay),
                }
            )
            text = json.dumps(body)
            return Call(
                name,
                dict(arguments),
                (None, None, None),
                message,
                answer=_answer(
                    message,
                    {"content": [{"type": "text", "text": text}], "structuredContent": body},
                ),
                report=kwargs,
            )
        stripped = dict(arguments)
        goals = pop_goal_params(name, stripped, dispositions)
        if caller.overlay is not None and name in caller.overlay.hidden_tools:
            return self._hidden(name, stripped, goals, message)
        forwarded = (
            message
            if len(stripped) == len(arguments)
            else {**message, "params": {**request.params, "arguments": stripped}}
        )
        return Call(name, stripped, goals, forwarded)

    @staticmethod
    def _hidden(
        name: str, arguments: dict[str, Any], goals: Goals, message: Mapping[str, Any]
    ) -> Call:
        text = f"Unknown tool: {name}"
        return Call(
            name,
            arguments,
            goals,
            message,
            answer=_answer(message, {"content": [{"type": "text", "text": text}], "isError": True}),
            hidden=True,
        )


def hidden_fields(call: Call, scrubber: Callable[[Any], Any]) -> ErrorResultFields:
    """The error fields of a hidden tool's answer: Baton's text, kept under any mode."""
    assert call.answer is not None
    return returned_error_fields(mode="full", scrubber=scrubber, result=call.answer["result"])


def record(trace: Any, answer: Any, mode: str, scrubber: Callable[[Any], Any]) -> None:
    """Record dispatch's answer on ``trace``: a JSON-RPC error, a tool error, or the result."""
    if isinstance(answer, Mapping) and isinstance(answer.get("error"), Mapping):
        error = answer["error"]
        fields = (
            ErrorResultFields(error_body="", result_capture=WITHHELD)
            if withholding(mode)
            else ErrorResultFields(
                error_body=str(scrubber(str(error.get("message", ""))))[:2000],
                result=scrubber(dict(error)),
            )
        )
        trace._observed_failure(JSONRPC_ERROR_TYPE, fields)
        return
    result = _result(answer)
    if is_error_result(result):
        trace._observed_failure(
            TOOL_ERROR_TYPE, returned_error_fields(mode=mode, scrubber=scrubber, result=result)
        )
        return
    trace.observed(result if result is not None else answer)


def _result(answer: Any) -> dict[str, Any] | None:
    if isinstance(answer, Mapping) and isinstance(answer.get("result"), Mapping):
        return dict(answer["result"])
    return None


def _answer(message: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": message.get("jsonrpc", "2.0"), "id": message.get("id"), "result": result}
