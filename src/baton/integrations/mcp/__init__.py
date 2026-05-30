"""MCP integration — wraps a vendor's FastMCP server with the Baton SDK.

Five-line vendor integration via ``install_baton(mcp, VendorConfig(...))``;
registers the BatonMiddleware on every MCP tool call and exposes the
vendor-namespaced annotation tool.

Requires the ``baton[mcp]`` install extra:

    pip install baton[mcp]

Example:

```python
from fastmcp import FastMCP
from baton.integrations.mcp import install_baton, VendorConfig

mcp = FastMCP("your-vendor-mcp")
handle = install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    console_url=os.environ["BATON_CONSOLE_URL"],
    api_key=os.environ["BATON_API_KEY"],
))
```
"""

from __future__ import annotations

from baton.integrations.mcp.install import BatonHandle, VendorConfig, install_baton

__all__ = [
    "BatonHandle",
    "VendorConfig",
    "install_baton",
]
