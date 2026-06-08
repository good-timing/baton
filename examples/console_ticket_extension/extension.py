"""ConsoleTicketExtension — consent-gated support-ticket extension.

Registers a ``create_support_ticket`` tool on the vendor's MCP server and
appends a condition-action directive to the annotation tool's description so
the agent offers ticket filing when it records friction.

This is the canonical ``BatonExtension`` consumer — it replaces the three
manual monkey-patches in the SingleStore demo's ``install_baton.patch``:

  - Tool description patch  → ``description_directive()``
  - Closure walk for session_id → ``on_handle(handle)``
  - Instructions prepend hack  → dropped (superseded by #85 + description path)

Usage (fastmcp adapter)::

    from baton.integrations.fastmcp import install_baton, VendorConfig
    from examples.console_ticket_extension.extension import ConsoleTicketExtension

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="acme",
            vendor_display_name="Acme",
            consent_token=os.environ["BATON_CONSENT_TOKEN"],
            extensions=[
                ConsoleTicketExtension(pylon_url="https://support.acme.example/tickets"),
            ],
        ),
    )
"""

from __future__ import annotations

from typing import Any

import httpx

from baton.extension import BatonExtension, BatonHandle

_DESCRIPTION_DIRECTIVE = (
    "IF signal_type is feature_gap, failure, or dead_end, you MUST in the "
    "same turn EITHER call create_support_ticket (ASK THE USER FOR PERMISSION "
    "first — tell them what you'll include and wait for yes/no) OR explicitly "
    "tell the user that support-ticket filing is available and offer to do it. "
    "Do NOT silently drop friction signals that rise to these severity levels."
)


class ConsoleTicketExtension(BatonExtension):
    """Consent-gated support-ticket extension.

    Registers ``create_support_ticket`` on the vendor MCP server and
    adds a directive that makes the agent offer to file tickets whenever it
    records a high-severity friction signal (feature_gap / failure / dead_end).

    ``handle.session_id`` is threaded into every ticket payload so the vendor's
    support team can look up the corresponding Baton event stream.
    """

    def __init__(self, pylon_url: str, pylon_token: str = "") -> None:
        self._pylon_url = pylon_url
        self._pylon_token = pylon_token
        self._handle: BatonHandle | None = None

    def register_tools(self, mcp: Any) -> None:
        @mcp.tool(
            name="create_support_ticket",
            description=(
                "File a support ticket with the vendor's support team. "
                "ASK THE USER FOR PERMISSION before calling this — say what "
                "you will include in the ticket body and wait for their yes/no. "
                "Call this after recording a friction annotation so the ticket "
                "carries the same session context as the captured signal."
            ),
        )
        async def create_support_ticket(
            title: str,
            body: str,
        ) -> dict[str, Any]:
            """File a support ticket. Returns {filed: true, session_id: ...}."""
            session_id = self._handle.session_id if self._handle else "unknown"
            payload = {"title": title, "body": body, "session_id": session_id}
            headers: dict[str, str] = {}
            if self._pylon_token:
                headers["Authorization"] = f"Bearer {self._pylon_token}"
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._pylon_url,
                    json=payload,
                    headers=headers,
                    timeout=10.0,
                )
                resp.raise_for_status()
            return {"filed": True, "session_id": session_id}

    def description_directive(self) -> str | None:
        return _DESCRIPTION_DIRECTIVE

    def on_handle(self, handle: BatonHandle) -> None:
        self._handle = handle
