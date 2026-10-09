"""Shared test helpers for filtering captured event JSON.

``surface_snapshot`` (see ``integrations._surface``) is a real, expected
event fired the first time a client lists tools (fastmcp adapter) or calls a
tool (mcp adapter) — most existing tool-call-focused tests predate it and
assert exact event/session sequences that don't account for it. Filtering it
out at the assertion site (rather than hiding it in the shared ``captured``
fixture) keeps it visible to any test that wants to assert on it directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def without_surface_snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ev for ev in events if ev["event_type"] != "surface_snapshot"]


#: What a ``tools/list`` request produces (SPEC §11.4.5). The client libraries
#: send one on their own after a tool call, to check the result's shape.
TOOL_LIST_EVENT_TYPES = {"tool_list_start", "tool_list_end", "tool_list_error"}


def without_tool_listings(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ev for ev in events if ev["event_type"] not in TOOL_LIST_EVENT_TYPES]


def principal_of(ev: dict[str, Any]) -> dict[str, Any] | None:
    """An event's ``principal`` object, or ``None`` when it carried none.

    ⚠ **Also the guard that the RETIRED flat ``principal_id`` has not come
    back**, and that is why every read of the principal goes through here
    rather than through ``ev.get("principal")`` at each site. A regression
    that re-emitted the flat field would otherwise read as a clean ``None``
    at every call site — the assertions would still be about the object, and
    the object would still be absent, so nothing would fail.

    Shared because two test files grew this guard independently, with
    different messages, and a third read the principal without one.
    """
    assert "principal_id" not in ev, (
        "the RETIRED flat field `principal_id` is back on the wire — SPEC §11.4 "
        f"carries the object: {ev.get('event_type')}"
    )
    principal = ev.get("principal")
    return None if principal is None else dict(principal)


#: The event types one tool call or annotation produces, as opposed to the
#: server's own ``surface_snapshot``.
CALLER_EVENT_TYPES = {"tool_call_start", "tool_call_end", "tool_call_error", "annotation"}


def read_events(path: Path | str) -> list[dict[str, Any]]:
    """Every event a ``FileSink`` wrote, in order.

    Lives here because fourteen test modules had grown a byte-identical
    private copy of these three lines and this is the module that exists for
    it. New tests call this; the existing copies are left alone rather than
    swept in a change that is about something else.
    """
    # utf-8 explicitly, to PIN the decode to the sink's declared encoding —
    # ⚠ not because a mis-decode is reachable today. `FileSink` writes with
    # `json.dumps(...)` at the default `ensure_ascii=True` (`sinks.py:136,173`),
    # so the file is pure ASCII and decodes the same under any ASCII-superset
    # locale. This stays correct if `ensure_ascii=False` is ever set, which is
    # the only reason it is here.
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
