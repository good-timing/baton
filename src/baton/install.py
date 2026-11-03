"""One entry point that routes to the right adapter, detecting on STRUCTURE.

**Both libraries name their server class ``FastMCP``.** The standalone
``fastmcp`` package and the official ``mcp`` SDK each export that name, they take
the same constructor argument, and ``install_baton`` has the same signature and
the same ``VendorConfig`` on both adapters — so the only thing distinguishing a
correct call from a wrong one is which module the caller imported from. That has
already cost two runtime guards, three wrong docstrings and one misread diagram,
and each guard exists because someone had already made the mistake.

``baton.install_baton`` removes the choice: hand it either server and it finds
the adapter. The per-adapter entry points keep working unchanged — they are the
shipped public contract, and this is additive.

**Detection is on the SEAM each adapter installs into, never on the module
path.** A module-path test would be shorter and is what the existing guards fall
back to, but it answers "where does this class live" when the question is "can
this adapter attach to it" — and those come apart on subclasses, on re-exports,
and on any upstream reorganisation. The seams:

- standalone ``fastmcp`` — a callable ``add_middleware``; the middleware chain is
  a supported public API and is precisely what ``integrations.standalone`` needs.
- official ``mcp`` SDK — a resolvable tool registry, which is what
  ``integrations.official`` wraps (and what a bare low-level ``Server`` lacks, which
  is why that shape is refused rather than half-installed).

**The middleware seam decides; the registry only breaks a tie it never has.**
``add_middleware`` is exclusive to the standalone package and the ordering rests
on that, measured across all four shipped shapes rather than assumed:

===========================  ================  ================
server                       add_middleware    tool registry
===========================  ================  ================
official ``mcp`` 1.x         no                yes
official ``mcp`` 2.x         no                yes
standalone ``fastmcp`` 2.14  **yes**           **yes**
standalone ``fastmcp`` 4.x   yes               no
===========================  ================  ================

**Corrected 2026-09-08, after 0.7.0 shipped the wrong rule.** This module was
written to treat both-signals as an ambiguity and refuse, on the stated belief
that the seams were disjoint. They are not, and never were on ``fastmcp`` 2.x,
which keeps a ``_tool_manager`` alongside its middleware chain — so the new
entry point raised ``TypeError`` for every server on the declared ``>=2.14``
floor, and its message blamed a hypothetical upstream move for a fact that
predates the module. Only ``baton.install_baton`` was affected;
``baton.integrations.standalone.install_baton`` routes correctly and always did.

**Neither signal is still an error**, and that direction is unchanged: a bare
low-level ``Server`` must be refused rather than half-installed, because a
mis-route installs a capture that looks healthy and produces nothing
(→ ``broken and unbuilt must not look alike``).
**The off switch.** Setting ``BATON_DISABLED=1`` in the
environment makes this function install NOTHING and raise nothing: no
middleware, no wrapped tools, no annotation tool on the surface, no
added instructions, no sink. The server behaves exactly as it would without
this call. See ``baton._optout``.

"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from baton._optout import capture_disabled
from baton.integrations._handle import disabled_handle
from baton.integrations._llm_text import _first_time
from baton.sinks import Sink, bounded_sink_call

if TYPE_CHECKING:
    from baton.integrations._annotation_name import InstallText
    from baton.integrations._config import VendorConfig
    from baton.integrations._handle import BatonHandle
    from baton.integrations._shared import CheckedConfig, Shared

logger = logging.getLogger(__name__)

__all__ = ["Baton", "create_baton", "install_baton"]


def _has_fastmcp_middleware_seam(server: Any) -> bool:
    """Standalone ``fastmcp``'s public middleware chain."""
    return callable(getattr(server, "add_middleware", None))


def _has_official_tool_registry(server: Any) -> bool:
    """The official SDK's tool registry, resolved through the same helper the
    adapter itself uses — so detection cannot drift from installation.

    Import-tolerant: ``mcp`` is an optional extra, and its absence means "not
    that shape here", never an error at import time.
    """
    try:
        from baton.integrations.official._registry import get_tool_manager
    except Exception:  # pragma: no cover - only when the extra is absent
        return False
    try:
        return get_tool_manager(server) is not None
    except Exception:
        return False


def install_baton(
    server: Any,
    config: VendorConfig | None = None,
    *,
    dsn: str | None = None,
) -> BatonHandle:
    """Install Baton into either MCP server implementation.

    Routes to ``baton.integrations.standalone`` for a standalone ``fastmcp``
    server and ``baton.integrations.official`` for the official SDK's, detecting on
    the seam each adapter needs. Raises ``TypeError`` — before mutating
    anything — only when the object is neither.

    ``dsn`` is the packed value from ``/account``, and the two-line install::

        from baton import install_baton

        install_baton(mcp, dsn="https://baton_pk_...@host/ten_.../echo-server")

    It is passed through untouched — the adapters resolve it, so one rule
    governs both and neither can drift into its own parse.
    """
    # ⚠ **Before the seam detection, because the detection's own miss RAISES.**
    # Both adapters carry this guard too — they are the shipped public entry
    # points and are called directly — but leaving it out here would keep one
    # throw path alive under a switch whose whole promise is that it cannot
    # break a boot: hand this function something it does not recognise with
    # BATON_DISABLED set, and it would still ``TypeError``.
    switch = capture_disabled()
    if switch is not None:
        # ``config.sink`` passed through for the same reason the adapters do
        # it: a sink the caller constructed is theirs to have closed, and this
        # door returns before either adapter is reached.
        return disabled_handle(switch, "install_baton", config.sink if config else None)

    adapter = _adapter_for(server)
    return adapter.install_baton(server, config, dsn=dsn)  # type: ignore[no-any-return]


def _adapter_for(server: Any, door: str = "baton.install_baton") -> Any:
    """The adapter package. fastmcp 2.x also has a tool registry, so the
    middleware seam is tested first."""
    if _has_fastmcp_middleware_seam(server):
        import baton.integrations.standalone as standalone

        logger.debug("baton: routing to the standalone fastmcp adapter")
        return standalone
    if _has_official_tool_registry(server):
        import baton.integrations.official as official

        logger.debug("baton: routing to the official mcp SDK adapter")
        return official
    raise _not_a_server(server, door)


def _not_a_server(server: Any, door: str = "baton.install_baton") -> TypeError:
    return TypeError(
        f"{door} does not recognise this object as an MCP server it "
        "can install into. It has no ``add_middleware`` (the standalone "
        "``fastmcp`` seam) and no resolvable tool registry (the official ``mcp`` "
        "SDK seam). A bare low-level ``mcp.server.lowlevel.Server`` lands here: "
        "it has no tool registry to wrap, so it needs its request handlers "
        "wrapped instead — not supported yet. If this IS a FastMCP or MCPServer, "
        "the installed ``mcp``/``fastmcp`` may be outside the supported range; "
        f"got {type(server).__module__}.{type(server).__qualname__}."
    )


class _Named:
    """A stand-in server: the install text reads only a server's name."""

    def __init__(self, name: str | None) -> None:
        self.name = name


class Baton:
    """One Baton for a process that builds a new MCP server per request. It
    owns the sink and the state the servers share, so a request has nothing
    to close. Use it from one event loop, as one server's capture is."""

    def __init__(
        self,
        *,
        sink: Sink,
        shared: Shared | None = None,
        checked: CheckedConfig | None = None,
        first_text: InstallText | None = None,
        _from_create_baton: bool = False,
    ) -> None:
        if not _from_create_baton:
            raise TypeError("make a Baton with baton.create_baton(), which checks the config")
        self._sink = sink
        self._shared = shared
        self._checked = checked
        self._texts: OrderedDict[str | None, InstallText] = OrderedDict()
        self._closed = self._warned_closed = False
        if first_text is not None:
            self._texts[None] = first_text

    def wrap(self, server: Any) -> None:
        """Install into one server. Raises ``TypeError`` only for an object that
        is not a server this SDK supports; the config was checked at create."""
        if self._checked is None:
            return
        adapter = _adapter_for(server, "Baton.wrap").install
        adapter.require_server(server)
        if self._closed:
            if not self._warned_closed:
                self._warned_closed = True
                logger.warning("baton: Baton.wrap after aclose; servers wrapped now record nothing")
            return
        if self._shared is None:
            return
        vendor_instructions = getattr(server, "instructions", None)
        try:
            adapter.attach(server, self._checked.config, self._text_for(server), self._shared)
        except Exception:
            # The config was checked at create, so this is the server's layout;
            # a request must not fail over capture (SPEC §11.2). Baton's text
            # goes, as it names a report tool that may not have been added.
            try:
                adapter.set_instructions(server, vendor_instructions)
            except Exception:
                pass
            if _first_time(self._shared.warned, "attach failed"):
                logger.exception(
                    "baton: Baton.wrap could not finish installing; this server's capture "
                    "may be partial, and later failures are logged at debug"
                )
            else:
                logger.debug("baton: Baton.wrap could not finish installing", exc_info=True)

    def _text_for(self, server: Any) -> InstallText:
        """With reports on, the text names the server, so it is computed once
        per server name; with them off, once. Its warnings follow suit."""
        from baton.integrations._annotation_name import (
            _name_the_vendor_chose,
            resolve_install_text,
        )

        assert self._checked is not None
        key = _name_the_vendor_chose(server) if self._checked.config.report_mode == "on" else None
        text = self._texts.get(key)
        if text is None:
            text = resolve_install_text(
                _Named(key),
                self._checked.config,
                display_name_given=self._checked.display_name_given,
            )
            self._texts[key] = text
            if len(self._texts) > _TEXTS_CAP:
                self._texts.popitem(last=False)
        else:
            self._texts.move_to_end(key)
        return text

    async def flush(self) -> None:
        """Returns within the sink's shutdown budget and never raises (SPEC §11.2 rule 7)."""
        await bounded_sink_call(self._sink, "flush")

    async def aclose(self) -> None:
        """Returns within the sink's shutdown budget and never raises (SPEC §11.2 rule 7)."""
        self._closed = True
        await bounded_sink_call(self._sink, "aclose")


_TEXTS_CAP = 64


def create_baton(config: VendorConfig | None = None, *, dsn: str | None = None) -> Baton:
    """Create once at startup, then ``wrap`` each server built per request. For
    a server that lives as long as the process, use ``install_baton``.

    Takes what ``install_baton`` takes and checks it the same way, here, so no
    request fails on configuration."""
    from baton.integrations._annotation_name import resolve_install_text
    from baton.integrations._handle import unconfigured_handle
    from baton.integrations._shared import build_shared, check_config

    switch = capture_disabled()
    if switch is not None:
        handle = disabled_handle(switch, "create_baton", config.sink if config else None)
        return Baton(sink=handle.sink, _from_create_baton=True)
    checked = check_config(config, dsn)
    # The server-free half of install's text check, before the sink is built.
    first_text = resolve_install_text(
        _Named(None), checked.config, display_name_given=checked.display_name_given
    )
    if checked.unconfigured:
        handle = unconfigured_handle("create_baton")
        return Baton(sink=handle.sink, checked=checked, _from_create_baton=True)
    shared = build_shared(checked.config)
    return Baton(
        sink=shared.sink,
        shared=shared,
        checked=checked,
        first_text=first_text,
        _from_create_baton=True,
    )
