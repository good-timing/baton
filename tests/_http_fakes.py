"""A stand-in for the ``urllib3.PoolManager`` an ``HttpSink`` sends with, passed
as ``_http_client``. It runs on the sink's sending thread."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

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


def fake_sink(answer: Answerer, **kwargs: Any) -> HttpSink:
    return HttpSink(
        url="http://collector.example",
        api_key="k",
        _http_client=FakePool(answer),  # type: ignore[arg-type]
        **kwargs,
    )


def pool_of(sink: HttpSink) -> FakePool:
    pool = sink._http_client
    assert isinstance(pool, FakePool)
    return pool
