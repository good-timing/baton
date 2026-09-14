"""Containment for vendor-supplied callables.

A hook is somebody else's code running inside our capture path, on the
vendor's hot path, per tool call. Everything here exists because of what that
code is allowed to do to a server that merely installed us.

**Why a worker thread.** A vendor's ``resolve_principal`` will usually do I/O to
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
(so a hook calling ``get_access_token()`` — the whole point of ``resolve_principal``
— read ``None``), and it had no ceiling (so a wedged dependency stranded one
thread per tool call). The contextvar copy is free here; **the ceiling is
NOT**, and this module claimed it was until it was measured — see
``HOOK_THREAD_CEILING``. Concurrency primitives are exactly
the kind of code where reimplementing to avoid a dependency costs more than
the dependency, and ``anyio`` is not a real dependency anyway: ``mcp`` requires
it at every point in the supported band (1.20.0 ``anyio>=4.5``, 2.2.0
``anyio>=4.10``) and ``fastmcp`` gets it through ``mcp``, so declaring it in
the integration extras is as free as ``pydantic`` already is.

``abandon_on_cancel=True`` is what lets the deadline fire while the hook is
still blocked: anyio's own wording is that the thread "will still run its
course but its return value ... will be ignored". ⚠ **It also releases anyio's
limiter token at that moment**, which is why anyio's thread limiter is not a
ceiling on wedged hooks and ``HOOK_THREAD_CEILING`` below exists.

⚠ **KNOWN LIMITATION, accepted deliberately: a truly wedged hook delays
process shutdown.** anyio's ``WorkerThread`` is created without
``daemon=True``, so it inherits non-daemon and Python's ``threading._shutdown``
joins it at interpreter exit. Measured on anyio **4.15.1**: a process that
abandoned a 120-second hook at a 0.3-second deadline was still parked in
``threading._shutdown`` six seconds later, under both ``asyncio.run`` and
``anyio.run``. A hook that is merely SLOW finishes and releases; only one
wedged forever (a connection blackhole with no driver timeout) holds exit, and
then only until the orchestrator's grace period expires — for at most
``HOOK_THREAD_CEILING`` of them, which is the second reason that ceiling is
not merely about memory. No events are lost —
the sink's ``aclose`` runs before interpreter exit. ``test_hook_containment``
pins this so we find out if anyio ever makes those threads daemon; the trade
was taken knowingly, against ~38 lines of threading we would otherwise own
forever and have already shipped bugs in.

**Cost, measured, and who pays it.** ~64µs per call. It is paid only where a
hook is CONFIGURED — the hook field defaults to ``None`` and the default path
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
import threading
from typing import Any

# ⚠ ``anyio`` is a CORE dependency, and this import is why. ``baton/__init__``
# imports ``VendorConfig``, which imports this module, on EVERY ``import
# baton`` — so while anyio sat in the ``[mcp]`` / ``[fastmcp]`` extras this
# line broke ``import baton`` outright on a core-only install: the library-API
# path (``Client`` / ``AsyncClient`` / ``Trace``), which never runs a vendor
# hook at all. Measured 2026-09-11 with anyio blocked by a meta-path finder.
# It was briefly fixed by importing anyio inside ``run_vendor_hook`` instead;
# 2026-09-11 (Ujwal) moved the dependency to core, so the import comes back to
# where it belongs. ⚠ **If anyio ever leaves core, this line is the break** —
# ``pyproject.toml`` carries the reasoning.
import anyio.to_thread

#: A hook gets this long, total, including any awaitable it returns. Five
#: seconds is the prior art's number and is chosen the same way: long enough
#: that a real directory or database lookup finishes, short enough that a
#: wedged one does not hold a tool call open past a human's patience.
HOOK_TIMEOUT_SECONDS = 5.0

#: Hook dispatches allowed to run at once, on a limiter of OUR OWN.
#:
#: ⚠ **Not anyio's default pool, and sharing it was measured harmful.** fastmcp
#: runs every sync tool handler, sync resource, sync prompt and sync dependency
#: through ``anyio.to_thread.run_sync`` with no limiter — the default 40-token
#: pool. A hook dispatched into that pool queues behind the vendor's own
#: handlers, INSIDE our deadline: measured 2026-09-11, with 40 slow sync tool
#: handlers in flight, a hook that returns instantly failed with "exceeded
#: 2.0s" having never run, after adding the entire budget as pure latency to
#: the vendor's tool call. A separate limiter makes the two pools independent
#: in both directions — the vendor's handlers no longer queue behind hooks
#: either.
HOOK_THREAD_CONCURRENCY = 40

#: Live vendor-hook threads past which a call is refused rather than started.
#:
#: ⚠ **anyio's thread limiter does not bound this, and believing it did was a
#: measured error.** Under ``abandon_on_cancel=True`` the limiter token is
#: returned the instant the deadline fires, while the worker stays parked in
#: the vendor's blocking call — so the NEXT tool call finds a free token and
#: starts thread N+1. Measured 2026-09-11: 65 SEQUENTIAL wedged hooks (a
#: dependency blackhole with no driver timeout — one tool call at a time, the
#: ordinary traffic shape) grew 65 threads against a limiter of 40. The
#: limiter holds only under CONCURRENT arrival, where the calls past it block
#: waiting for a token and expire before dispatching, which is the shape the
#: original test happened to use and the reason it could not fail.
#:
#: Every one of those threads is non-daemon (see the module note), so the leak
#: compounds the shutdown delay rather than merely wasting memory.
#:
#: ⚠ **Deliberately ABOVE ``HOOK_THREAD_CONCURRENCY``, and it was 40 — equal to
#: it — for one commit, which made it fire on health.** At most
#: ``HOOK_THREAD_CONCURRENCY`` hooks can be running-and-awaited, so an equal
#: ceiling is reached at full legitimate utilization: measured 2026-09-11, 40
#: concurrent 200ms lookups — nothing wedged, every one of them about to
#: succeed — got the 41st refused, and told the vendor their working hook was
#: wedged. With headroom, the only way to reach this number is abandoned
#: threads piling up, which is the condition the ceiling is named for. The gap
#: is what "wedged" means here: 64 live means at least 24 are abandoned.
HOOK_THREAD_CEILING = 64

# Counted by the WORKER, not reserved by the caller. A reservation taken
# before ``run_sync`` leaks whenever the dispatch is cancelled before the
# vendor's function actually runs — waiting for an anyio token is exactly such
# a window — and a leaked slot is permanent, which would brick every hook
# after enough of them. Incrementing inside the thread cannot leak: the same
# frame that increments always decrements. The cost is that the check is a
# check and not a reservation, so a BURST can overshoot by however many calls
# pass it at once; anyio's limiter bounds that overshoot to its own token
# count, and the unbounded SEQUENTIAL case — the one that was real — is
# single-in-flight and so exact.
_live_lock = threading.Lock()
_live_hook_threads = 0

# Built lazily and once, and the reason is narrower than "it needs a loop".
# Measured on anyio 4.15.1: ``CapacityLimiter(n)`` constructed with NO running
# loop returns a ``CapacityLimiterAdapter`` that defers creating the real
# backend limiter until first use, while the same call INSIDE a running loop
# returns a backend limiter bound to that loop there and then. This module is
# imported whenever ``baton`` is, so which of those two a module-level constant
# got would depend on the vendor's import site. Building it at first use is
# always the second case, deliberately, rather than by accident of where the
# import happened.
#
# ⚠ It is process-global, unlike anyio's DEFAULT limiter, which is per-event-
# loop (a ``RunVar``). One server, one loop, so the distinction does not arise
# in the shape this ships into; a process that ran hooks under two loops would
# share one ceiling across both. Measured to work across sequential loops
# — the suite runs a fresh loop per test — but that is an observation, not a
# property being relied on.
_limiter: Any = None


def _hook_limiter() -> Any:
    global _limiter
    if _limiter is None:
        import anyio

        _limiter = anyio.CapacityLimiter(HOOK_THREAD_CONCURRENCY)
    return _limiter


def live_hook_threads() -> int:
    """Vendor-hook worker threads currently inside a vendor's callable."""
    with _live_lock:
        return _live_hook_threads


def _counted(fn: Any, args: tuple[Any, ...]) -> Any:
    """Wrap ``fn`` so the thread it runs on is counted for as long as it runs."""

    def call() -> Any:
        global _live_hook_threads
        with _live_lock:
            _live_hook_threads += 1
        try:
            return fn(*args)
        finally:
            with _live_lock:
                _live_hook_threads -= 1

    return call


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
            # Refused rather than queued: a caller waiting for a slot would
            # spend its whole budget waiting and then report the deadline,
            # which reads as "this hook is slow" when the truth is "this
            # vendor's dependency is down and the threads are already parked".
            #
            # ⚠ The count is process-wide, so a wedged hook in one place
            # refuses hooks everywhere. That is the intent — the exhausted
            # resource is threads, not a particular hook — but it means this
            # message names the caller, not necessarily the culprit. It was
            # written when there were TWO hook kinds; ``resolve_session_id``
            # was removed 2026-09-12 and ``resolve_principal`` is now the only
            # caller, so today the caller and the culprit coincide. Kept
            # process-wide rather than narrowed: the next hook re-creates the
            # case, and a ceiling that has to be re-widened is worse than one
            # that never assumed a count.
            live = live_hook_threads()
            if live >= HOOK_THREAD_CEILING:
                raise HookFailed(
                    f"{hook_name} hook refused: {live} vendor-hook threads are "
                    f"still running past their deadline (ceiling "
                    f"{HOOK_THREAD_CEILING}); a hook's dependency is wedged"
                )
            # ``abandon_on_cancel=True`` so the deadline can fire while the
            # hook is still blocked; the thread runs its course and its result
            # is discarded. anyio copies the caller's context into the worker,
            # so a hook may read ``get_access_token()`` / ``get_http_headers()``
            # — both contextvar-backed, and the reason this call exists.
            result = await anyio.to_thread.run_sync(
                _counted(fn, args), abandon_on_cancel=True, limiter=_hook_limiter()
            )
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
