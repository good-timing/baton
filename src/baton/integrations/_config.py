"""Shared VendorConfig used by both adapter ``install_baton`` functions."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from baton.sinks import Sink, StdoutSink

logger = logging.getLogger(__name__)

# Vendor IDs become annotation tool name prefixes; same client-pattern as
# annotation tool names. Reject dots so the default tool name is valid.
_VENDOR_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")

# Per-tool intent-param injection modes (mirrors baton-proxy's BATON_INTENT_PARAM).
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required", "off"})


@dataclass(frozen=True)
class SessionResolutionContext:
    """Normalized input to ``VendorConfig.resolve_session_id``.

    Deliberately does not carry the raw SDK ``Context`` object — the
    official ``mcp`` and standalone ``fastmcp`` libraries expose different,
    adapter-specific ``Context`` types. This shape is what's already
    extracted for both adapters (headers, meta), so one hook works
    unmodified regardless of which adapter a vendor is on.
    """

    headers: Mapping[str, str] | None
    meta: dict[str, Any] | None
    tool_name: str
    arguments: dict[str, Any]


ResolveSessionIdHook = Callable[[SessionResolutionContext], "Awaitable[str | None] | str | None"]


async def resolve_via_hook(
    hook: ResolveSessionIdHook, context: SessionResolutionContext
) -> str | None:
    """Call a vendor's ``resolve_session_id`` hook and normalize its result.

    Never raises — an exception is logged and treated as a miss so the
    caller falls through to the SPEC §3.4 ladder unchanged. Accepts sync or
    async hooks (mirrors ``VendorConfig.scrubber``'s calling convention).
    """
    try:
        result = hook(context)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        logger.warning("baton: resolve_session_id hook raised; falling through", exc_info=True)
        return None
    return result if isinstance(result, str) and result else None


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
    """PII scrubber per SPEC §7. Default (None) uses ``baton.scrub.Scrubber``
    — recursive walker with email/Bearer/sk-*/AKIA*/JWT/CC-Luhn/phone
    patterns + field-name overrides on by default. Pass
    ``baton.scrub.identity_scrub`` to opt out, or supply your own."""

    intent_param_mode: str = "optional"
    """Per-tool intent-param injection (mirrors baton-extmcp's vendor-neutral
    naming). ``"optional"`` (default) injects ``user_goal``/``expected_result``
    string params on every wrapped tool's input schema; ``"required"`` also
    adds ``user_goal`` to each tool's ``required`` list (``expected_result``
    stays optional regardless); ``"off"`` disables injection. Both params are
    stripped before the vendor handler runs, so the tool never sees them. This
    is what captures intent on runtimes that drop ``instructions`` (notably
    Claude Desktop) — where the annotation tool alone yields nothing."""

    resolve_session_id: ResolveSessionIdHook | None = None
    """Optional vendor-supplied session-id resolver, checked BEFORE the SPEC
    §3.4 ladder (rung 0) — a vendor who already has their own session/auth
    concept can hand Baton a real correlation key directly, bypassing MCP
    transport/meta entirely. The only mechanism that works on new-spec
    (SEP-2567) and true-stateless HTTP, where nothing MCP-native is
    observable by protocol design. A non-empty string return wins outright;
    ``None``/empty or a raised exception (logged, never propagated) falls
    through to the ladder unchanged. Return an opaque, non-PII id —
    passed through raw, not hashed; hashing/derivation is the vendor's
    responsibility if the raw value is sensitive. Sync or async."""


def _validate_vendor_config(config: VendorConfig) -> None:
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
    if config.intent_param_mode not in _INTENT_PARAM_MODES:
        raise ValueError(
            f"VendorConfig.intent_param_mode {config.intent_param_mode!r} must be "
            f"one of {sorted(_INTENT_PARAM_MODES)}."
        )
