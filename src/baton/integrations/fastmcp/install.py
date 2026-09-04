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

import logging
from typing import Any

from fastmcp import FastMCP

from baton._state import ProactiveTracker, SessionCounter
from baton._uuid import uuid7
from baton.integrations._config import VendorConfig, _validate_vendor_config
from baton.integrations._handle import BatonHandle
from baton.integrations._llm_text import build_server_instructions
from baton.integrations._surface import build_server_meta
from baton.integrations.fastmcp.annotation import (
    derive_annotation_tool_name,
    register_annotation_tool,
)
from baton.integrations.fastmcp.middleware import BatonMiddleware
from baton.integrations.mcp._registry import get_tool_manager
from baton.scrub import Scrubber

logger = logging.getLogger(__name__)


def _require_fastmcp_server(mcp: Any) -> None:
    """Refuse a server this adapter can't install into, BEFORE it mutates one.

    Mirror of ``baton.integrations.mcp._compat.require_high_level_server``, for
    the mirror-image failure. Install below captures a surface snapshot, then
    WRITES server instructions, and only then calls ``add_middleware``. Handed
    the official mcp SDK's server **on mcp 1.x**, the first two steps succeed —
    ``_mcp_server`` is there, and the read-only-property fallback writes the
    instructions straight to it — so install died at ``add_middleware`` on a
    server already advertising an annotation tool that was never registered.
    A half-install, not a refusal. (On mcp 2.x the same object failed earlier
    and more honestly: the backing was renamed ``_lowlevel_server``, which this
    adapter does not route through a compat shim, so the instructions write
    itself raised and nothing was mutated. The guard makes both cases the same
    clean refusal.)

    ``add_middleware`` is the seam: it is what this adapter cannot work without
    and what actually failed. Duck-typed rather than ``isinstance(mcp,
    FastMCP)`` — the mcp adapter is duck-typed on the same reasoning, and a
    vendor proxy that forwards the API should still install.

    An ``mcp.*`` object is only sent to the sibling adapter if that adapter can
    actually take it, which is why the tool registry is probed here too. A bare
    low-level ``mcp.server.Server`` is also an ``mcp.*`` object and the sibling
    guard refuses it — pointing there would be a round trip ending in a second
    refusal, the wrong-advice failure this whole area exists to remove.
    """
    if callable(getattr(mcp, "add_middleware", None)):
        return

    if type(mcp).__module__.split(".")[0] == "mcp":
        try:
            registry = get_tool_manager(mcp)
        except AttributeError:
            registry = None
        if registry is None:
            raise TypeError(
                "baton: this looks like the official mcp SDK's low-level "
                "``mcp.server.Server`` — the API the reference servers (git, "
                "time, fetch) are written against. Neither adapter supports it "
                "yet: this one captures on the standalone ``fastmcp`` "
                "library's middleware chain, and "
                "``baton.integrations.mcp.install_baton`` captures on the "
                "high-level server's tool registry, which a low-level Server "
                "has none of."
            )
        raise TypeError(
            "baton: this is the official mcp SDK's high-level server, but "
            "``baton.integrations.fastmcp.install_baton`` adapts the "
            "standalone ``fastmcp`` library — different library, different "
            "hook mechanism (middleware vs. tool-handler wrapping). Use "
            "``baton.integrations.mcp.install_baton`` instead; same signature, "
            "same VendorConfig."
        )
    raise TypeError(
        "baton: install_baton needs the standalone ``fastmcp`` library's "
        "FastMCP, and this object has no ``add_middleware`` — the middleware "
        "chain is the seam this adapter captures on. If this IS a fastmcp "
        "FastMCP, then it is a version problem: pin fastmcp>=2.10,<4."
    )


def install_baton(mcp: FastMCP, config: VendorConfig) -> BatonHandle:
    """Install Baton into a FastMCP server. See module docstring for usage."""
    # FIRST, before any validation or mutation: everything below assumes this
    # library's FastMCP, and the failure downstream is both late and
    # uninformative — the surface capture and the instructions write both
    # succeed on the official SDK's server, and only ``add_middleware`` finally
    # dies, on a server Baton has already modified.
    _require_fastmcp_server(mcp)
    _validate_vendor_config(config)

    # Default to a fresh Scrubber per install so PII redaction is on out
    # of the box; vendors needing raw payloads pass ``identity_scrub``
    # via ``VendorConfig.scrubber``.
    scrubber = config.scrubber or Scrubber()
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    # Shared across the middleware (synthesises a proactive from the first
    # injected intent) and the annotation tool (emits one when called
    # proactively) so a session opens at most one proactive.
    proactive_tracker = ProactiveTracker()
    sink = config.sink

    annotation_tool_name = derive_annotation_tool_name(
        config.vendor_id, config.annotation_tool_name
    )

    # Captured BEFORE any Baton mutation below — the vendor-true baseline the
    # surface-snapshot hash is authored against. See integrations._surface.
    # Best-effort: unlike the instructions write below (load-bearing), a
    # capture failure here must not block install — the vendor's server
    # still needs to start even on a future fastmcp layout that drops or
    # renames the private ``_mcp_server`` attribute. Degrades to an empty
    # server_meta (surface_snapshot's server_info/capabilities/instructions
    # come through as null; tool capture is unaffected).
    try:
        server_meta = build_server_meta(mcp._mcp_server)
    except AttributeError:
        logger.exception("baton: surface-snapshot server_meta capture failed at install")
        server_meta = {}

    # Server instructions — load-bearing on instruction-aware runtimes.
    # FastMCP >=1.10 made `instructions` a read-only property; fall back to the
    # backing MCPServer attribute when the public setter isn't available.
    instructions = build_server_instructions(
        vendor_display_name=config.vendor_display_name,
        annotation_tool_name=annotation_tool_name,
        proactive_mode=config.proactive_mode,
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
            intent_param_mode=config.intent_param_mode,
            proactive_tracker=proactive_tracker,
            resolve_session_id_hook=config.resolve_session_id,
            server_meta=server_meta,
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
        proactive_mode=config.proactive_mode,
        scrubber=scrubber,
        proactive_tracker=proactive_tracker,
        resolve_session_id_hook=config.resolve_session_id,
    )

    return BatonHandle(
        sink=sink,
        annotation_tool_name=annotation_tool_name,
        vendor_id=config.vendor_id,
        session_id=fallback_session_id,
    )
