"""MCP integration — wraps a server built on the STANDALONE ``fastmcp`` library.

That is ``fastmcp.FastMCP`` (the separate project, v2.x-4.x), NOT the official
Anthropic ``mcp`` package. Both libraries name their server class ``FastMCP``
and both adapters' ``install_baton`` take the same arguments, so this is the
easy mixup to make — if your import is ``from mcp.server.fastmcp import
FastMCP`` (mcp 1.x) or ``from mcp.server.mcpserver import MCPServer`` (mcp
2.x), you want ``baton.integrations.mcp`` instead. Passing the wrong one is
refused up front with a message naming the other adapter.

Five-line vendor integration via ``install_baton(mcp, VendorConfig(...))``;
registers the BatonMiddleware on every MCP tool call and exposes the
vendor-namespaced annotation tool.

Requires the ``baton-sdk[fastmcp]`` install extra — note ``[fastmcp]``, not
``[mcp]``: the latter installs the official SDK, which this adapter does not
target and cannot wrap.

    pip install baton-sdk[fastmcp]

Example:

```python
from fastmcp import FastMCP
from baton.integrations.fastmcp import install_baton, VendorConfig
from baton.sinks import StdoutSink

mcp = FastMCP("your-vendor-mcp")
handle = install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    consent_token=os.environ["BATON_CONSENT_TOKEN"],
    sink=StdoutSink(),  # or FileSink / HttpSink / MultiSink
))
```
"""

from __future__ import annotations

from baton.integrations._config import SessionResolutionContext, VendorConfig
from baton.integrations._handle import BatonHandle
from baton.integrations.fastmcp.install import install_baton

__all__ = [
    "BatonHandle",
    "SessionResolutionContext",
    "VendorConfig",
    "install_baton",
]
