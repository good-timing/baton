"""Containment for vendor-supplied callables — ``integrations/_hooks.py``.

A hook is somebody else's code on the vendor's hot path, per tool call. These
tests are the properties that keep it from taking the server with it, and each
one is here because the behaviour it pins was measured wrong first.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
from typing import Any

import anyio.to_thread
import pytest

from baton.integrations._config import SessionResolutionContext, resolve_via_hook
from baton.integrations._hooks import HookFailed, run_vendor_hook

LOG = logging.getLogger("test-hooks")


async def _run(fn: Any, *args: Any, **kw: Any) -> Any:
    return await run_vendor_hook(fn, *args, hook_name="probe", logger=LOG, **kw)


# --- context propagation ----------------------------------------------------
#
# The highest-value test in this file. A bare ``threading.Thread`` starts with
# a FRESH, EMPTY context, and the hook whose entire purpose is reading identity
# reads it from a contextvar.

_TOKEN: contextvars.ContextVar[str | None] = contextvars.ContextVar("token", default=None)


async def test_a_sync_hook_can_read_the_callers_contextvars() -> None:
    """``get_access_token()`` and ``get_http_headers()`` are BOTH
    contextvar-backed, on both adapters — the adapters' own docstrings say so.

    Without ``copy_context`` the natural hook a vendor writes —
    ``def resolve_user(ctx): return Principal(user_id=get_access_token()...)``
    — reads ``None`` on the worker thread, raises ``AttributeError``, gets
    contained, and produces no identity. Silently. Forever. Measured that way
    before this test existed.
    """
    _TOKEN.set("VERIFIED")

    def sync_hook(_c: Any) -> str | None:
        return _TOKEN.get()

    assert await _run(sync_hook, None) == "VERIFIED"


async def test_sync_and_async_hooks_see_the_SAME_context() -> None:
    """The module claims the thread is a pure-cost detail for async hooks. That
    is only true if both paths observe identical context; otherwise the choice
    of ``def`` vs ``async def`` silently changes what a hook can read."""
    _TOKEN.set("VERIFIED")

    def sync_hook(_c: Any) -> str | None:
        return _TOKEN.get()

    async def async_hook(_c: Any) -> str | None:
        return _TOKEN.get()

    assert await _run(sync_hook, None) == await _run(async_hook, None) == "VERIFIED"


async def test_a_hook_cannot_leak_a_contextvar_back_into_the_caller() -> None:
    """``copy_context`` is a COPY. A hook setting a var must not rewrite the
    request's own context — containment runs in both directions."""
    _TOKEN.set("CALLER")

    def meddle(_c: Any) -> str:
        _TOKEN.set("HOOK-OWNED")
        return "done"

    await _run(meddle, None)
    assert _TOKEN.get() == "CALLER"


# --- the thread ceiling -----------------------------------------------------


async def test_wedged_hooks_cannot_grow_threads_without_bound() -> None:
    """The deadline abandons the FUTURE, never the thread.

    A vendor dependency that stops answering without a reset (a connection
    blackhole, no driver-side timeout) wedges one worker per tool call.
    Unbounded, the containment layer becomes the thing that takes the server
    down — the precise failure it exists to prevent. anyio's default thread
    limiter is the ceiling; a call past it waits for a token and fails on its
    own deadline instead of starting thread N+1.
    """
    release = threading.Event()
    limit = anyio.to_thread.current_default_thread_limiter().total_tokens

    def wedged(_c: Any) -> str:
        release.wait(timeout=30)
        return "never-observed"

    before = threading.active_count()
    try:
        results = await asyncio.gather(
            *(_run(wedged, None, timeout=0.05) for _ in range(int(limit) + 25)),
            return_exceptions=True,
        )
        assert all(isinstance(r, HookFailed) for r in results), results
        grew = threading.active_count() - before
        assert grew <= limit + 5, (
            f"threads grew by {grew} for {int(limit) + 25} wedged hooks; "
            f"the limiter ({limit}) is not holding"
        )
    finally:
        release.set()


async def test_a_worker_is_returned_to_the_pool_when_the_hook_finishes() -> None:
    """The ceiling must bound WEDGED hooks, not total calls ever made. A leak
    here would brick every hook after the first 40 tool calls."""
    limit = int(anyio.to_thread.current_default_thread_limiter().total_tokens)
    for _ in range(limit + 8):
        assert await _run(lambda _c: "ok", None) == "ok"


# --- the timeout ------------------------------------------------------------


async def test_a_hooks_OWN_TimeoutError_is_not_reported_as_the_budget() -> None:
    """An async hook wrapping its own lookup in ``asyncio.timeout``, or using
    an httpx/socket timeout, raises ``TimeoutError`` from inside our budget.

    Blaming our deadline points a vendor at a knob that is not the problem and
    ``from None`` would erase the real cause from the ``exc_info`` their logs
    are about to print.
    """

    async def own_timeout(_c: Any) -> str:
        raise TimeoutError("the vendor's own 1s lookup budget")

    with pytest.raises(HookFailed) as caught:
        await _run(own_timeout, None, timeout=30.0)
    assert "exceeded" not in str(caught.value), str(caught.value)
    assert "raised TimeoutError" in str(caught.value)
    assert caught.value.__cause__ is not None, "the real cause was discarded"


async def test_the_budget_expiring_IS_reported_as_the_budget() -> None:
    """The other side, so the discrimination above is proven in both
    directions rather than just made harder to trigger."""

    def slow(_c: Any) -> str:
        time.sleep(2.0)
        return "late"

    started = time.monotonic()
    with pytest.raises(HookFailed, match=r"exceeded 0\.1s"):
        await _run(slow, None, timeout=0.1)
    elapsed = time.monotonic() - started
    # ⚠ The TIMING is the assertion, not the exception. Without
    # ``abandon_on_cancel=True`` anyio shields the waiting task while the
    # thread runs, so the deadline still fires — just after the hook finishes,
    # 2s late, having blocked the caller for exactly as long as having no
    # timeout at all. Found by mutation: flipping that flag left this test
    # green until the clock was checked.
    assert elapsed < 1.0, (
        f"the deadline took {elapsed:.1f}s to fire against a 2s hook — the "
        f"caller was made to wait for the hook it had already given up on"
    )


# --- process exit -----------------------------------------------------------


def test_a_wedged_hook_DELAYS_shutdown_a_known_anyio_limitation() -> None:
    """⚠ **This test pins a limitation, not a guarantee. If it FAILS, that is
    good news — delete it and the note in ``_hooks``.**

    anyio's ``WorkerThread`` is created without ``daemon=True``, so it inherits
    non-daemon and Python's ``threading._shutdown`` joins it at interpreter
    exit. ``abandon_on_cancel=True`` is about the RETURN VALUE — anyio's own
    docs say the thread "will still run its course but its return value ...
    will be ignored" — not about process lifetime. Measured on anyio 4.15.1
    under both ``asyncio.run`` and ``anyio.run``.

    Accepted deliberately (Ujwal, 2026-09-11) against ~38 lines of hand-rolled
    threading that had already shipped two bugs. A merely SLOW hook finishes
    and releases; only one wedged forever holds exit, and no events are lost —
    the sink's ``aclose`` runs before interpreter exit.

    A subprocess because the property IS process exit: nothing asserted from
    inside one interpreter can observe it.
    """
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = textwrap.dedent(f"""
        import asyncio, logging, sys, time
        sys.path.insert(0, {src!r})
        from baton.integrations._hooks import run_vendor_hook, HookFailed
        logging.disable(logging.CRITICAL)

        def wedged(_c):
            time.sleep(3)

        async def main():
            try:
                await run_vendor_hook(
                    wedged, None, hook_name="probe",
                    logger=logging.getLogger("p"), timeout=0.2,
                )
            except HookFailed:
                pass

        asyncio.run(main())
    """)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed >= 2.0, (
        f"the process exited in {elapsed:.1f}s despite a 3s wedged hook and a "
        f"0.2s deadline — anyio's worker threads appear to be daemon now. "
        f"That removes the limitation documented in _hooks.py: delete this "
        f"test and that note."
    )


# --- the session hook's own validation --------------------------------------


@pytest.mark.parametrize(
    "blank",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="space"),
        pytest.param("\t\n", id="tab-newline"),
        pytest.param("   ", id="padded-CHAR-column"),
    ],
)
async def test_a_blank_session_id_from_a_hook_is_a_miss(blank: str) -> None:
    """``session_id`` is the PRIMARY grouping key and the hook's value is
    passed through raw — nothing downstream normalizes it.

    A hook returning whitespace would file every such call under one session
    and merge strangers' conversations, which is strictly worse than the same
    bug on ``user_id``: that one misattributes an actor, this one invents a
    shared conversation. Falling through to SPEC §3.4's ladder yields a real
    id. AgentCat's equivalent already strips; ours did not until 2026-09-11.
    """
    from baton.integrations._config import SessionResolutionContext, resolve_via_hook

    ctx = SessionResolutionContext(headers=None, meta=None, tool_name="l", arguments={})
    assert await resolve_via_hook(lambda _c: blank, ctx) is None


async def test_a_padded_session_id_is_stripped_not_split() -> None:
    """Two hooks differing only in padding must not become two sessions — the
    vendor's intent is the id, not the whitespace around it."""
    from baton.integrations._config import SessionResolutionContext, resolve_via_hook

    ctx = SessionResolutionContext(headers=None, meta=None, tool_name="l", arguments={})
    assert await resolve_via_hook(lambda _c: "  sess-42  ", ctx) == "sess-42"
    assert await resolve_via_hook(lambda _c: "sess-42", ctx) == "sess-42"


# --- the session hook gets the SAME containment -----------------------------
#
# ⚠ Every test above drives ``run_vendor_hook`` or the identity path. Both
# hooks route through the same function, but "both are wired" was only ever
# checked by reading the source — and a mutation that called
# ``resolve_session_id`` inline, untimed and context-less passed the ENTIRE
# suite. These are what make the second hook's containment a tested property
# rather than an observed one.

_SESSION_CTX = SessionResolutionContext(headers=None, meta=None, tool_name="l", arguments={})


async def test_a_blocking_session_hook_does_not_stall_concurrent_calls() -> None:
    """The session hook is likelier than the identity one to do I/O — it
    exists for vendors who already have their own session concept to look up."""
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    def blocks(_c: Any) -> str:
        time.sleep(0.3)
        return "sess-1"

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    got = await resolve_via_hook(blocks, _SESSION_CTX)
    stop.set()
    await beat
    assert got == "sess-1"
    assert ticks > 10, f"the event loop was blocked during the session hook: {ticks} ticks"


async def test_a_wedged_session_hook_falls_through_to_the_ladder() -> None:
    """Without a deadline the vendor's tool call waits forever on their own
    lookup. Falling through costs a real session id from SPEC §3.4 instead."""
    import baton.integrations._hooks as hooks_mod

    def wedged(_c: Any) -> str:
        time.sleep(5.0)
        return "sess-1"

    original = hooks_mod.HOOK_TIMEOUT_SECONDS
    hooks_mod.HOOK_TIMEOUT_SECONDS = 0.2
    try:
        started = time.monotonic()
        assert await resolve_via_hook(wedged, _SESSION_CTX) is None
        assert time.monotonic() - started < 1.5
    finally:
        hooks_mod.HOOK_TIMEOUT_SECONDS = original


async def test_a_sync_session_hook_can_read_the_callers_contextvars() -> None:
    """``extract_headers`` is contextvar-backed on the standalone adapter, and
    a session hook reading request state is the documented use — the same
    reason this mattered for identity."""
    _TOKEN.set("VERIFIED")

    def sync_hook(_c: Any) -> str | None:
        return _TOKEN.get()

    assert await resolve_via_hook(sync_hook, _SESSION_CTX) == "VERIFIED"
