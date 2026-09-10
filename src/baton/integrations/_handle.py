"""Shared ``BatonHandle`` — returned by both adapter ``install_baton()`` functions.

Extracted from fastmcp/install.py and mcp/install.py (were byte-for-byte
identical). Both adapters now import from here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from baton.sinks import Sink

if TYPE_CHECKING:
    import httpx

_log = logging.getLogger("baton")


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
        disabled_switch: str | None = None,
    ) -> None:
        from baton.sinks import HttpSink

        self.sink = sink
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id
        self.session_id = session_id
        # The env var that switched capture off, or None. Load-bearing twice
        # below: it suppresses the Console URL so a disabled handle cannot make
        # a network call, and it lets ``escalate`` name the real cause.
        self._disabled_switch = disabled_switch
        # Extracted from HttpSink when present; None in dev mode (StdoutSink/FileSink)
        # and always None when capture is off — a handle that holds a vendor's
        # own HttpSink (so it can still close it) must not go on to USE it.
        usable = sink if isinstance(sink, HttpSink) and disabled_switch is None else None
        self._console_url: str | None = usable.url if usable else None
        self._console_api_key: str | None = usable.api_key if usable else None
        # Shared httpx client for escalate() calls — created lazily, closed in aclose().
        self._http_client: httpx.AsyncClient | None = None

    async def flush(self) -> None:
        """Flush any pending events held by the sink."""
        await self.sink.flush()

    async def aclose(self) -> None:
        """Flush and release sink resources. Subsequent writes raise."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        await self.sink.aclose()

    async def escalate(
        self,
        annotation_seq: int | None = None,
        *,
        session_id: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, str | None]:
        """File a support ticket for the current session via the Console.

        Calls ``POST {console_url}/v0/escalate`` synchronously and returns
        ``{"ticket_id": "...", "ticket_url": "..."}`` so the calling tool can
        surface the ticket URL to the user in the same response turn.

        ``annotation_seq`` is the sequence number of the reactive annotation to
        escalate. If omitted, the Console resolves to the latest reactive
        annotation in the session.

        ``session_id`` — the session identifier under which events were emitted.
        For the FastMCP adapter, pass ``ctx.session_id`` from the tool's
        ``Context`` argument so the ID matches what the middleware filed events
        under. If omitted, falls back to ``self.session_id`` (safe for the MCP
        adapter which always uses the fallback ID, and for dev/test).

        Falls back to ``{"ticket_id": "queued", "ticket_url": None}`` when no
        Console URL is configured (dev mode — StdoutSink / FileSink).
        """
        if self._disabled_switch is not None:
            # ⚠ Named separately from dev mode, because reusing that message
            # here told a vendor to "switch to HttpSink" when their sink was
            # never the problem — advice that cannot work, at WARNING, while
            # the true cause sat at INFO where nothing shows it. A message that
            # confidently names the wrong cause is worse than a vague one.
            _log.warning(
                "handle.escalate() called but Baton capture is OFF (%s is set), "
                "so there is no session to escalate. Unset it to file real "
                "tickets.",
                self._disabled_switch,
            )
            return {"ticket_id": "queued", "ticket_url": None}
        if self._console_url is None:
            _log.warning(
                "handle.escalate() called but sink has no Console URL "
                "(dev mode — using StdoutSink or FileSink). "
                "Switch to HttpSink to file real tickets."
            )
            return {"ticket_id": "queued", "ticket_url": None}

        resolved_session_id = session_id if session_id else self.session_id
        body: dict[str, object] = {"session_id": resolved_session_id}
        if annotation_seq is not None:
            body["annotation_seq"] = annotation_seq

        # Reuse a shared client across calls — avoids a TCP+TLS handshake per escalation.
        if self._http_client is None:
            import httpx  # console_url is set ⇒ HttpSink in use ⇒ [http] extra installed

            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

        response = await self._http_client.post(
            f"{self._console_url}/v0/escalate",
            json=body,
            headers={"Authorization": f"Bearer {self._console_api_key}"},
        )
        response.raise_for_status()
        data = response.json()

        return {
            "ticket_id": str(data.get("ticket_id", "")),
            "ticket_url": data.get("ticket_url"),
        }


def disabled_handle(switch: str, surface: str, supplied_sink: Sink | None = None) -> BatonHandle:
    """The handle ``install_baton`` returns when the off switch is set.

    Shaped so a vendor's existing code keeps working untouched: ``flush()`` and
    ``aclose()`` in a ``finally`` do nothing, and ``escalate()`` takes the
    already-existing dev-mode path (no Console URL ⇒ a logged warning and a
    queued ticket id) rather than needing a branch of its own. Nothing here is
    a second way to configure capture off — the switch is an environment
    variable, deliberately, so the recipe C14 writes has one story to tell.

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
        # Nothing writes to it (nothing is wrapped) and ``_disabled_switch``
        # stops it being used as a Console endpoint.
        sink=supplied_sink if supplied_sink is not None else DisabledSink(),
        annotation_tool_name="",
        vendor_id="",
        session_id="baton-disabled",
        disabled_switch=switch,
    )
