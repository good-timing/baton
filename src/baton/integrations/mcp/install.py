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

import logging

from baton._state import ProactiveTracker, SessionCounter
from baton._uuid import uuid7
from baton.integrations._config import VendorConfig, _validate_vendor_config
from baton.integrations._handle import BatonHandle
from baton.integrations._llm_text import build_server_instructions
from baton.integrations._surface import build_server_meta
from baton.integrations.mcp._compat import (
    MCPServerClass as FastMCP,
)
from baton.integrations.mcp._compat import (
    get_lowlevel_server,
    require_high_level_server,
    set_server_instructions,
)
from baton.integrations.mcp._tool_wrap import install_wraps
from baton.integrations.mcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.scrub import Scrubber

logger = logging.getLogger(__name__)


def install_baton(mcp: FastMCP, config: VendorConfig) -> BatonHandle:
    """Install Baton into an official-SDK FastMCP server. See module docstring for usage."""
    # FIRST, before any validation or mutation: everything below assumes a
    # high-level server, and the failures downstream are both late and
    # uninformative — the low-level-server lookup is caught and merely logged,
    # the instructions write silently succeeds on a bare ``Server``, and only
    # ``install_wraps`` finally dies, on a server Baton has already modified.
    require_high_level_server(mcp)
    _validate_vendor_config(config)

    # Default to a fresh Scrubber per install so PII redaction is on out
    # of the box; vendors needing raw payloads pass ``identity_scrub``
    # via ``VendorConfig.scrubber``.
    scrubber = config.scrubber or Scrubber()
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    # Shared across the wrap layer (synthesises a proactive from the first
    # injected intent) and the annotation tool (emits one when called
    # proactively) so a session opens at most one proactive.
    proactive_tracker = ProactiveTracker()
    sink = config.sink

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id, config.annotation_tool_name
    )

    # Captured BEFORE any Baton mutation below — the vendor-true baseline the
    # surface-snapshot hash is authored against. See integrations._surface.
    # Best-effort: unlike set_server_instructions below (load-bearing, fails
    # loud), a capture failure here must not block install — the vendor's
    # server still needs to start even on a future/unknown mcp layout where
    # get_lowlevel_server can't find the low-level server. Degrades to an
    # empty server_meta (surface_snapshot's server_info/capabilities/
    # instructions come through as null; tool capture is unaffected).
    try:
        server_meta = build_server_meta(get_lowlevel_server(mcp))
    except AttributeError:
        logger.exception("baton: surface-snapshot server_meta capture failed at install")
        server_meta = {}

    # Server instructions — load-bearing on instruction-aware runtimes. The
    # ``instructions`` property is read-only on both mcp 1.x and 2.0; the
    # writable backing differs across the rename (``_mcp_server`` →
    # ``_lowlevel_server``), so route through the compat helper.
    instructions = build_server_instructions(
        vendor_display_name=config.vendor_display_name,
        annotation_tool_name=annotation_tool_name,
        proactive_mode=config.proactive_mode,
    )
    set_server_instructions(mcp, instructions)

    # Wrap currently-registered tools + patch add_tool for future ones.
    install_wraps(
        mcp,
        tenant_id=config.vendor_id,
        vendor_id=config.vendor_id,
        consent_token=config.consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        default_agent_runtime=config.default_agent_runtime,
        scrubber=scrubber,
        annotation_tool_name=annotation_tool_name,
        intent_param_mode=config.intent_param_mode,
        proactive_tracker=proactive_tracker,
        resolve_session_id_hook=config.resolve_session_id,
        server_meta=server_meta,
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
        proactive_mode=config.proactive_mode,
        scrubber=scrubber,
        proactive_tracker=proactive_tracker,
    )

    return BatonHandle(
        sink=sink,
        annotation_tool_name=annotation_tool_name,
        vendor_id=config.vendor_id,
        session_id=fallback_session_id,
    )
