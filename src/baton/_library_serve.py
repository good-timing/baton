"""``serve()``: Baton around a vendor's own MCP dispatch function (SPEC §5.6).

Each step here is pure and may raise; the client's ``serve()`` guards each one,
so a failure serves the vendor's own answer."""

from __future__ import annotations

import inspect
import json
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from baton._capture_settings import CaptureSettings, SettingsDocument
from baton._library_surface import INTENT_PARAM_MODE, Surface, known_tools, serve_tools
from baton._report_tool import ReportTool
from baton.integrations._annotation_name import AnnotationNames
from baton.integrations._error_result import (
    TOOL_ERROR_TYPE,
    ErrorResultFields,
    jsonrpc_error_fields,
    returned_error_fields,
)
from baton.integrations._identity_adapter import ResolvedCaller
from baton.integrations._llm_text import (
    USER_GOAL_PARAM_NAME,
    Goals,
    build_refusal_text,
    build_report_pointer,
    join_instructions,
    pop_goal_params,
    unknown_tool_text,
)
from baton.integrations._per_caller import baton_paragraphs, pointer_shown, tools_for_caller
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
    kind: Literal["forward", "report", "refused", "hidden"] = "forward"
    answer: dict[str, Any] | None = None
    """Baton's own answer, unless the call is forwarded."""
    report: dict[str, Any] | None = None
    """``annotate()``'s arguments, for an accepted report."""

    def trace_kwargs(self) -> dict[str, Any]:
        intent, expected, workflow = self.goals
        return {
            "tool_name": self.name,
            "intent": intent,
            "expected_outcome": expected,
            "workflow": workflow,
            "params": self.arguments,
        }


@dataclass(frozen=True)
class Listed:
    answer: Any
    surface: Surface | None
    """``None`` for a paged list: one page is not the server's surface."""
    seam: Callable[[], dict[str, Any]]
    server_overlay: str | None


Report = tuple[AnnotationNames, ReportTool]
VendorTools = list[Any] | Callable[[], list[Any]]


class Serving:
    """What one client's ``serve()`` remembers between requests: the vendor's
    ``initialize`` values, and what it knows of the vendor's tools from the
    lists it served and from ``tools``."""

    def __init__(
        self,
        *,
        report: Report | None,
        settings: CaptureSettings | None,
        tools: VendorTools | None = None,
    ) -> None:
        self._report = report
        self._settings = settings
        self._lock = threading.Lock()
        self._server_meta: dict[str, Any] = {
            "server_info": None,
            "capabilities": None,
            "instructions": None,
        }
        self._dispositions: dict[str, dict[str, str]] = {}
        self._vendor_has_report_name = False
        self._tools_lock = threading.Lock()
        self._unread_tools = tools

    def _read_vendor_tools(self) -> None:
        """Learn the vendor's ``tools`` once per process. Never raises: without
        them, a call to a tool not yet listed is served as on a cold process."""
        if self._unread_tools is None:
            return
        # Held until the tools are remembered, so a concurrent first request
        # waits for them rather than being served as on a cold process.
        with self._tools_lock:
            source = self._unread_tools
            if source is None:
                return
            try:
                tools = source() if callable(source) else source
                if inspect.isawaitable(tools):
                    getattr(tools, "close", lambda: None)()
                    raise TypeError("tools= must be a list or a plain function, not async")
                if not isinstance(tools, list):
                    raise TypeError(f"tools= gave a {type(tools).__name__}, not a list")
                self._remember(*known_tools(tools))
            except Exception:
                logger.warning(
                    "baton: could not read tools=; a call this process has not seen "
                    "listed loses user_goal",
                    exc_info=True,
                )
            finally:
                self._unread_tools = None

    def _remember(self, dispositions: dict[str, dict[str, str]], names: set[str]) -> None:
        clash = self._report is not None and self._report[1].name in names
        with self._lock:
            new = dispositions.keys() - self._dispositions.keys()
            if len(self._dispositions) + len(new) > _REMEMBERED_TOOLS:
                self._dispositions.clear()
            self._dispositions.update(dispositions)
            first_clash = clash and not self._vendor_has_report_name
            self._vendor_has_report_name = self._vendor_has_report_name or clash
        if first_clash:
            logger.warning(
                "baton: your tool %r has the report tool's name, so Baton serves yours "
                "and no report tool. Set vendor_display_name to give Baton's another name.",
                self._report[1].name if self._report is not None else None,
            )

    def _ours(self) -> Report | None:
        """The report tool, unless a vendor tool has its name."""
        with self._lock:
            return None if self._vendor_has_report_name else self._report

    def _document(self) -> SettingsDocument | None:
        return self._settings.document if self._settings is not None else None

    def initialized(self, answer: Any) -> Any:
        result = _result(answer)
        if result is None:
            return answer
        vendor = result.get("instructions")
        self._read_vendor_tools()
        with self._lock:
            self._server_meta = {
                "server_info": result.get("serverInfo"),
                "capabilities": result.get("capabilities"),
                "instructions": vendor,
            }
            native_goal = any(
                d.get(USER_GOAL_PARAM_NAME) == "native" for d in self._dispositions.values()
            )
        report = self._ours()
        document = self._document()
        pointer = (
            build_report_pointer(
                vendor_display_name=report[0].vendor_display_name,
                annotation_tool_name=report[0].annotation_tool_name,
            )
            if report is not None
            else None
        )
        ours, _ = baton_paragraphs(
            pointer if pointer_shown(pointer, document) else None,
            document.server_overlay if document is not None else None,
            subagent_line=not native_goal,
        )
        return {**answer, "result": {**result, "instructions": join_instructions(vendor, ours)}}

    def listed(self, request: Request, answer: Any, caller: ResolvedCaller) -> Listed | None:
        result = _result(answer)
        tools = result.get("tools") if result is not None else None
        if result is None or not isinstance(tools, list):
            return None
        self._read_vendor_tools()
        with self._lock:
            meta = dict(self._server_meta)
        served = serve_tools(tools, **meta)
        self._remember(
            served.dispositions,
            {t["name"] for t in tools if isinstance(t, Mapping) and isinstance(t.get("name"), str)},
        )
        report = self._ours()
        first_page = request.params.get("cursor") is None
        out = list(served.tools)
        if report is not None and first_page:
            tool = report[1]
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
            )
        document = self._document()
        overlay = document.server_overlay if document is not None else None
        listed = tools_for_caller(
            {**result, "tools": out},
            caller,
            intent_param_mode=INTENT_PARAM_MODE,
            annotation_tool_name=report[1].name if report is not None else None,
        )
        surface = served.surface if first_page and result.get("nextCursor") is None else None
        return Listed(
            {**answer, "result": listed},
            surface,
            lambda: adapter_seam_augmentations(
                names=report[0] if report is not None else None,
                intent_param_mode=INTENT_PARAM_MODE,
                surface_tools=surface[0]["tools"] if surface is not None else [],
                applies_overlays=self._settings is not None,
                server_overlay=overlay,
            ),
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
        self._read_vendor_tools()
        with self._lock:
            dispositions = self._dispositions.get(name)
        report = self._ours()
        if report is not None and name == report[1].name:
            return self._report_call(report[1], name, arguments, message, caller)
        stripped = dict(arguments)
        goals = pop_goal_params(name, stripped, dispositions)
        if caller.overlay is not None and name in caller.overlay.hidden_tools:
            return _hidden(name, stripped, goals, message)
        return Call(name, stripped, goals, _forwarded(message, request.params, stripped, arguments))

    @staticmethod
    def _report_call(
        tool: ReportTool,
        name: str,
        arguments: Mapping[str, Any],
        message: Mapping[str, Any],
        caller: ResolvedCaller,
    ) -> Call:
        if caller.report_mode("on") == "off":
            return _hidden(name, dict(arguments), _NO_GOALS, message)
        kwargs = tool.annotate_kwargs(arguments)
        body: dict[str, Any] = (
            {"ok": True}
            if kwargs is not None
            else {
                "ok": False,
                "error": build_refusal_text(annotation_tool_name=name, overlay=caller.overlay),
            }
        )
        return Call(
            name,
            dict(arguments),
            _NO_GOALS,
            message,
            kind="report" if kwargs is not None else "refused",
            answer=_answer(
                message,
                {
                    "content": [{"type": "text", "text": json.dumps(body)}],
                    "structuredContent": body,
                },
            ),
            report=kwargs,
        )


_NO_GOALS: Goals = (None, None, None)


def _hidden(name: str, arguments: dict[str, Any], goals: Goals, message: Mapping[str, Any]) -> Call:
    result = {"content": [{"type": "text", "text": unknown_tool_text(name)}], "isError": True}
    return Call(name, arguments, goals, message, kind="hidden", answer=_answer(message, result))


def _forwarded(
    message: Mapping[str, Any],
    params: Mapping[str, Any],
    stripped: dict[str, Any],
    arguments: Mapping[str, Any],
) -> Any:
    if len(stripped) == len(arguments):
        return message
    return {**message, "params": {**params, "arguments": stripped}}


def without_user_goal(message: Any) -> Any:
    """``message`` without ``user_goal``, for when Baton's plan failed: the
    list it served may have added it. Never raises."""
    try:
        params = message["params"]
        arguments = params["arguments"]
        if USER_GOAL_PARAM_NAME not in arguments:
            return message
        stripped = {k: v for k, v in arguments.items() if k != USER_GOAL_PARAM_NAME}
        return {**message, "params": {**params, "arguments": stripped}}
    except Exception:
        return message


def hidden_fields(call: Call, scrubber: Callable[[Any], Any]) -> ErrorResultFields:
    """Baton's own text, kept under any mode."""
    answer = call.answer
    assert answer is not None
    return _fields(
        lambda: returned_error_fields(mode="full", scrubber=scrubber, result=answer["result"])
    )


def record(trace: Any, answer: Any, mode: str, scrubber: Callable[[Any], Any]) -> None:
    """Record dispatch's answer on ``trace``: a JSON-RPC error, a tool error, or the result."""
    if isinstance(answer, Mapping) and isinstance(answer.get("error"), Mapping):
        error = answer["error"]
        trace._observed_failure(
            JSONRPC_ERROR_TYPE,
            _fields(lambda: jsonrpc_error_fields(mode=mode, scrubber=scrubber, error=error)),
        )
        return
    result = _result(answer)
    if _is_error(result):
        trace._observed_failure(
            TOOL_ERROR_TYPE,
            _fields(lambda: returned_error_fields(mode=mode, scrubber=scrubber, result=result)),
        )
        return
    trace.observed(result if result is not None else answer)


def _fields(build: Callable[[], ErrorResultFields]) -> ErrorResultFields:
    """A raising scrubber loses the error's text, not the fact that the call failed."""
    try:
        return build()
    except Exception:
        logger.exception("baton: the configured scrubber raised on an error answer")
        return ErrorResultFields(error_body="")


def _is_error(result: Mapping[str, Any] | None) -> bool:
    return (
        result is not None
        and isinstance(result.get("content"), list)
        and result.get("isError") is True
    )


def _result(answer: Any) -> dict[str, Any] | None:
    if isinstance(answer, Mapping) and isinstance(answer.get("result"), Mapping):
        return dict(answer["result"])
    return None


def _answer(message: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": message.get("jsonrpc", "2.0"), "id": message.get("id"), "result": result}
