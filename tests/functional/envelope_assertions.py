"""Shared assertion core for the event envelope (SPEC §11.4).

The logic here is modeled on
``examples/library_api_smoke_test/smoke_test_library.py``'s
``assert_events`` — but that example is NOT refactored to import this
module, and still carries its own local copy of the same checks. That's
intentional: the example is documented (CLAUDE.md) as a copy-paste-friendly
integration-test starting point for vendors, so it must stay self-contained
with no dependency on the ``tests/`` package. The two are maintained in
parallel, not shared; if you change the required-envelope-field set here,
update the example's local copy too (it says so at the call site).

This module's actual "one implementation instead of a divergent copy"
claim is narrower: ``tests/functional/test_cross_path_envelope.py`` reuses
ONE set of assertions across all three capture paths (mcp adapter, fastmcp
adapter, library API) instead of writing three per-adapter copies.

Plain module, no pytest import: assertions are plain ``assert`` statements
on plain ``dict``/``list`` data (the collector's on-the-wire JSON view of
each event), so this is usable from any context that can produce that
shape.
"""

from __future__ import annotations

from typing import Any

from baton import SignalType

# Mirrors baton.events._EventEnvelope's required (non-Optional) fields per
# SPEC §11.4. Deliberately excludes the envelope's nullable fields
# (user_id, runtime_meta) — their absence-vs-null is not a shape defect.
REQUIRED_ENVELOPE_FIELDS = {
    "event_id",
    "event_type",
    "tenant_id",
    "vendor_id",
    "session_id",
    "sequence_number",
    "captured_at",
    "consent_token",
    "sdk_version",
    "agent_runtime",
    "payload",
}


def assert_envelope_shape(events: list[dict[str, Any]]) -> None:
    """Every event carries the full required envelope (SPEC §11.4)."""
    for e in events:
        missing = REQUIRED_ENVELOPE_FIELDS - set(e.keys())
        assert not missing, f"event missing envelope fields {missing}: {e}"


def assert_sequence_monotonic_per_session(events: list[dict[str, Any]]) -> None:
    """``sequence_number`` is monotonic within each ``session_id`` — the
    invariant the Console worker's correlation logic (SPEC §11.5) depends
    on to order a session's events regardless of ``event_type``."""
    by_session: dict[str, list[int]] = {}
    for e in events:
        by_session.setdefault(e["session_id"], []).append(e["sequence_number"])
    for session_id, seqs in by_session.items():
        assert seqs == sorted(seqs), (
            f"sequence numbers not monotonic for session {session_id[:8]}: {seqs}"
        )


def assert_signal_types_valid(events: list[dict[str, Any]]) -> None:
    """Any populated ``annotation.payload.signal_type`` is one of the eight
    canonical SPEC §3.1 values — a typo'd or adapter-specific value here
    would silently bucket as "other" (or drop) on the Console side."""
    valid = {m.value for m in SignalType}
    for e in events:
        if e["event_type"] != "annotation":
            continue
        signal_type = e["payload"].get("signal_type")
        if signal_type is not None:
            assert signal_type in valid, f"unknown signal_type {signal_type!r}: {e}"
