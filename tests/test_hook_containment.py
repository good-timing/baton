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

import baton.integrations._hooks as hooks_mod
from baton.integrations._hooks import HookFailed, live_hook_threads, run_vendor_hook

LOG = logging.getLogger("test-hooks")


async def _run(fn: Any, *args: Any, **kw: Any) -> Any:
    return await run_vendor_hook(fn, *args, hook_name="probe", logger=LOG, **kw)


def _drain_hook_threads(timeout: float = 10.0) -> None:
    """Wait for released hook threads to leave the vendor's callable.

    The ceiling is module state shared by every test in this file, and a
    released thread decrements it slightly AFTER the ``Event`` is set — so a
    test that wedges to the ceiling and returns immediately would hand the
    next one a full counter and a refusal it never asked for.
    """
    deadline = time.monotonic() + timeout
    while live_hook_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not live_hook_threads(), (
        f"{live_hook_threads()} hook threads still counted after draining"
    )


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
    down — the precise failure it exists to prevent.

    ⚠ **SERIAL, and that is the whole test.** This fired all of them through
    one ``asyncio.gather`` until 2026-09-11 and could not fail: the calls past
    anyio's limiter blocked WAITING FOR A TOKEN and expired on their own
    deadline before ever dispatching a thread, so growth landed at the limiter
    and the assertion passed while the leak was real. Serial is also the
    honest traffic shape — a hook runs once per tool call.
    """
    release = threading.Event()
    ceiling = hooks_mod.HOOK_THREAD_CEILING

    def wedged(_c: Any) -> str:
        release.wait(timeout=30)
        return "never-observed"

    before = threading.active_count()
    try:
        for _ in range(ceiling + 25):
            with pytest.raises(HookFailed):
                await _run(wedged, None, timeout=0.05)
        grew = threading.active_count() - before
        assert grew <= ceiling + 5, (
            f"threads grew by {grew} for {ceiling + 25} wedged hooks; "
            f"the ceiling ({ceiling}) is not holding"
        )
    finally:
        release.set()
        _drain_hook_threads()


async def test_a_hook_does_not_queue_behind_the_vendors_OWN_sync_handlers() -> None:
    """Hook dispatch runs on a limiter of ours, not anyio's default pool.

    fastmcp puts every sync tool handler, resource, prompt and dependency
    through ``anyio.to_thread.run_sync`` with no limiter — the same default
    40-token pool. Sharing it means a hook queues behind the vendor's own
    handlers INSIDE our deadline, so a hook that returns instantly reports
    "exceeded 5.0s" having never run, after donating the whole budget to the
    tool call it was supposed to be cheap for. Measured exactly that way
    before the limiter was separated.
    """
    release = threading.Event()
    default_pool = int(anyio.to_thread.current_default_thread_limiter().total_tokens)

    def vendor_sync_tool_handler() -> None:
        release.wait(timeout=30)

    holders = [
        asyncio.create_task(anyio.to_thread.run_sync(vendor_sync_tool_handler))
        for _ in range(default_pool)
    ]
    try:
        await asyncio.sleep(0.3)  # let them all take a token
        started = time.monotonic()
        assert await _run(lambda _c: "user-1", None, timeout=2.0) == "user-1"
        assert time.monotonic() - started < 1.0, (
            "the hook waited on the default pool the vendor's handlers own"
        )
    finally:
        release.set()
        await asyncio.gather(*holders, return_exceptions=True)


async def test_a_healthy_but_SATURATED_hook_is_not_refused_as_wedged() -> None:
    """The ceiling must fire on a LEAK, not on load.

    It was equal to the dispatch concurrency for one commit, which made full
    legitimate utilization indistinguishable from a wedge: 40 concurrent
    200ms lookups — nothing stuck, every one about to succeed — got the next
    call refused, with a message telling the vendor their working hook was
    wedged. The ceiling now sits above the concurrency, so reaching it takes
    threads that outlived their deadline.
    """

    def healthy_lookup(_c: Any) -> str:
        time.sleep(0.2)
        return "user-1"

    inflight = [
        asyncio.create_task(_run(healthy_lookup, None, timeout=5.0))
        for _ in range(hooks_mod.HOOK_THREAD_CONCURRENCY)
    ]
    try:
        await asyncio.sleep(0.1)
        assert await _run(healthy_lookup, None, timeout=5.0) == "user-1"
    finally:
        assert all(r == "user-1" for r in await asyncio.gather(*inflight))


async def test_the_ceiling_refusal_names_itself_and_is_not_the_deadline() -> None:
    """A refused call and an expired one are different operational problems —
    one says the vendor's dependency is down, the other says this hook is slow.
    Reporting them with one message sends an operator to the wrong knob."""
    release = threading.Event()

    def wedged(_c: Any) -> str:
        release.wait(timeout=30)
        return "never-observed"

    try:
        for _ in range(hooks_mod.HOOK_THREAD_CEILING):
            with pytest.raises(HookFailed):
                await _run(wedged, None, timeout=0.05)
        with pytest.raises(HookFailed) as refused:
            await _run(wedged, None, timeout=0.05)
        assert "refused" in str(refused.value), str(refused.value)
        assert "exceeded" not in str(refused.value), str(refused.value)
    finally:
        release.set()
        _drain_hook_threads()


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


# --- rung 4's own blank/padding rule -----------------------------------------
#
# These two arrived as "rung 4 gets rung 0's rule". Rung 0
# (``VendorConfig.resolve_session_id``) was REMOVED 2026-09-12 along with the
# five tests above them, so the rule no longer has a rung to be borrowed FROM
# — it is now rung 4's own, and RFC 9110 §5.5 is the whole of its authority.


@pytest.mark.parametrize(
    "blank",
    [
        pytest.param("", id="empty"),
        pytest.param(" ", id="space"),
        pytest.param("\t\n", id="tab-newline"),
    ],
)
def test_a_blank_mcp_session_id_HEADER_is_a_miss(blank: str) -> None:
    """A blank value merges strangers, and the ladder has a real id one rung
    down. h11 strips inbound headers, so this is reachable only through
    whatever mapping the transport supplies — which is precisely why the
    check belongs at the read.
    """
    from baton.integrations._session import session_id_from_headers

    assert session_id_from_headers({"mcp-session-id": blank}) is None


def test_a_padded_mcp_session_id_HEADER_is_stripped_not_split() -> None:
    """RFC 9110 §5.5: surrounding whitespace is not part of a field value, so
    these are ONE session, not two."""
    from baton.integrations._session import session_id_from_headers

    assert session_id_from_headers({"mcp-session-id": "  sess-42  "}) == "sess-42"
    assert session_id_from_headers({"mcp-session-id": "sess-42"}) == "sess-42"
