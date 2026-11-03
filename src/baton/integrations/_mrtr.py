"""Requests that pause for the client's input (SEP-2322, mcp>=2.0)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def resumes_a_pause(request: Any) -> bool:
    """The retry of a paused request carries the client's answers. Never raises."""
    if request is None:
        return False
    try:
        return (
            getattr(request, "input_responses", None) is not None
            or getattr(request, "request_state", None) is not None
        )
    except Exception:
        return False


def paused(result: Any) -> bool:
    """Typed or wire-shaped, as mcp's ``is_input_required`` accepts both."""
    if isinstance(result, Mapping):
        return result.get("resultType") == "input_required"
    return getattr(result, "result_type", None) == "input_required"


def client_can_resume(request_context: Any) -> bool:
    """False when mcp will fail the pause because the client's protocol predates
    pausing; mcp and fastmcp decide by mcp's own version set, so this reads it.
    A context with no protocol at all is not one Baton can judge."""
    try:
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS
    except ImportError:
        return True
    if not hasattr(request_context, "protocol_version"):
        return True
    return request_context.protocol_version in MODERN_PROTOCOL_VERSIONS


def pause_refused(read_request_context: Any) -> bool:
    """mcp's official server will fail this pause after Baton's seam has
    returned, because the client cannot take one. Never raises."""
    try:
        return not client_can_resume(read_request_context())
    except Exception:
        return False


MCP_PAUSE_REFUSED = "Handler returned an invalid result"


def mcp_refusal() -> BaseException:
    """The error mcp's official server sends a client that cannot take a pause."""
    try:
        from mcp.shared.exceptions import MCPError
        from mcp_types import INTERNAL_ERROR

        return MCPError(code=INTERNAL_ERROR, message=MCP_PAUSE_REFUSED)
    except Exception:
        return RuntimeError(MCP_PAUSE_REFUSED)
