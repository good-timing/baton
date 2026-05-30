"""``install_baton`` — the five-line vendor integration entry point.

Wires together the EventEmitter, BatonMiddleware, and the vendor-namespaced
annotation tool. Vendor calls this once after constructing their FastMCP
server; everything downstream (event emission, retry, buffering, dispatch
back at the Console worker) is handled by the SDK.

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

Returns a ``BatonHandle`` exposing ``flush()`` + ``aclose()`` for graceful
shutdown.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid7

from fastmcp import FastMCP

from baton._state import SessionCounter
from baton.emitter import EventEmitter
from baton.integrations.mcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.integrations.mcp.instructions import build_server_instructions
from baton.integrations.mcp.middleware import BatonMiddleware
from baton.scrub import identity_scrub

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
    (SPEC §5.5): no Baton-branded strings reach the calling agent."""

    console_url: str
    """Base URL of the Console ingest endpoint (e.g.,
    ``"https://acme.console.example.com"``). SDK posts events to
    ``{console_url}/v0/events`` per SPEC §2.1."""

    api_key: str
    """Bearer token for ingest auth. Per-vendor, generated at tenant
    provisioning."""

    consent_token: str = ""
    """End-user consent token attached to every emitted event per SPEC §2.3 +
    §3.1 (the Console MUST reject events missing it). v0 form: a single UUID
    granted at SDK init; v0.x will extend to per-end-user OAuth-scoped tokens
    (CHARTER OD-2). Treated as effectively required — empty string raises at
    ``install_baton`` time."""

    annotation_tool_name: str | None = None
    """Optional override for the annotation tool name. Default is
    ``{vendor_id}_annotate``."""

    default_agent_runtime: str = "unknown"
    """Default value for the ``agent_runtime`` field on emitted events when
    the SDK can't detect from ``_meta``. Set this explicitly when shipping
    into a known runtime (e.g., ``"claude-code"`` for a Claude Code plugin)."""

    scrubber: Callable[[Any], Any] | None = None
    """PII scrubber per SPEC §7. Default (None) uses the v0.2 identity-scrub
    placeholder; real PII rules land in Day 4+ implementation."""


class BatonHandle:
    """Handle returned from ``install_baton`` for graceful shutdown."""

    def __init__(
        self,
        *,
        emitter: EventEmitter,
        annotation_tool_name: str,
        vendor_id: str,
    ) -> None:
        self.emitter = emitter
        self.annotation_tool_name = annotation_tool_name
        self.vendor_id = vendor_id

    async def flush(self) -> None:
        """Flush pending events to the Console. Returns when the buffer is
        empty or the circuit breaker is open."""
        await self.emitter.flush()

    async def aclose(self) -> None:
        """Flush pending events and close the HTTP client. Subsequent emit
        calls on this handle's emitter will raise ``RuntimeError``."""
        await self.emitter.aclose()


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
            "VendorConfig.consent_token is required per SPEC §2.3 — the Console "
            "MUST reject events without a valid consent_token. v0 form: a single "
            "UUID granted at SDK init."
        )

    scrubber = config.scrubber or identity_scrub
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    emitter = EventEmitter(
        console_url=config.console_url,
        api_key=config.api_key,
    )

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id, config.annotation_tool_name
    )

    # Server instructions — load-bearing on instruction-aware runtimes.
    mcp.instructions = build_server_instructions(
        vendor_display_name=config.vendor_display_name,
        annotation_tool_name=annotation_tool_name,
    )

    # Middleware emits tool_call_* events; skips them for the annotation tool
    # (the annotation handler emits its own annotation event).
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id=config.vendor_id,
            consent_token=config.consent_token,
            emitter=emitter,
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
        emitter=emitter,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=config.default_agent_runtime,
        annotation_tool_name=config.annotation_tool_name,
        scrubber=scrubber,
    )

    return BatonHandle(
        emitter=emitter,
        annotation_tool_name=annotation_tool_name,
        vendor_id=config.vendor_id,
    )
