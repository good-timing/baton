"""BatonHandle + BatonExtension — composable vendor extension protocol.

``BatonHandle`` is returned by ``install_baton`` and shared here so both
adapter packages and the extension protocol reference the same class.

``BatonExtension`` is a base class for vendor-authored extensions. Subclass
it and override the channels you need; the default no-ops let you ignore the
rest.

Six channels:

1. ``register_tools`` — add tools to the MCP server (captured automatically).
2. ``description_directive`` — append a condition-action directive to the
   annotation tool's description (no truncation risk; loaded on every call).
3. ``instructions_slice`` — contribute text to server instructions (budgeted;
   shares the ~2K-char Claude Code cap with the base template).
4. ``on_handle`` — receive the live ``BatonHandle`` post-install for
   correlation (``handle.session_id`` ties tickets / logs to the event stream).

Channels 5 (response_directives / #88) and 6 (schema_state) are planned
follow-ups; the four above retire all three monkey-patches in the current
SingleStore demo patch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baton.sinks import Sink


class BatonHandle:
    """Handle returned from ``install_baton`` for lifecycle management and
    session correlation.

    Attributes:
        sink: The configured sink; flush / aclose delegate here.
        annotation_tool_name: The resolved annotation tool name for this
            vendor (e.g., ``"acme_annotate"``).
        vendor_id: The vendor ID supplied to ``VendorConfig``.
        session_id: Process-lifetime session ID baked into every emitted event.
            Use this in extension-registered tools (e.g., ``create_support_ticket``)
            to correlate vendor-side artifacts back to the Baton event stream.
            Equivalent to the ``session_id`` on all emitted ``_EventEnvelope``
            objects. See SPEC §3.4 for the layered fallback resolution.
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
        """Flush pending events held by the sink."""
        await self.sink.flush()

    async def aclose(self) -> None:
        """Flush and release sink resources. Subsequent writes raise."""
        await self.sink.aclose()


class BatonExtension:
    """Base class for composable Baton vendor extensions.

    Override the channels you need; unused channels are safe no-ops by default.

    **Installation order inside ``install_baton``:**

    1. ``register_tools(mcp)`` — called after Baton's capture middleware /
       tool-wrapping is applied, so extension tools are captured automatically.
    2. ``on_handle(handle)`` — called after all tools are registered, with the
       fully-initialised ``BatonHandle``. Store ``handle.session_id`` here for
       use in your registered tools.

    Example::

        class TicketExtension(BatonExtension):
            def __init__(self, ticket_url: str) -> None:
                self._ticket_url = ticket_url
                self._session_id: str = ""

            def register_tools(self, mcp: Any) -> None:
                @mcp.tool(name="file_ticket", description="File a support ticket.")
                async def file_ticket(title: str, body: str) -> dict:
                    # self._session_id is populated by on_handle before any call
                    ...

            def description_directive(self) -> str | None:
                return (
                    "IF signal_type is feature_gap or failure, MUST either call "
                    "file_ticket (ask user first) or offer to do so."
                )

            def on_handle(self, handle: BatonHandle) -> None:
                self._session_id = handle.session_id
    """

    def register_tools(self, mcp: Any) -> None:
        """Register additional tools on the MCP server.

        ``mcp`` is the same object passed to ``install_baton``. Call
        ``mcp.tool(...)`` here as you would in the vendor's own setup code.
        Baton's capture layer is already applied at this point, so calls to
        extension tools are emitted as ``tool_call_start`` / ``tool_call_end``
        events automatically.
        """

    def description_directive(self) -> str | None:
        """Additional directive appended to the annotation tool's description.

        The annotation tool is the **strongest reliable channel**: its
        description is loaded on every call, immune to server-instructions
        truncation. Use this for condition-action rules that must fire at
        annotation time, e.g.:

            "IF signal_type is feature_gap, MUST call file_ticket or offer to."

        Return ``None`` to contribute nothing (default).
        """
        return None

    def instructions_slice(self) -> str | None:
        """Text appended to server instructions (shares the ~2K-char budget).

        Server instructions are subject to Claude Code's empirical ~2087-char
        truncation cap. The base template uses ~1.1K chars, leaving ~900 chars
        for extensions. Prefer ``description_directive`` for behavioural guidance
        — it has no cap and fires at call time.

        Return ``None`` to contribute nothing (default).
        """
        return None

    def on_handle(self, handle: BatonHandle) -> None:
        """Called after ``install_baton`` completes; receives the live handle.

        ``handle.session_id`` is the process-lifetime session ID embedded in
        every emitted event. Store it here for use in tools registered via
        ``register_tools`` to correlate vendor-side artifacts (tickets, logs)
        back to the Baton event stream.
        """
