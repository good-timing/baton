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
