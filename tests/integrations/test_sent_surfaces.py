"""The bounded record of surface hashes sent, shared by both adapters and the library."""

from __future__ import annotations

import pytest

from baton.integrations._surface import SentSurfaces


@pytest.fixture(autouse=True)
def _cap_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("baton.integrations._surface.SENT_SURFACES_CAP", 2)


def test_the_least_recently_seen_hash_is_dropped_first() -> None:
    sent = SentSurfaces()
    sent.mark("a")
    sent.mark("b")
    assert sent.was_sent("a")
    sent.mark("c")
    assert sent.was_sent("a")
    assert sent.was_sent("c")
    assert not sent.was_sent("b")


def test_an_unmarked_hash_is_sent_again() -> None:
    sent = SentSurfaces()
    sent.mark("a")
    sent.unmark("a")
    sent.unmark("never-marked")
    assert not sent.was_sent("a")
