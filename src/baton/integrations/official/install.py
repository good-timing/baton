"""``install_baton`` — vendor entry point for the official mcp SDK.

Wires together a ``Sink``, the tool-handler wrap layer, the tool list
capture, and, under ``report_mode="on"``, the vendor-namespaced annotation tool
against the official Anthropic ``mcp.server.fastmcp.FastMCP`` class.

```python
from mcp.server.fastmcp import FastMCP
from baton.integrations.official import install_baton, VendorConfig
from baton.sinks import StdoutSink

mcp = FastMCP("your-vendor-mcp")
handle = install_baton(mcp, VendorConfig(
    vendor_id="your-vendor",
    vendor_display_name="Your Vendor",
    sink=StdoutSink(),
))
```

For the standalone ``fastmcp`` library, use ``baton.integrations.standalone``
instead — different library, different hook mechanism (middleware vs.
tool-handler wrapping).

The whole install, for a server that SHIPS — one value from ``/account``
carrying the ingest host, the workspace, the server and the key::

    from baton import install_baton

    install_baton(mcp, dsn="https://baton_pk_...@ingest.example.com/ten_.../acme")

That form exists because a distributable server runs on every user's machine:
five environment variables cannot ship with it, so a key that has to arrive
that way means events that never arrive at all. ``VendorConfig`` stays the door
for everything else — a scrubber, injection modes, identity options — and takes
a ``dsn=`` field of its own so the two combine.

**The off switch.** Setting ``BATON_DISABLED=1`` in the
environment makes this function install NOTHING and raise nothing: no
middleware, no wrapped tools, no annotation tool on the surface, no
added instructions, no sink. The server behaves exactly as it would without
this call. See ``baton._optout``.

"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from baton._optout import capture_disabled
from baton._state import SessionCounter
from baton._uuid import uuid7
from baton.integrations._annotation_name import resolve_install_text
from baton.integrations._config import (
    VendorConfig,
    _resolve_tenant_id,
    _validate_vendor_config,
    build_config,
    resolve_sink,
)
from baton.integrations._handle import BatonHandle, disabled_handle
from baton.integrations._lifecycle import (
    CallSeam,
    CapturedRequest,
    install_lifecycle_capture,
    warn_if_results_are_withheld,
)
from baton.integrations._llm_text import with_vendor_instructions
from baton.integrations._surface import build_server_meta
from baton.integrations.official import _auth
from baton.integrations.official._compat import (
    MCPServerClass as FastMCP,
)
from baton.integrations.official._compat import (
    get_lowlevel_server,
    require_high_level_server,
    set_server_instructions,
)
from baton.integrations.official._registry import get_tool_registry
from baton.integrations.official._tool_wrap import (
    _extract_headers_from_context,
    _extract_meta_from_context,
    _resolve_call_session,
    install_wraps,
    observe_transport,
)
from baton.integrations.official.annotation import register_annotation_tool
from baton.scrub import Scrubber

logger = logging.getLogger(__name__)


class _RequestOnly:
    """Stands in for the ``Context`` a tool call has; a list request has none."""

    def __init__(self, request_context: Any) -> None:
        self.request_context = request_context
        self.session = getattr(request_context, "session", None)


def install_baton(
    mcp: FastMCP,
    config: VendorConfig | None = None,
    *,
    dsn: str | None = None,
) -> BatonHandle:
    """Install Baton into an official-SDK FastMCP server. See module docstring for usage."""
    # ⚠ **FIRST — ahead of the server-shape guard, the config build and every
    # validation below.** Off means install nothing and never throw, so this
    # cannot sit after a check that raises: a switch that can still abort a
    # vendor's boot is worse than no switch. It also has to precede
    # ``build_config``, which would otherwise construct an ``HttpSink`` (and
    # its httpx client) from a dsn for a capture that is not going to happen.
    switch = capture_disabled()
    if switch is not None:
        return disabled_handle(
            switch,
            "install_baton (official mcp SDK adapter)",
            config.sink if config is not None else None,
        )

    # FIRST, before any validation or mutation: everything below assumes a
    # high-level server, and the failures downstream are both late and
    # uninformative — the low-level-server lookup is caught and merely logged,
    # the instructions write silently succeeds on a bare ``Server``, and only
    # ``install_wraps`` finally dies, on a server Baton has already modified.
    require_high_level_server(mcp)
    # Read BEFORE ``build_config``, which fills an unset display name in from
    # the DSN and so leaves a defaulted value and a chosen one looking alike.
    display_name_given = config is not None and bool(config.vendor_display_name)
    config = build_config(config, dsn)
    _validate_vendor_config(config)
    warn_if_results_are_withheld(config.result_capture_mode)

    # Default to a fresh Scrubber per install so PII redaction is on out
    # of the box; vendors needing raw payloads pass ``identity_scrub``
    # via ``VendorConfig.scrubber``.
    scrubber = config.scrubber or Scrubber()
    # Resolved once and passed to both capture paths. Two independent
    # resolutions could disagree, and an annotation filed under a different
    # tenant than its tool call is unjoinable — the one correlation the
    # sensor exists to produce.
    tenant_id = _resolve_tenant_id(config.tenant_id, config.vendor_id)
    fallback_session_id = f"sdk-{uuid7()}"
    counter = SessionCounter()
    sink = resolve_sink(config)

    text = resolve_install_text(mcp, config, display_name_given=display_name_given)
    annotation_tool_name = text.names.annotation_tool_name if text.names else ""

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
    set_server_instructions(mcp, with_vendor_instructions(mcp, text.instructions))

    # Wrap currently-registered tools + patch add_tool for future ones.
    param_registry = install_wraps(
        mcp,
        tenant_id=tenant_id,
        vendor_id=config.vendor_id,
        consent_token=config.consent_token,
        sink=sink,
        counter=counter,
        fallback_session_id=fallback_session_id,
        scrubber=scrubber,
        annotation_tool_name=annotation_tool_name,
        intent_param_mode=config.intent_param_mode,
        result_capture_mode=config.result_capture_mode,
        resolve_principal_hook=config.resolve_principal,
        server_meta=server_meta,
    )

    async def read_request(request_context: Any) -> CapturedRequest:
        context = _RequestOnly(request_context)
        headers = _extract_headers_from_context(context)
        transport = observe_transport(context)
        return CapturedRequest(
            meta=_extract_meta_from_context(context),
            headers=headers,
            handshake_context=context,
            transport_observed=transport,
            session=await _resolve_call_session(
                headers=headers, fallback=fallback_session_id, transport_observed=transport
            ),
        )

    install_lifecycle_capture(
        lambda: get_lowlevel_server(mcp),
        tenant_id=tenant_id,
        vendor_id=config.vendor_id,
        consent_token=config.consent_token,
        sink=sink,
        counter=counter,
        scrubber=scrubber,
        resolve_principal_hook=config.resolve_principal,
        read_request=read_request,
        read_access_token=_auth.current_access_token,
        call_seam=CallSeam(
            annotation_tool_name=annotation_tool_name,
            result_capture_mode=config.result_capture_mode,
            dispositions=_registry_lookup(param_registry),
            knows_tool=_registry_answers(mcp),
        ),
    )

    # Register the annotation tool LAST so the wrap layer's add_tool patch
    # knows to skip wrapping it (by name match).
    if text.names is not None:
        register_annotation_tool(
            mcp,
            vendor_id=config.vendor_id,
            vendor_display_name=text.names.vendor_display_name,
            tenant_id=tenant_id,
            consent_token=config.consent_token,
            sink=sink,
            counter=counter,
            fallback_session_id=fallback_session_id,
            # The RESOLVED name, not ``config.annotation_tool_name``, so the tool,
            # the capture layer's skip and the instructions name the same tool.
            annotation_tool_name=annotation_tool_name,
            resolve_principal_hook=config.resolve_principal,
            scrubber=scrubber,
        )

    return BatonHandle(
        sink=sink,
        annotation_tool_name=annotation_tool_name,
        vendor_id=config.vendor_id,
        session_id=fallback_session_id,
    )


def _registry_lookup(
    param_registry: dict[str, dict[str, str]],
) -> Callable[[str], Awaitable[dict[str, str] | None]]:
    async def dispositions(name: str) -> dict[str, str] | None:
        return param_registry.get(name)

    return dispositions


def _registry_answers(mcp: Any) -> Callable[[str], bool | None]:
    """Whether a tool exists, as far as the registry can say. A server whose
    ``call_tool`` is its own may serve tools the registry does not hold."""
    if type(mcp).call_tool is not FastMCP.call_tool:
        return lambda _name: None
    return lambda name: name in get_tool_registry(mcp)
