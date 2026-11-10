"""Capture settings read from the collector (SPEC §2.6): fetch, match, apply."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal, get_args

from baton._overlays import Overlay, overlay_for, read_overlays
from baton._result_capture import is_registered_mode
from baton._white_space import usable_text
from baton.events import CaptureObserved
from baton.sinks import _LIVE_SENDERS, _pool_through, _proxy_for, _require_urllib3

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
IntentParamMode = Literal["optional", "required"]
ReportMode = Literal["on", "off"]
INTENT_PARAM_MODES: frozenset[str] = frozenset(get_args(IntentParamMode))
REPORT_MODES: frozenset[str] = frozenset(get_args(ReportMode))


def clean_labels(values: Iterable[Any]) -> tuple[str, ...]:
    """Usable labels in the order given, without repeats (SPEC §11.4)."""
    kept: dict[str, None] = {}
    for value in values:
        if usable_text(value, LABEL_MAX_LENGTH):
            # A plain copy: a str subclass can refuse to hash.
            kept[str.__str__(value)] = None
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
    """The settings one call runs under."""

    capture: CaptureObserved
    # ``None`` in any of these means this config's value.
    result_capture_mode: str | None = None
    intent_param_mode: str | None = None
    report_mode: str | None = None
    overlay: Overlay | None = None


@dataclass(frozen=True)
class Audience:
    """One audience, its ``match`` checked once when the document is read."""

    settings: CallSettings
    labels: frozenset[str] | None = None
    clients: frozenset[str] | None = None
    share_threshold: int | None = None

    @property
    def matches_everyone(self) -> bool:
        return self.labels is None and self.clients is None and self.share_threshold is None

    def matches(self, caller: Caller, bucket: int | None) -> bool:
        if self.labels is not None and self.labels.isdisjoint(caller.labels):
            return False
        if self.clients is not None and caller.client_name not in self.clients:
            return False
        if self.share_threshold is not None:
            if bucket is None or bucket >= self.share_threshold:
                return False
        return True


@dataclass(frozen=True)
class SettingsDocument:
    version: int
    audiences: tuple[Audience, ...]
    unmatched: CallSettings
    uses_share: bool
    server_overlay: Overlay | None = None

    def settings_for(self, caller: Caller, vendor_id: str) -> CallSettings:
        bucket = (
            share_bucket(vendor_id, caller.share_key)
            if self.uses_share and caller.share_key is not None
            else None
        )
        for audience in self.audiences:
            if audience.matches(caller, bucket):
                return audience.settings
        return self.unmatched

    @functools.cached_property
    def hidden_somewhere(self) -> frozenset[str]:
        """The tools some audience's overlay hides."""
        return frozenset(
            name
            for a in self.audiences
            if a.settings.overlay is not None
            for name in a.settings.overlay.hidden_tools
        )

    def reports_on_for_anyone(self, code_value: str) -> bool:
        """Whether some caller can get reports, so the instructions point to the tool."""
        reachable = [a.settings for a in self.audiences]
        if not any(a.matches_everyone for a in self.audiences):
            reachable.append(self.unmatched)
        return any((s.report_mode or code_value) == "on" for s in reachable)


def _string_set(value: Any) -> frozenset[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return frozenset(value)


def _known(value: Any, values: frozenset[str]) -> bool:
    # A str check first: an unhashable value must not raise here.
    return isinstance(value, str) and value in values


def _audience(
    version: int,
    audience_id: str,
    match: dict[str, Any],
    capture: dict[str, Any],
    overlay: Overlay | None,
) -> Audience | None:
    """``None`` for an audience that can match nobody (SPEC §2.6)."""
    if not set(match) <= _MATCH_KEYS:
        return None
    labels = clients = threshold = None
    if "label" in match and (labels := _string_set(match["label"])) is None:
        return None
    if "client" in match and (clients := _string_set(match["client"])) is None:
        return None
    if "share" in match:
        share = match["share"]
        # The range test alone also refuses NaN, inf and integers too big for a float.
        if isinstance(share, bool) or not isinstance(share, int | float) or not 0 < share <= 1:
            return None
        # Half up, not round(): SPEC §2.6, pinned by test_a_half_rounds_up.
        threshold = math.floor(share * 10000 + 0.5)
    results, goal, reports = (capture.get(k) for k in ("results", "goal", "reports"))
    settings = CallSettings(
        CaptureObserved(
            settings_version=version,
            audience=audience_id,
            overlay=overlay.id if overlay is not None else None,
        ),
        result_capture_mode=results if is_registered_mode(results) else None,
        intent_param_mode=goal if _known(goal, INTENT_PARAM_MODES) else None,
        report_mode=reports if _known(reports, REPORT_MODES) else None,
        overlay=overlay,
    )
    return Audience(settings, labels, clients, threshold)


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
    overlays = read_overlays(data.get("overlays"))
    parsed: list[Audience] = []
    for audience in audiences:
        if not isinstance(audience, dict):
            return None
        audience_id, match, capture = (audience.get(k) for k in ("id", "match", "capture"))
        if not isinstance(audience_id, str):
            return None
        if not isinstance(match, dict) or not isinstance(capture, dict):
            return None
        overlay = overlay_for(overlays, audience.get("overlay"), "audience")
        if (readable := _audience(version, audience_id, match, capture, overlay)) is not None:
            parsed.append(readable)
    return SettingsDocument(
        version,
        tuple(parsed),
        unmatched=CallSettings(CaptureObserved(settings_version=version)),
        uses_share=any(a.share_threshold is not None for a in parsed),
        server_overlay=overlay_for(overlays, data.get("server_overlay"), "server"),
    )


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
        self._warned = False

    @property
    def document(self) -> SettingsDocument | None:
        return self._document

    def start(self) -> None:
        _LIVE_SENDERS.add(self)
        self._pool = None
        threading.Thread(target=self._run, name="baton-settings", daemon=True).start()

    def reset_in_child(self) -> None:
        """A forked child has no reading thread, and the pool's sockets are the
        parent's."""
        if not self._stop.is_set():
            self._stop = threading.Event()
            self.start()

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

    @property
    def document(self) -> SettingsDocument | None:
        return self._fetcher.document

    def for_caller(self, caller: Caller) -> CallSettings | None:
        """``None`` while no document is held, and this config's values apply."""
        document = self.document
        return None if document is None else document.settings_for(caller, self._vendor_id)
