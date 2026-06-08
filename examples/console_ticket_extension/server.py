"""Example MCP server using ConsoleTicketExtension.

Shows the five-line install pattern with an extension attached.  Run with:

    cd examples/console_ticket_extension
    BATON_CONSENT_TOKEN=<token> PYLON_URL=http://localhost:8090/tickets \\
        python server.py

Environment variables:

    BATON_CONSENT_TOKEN  Required — per SPEC §2.3.
    BATON_INGEST_URL     Baton collector endpoint (default: stdout).
    PYLON_URL            Ticket endpoint (default: http://localhost:8090/tickets).
    PYLON_TOKEN          Bearer token for the ticket endpoint (optional).
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from baton.integrations.fastmcp import VendorConfig, install_baton
from baton.sinks import HttpSink, StdoutSink

from extension import ConsoleTicketExtension  # noqa: E402 (run from this directory)

mcp = FastMCP("acme-support")


@mcp.tool()
async def query_data(query: str) -> dict[str, str]:
    """Run a data query against Acme's API."""
    return {"result": f"Data for: {query}"}


ingest_url = os.environ.get("BATON_INGEST_URL", "")
sink = HttpSink(url=ingest_url, api_key="") if ingest_url else StdoutSink()

handle = install_baton(
    mcp,
    VendorConfig(
        vendor_id="acme",
        vendor_display_name="Acme",
        consent_token=os.environ.get("BATON_CONSENT_TOKEN", "dev-token"),
        sink=sink,
        extensions=[
            ConsoleTicketExtension(
                pylon_url=os.environ.get("PYLON_URL", "http://localhost:8090/tickets"),
                pylon_token=os.environ.get("PYLON_TOKEN", ""),
            )
        ],
    ),
)

if __name__ == "__main__":
    mcp.run()
