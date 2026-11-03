"""SPEC §11.2 rule 5: session counters are bounded, and observed sessions are
numbered by the clock too."""

from __future__ import annotations

import time

from baton._state import SESSION_CAP, SessionCounter
from baton.integrations._session import ResolvedSession, next_sequence
from baton.integrations._shared import ServerCounter


async def test_minted_session_counts_from_one() -> None:
    counter = SessionCounter()
    assert [await counter.next("s") for _ in range(3)] == [1, 2, 3]


async def test_keeps_at_most_the_cap_and_forgets_the_oldest() -> None:
    counter = SessionCounter()
    await counter.next("first")
    for i in range(SESSION_CAP):
        await counter.next(f"other-{i}")
    assert SESSION_CAP == 4096
    assert len(counter._counters) == SESSION_CAP
    assert await counter.next("first") == 1


async def test_forgets_the_least_recently_used_not_the_first_added() -> None:
    counter = SessionCounter()
    await counter.next("busy")
    for i in range(SESSION_CAP - 1):
        await counter.next(f"idle-{i}")
    await counter.next("busy")
    await counter.next("new")
    assert await counter.next("busy") == 3
    assert await counter.next("idle-0") == 1


async def test_observed_session_is_numbered_at_least_by_the_clock() -> None:
    counter = SessionCounter()
    before = time.time_ns() // 1000
    first = await counter.next("s", observed=True)
    second = await counter.next("s", observed=True)
    assert first >= before
    assert second > first


async def test_next_sequence_numbers_an_observed_session_by_the_clock() -> None:
    counter = SessionCounter()
    before = time.time_ns() // 1000
    assert await next_sequence(counter, ResolvedSession("o", "observed")) >= before
    assert await next_sequence(counter, ResolvedSession("m", "minted")) == 1


async def test_server_counter_passes_observed_on_to_the_shared_counter() -> None:
    server = ServerCounter(SessionCounter(), "own")
    before = time.time_ns() // 1000
    assert await server.next("client", observed=True) >= before
    assert await server.next("own") == 1
