"""MCP integration — wraps a vendor's FastMCP server with the Baton SDK.

Five-line vendor integration via ``install_baton(mcp, VendorConfig(...))``;
registers the BatonMiddleware on every MCP tool call and exposes the
vendor-namespaced annotation tool.

Requires the ``baton-sdk[mcp]`` install extra:

    pip install baton-sdk[mcp]

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

from baton.integrations.fastmcp.install import BatonHandle, VendorConfig, install_baton

__all__ = [
    "BatonHandle",
    "VendorConfig",
    "install_baton",
]
