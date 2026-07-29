"""A small, representative published MCP server — the kind you'd find on r/mcp.

This file stands in for *someone else's* FastMCP server. The only Baton-specific
lines are the three inside the `--- Baton (added in the PR) ---` block below;
everything else is the vendor's original code, untouched.

Run the demo against it with `python examples/pr-wrap/demo.py`.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

# ----------------------------------------------------------------------------
# The vendor's original server — a tiny bookmarks tool. Unchanged by the PR.
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
# --- Baton (added in the PR) — 3 lines, zero new dependencies -----------------
# `pydantic`/`httpx` already ship with `mcp`/`fastmcp`; only `baton-sdk` is new.
# StdoutSink writes JSONL to stderr — no infra, no account, nothing to configure.
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
