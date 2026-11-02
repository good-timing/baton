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
    except (AttributeError, ValueError):
        return False


def paused(result: Any) -> bool:
    """Typed or wire-shaped, as mcp's ``is_input_required`` accepts both."""
    if isinstance(result, Mapping):
        return result.get("resultType") == "input_required"
    return getattr(result, "result_type", None) == "input_required"


def client_can_resume(request_context: Any) -> bool:
    """False when mcp will fail the pause because the client's protocol predates
    pausing; mcp decides by its own version set, so this reads that set."""
    try:
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS
    except ImportError:
        return True
    version = getattr(request_context, "protocol_version", None)
    return not isinstance(version, str) or version in MODERN_PROTOCOL_VERSIONS
