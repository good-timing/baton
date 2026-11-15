"""Fakes for test changes by id (SPEC §2.6). Imports no fastmcp, so the
``mcp-matrix`` leg can use it."""

from __future__ import annotations

import json
import time
from typing import Any

from baton._capture_settings import CaptureSettings, SettingsFetcher

CHANGE = "chg_1"


def document(description: str, *, candidate: str | None = None, **extra: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 4,
        "audiences": [
            {
                "id": "everyone",
                "match": {},
                "capture": {"goal": "required", "reports": "off", "results": "full"},
                "overlay": "ov_1",
            }
        ],
        "overlays": {"ov_1": {"actions": [{"target": {"tool": "lookup"}, "replace": description}]}},
        **extra,
    }
    if candidate is not None:
        document["candidate"] = candidate
    return document


TODAY = document("Today's text.")
CHANGED = document("The change's text.", candidate=CHANGE)


class CollectorResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.data = json.dumps(body).encode() if body is not None else b""
        self.headers: dict[str, str] = {}


class Collector:
    """Answers today's settings, and each change id from ``changes``: a
    document, an HTTP status, or an exception."""

    def __init__(self, changes: dict[str, Any], today: dict[str, Any] = TODAY) -> None:
        self.changes = changes
        self.today = today
        self.urls: list[str] = []

    def urlopen(self, method: str, url: str, **_: Any) -> CollectorResponse:
        self.urls.append(url)
        if "candidate=" not in url:
            return CollectorResponse(200, self.today)
        answer = self.changes[url.rsplit("candidate=", 1)[1]]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return CollectorResponse(answer, {"error": "x"})
        return CollectorResponse(200, answer)

    def change_reads(self) -> list[str]:
        return [u.rsplit("candidate=", 1)[1] for u in self.urls if "candidate=" in u]


def candidate_settings(collector: Collector, candidate: str | None = None) -> CaptureSettings:
    fetcher = SettingsFetcher(
        origin="https://h.example.com",
        api_key="baton_pk_x",
        tenant_id="ten_1",
        vendor_id="srv",
        refresh_s=60,
        make_pool=lambda: collector,
    )
    return CaptureSettings(
        vendor_id="srv", fetcher=fetcher, candidate=candidate, start_timeout_s=0.5
    )


def started(collector: Collector, candidate: str | None = None) -> CaptureSettings:
    settings = candidate_settings(collector, candidate)
    settings.start()
    wait_for(lambda: settings.document is not None)
    return settings


def wait_for(done: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not done():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)
