"""``install_baton`` — vendor entry point for the official mcp SDK.

Wires together a ``Sink``, the tool-handler wrap layer, and the
vendor-namespaced annotation tool against the official Anthropic
``mcp.server.fastmcp.FastMCP`` class.

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
```

For the standalone ``fastmcp`` library, use ``baton.integrations.fastmcp``
instead — different library, different hook mechanism (middleware vs.
tool-handler wrapping).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid7

from mcp.server.fastmcp import FastMCP

from baton._state import SessionCounter
from baton.integrations.mcp._tool_wrap import install_wraps
from baton.integrations.mcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.integrations.mcp.instructions import build_server_instructions
from baton.scrub import identity_scrub
from baton.sinks import Sink, StdoutSink

_VENDOR_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


@dataclass
class VendorConfig:
    """Vendor-side configuration for ``install_baton``."""

    vendor_id: str
    vendor_display_name: str
    consent_token: str = ""
    sink: Sink = field(default_factory=StdoutSink)
    annotation_tool_name: str | None = None
    default_agent_runtime: str = "unknown"
    scrubber: Callable[[Any], Any] | None = None


class BatonHandle:
    """Handle returned from ``install_baton`` for graceful shutdown."""

    def __init__(
        self,
        *,
        sink: Sink,
        annotation_tool_name: str,
        vendor_id: str,
    ) -> None:
        self.sink = sink
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id

    async def flush(self) -> None:
        """Flush pending events held by the sink."""
        await self.sink.flush()

    async def aclose(self) -> None:
        """Flush and release sink resources. Subsequent writes raise."""
        await self.sink.aclose()


def install_baton(mcp: FastMCP, config: VendorConfig) -> BatonHandle:
    """Install Baton into an official-SDK FastMCP server. See module docstring for usage."""
    if not _VENDOR_ID_PATTERN.match(config.vendor_id):
        raise ValueError(
            f"vendor_id {config.vendor_id!r} must match "
            f"{_VENDOR_ID_PATTERN.pattern!r} — used as the default annotation "
            f"tool name prefix; dots and other separators are rejected by "
            f"Claude Desktop's tool-name validator."
        )
    if not config.consent_token:
        raise ValueError(
            "VendorConfig.consent_token is required per SPEC §2.3 — events "
            "without a valid consent_token MUST be rejected by the consumer. "
            "v0 form: a single UUID granted at SDK init."
        )

    scrubber = config.scrubber or identity_scrub
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    sink = config.sink

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id, config.annotation_tool_name
    )

    # Server instructions — load-bearing on instruction-aware runtimes.
    # The official ``FastMCP.instructions`` is a read-only property; the
    # backing storage lives on ``_mcp_server.instructions``.
    instructions = build_server_instructions(
        vendor_display_name=config.vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )
    try:
        mcp.instructions = instructions  # type: ignore[misc]
    except AttributeError:
        mcp._mcp_server.instructions = instructions

    # Wrap currently-registered tools + patch add_tool for future ones.
    install_wraps(
        mcp,
        tenant_id=config.vendor_id,
        consent_token=config.consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=config.default_agent_runtime,
        scrubber=scrubber,
        annotation_tool_name=annotation_tool_name,
    )

    # Register the annotation tool LAST so the wrap layer's add_tool patch
    # knows to skip wrapping it (by name match).
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
    )
