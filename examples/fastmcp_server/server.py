"""A small FastMCP server (a tiny bookmarks tool) wrapped with Baton.

Everything above the `--- Baton integration ---` block is an ordinary FastMCP
server; the only Baton-specific code is the `install_baton(...)` block below.

Run the demo against it with `python examples/fastmcp_server/demo.py`.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

# ----------------------------------------------------------------------------
# An ordinary FastMCP server — a tiny bookmarks tool.
# ----------------------------------------------------------------------------

mcp = FastMCP("bookmarks")

_BOOKMARKS: dict[str, str] = {}


@mcp.tool()
def save_bookmark(name: str, url: str) -> str:
    """Save a URL under a short name."""
    _BOOKMARKS[name] = url
    return f"saved {name!r} -> {url}"


@mcp.tool()
def get_bookmark(name: str) -> str:
    """Look up a saved URL by name."""
    if name not in _BOOKMARKS:
        return f"no bookmark named {name!r}"
    return _BOOKMARKS[name]


# ----------------------------------------------------------------------------
# --- Baton integration — three lines --------------------------------------------
# StdoutSink writes JSONL to stderr, so this needs no backend or configuration.
# ----------------------------------------------------------------------------
from baton.integrations.fastmcp import VendorConfig, install_baton  # noqa: E402
from baton.sinks import StdoutSink  # noqa: E402

install_baton(
    mcp,
    VendorConfig(
        vendor_id="bookmarks",
        vendor_display_name="Bookmarks",
        consent_token=os.environ.get("BATON_CONSENT_TOKEN", "demo-local"),
        sink=StdoutSink(),  # swap for HttpSink(...) to ship to a Console
    ),
)
# ----------------------------------------------------------------------------


if __name__ == "__main__":
    mcp.run()
