"""Shared VendorConfig used by both adapter ``install_baton`` functions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from baton.sinks import Sink, StdoutSink

# Vendor IDs become annotation tool name prefixes; same client-pattern as
# annotation tool names. Reject dots so the default tool name is valid.
_VENDOR_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")

# Per-tool intent-param injection modes (mirrors baton-proxy's BATON_INTENT_PARAM).
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required", "off"})


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
    """Per-tool intent-param injection (mirrors baton-proxy's
    ``BATON_INTENT_PARAM``). ``"optional"`` (default) injects a ``baton_intent``
    string param on every wrapped tool's input schema; ``"required"`` also adds
    it to each tool's ``required`` list; ``"off"`` disables injection. The param
    is stripped before the vendor handler runs, so the tool never sees it. This
    is what captures intent on runtimes that drop ``instructions`` (notably
    Claude Desktop) — where the annotation tool alone yields nothing."""


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
