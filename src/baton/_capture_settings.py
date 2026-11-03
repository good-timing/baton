"""Capture settings read from the collector (SPEC §2.6): fetch, match, apply."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from baton._result_capture import is_registered_mode
from baton._white_space import usable_text
from baton.events import CaptureObserved
from baton.sinks import _pool_through, _proxy_for, _require_urllib3

logger = logging.getLogger("baton")

SETTINGS_PATH = "/v0/settings"
TENANT_HEADER = "X-Baton-Tenant-Id"
SETTINGS_MODES = frozenset({"local", "console"})
DEFAULT_REFRESH_S = 60.0
MIN_REFRESH_S = 15.0
REQUEST_TIMEOUT_S = 10.0

LABELS_MAX = 20
LABEL_MAX_LENGTH = 64

_MATCH_KEYS = frozenset({"label", "client", "share"})


def clean_labels(values: Iterable[Any]) -> tuple[str, ...]:
    """Usable labels in the order given, without repeats (SPEC §11.4)."""
    kept: dict[str, None] = {}
    for value in values:
        if usable_text(value, LABEL_MAX_LENGTH):
            kept[value] = None
            if len(kept) == LABELS_MAX:
                break
    return tuple(kept)


def share_bucket(vendor_id: str, key: str) -> int:
    digest = hashlib.sha256(f"{vendor_id}\n{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 10000


@dataclass(frozen=True)
class Caller:
    """What a ``match`` rule can see about one request."""

    labels: tuple[str, ...] = ()
    client_name: str | None = None
    share_key: str | None = None


@dataclass(frozen=True)
class CallSettings:
    """The settings one call runs under. ``None`` means the code value."""

    result_capture_mode: str | None
    capture: CaptureObserved


@dataclass(frozen=True)
class Audience:
    """One audience, its ``match`` checked once when the document is read."""

    settings: CallSettings
    readable: bool
    labels: frozenset[str] | None = None
    clients: frozenset[str] | None = None
    share_threshold: int | None = None

    def matches(self, caller: Caller, bucket: Callable[[], int | None]) -> bool:
        if not self.readable:
            return False
        if self.labels is not None and self.labels.isdisjoint(caller.labels):
            return False
        if self.clients is not None and caller.client_name not in self.clients:
            return False
        if self.share_threshold is not None:
            caller_bucket = bucket()
            if caller_bucket is None or caller_bucket >= self.share_threshold:
                return False
        return True


@dataclass(frozen=True)
class SettingsDocument:
    version: int
    audiences: tuple[Audience, ...]

    def settings_for(self, caller: Caller, vendor_id: str) -> CallSettings:
        @functools.cache
        def bucket() -> int | None:
            return None if caller.share_key is None else share_bucket(vendor_id, caller.share_key)

        for audience in self.audiences:
            if audience.matches(caller, bucket):
                return audience.settings
        return CallSettings(None, CaptureObserved(settings_version=self.version))


def _string_set(value: Any) -> frozenset[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


def _audience(
    version: int, audience_id: str, match: dict[str, Any], capture: dict[str, Any]
) -> Audience:
    results = capture.get("results")
    settings = CallSettings(
        results if is_registered_mode(results) else None,
        CaptureObserved(settings_version=version, audience=audience_id),
    )
    unreadable = Audience(settings, readable=False)
    if not set(match) <= _MATCH_KEYS:
        return unreadable
    labels = clients = threshold = None
    if "label" in match and (labels := _string_set(match["label"])) is None:
        return unreadable
    if "client" in match and (clients := _string_set(match["client"])) is None:
        return unreadable
    if "share" in match:
        share = match["share"]
        if isinstance(share, bool) or not isinstance(share, int | float):
            return unreadable
        if not math.isfinite(share) or not 0 < share <= 1:
            return unreadable
        # Half up, not round(): SPEC §2.6, pinned by test_a_half_rounds_up.
        threshold = math.floor(share * 10000 + 0.5)
    return Audience(settings, True, labels, clients, threshold)


def parse_document(body: bytes) -> SettingsDocument | None:
    """The document in ``body``, or ``None`` when it is not a valid one (SPEC §2.6)."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    audiences = data.get("audiences")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    if not isinstance(audiences, list) or not audiences:
        return None
    parsed: list[Audience] = []
    for audience in audiences:
        if not isinstance(audience, dict):
            return None
        audience_id, match, capture = (audience.get(k) for k in ("id", "match", "capture"))
        if not isinstance(audience_id, str):
            return None
        if not isinstance(match, dict) or not isinstance(capture, dict):
            return None
        parsed.append(_audience(version, audience_id, match, capture))
    return SettingsDocument(version, tuple(parsed))


class SettingsFetcher:
    """Reads the document on a background thread and holds the last good one."""

    def __init__(
        self,
        *,
        origin: str,
        api_key: str,
        tenant_id: str,
        vendor_id: str,
        refresh_s: float,
        make_pool: Callable[[], Any] | None = None,
    ) -> None:
        query = urllib.parse.urlencode({"server": vendor_id})
        self._url = f"{origin}{SETTINGS_PATH}?{query}"
        self._headers = {"Authorization": f"Bearer {api_key}", TENANT_HEADER: tenant_id}
        self._refresh_s = refresh_s
        # The proxy is looked up here, at install, as HttpSink does.
        self._make_pool = make_pool or functools.partial(_pool_through, _proxy_for(origin))
        self._pool: Any = None
        self._etag: str | None = None
        self._document: SettingsDocument | None = None
        self._stop = threading.Event()
        self._pid: int | None = None
        self._warned = False

    @property
    def document(self) -> SettingsDocument | None:
        if self._pid is not None and self._pid != os.getpid() and not self._stop.is_set():
            # A forked child has no reading thread.
            self.start()
        return self._document

    def start(self) -> None:
        self._pid = os.getpid()
        self._pool = None
        threading.Thread(target=self._run, name="baton-settings", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.read_once()
            self._stop.wait(self._refresh_s)

    def read_once(self) -> None:
        """One request. Never raises: a failure keeps what is held."""
        try:
            self._read()
        except Exception:
            logger.debug("baton: reading capture settings failed", exc_info=True)

    def _read(self) -> None:
        urllib3 = _require_urllib3()

        headers = dict(self._headers)
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        if self._pool is None:
            self._pool = self._make_pool()
        response = self._pool.urlopen(
            "GET",
            self._url,
            headers=headers,
            timeout=urllib3.Timeout(total=REQUEST_TIMEOUT_S),
            retries=False,
            redirect=False,
        )
        if response.status == 204:
            self._document, self._etag = None, None
        elif response.status == 200:
            document = parse_document(response.data)
            if document is None:
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "baton: the collector sent capture settings this SDK cannot read"
                    )
                return
            self._document = document
            self._etag = response.headers.get("etag")


class CaptureSettings:
    """Per-call settings from the matched audience (SPEC §2.6)."""

    def __init__(self, *, vendor_id: str, fetcher: SettingsFetcher) -> None:
        self._vendor_id = vendor_id
        self._fetcher = fetcher

    def start(self) -> None:
        self._fetcher.start()

    def stop(self) -> None:
        self._fetcher.stop()

    def for_caller(self, caller: Caller) -> CallSettings | None:
        """``None`` until a document is held: the code values apply."""
        document = self._fetcher.document
        return None if document is None else document.settings_for(caller, self._vendor_id)
