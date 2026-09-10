"""The off switch — one environment variable that stops Baton doing anything.

``BATON_DISABLED=1``, and deliberately nothing else.

**Off means INSTALL NOTHING, and it means NEVER THROW.** Not capture-and-
discard: no middleware, no tool wrapping, no annotation tool on the surface, no
instructions rewrite, no sink, no buffer, no background thread. The vendor's
server starts and behaves exactly as it would if ``install_baton`` were not in
the file at all. That is the contract, and both halves are load-bearing:

- **Nothing on stdout, ever.** A stdio MCP server speaks JSON-RPC on stdout, so
  a courteous "Baton is disabled" line printed there corrupts the stream and
  breaks the server — in precisely the deployment shape this switch exists for.
  The one line below goes to a logger, which writes to stderr by default.
- **The switch cannot break a server.** Every guard the SDK would otherwise
  raise from — a server object of the wrong shape, a config missing its
  ``vendor_id``, an unparseable DSN — is skipped along with everything else,
  because a promise of "set this and Baton stops" that can still abort a boot
  is worse than no switch at all.

⚠ **The consequence of never-throw, recorded rather than argued away:** a
vendor whose CI exports ``BATON_DISABLED`` globally will not learn that their
``install_baton`` call is malformed, because the call cannot fail while the
switch is on. It surfaces the first time capture is enabled. That is the price
of the contract above; ``log_disabled`` below is what softens it, and its
docstring says how far that goes.

⚠ **Read at install/init time, once, and never re-read.** A process that starts
with capture on keeps it on: this is a boot-time switch, not a live one. A
re-read per event would let a mid-flight environment change split one session's
events across two answers.
"""

from __future__ import annotations

import logging
import os

from baton.events import Event
from baton.sinks import Sink

logger = logging.getLogger(__name__)

__all__ = ["DisabledSink", "capture_disabled", "log_disabled"]

_SWITCH = "BATON_DISABLED"

# ⚠ **Permissive toward the opt-out, on purpose.** The documented form is
# ``=1``, but someone who writes ``BATON_DISABLED=true`` has said what they want
# as plainly as someone who writes ``1``, and the two failure directions are not
# symmetric: honouring an unintended opt-out costs some telemetry, while
# ignoring an intended one collects data from a person who asked us not to.
# So anything that is not an explicit "off" counts as on.
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def capture_disabled() -> str | None:
    """The NAME of the variable switching capture off, or ``None``.

    The name rather than a bool, so the log line and the disabled handle can
    say what did it without every entry point repeating the string.
    """
    value = os.environ.get(_SWITCH)
    if value is not None and value.strip().lower() not in _OFF_VALUES:
        return _SWITCH
    return None


def log_disabled(switch: str, surface: str) -> None:
    """Say so once, on the logger, never on stdout.

    INFO rather than WARNING: a line emitted on every process start, for a
    switch somebody set deliberately, is how a codebase teaches people to
    ignore its warnings.

    ⚠ **Not a discoverability guarantee, and it must not be sold as one.**
    Python's default handler emits WARNING and above, so in a server that
    configures no logging this prints nothing. It is for the vendor who turns
    logging up while asking why no events are arriving. The answer for one who
    has not thought to look is vendor-facing documentation, which is a
    different piece of work.
    """
    logger.info(
        "baton: %s is set, so capture is OFF — %s installed nothing and will "
        "emit no events. Unset it to re-enable.",
        switch,
        surface,
    )


class DisabledSink(Sink):
    """A sink for a handle that will never be handed an event.

    It exists for the TYPE, not for the behaviour: ``BatonHandle.sink`` is
    declared as a ``Sink`` and vendor code may call ``flush()``/``aclose()`` on
    the handle in a ``finally``. Nothing writes to it, because when the switch
    is on nothing is wrapped in the first place — which is the difference
    between this and a discard sink, and the reason it must never be offered as
    a way to "turn capture off" from config.

    ``write`` accepts and drops rather than raising: a handle that explodes on
    use would be the switch breaking the server through the back door.
    """

    async def write(self, event: Event) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def aclose(self) -> None:
        return None
