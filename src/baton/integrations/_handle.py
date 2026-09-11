"""Shared ``BatonHandle`` — returned by both adapter ``install_baton()`` functions.

Extracted from fastmcp/install.py and mcp/install.py (were byte-for-byte
identical). Both adapters now import from here.
"""

from __future__ import annotations

from baton.sinks import Sink


class BatonHandle:
    """Handle returned from ``install_baton`` for graceful shutdown and
    session correlation.

    ``session_id`` is the process-lifetime identifier baked into every emitted
    event. Vendor tools that need to correlate external artifacts (e.g., a
    Console-issued support ticket) with the Baton event stream should include
    this value in their payloads.
    """

    def __init__(
        self,
        *,
        sink: Sink,
        annotation_tool_name: str,
        vendor_id: str,
        session_id: str,
    ) -> None:
        self.sink = sink
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id
        self.session_id = session_id

    async def flush(self) -> None:
        """Flush any pending events held by the sink."""
        await self.sink.flush()

    async def aclose(self) -> None:
        """Flush and release sink resources. Subsequent writes raise."""
        await self.sink.aclose()


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
    from baton._optout import DisabledSink, log_disabled

    log_disabled(switch, surface)
    return BatonHandle(
        # A sink the VENDOR constructed is held rather than dropped, so their
        # ``handle.aclose()`` still releases it — we took ownership of that
        # object the moment they passed it, and the switch does not undo that.
        # Nothing writes to it: nothing is wrapped, and a handle has no
        # network path of its own to reach it by.
        sink=supplied_sink if supplied_sink is not None else DisabledSink(),
        annotation_tool_name="",
        vendor_id="",
        session_id="baton-disabled",
    )
