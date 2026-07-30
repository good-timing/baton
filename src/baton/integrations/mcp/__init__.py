"""MCP integration — wraps a vendor's official ``mcp.server.fastmcp.FastMCP``.

Five-line vendor integration via ``install_baton(mcp, VendorConfig(...))``;
wraps each registered tool's handler to emit ``tool_call_*`` events around
the vendor's tool fn, and exposes the vendor-namespaced annotation tool.

Targets the official Anthropic ``mcp`` package's server class — on mcp 1.x::

    from mcp.server.fastmcp import FastMCP

or on mcp 2.0 (renamed; same API)::

    from mcp.server.mcpserver import MCPServer

``install_baton`` supports both (see ``_compat.py``). For the standalone
``fastmcp`` library, use ``baton.integrations.fastmcp`` (different library,
different hook mechanism).

Requires the ``baton-sdk[mcp]`` install extra:

    pip install baton-sdk[mcp]

Example:

```python
from mcp.server.fastmcp import FastMCP
from baton.integrations.mcp import install_baton, VendorConfig
from baton.sinks import StdoutSink

mcp = FastMCP("your-vendor-mcp")
handle = install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=StdoutSink(),
))

@mcp.tool()
def your_tool(...): ...
```

Tools registered AFTER ``install_baton(...)`` are also wrapped — the
``add_tool`` method is patched so new registrations get the same emission
wrapping.
"""

from __future__ import annotations

from baton.integrations._config import VendorConfig
from baton.integrations._handle import BatonHandle
from baton.integrations.mcp.install import install_baton

__all__ = [
    "BatonHandle",
    "VendorConfig",
    "install_baton",
]
