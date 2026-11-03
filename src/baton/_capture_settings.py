"""Capture settings read from the collector (SPEC §2.6): fetch, match, apply."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from baton._white_space import is_blank
from baton.events import CaptureObserved

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
_RESULT_MODES = frozenset({"full", "off"})


def usable_label(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > LABEL_MAX_LENGTH or is_blank(value):
        return False
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def clean_labels(values: Iterable[Any]) -> tuple[str, ...]:
    """Usable labels in the order given, without repeats, at most 20 (SPEC §11.4)."""
    kept: list[str] = []
    for value in values:
        if usable_label(value) and value not in kept:
            kept.append(value)
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
class Audience:
    id: str
    match: dict[str, Any]
    capture: dict[str, Any]

    def matches(self, caller: Caller, vendor_id: str) -> bool:
        if not set(self.match) <= _MATCH_KEYS:
            return False
        if "label" in self.match:
            wanted = self.match["label"]
            if not isinstance(wanted, list) or not any(w in caller.labels for w in wanted):
                return False
        if "client" in self.match:
            names = self.match["client"]
            if not isinstance(names, list) or caller.client_name not in names:
                return False
        if "share" in self.match:
            share = self.match["share"]
            if isinstance(share, bool) or not isinstance(share, int | float):
                return False
            if not math.isfinite(share) or not 0 < share <= 1 or caller.share_key is None:
                return False
            if share_bucket(vendor_id, caller.share_key) >= math.floor(share * 10000 + 0.5):
                return False
        return True


@dataclass(frozen=True)
class SettingsDocument:
    version: int
    audiences: tuple[Audience, ...]

    def audience_for(self, caller: Caller, vendor_id: str) -> Audience | None:
        for audience in self.audiences:
            if audience.matches(caller, vendor_id):
                return audience
        return None


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
        parsed.append(Audience(audience_id, match, capture))
    return SettingsDocument(version, tuple(parsed))


@dataclass(frozen=True)
class CallSettings:
    """The settings one call runs under."""

    result_capture_mode: str
    capture: CaptureObserved | None = None


class SettingsFetcher:
    """Reads the document on a background thread and holds the last good one."""

    def __init__(
        self,
        *,
        origin: str,
        api_key: str,
        tenant_id: str,
        vendor_id: str,
        refresh_s: float = DEFAULT_REFRESH_S,
        opener: Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]
        | None = None,
    ) -> None:
        query = urllib.parse.urlencode({"server": vendor_id})
        self._url = f"{origin}{SETTINGS_PATH}?{query}"
        self._headers = {"Authorization": f"Bearer {api_key}", TENANT_HEADER: tenant_id}
        self._refresh_s = refresh_s
        self._open = opener or _open_without_redirects
        self._etag: str | None = None
        self._document: SettingsDocument | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False

    @property
    def document(self) -> SettingsDocument | None:
        return self._document

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="baton-settings", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.read_once()
            except Exception:
                logger.debug("baton: reading capture settings failed", exc_info=True)
            self._stop.wait(self._refresh_s)

    def read_once(self) -> None:
        """One request. Never raises: a failure keeps what is held."""
        headers = dict(self._headers)
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        try:
            status, response_headers, body = self._open(
                urllib.request.Request(self._url, headers=headers), REQUEST_TIMEOUT_S
            )
        except Exception:
            logger.debug("baton: reading capture settings failed", exc_info=True)
            return
        if status == 204:
            self._document, self._etag = None, None
        elif status == 200:
            document = parse_document(body)
            if document is None:
                if not self._warned:
                    self._warned = True
                    logger.warning(
                        "baton: the collector sent capture settings this SDK cannot read"
                    )
                return
            self._document = document
            self._etag = response_headers.get("etag")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _open_without_redirects(
    request: urllib.request.Request, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, b""


class CaptureSettings:
    """Per-call settings: the code values, or the matched audience's (SPEC §2.6)."""

    def __init__(
        self,
        *,
        vendor_id: str,
        result_capture_mode: str,
        fetcher: SettingsFetcher | None = None,
    ) -> None:
        self._vendor_id = vendor_id
        self._code = CallSettings(result_capture_mode=result_capture_mode)
        self._fetcher = fetcher

    def start(self) -> None:
        if self._fetcher is not None:
            self._fetcher.start()

    def stop(self) -> None:
        if self._fetcher is not None:
            self._fetcher.stop()

    def for_caller(self, caller: Caller) -> CallSettings:
        document = self._fetcher.document if self._fetcher is not None else None
        if document is None:
            return self._code
        audience = document.audience_for(caller, self._vendor_id)
        capture = CaptureObserved(
            settings_version=document.version,
            audience=audience.id if audience is not None else None,
        )
        results = audience.capture.get("results") if audience is not None else None
        return CallSettings(
            result_capture_mode=results
            if isinstance(results, str) and results in _RESULT_MODES
            else self._code.result_capture_mode,
            capture=capture,
        )
