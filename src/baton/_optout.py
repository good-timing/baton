"""The off switch — one environment variable that stops Baton doing anything.

``BATON_DISABLED=1``, and deliberately nothing else.

⚠ **``DO_NOT_TRACK`` was implemented and then REVERSED (2026-09-10), so do not
re-add it on the strength of the convention alone.** The argument for honouring
it was that someone who sets it once, globally, should not have to learn our
variable's name. **Measured, that is false for the deployment this switch
exists for.** An MCP client does not hand its own environment to the server it
spawns — both SDKs pass a fixed allowlist (``HOME``, ``LOGNAME``, ``PATH``,
``SHELL``, ``TERM``, ``USER`` in the Python one, the same shape in the
TypeScript one that Claude Code and Desktop use), and ``DO_NOT_TRACK`` is not
on it. So a global export never reaches a stdio server; the user has to put it
in their client config's ``env`` block, where they could as easily have typed
``BATON_DISABLED=1``. The convenience the convention was worth buying does not
exist here.

What it cost instead was real: a contributor with ``DO_NOT_TRACK`` exported —
Homebrew and the .NET CLI both honour it, so people do have it set — got a
silently disabled SDK and a broad red test suite.

It still worked on the library-API path, where the process is started from the
user's own shell. If that path ever becomes the main one, this is the note to
re-read; the reversal is about MCP's spawn model, not about the convention
being wrong.

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
vendor whose CI exports ``DO_NOT_TRACK`` globally will not learn that their
``install_baton`` call is malformed, because the call cannot fail while the
switch is on. It surfaces the first time capture is enabled. That is the price
of the contract above. The disabled path logs a line naming which variable
fired, which helps whoever has logging turned up — but at INFO that line is
invisible by default, so it is a debugging aid and NOT the answer to "broken
and unbuilt must not look alike". That answer is vendor-facing documentation,
which is a different piece of work.

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

# ⚠ **Permissive toward the opt-out, on purpose.** The convention says ``=1``,
# but someone who writes ``DO_NOT_TRACK=true`` has said what they want as
# plainly as someone who writes ``1``, and the two failure directions are not
# symmetric: honouring an unintended opt-out costs some telemetry, while
# ignoring an intended one collects data from a person who asked us not to.
# So anything that is not an explicit "off" counts as on.
_OFF_VALUES = frozenset({"", "0", "false", "no", "off"})


def capture_disabled() -> str | None:
    """The NAME of the variable switching capture off, or ``None``.

    The name rather than a bool, so the log line and the disabled handle can
    say which variable did it. There is only one today; returning the name
    keeps the callers unchanged if that ever stops being true.
    """
    value = os.environ.get(_SWITCH)
    if value is not None and value.strip().lower() not in _OFF_VALUES:
        return _SWITCH
    return None


def log_disabled(switch: str, surface: str) -> None:
    """Say so once, on the logger, never on stdout.

    INFO rather than WARNING: for ``DO_NOT_TRACK`` this is an end user's
    standing preference being honoured, and warning them about it once per
    process start is nagging someone for a choice they already made. The real
    discoverability answer is the vendor-facing documentation, not a log level.
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
