"""Shared ``BatonHandle`` — returned by both adapter ``install_baton()`` functions.

Extracted from fastmcp/install.py and mcp/install.py (were byte-for-byte
identical). Both adapters now import from here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baton.sinks import Sink, bounded_sink_call, vendor_sink

if TYPE_CHECKING:
    from baton.integrations._shared import Shared


class BatonHandle:
    """Handle returned from ``install_baton`` for graceful shutdown and
    session correlation.

    ``session_id`` is the process-lifetime identifier baked into every emitted
    event. Vendor tools that need to correlate external artifacts (e.g., a
    Console-issued support ticket) with the Baton event stream should include
    this value in their payloads.

    ``annotation_tool_name`` is ``""`` when no annotation tool was registered.
    """

    def __init__(
        self,
        *,
        sink: Sink,
        annotation_tool_name: str,
        vendor_id: str,
        session_id: str,
        shared: Shared | None = None,
    ) -> None:
        self.sink = vendor_sink(sink)
        self._sink = sink
        self._shared = shared
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id
        self.session_id = session_id

    async def flush(self) -> None:
        """Flush any pending events held by the sink. Returns within the sink's
        shutdown budget plus a quarter second, and never raises (SPEC §11.2 rule 7)."""
        await bounded_sink_call(self._sink, "flush")

    async def aclose(self) -> None:
        """Stop reading capture settings, then flush and release sink resources.
        Returns within the sink's shutdown budget plus a quarter second, and
        never raises (SPEC §11.2 rule 7)."""
        if self._shared is not None:
            self._shared.stop()
        await bounded_sink_call(self._sink, "aclose")


def disabled_handle(switch: str, surface: str, supplied_sink: Sink | None = None) -> BatonHandle:
    """The handle ``install_baton`` returns when the off switch is set.

    Shaped so a vendor's existing code keeps working untouched: ``flush()`` and
    ``aclose()`` in a ``finally`` do nothing. Nothing here is a second way to
    configure capture off — the switch is an environment variable, deliberately,
    so the recipe C14 writes has one story to tell.

    ⚠ This used to carry a ``disabled_switch`` through to the handle, which
    suppressed the Console URL so a disabled handle could not make a network
    call, and let ``escalate()`` name the switch as the real cause. Both
    readers went with ``escalate()``; a handle makes no network calls at all
    now, so there is nothing left to suppress.

    ``vendor_id`` is empty and ``session_id`` is a constant, because when the
    switch is on nothing was resolved: inventing a session id would put a real
    identifier on a handle whose whole meaning is that no events exist under it.
    """
    from baton._optout import log_disabled

    log_disabled(switch, surface)
    return _noop_handle("baton-disabled", supplied_sink)


def unconfigured_handle(surface: str) -> BatonHandle:
    """As Sentry does with no DSN: the vendor's server starts as usual."""
    import logging

    logging.getLogger(__name__).warning(
        "baton: no DSN (dsn= or BATON_DSN) and no vendor_id, so %s installed nothing and records nothing",
        surface,
    )
    return _noop_handle("baton-unconfigured", None)


def _noop_handle(session_id: str, supplied_sink: Sink | None) -> BatonHandle:
    from baton._optout import DisabledSink

    return BatonHandle(
        # A sink the VENDOR constructed is held rather than dropped, so their
        # ``handle.aclose()`` still releases it.
        sink=supplied_sink if supplied_sink is not None else DisabledSink(),
        annotation_tool_name="",
        vendor_id="",
        session_id=session_id,
    )
