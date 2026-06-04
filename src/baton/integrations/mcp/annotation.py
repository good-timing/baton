"""Annotation tool registration — SPEC §5.1.1 — for the official mcp SDK's
``mcp.server.fastmcp.FastMCP``.

Mirrors ``baton.integrations.fastmcp.annotation`` but registers via the
official SDK's ``@mcp.tool(...)`` decorator.

**Note on `from __future__ import annotations` (intentionally omitted):**
mcp <=1.20 introspects tool signatures via ``inspect.signature(fn)`` and
does ``issubclass(param.annotation, Context)`` to detect Context kwargs.
That ``issubclass`` would crash on stringified annotations (which is what
``from __future__ import annotations`` produces). Keeping annotations as
live types lets ``get_origin`` correctly identify union/generic types and
skip the ``issubclass`` call. Required for mcp 1.10/1.20 support per
CHANGELOG 0.2.0 + CI matrix.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP

from baton._state import SessionCounter
from baton.events import AnnotationEvent, AnnotationPayload
from baton.scrub import identity_scrub
from baton.sinks import Sink

_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_DEFAULT_DESCRIPTION_TEMPLATE = (
    "Attach structured signal context — intent, expected outcome, and any "
    "friction observed — to {vendor_display_name} tool calls. Populate "
    "before a tool call (intent + expected_outcome + workflow) and again "
    "after if the result was unhelpful (signal_type + suggested_improvement)."
)


def derive_annotation_tool_name(vendor_id: str, override: str | None = None) -> str:
    """Resolve the annotation tool name. Default is ``{vendor_id}_annotate``.

    Raises ``ValueError`` if the resulting name violates the strict
    cross-runtime client pattern.
    """
    name = override or f"{vendor_id}_annotate"
    if not _TOOL_NAME_PATTERN.match(name):
        raise ValueError(
            f"Annotation tool name {name!r} violates the cross-runtime "
            f"pattern {_TOOL_NAME_PATTERN.pattern!r} (Claude Desktop and others "
            f"reject names with dots or other separators)."
        )
    return name


def register_annotation_tool(
    mcp: FastMCP,
    *,
    vendor_id: str,
    vendor_display_name: str,
    tenant_id: str,
    consent_token: str,
    sink: Sink,
    counter: SessionCounter,
    fallback_session_id: str,
    default_agent_runtime: str = "unknown",
    annotation_tool_name: str | None = None,
    scrubber: Callable[[Any], Any] = identity_scrub,
) -> str:
    """Register the annotation tool on ``mcp``. Returns the resolved tool name."""
    name = derive_annotation_tool_name(vendor_id, annotation_tool_name)
    description = _DEFAULT_DESCRIPTION_TEMPLATE.format(vendor_display_name=vendor_display_name)

    @mcp.tool(name=name, description=description)
    async def _annotate(
        intent: str | None = None,
        expected_outcome: str | None = None,
        signal_type: str | None = None,
        workflow: str | None = None,
        suggested_improvement: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Session id: we don't accept a Context kwarg because (a) we don't
        # use it (always fall back to fallback_session_id), and (b) older
        # mcp versions (<1.20) call `issubclass(param.annotation, Context)`
        # on each kwarg in Tool.from_function — that crashes on
        # parameterized generics like `Context[Any, Any, Any]`. Threading
        # Context through is a follow-up when we want true per-session
        # correlation; until then fallback_session_id is honest.
        session_id = fallback_session_id
        seq = await counter.next(session_id)
        await sink.write(
            AnnotationEvent(
                tenant_id=tenant_id,
                consent_token=consent_token,
                session_id=session_id,
                sequence_number=seq,
                captured_at=datetime.now(UTC),
                agent_runtime=default_agent_runtime,
                payload=AnnotationPayload(
                    intent=scrubber(intent) if intent else None,
                    expected_outcome=(scrubber(expected_outcome) if expected_outcome else None),
                    signal_type=signal_type,
                    workflow=workflow,
                    suggested_improvement=(
                        scrubber(suggested_improvement) if suggested_improvement else None
                    ),
                    context=scrubber(context) if context else None,
                ),
            )
        )
        return {"ok": True}

    return name
