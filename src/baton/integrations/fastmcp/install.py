"""``install_baton`` — the vendor's MCP integration entry point.

Wires together a ``Sink``, the ``BatonMiddleware``, and the vendor-namespaced
annotation tool. Vendor calls this once after constructing their FastMCP
server; everything downstream (event capture, sink-specific egress, dispatch
back at the destination) is handled by the SDK.

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

If ``sink`` is omitted, defaults to ``StdoutSink()`` — zero-config dev mode
that writes one JSON envelope per line to stderr. Swap in ``HttpSink(...)``
when you're ready to ship events to a collector.

Returns a ``BatonHandle`` exposing ``flush()`` + ``aclose()`` for graceful
shutdown.
"""

from __future__ import annotations

from fastmcp import FastMCP
from uuid6 import uuid7

from baton._state import SessionCounter
from baton.integrations._config import VendorConfig, _validate_vendor_config
from baton.integrations._handle import BatonHandle
from baton.integrations._llm_text import build_server_instructions
from baton.integrations.fastmcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.integrations.fastmcp.middleware import BatonMiddleware
from baton.scrub import Scrubber


def install_baton(mcp: FastMCP, config: VendorConfig) -> BatonHandle:
    """Install Baton into a FastMCP server. See module docstring for usage."""
    _validate_vendor_config(config)

    # Default to a fresh Scrubber per install so PII redaction is on out
    # of the box; vendors needing raw payloads pass ``identity_scrub``
    # via ``VendorConfig.scrubber``.
    scrubber = config.scrubber or Scrubber()
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    sink = config.sink

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id, config.annotation_tool_name
    )

    # Server instructions — load-bearing on instruction-aware runtimes.
    # FastMCP >=1.10 made `instructions` a read-only property; fall back to the
    # backing MCPServer attribute when the public setter isn't available.
    instructions = build_server_instructions(
        vendor_display_name=config.vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )
    try:
        mcp.instructions = instructions
    except AttributeError:
        mcp._mcp_server.instructions = instructions

    # Middleware emits tool_call_* events; skips them for the annotation tool
    # (the annotation handler emits its own annotation event).
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id=config.vendor_id,
            vendor_id=config.vendor_id,
            consent_token=config.consent_token,
            sink=sink,
            counter=counter,
            fallback_session_id=fallback_session_id,
            default_agent_runtime=config.default_agent_runtime,
            scrubber=scrubber,
            annotation_tool_name=annotation_tool_name,
        )
    )

    register_annotation_tool(
        mcp,
        vendor_id=config.vendor_id,
        vendor_display_name=config.vendor_display_name,
        tenant_id=config.vendor_id,
        consent_token=config.consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=config.default_agent_runtime,
        annotation_tool_name=config.annotation_tool_name,
        scrubber=scrubber,
    )

    return BatonHandle(
        sink=sink,
        annotation_tool_name=annotation_tool_name,
        vendor_id=config.vendor_id,
        session_id=fallback_session_id,
    )
