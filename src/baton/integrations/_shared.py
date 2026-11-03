"""What one install sets up once and every server it attaches to shares."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from baton._capture_settings import CaptureSettings
from baton._state import SessionCounter
from baton._uuid import uuid7
from baton.integrations._config import (
    VendorConfig,
    _resolve_tenant_id,
    _validate_vendor_config,
    build_capture_settings,
    build_config,
    nothing_configured,
    resolve_sink,
    with_stand_in_identity,
)
from baton.integrations._surface import SentSurfaces
from baton.scrub import Scrubber
from baton.sinks import Sink


@dataclass
class Shared:
    scrubber: Callable[[Any], Any]
    tenant_id: str
    sink: Sink
    counter: SessionCounter = field(default_factory=SessionCounter)
    # Servers built per request list the same tools; the surface is sent once.
    sent_surfaces: SentSurfaces = field(default_factory=SentSurfaces)
    # And the warnings they would each repeat are said once.
    warned: set[str] = field(default_factory=set)
    capture_settings: CaptureSettings | None = None

    def start(self) -> None:
        if self.capture_settings is not None:
            self.capture_settings.start()

    def stop(self) -> None:
        if self.capture_settings is not None:
            self.capture_settings.stop()


class ServerCounter(SessionCounter):
    """One server's counter: its own fallback session is numbered here and
    freed with the server; every other session goes to the shared counter,
    since servers built per request can see the same client session."""

    def __init__(self, shared: SessionCounter, own_session_id: str) -> None:
        super().__init__()
        self._shared = shared
        self._own = own_session_id

    async def next(self, session_id: str) -> int:
        if session_id == self._own:
            return await super().next(session_id)
        return await self._shared.next(session_id)


def server_session(shared: Shared) -> tuple[ServerCounter, str]:
    """A new server's own fallback session, as one connection's (SPEC §3.4), and
    the counter it numbers events with."""
    session_id = f"sdk-{uuid7()}"
    return ServerCounter(shared.counter, session_id), session_id


def build_shared(config: VendorConfig) -> Shared:
    """For a checked config. Builds the sink, so call it after the last check."""
    tenant_id = _resolve_tenant_id(config.tenant_id, config.vendor_id)
    return Shared(
        # A fresh Scrubber per install or Baton, so PII redaction is on by default.
        scrubber=config.scrubber or Scrubber(),
        tenant_id=tenant_id,
        sink=resolve_sink(config),
        capture_settings=build_capture_settings(config, tenant_id),
    )


@dataclass
class CheckedConfig:
    config: VendorConfig
    display_name_given: bool
    # Nothing says where events go: the install checks, then adds nothing.
    unconfigured: bool


def check_config(config: VendorConfig | None, dsn: str | None) -> CheckedConfig:
    """Every check that needs no server. With nothing configured the config gets
    a stand-in identity, so it runs the same checks (SPEC §11.2 rule 10)."""
    from baton.integrations._lifecycle import warn_if_results_are_withheld

    # Read BEFORE ``build_config`` and the stand-in, which fill an unset display
    # name in and so leave a defaulted value and a chosen one looking alike.
    display_name_given = config is not None and bool(config.vendor_display_name)
    unconfigured = nothing_configured(config, dsn)
    if unconfigured:
        config = with_stand_in_identity(config)
    built = build_config(config, dsn)
    _validate_vendor_config(built)
    if not unconfigured:
        warn_if_results_are_withheld(built.result_capture_mode)
    return CheckedConfig(built, display_name_given, unconfigured)
