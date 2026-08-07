"""Shared test helpers for filtering captured event JSON.

``surface_snapshot`` (see ``integrations._surface``) is a real, expected
event fired the first time a client lists tools (fastmcp adapter) or calls a
tool (mcp adapter) — most existing tool-call-focused tests predate it and
assert exact event/session sequences that don't account for it. Filtering it
out at the assertion site (rather than hiding it in the shared ``captured``
fixture) keeps it visible to any test that wants to assert on it directly.
"""

from __future__ import annotations

from typing import Any


def without_surface_snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ev for ev in events if ev["event_type"] != "surface_snapshot"]
