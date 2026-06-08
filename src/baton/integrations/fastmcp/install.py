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

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastmcp import FastMCP
from uuid6 import uuid7

from baton._state import SessionCounter
from baton.integrations.fastmcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.integrations.fastmcp.instructions import build_server_instructions
from baton.integrations.fastmcp.middleware import BatonMiddleware
from baton.scrub import identity_scrub
from baton.sinks import Sink, StdoutSink

# Vendor IDs become annotation tool name prefixes; same client-pattern as
# annotation tool names. Reject dots so the default tool name is valid.
_VENDOR_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


@dataclass
class VendorConfig:
    """Vendor-side configuration for ``install_baton``."""

    vendor_id: str
    """Short stable identifier for the vendor (e.g., ``"acme"``,
    ``"example-vendor"``). Becomes the default annotation tool name prefix
    (``{vendor_id}_annotate``); must match the cross-runtime tool-name pattern."""

    vendor_display_name: str
    """Human-readable vendor name used in server instructions, annotation
    tool description, and any LLM-facing strings. Whitelabel obligation
    (SPEC §5.4): no Baton-branded strings reach the calling agent."""

    consent_token: str = ""
    """End-user consent token attached to every emitted event per SPEC §2.3 +
    §3.1 (the consumer of the events MUST reject events missing it). v0 form:
    a single UUID granted at SDK init; v0.x will extend to per-end-user
    OAuth-scoped tokens (CHARTER ADR-1). Treated as effectively required —
    empty string raises at ``install_baton`` time."""

    sink: Sink = field(default_factory=StdoutSink)
    """Where events go. Defaults to ``StdoutSink()`` — zero-config dev mode
    that writes JSON Lines to stderr. Pass an ``HttpSink`` to ship to a
    collector, ``FileSink`` to capture for later analysis, or ``MultiSink``
    to fan out (e.g., stdout + http during development)."""

    annotation_tool_name: str | None = None
    """Optional override for the annotation tool name. Default is
    ``{vendor_id}_annotate``."""

    default_agent_runtime: str = "unknown"
    """Default value for the ``agent_runtime`` field on emitted events when
    the SDK can't detect from ``_meta``. Set this explicitly when shipping
    into a known runtime (e.g., ``"claude-code"`` for a Claude Code plugin)."""

    scrubber: Callable[[Any], Any] | None = None
    """PII scrubber per SPEC §7. Default (None) uses the identity-scrub
    placeholder; vendors handling sensitive data should supply their own."""


class BatonHandle:
    """Handle returned from ``install_baton`` for graceful shutdown and
    session correlation.

    ``session_id`` is the process-lifetime identifier baked into every emitted
    event. Vendor tools that need to correlate external artifacts (e.g., a
    Console-issued support ticket) with the Baton event stream should include
    this value in their payloads.
    """

    def __init__(
        self,
        *,
        sink: Sink,
        annotation_tool_name: str,
        vendor_id: str,
        session_id: str,
    ) -> None:
        self.sink = sink
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id
        self.session_id = session_id

    async def flush(self) -> None:
        """Flush any pending events held by the sink."""
        await self.sink.flush()

    async def aclose(self) -> None:
        """Flush and release sink resources. Subsequent writes raise."""
        await self.sink.aclose()


def install_baton(mcp: FastMCP, config: VendorConfig) -> BatonHandle:
    """Install Baton into a FastMCP server. See module docstring for usage."""
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
