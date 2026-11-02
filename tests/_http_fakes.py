"""A stand-in for the ``urllib3.PoolManager`` an ``HttpSink`` sends with, passed
as ``_http_client``. It runs on the sink's sending thread."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import urllib3

from baton.events import ToolCallStartEvent, ToolCallStartPayload
from baton.sinks import HttpSink

#: Set by ``tests/conftest.py`` after each test, so a thread held by
#: ``never_answers`` ends.
RELEASE = threading.Event()


@dataclass
class Answer:
    status: int
    data: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


#: Called with the path and the parsed body; answers a status or an ``Answer``,
#: or raises a ``urllib3`` exception for a network failure.
Answerer = Callable[[str, Any], "int | Answer"]


def events_in(path: str, body: Any) -> list[dict[str, Any]]:
    return list(body["events"]) if path.endswith("/batch") else [body]


class FakePool:
    def __init__(self, answer: Answerer) -> None:
        self._answer = answer
        self.requests: list[tuple[str, Any]] = []
        self.lock = threading.Lock()

    def urlopen(self, method: str, url: str, *, body: bytes, **kwargs: Any) -> Answer:
        path = urlsplit(url).path
        parsed = json.loads(body)
        with self.lock:
            self.requests.append((path, parsed))
        result = self._answer(path, parsed)
        return result if isinstance(result, Answer) else Answer(result)

    def clear(self) -> None:
        pass

    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [e for path, body in self.requests for e in events_in(path, body)]


def never_answers(path: str, body: Any) -> int:
    RELEASE.wait()
    return 503


def hold_first(gate: threading.Event, then: Answerer = lambda path, body: 200) -> Answerer:
    """Holds the first request until ``gate`` is set, then answers 200; ``then``
    answers the rest."""
    answered = 0

    def answer(path: str, body: Any) -> int | Answer:
        nonlocal answered
        answered += 1
        if answered == 1:
            gate.wait()
            return 200
        return then(path, body)

    return answer


def fake_sink(answer: Answerer, **kwargs: Any) -> HttpSink:
    kwargs.setdefault("max_retries", 0)
    return HttpSink(
        url="http://collector.example",
        api_key="k",
        _http_client=FakePool(answer),  # type: ignore[arg-type]
        **kwargs,
    )


def make_event(sequence_number: int = 1) -> ToolCallStartEvent:
    return ToolCallStartEvent(
        tenant_id="ten_test",
        vendor_id="ten_test",
        session_id="sess_test",
        sequence_number=sequence_number,
        captured_at=datetime.now(UTC),
        consent_token="ct_test",
        agent_runtime="claude-code",
        call_id="call_test",
        payload=ToolCallStartPayload(tool_name="t"),
    )


async def until(condition: Callable[[], object], seconds: float = 3.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


class ReportCollector:
    """Answers a request holding events by a list of statuses, and one holding
    only a ``client_report`` with ``report_status``; None fails to connect."""

    def __init__(self, event_statuses: list[int], report_status: int | None = 202) -> None:
        self._event_statuses: Iterator[int] = iter(event_statuses)
        self.report_status = report_status
        self.reports: list[dict[str, Any]] = []
        self.delivered = 0

    def __call__(self, path: str, body: Any) -> int | Answer:
        events = events_in(path, body)
        self.reports += [e for e in events if e["event_type"] == "client_report"]
        tool_events = [e for e in events if e["event_type"] != "client_report"]
        if not tool_events:
            if self.report_status is None:
                raise urllib3.exceptions.ProtocolError("down")
            return self.report_status
        status = next(self._event_statuses)
        self.delivered += len(tool_events) if status == 202 else 0
        return status


def pool_of(sink: HttpSink) -> FakePool:
    pool = sink._sender.pool
    assert isinstance(pool, FakePool)
    return pool
