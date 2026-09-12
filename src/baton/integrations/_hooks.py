"""Containment for vendor-supplied callables.

A hook is somebody else's code running inside our capture path, on the
vendor's hot path, per tool call. Everything here exists because of what that
code is allowed to do to a server that merely installed us.

**Why a worker thread.** A vendor's ``resolve_user`` will usually do I/O to
answer — a directory lookup, a database read — and the natural way to write
that is a plain ``def``. Called inline on the event loop, a blocking hook
suspends not just its own request but EVERY concurrent tool call on the
server. Measured 2026-09-11 before this module existed: a hook doing
``time.sleep(0.5)`` let ONE heartbeat tick through where ~25 were due.

**Why a timeout, separately.** An outer timeout cannot rescue a blocking call
that holds the loop — measured on the same run, a 30-second hook was not
interrupted by an enclosing ``asyncio.wait_for(timeout=2)``; it returned after
the full 30 seconds. The timeout only means anything once the call is off the
loop, so the two halves are one fix and neither works alone.

**Why ``anyio`` rather than a hand-rolled thread.** This module ran on its own
``threading.Thread`` until 2026-09-11, and got two of the four properties a
worker needs WRONG on the first pass: it dropped the caller's ``contextvars``
(so a hook calling ``get_access_token()`` — the whole point of ``resolve_user``
— read ``None``), and it had no ceiling (so a wedged dependency stranded one
thread per tool call). Both are free here. Concurrency primitives are exactly
the kind of code where reimplementing to avoid a dependency costs more than
the dependency, and ``anyio`` is not a real dependency anyway: ``mcp`` requires
it at every point in the supported band (1.20.0 ``anyio>=4.5``, 2.2.0
``anyio>=4.10``) and ``fastmcp`` gets it through ``mcp``, so declaring it in
the integration extras is as free as ``pydantic`` already is.

``abandon_on_cancel=True`` is what lets the deadline fire while the hook is
still blocked: anyio's own wording is that the thread "will still run its
course but its return value ... will be ignored".

⚠ **KNOWN LIMITATION, accepted deliberately: a truly wedged hook delays
process shutdown.** anyio's ``WorkerThread`` is created without
``daemon=True``, so it inherits non-daemon and Python's ``threading._shutdown``
joins it at interpreter exit. Measured on anyio **4.15.1**: a process that
abandoned a 120-second hook at a 0.3-second deadline was still parked in
``threading._shutdown`` six seconds later, under both ``asyncio.run`` and
``anyio.run``. A hook that is merely SLOW finishes and releases; only one
wedged forever (a connection blackhole with no driver timeout) holds exit, and
then only until the orchestrator's grace period expires. No events are lost —
the sink's ``aclose`` runs before interpreter exit. ``test_hook_containment``
pins this so we find out if anyio ever makes those threads daemon; the trade
was taken knowingly, against ~38 lines of threading we would otherwise own
forever and have already shipped bugs in.

**Cost, measured, and who pays it.** ~64µs per call. It is paid only where a
hook is CONFIGURED — both hook fields default to ``None`` and the default path
never reaches this module — and an ``async def`` hook skips the thread
entirely (measured 1.6µs), because a coroutine cannot block the loop by being
called.

**Not the scrubber.** ``VendorConfig.scrubber`` is typed ``Callable[[Any],
Any]`` — sync by contract — and runs 36 call sites' worth of regex per event.
That is CPU work under the GIL, where a thread buys nothing and costs the
switch on every event rather than on every configured hook. Different shape,
different fix, deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

import anyio.to_thread

#: A hook gets this long, total, including any awaitable it returns. Five
#: seconds is the prior art's number and is chosen the same way: long enough
#: that a real directory or database lookup finishes, short enough that a
#: wedged one does not hold a tool call open past a human's patience.
HOOK_TIMEOUT_SECONDS = 5.0


class HookFailed(Exception):
    """A vendor hook raised, timed out, or could not be run.

    An ``Exception``, deliberately — every caller already wraps hook execution
    in ``except Exception`` and treats a failure as a miss, so containment
    degrades a hook to "returned nothing" through the path that already
    existed rather than needing a second one.
    """


async def run_vendor_hook(
    fn: Any,
    *args: Any,
    hook_name: str,
    logger: logging.Logger,
    timeout: float | None = None,
) -> Any:
    """Run a vendor callable off the hot path and return its value.

    Raises ``HookFailed`` if the hook raised, timed out, or returned an
    awaitable that did either. Propagates ``asyncio.CancelledError`` when the
    ENCLOSING task is being cancelled — a capture hook may not swallow a
    cancellation, or a cancelled request keeps running on the vendor's server.

    Sync or async. An ``async def`` is awaited directly, under the same
    timeout: it cannot block the loop by being called, so a thread would add
    a context switch and buy nothing.
    """
    # Read at CALL time, not bound as a default argument. A default is
    # evaluated once at import, so ``HOOK_TIMEOUT_SECONDS`` would be frozen
    # there and patching the constant — which is how the timeout is tested,
    # and how a future knob would reach it — would silently do nothing.
    budget = HOOK_TIMEOUT_SECONDS if timeout is None else timeout
    # ⚠ **``asyncio.timeout``, NOT ``anyio.fail_after``, and this was tried the
    # other way.** Pairing anyio's threads with anyio's cancellation looks more
    # coherent and makes ``abandon_on_cancel`` load-bearing — measured, under
    # ``fail_after`` the flag decides everything (``True`` → TimeoutError at
    # 0.10s, ``False`` → the 2s hook's value returned in full with the deadline
    # ignored), while under ``asyncio.timeout`` the deadline fires at 0.10s
    # either way because anyio's shield only applies to anyio's own
    # cancellation.
    #
    # **It was reverted because it broke something that matters more.**
    # ``fail_after`` cancels through an anyio ``CancelScope``, which does not
    # bump asyncio's ``Task.cancelling()`` counter — so the discrimination
    # below read a GENUINE task cancellation as a hook cancelling itself and
    # CONTAINED it. A capture hook that swallows a cancellation leaves a
    # cancelled request running on the vendor's server, which is worse than a
    # belt-and-braces flag. ``abandon_on_cancel=True`` stays because it is the
    # correct intent and costs nothing; it is simply inert under the
    # cancellation this module actually uses, and a mutation of it stays green
    # for that reason rather than for want of a test.
    #
    # Bound as ``deadline`` so ``expired()`` can tell OUR deadline from a
    # ``TimeoutError`` the hook raised itself — an ``asyncio.timeout(1)``
    # around its own lookup, or an httpx/socket timeout. Attributing those to
    # the budget tells a vendor to tune a knob that is not the problem, and
    # ``from None`` would erase the real cause from the ``exc_info`` their logs
    # are about to print.
    deadline = asyncio.timeout(budget)
    try:
        async with deadline:
            if inspect.iscoroutinefunction(fn):
                return await fn(*args)
            # ``abandon_on_cancel=True`` so the deadline can fire while the
            # hook is still blocked; the thread runs its course and its result
            # is discarded. anyio copies the caller's context into the worker,
            # so a hook may read ``get_access_token()`` / ``get_http_headers()``
            # — both contextvar-backed, and the reason this call exists.
            result = await anyio.to_thread.run_sync(lambda: fn(*args), abandon_on_cancel=True)
            # A plain ``def`` can still return a coroutine — a lambda wrapping
            # an async call is the shape. Awaited under the SAME deadline, so
            # the budget is for the hook, not per stage of it.
            if inspect.isawaitable(result):
                return await result
            return result
    except HookFailed:
        raise
    except TimeoutError as exc:
        if deadline.expired():
            raise HookFailed(f"{hook_name} hook exceeded {budget}s") from None
        raise HookFailed(f"{hook_name} hook raised TimeoutError: {exc}") from exc
    except asyncio.CancelledError:
        task = asyncio.current_task()
        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling) and cancelling() == 0:
            # No external cancellation is pending, so an async hook raised
            # CancelledError of its own accord. Contained: a hook's private
            # control flow may not cancel the vendor's tool call.
            raise HookFailed(f"{hook_name} hook raised CancelledError spontaneously") from None
        raise
    except Exception as exc:
        raise HookFailed(f"{hook_name} hook raised {type(exc).__name__}: {exc}") from exc
