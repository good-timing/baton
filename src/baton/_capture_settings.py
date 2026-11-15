"""Capture settings read from the collector (SPEC §2.6): fetch, match, apply."""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from baton._overlays import Overlay, overlay_for, read_overlays
from baton._result_capture import is_registered_mode
from baton._white_space import strip_white_space, usable_text
from baton.events import CaptureObserved
from baton.sinks import _LIVE_SENDERS, _pool_through, _proxy_for, _require_urllib3

logger = logging.getLogger("baton")

SETTINGS_PATH = "/v0/settings"
TENANT_HEADER = "X-Baton-Tenant-Id"
SETTINGS_MODES = frozenset({"local", "console"})
DEFAULT_REFRESH_S = 60.0
MIN_REFRESH_S = 15.0
REQUEST_TIMEOUT_S = 10.0

CANDIDATE_ENV = "BATON_CANDIDATE"
CANDIDATE_HEADER = "baton-candidate"
CANDIDATES_HELD = 32
CANDIDATE_READS_AT_ONCE = 4
CANDIDATE_START_TIMEOUT_S = 5.0
# The header is the caller's: it becomes a query value and a cache key.
_CANDIDATE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

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


def candidate_id(value: object) -> str | None:
    """``value`` as a change id, stripped, or ``None`` when it cannot be one."""
    if not isinstance(value, str):
        return None
    # A plain copy: a str subclass can refuse to hash.
    value = strip_white_space(str.__str__(value))
    return value if _CANDIDATE_ID.fullmatch(value) else None


def header_candidate(headers: Mapping[str, Any] | None) -> str | None:
    """The ``Baton-Candidate`` header's change id. The adapters' header
    mappings are lowercased or case-insensitive. Never raises."""
    if not headers:
        return None
    try:
        return candidate_id(headers.get(CANDIDATE_HEADER))
    except Exception:
        return None


def env_candidate() -> str | None:
    """``BATON_CANDIDATE``, or ``None`` when unset. A value that cannot be an
    id is logged as an ERROR."""
    value = os.environ.get(CANDIDATE_ENV)
    if not value:
        return None
    candidate = candidate_id(value)
    if candidate is None:
        logger.error("baton: %s is not a change id; today's settings apply.", CANDIDATE_ENV)
    return candidate


@dataclass(frozen=True)
class Caller:
    """What a ``match`` rule can see about one request."""

    labels: tuple[str, ...] = ()
    client_name: str | None = None
    share_key: str | None = None
    # From the request's header; selects a document, never an audience.
    candidate: str | None = None


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

    @functools.cached_property
    def audience_pieces(self) -> frozenset[str]:
        """The pieces of Baton's text some audience's overlay changes."""
        return frozenset(
            action.piece
            for a in self.audiences
            if a.settings.overlay is not None
            for action in a.settings.overlay.actions
            if action.piece is not None
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
    candidate: str | None,
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
            candidate=candidate,
        ),
        result_capture_mode=results if is_registered_mode(results) else None,
        intent_param_mode=goal if _known(goal, INTENT_PARAM_MODES) else None,
        report_mode=reports if _known(reports, REPORT_MODES) else None,
        overlay=overlay,
    )
    return Audience(settings, labels, clients, threshold)


def parse_document(body: bytes, candidate: str | None = None) -> SettingsDocument | None:
    """The document in ``body``, or ``None`` when it is not a valid one (SPEC §2.6).
    With ``candidate``, valid only when it names that change."""
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
    if candidate is not None and data.get("candidate") != candidate:
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
        readable = _audience(version, audience_id, match, capture, overlay, candidate)
        if readable is not None:
            parsed.append(readable)
    return SettingsDocument(
        version,
        tuple(parsed),
        unmatched=CallSettings(CaptureObserved(settings_version=version, candidate=candidate)),
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
        self._make_pool = make_pool or functools.partial(
            # The poll and the change reads can be in flight at once.
            _pool_through,
            _proxy_for(origin),
            maxsize=CANDIDATE_READS_AT_ONCE + 1,
        )
        self._pool: Any = None
        self._pool_lock = threading.Lock()
        self._etag: str | None = None
        self._document: SettingsDocument | None = None
        self._stop = threading.Event()
        self._warned = False

    @property
    def document(self) -> SettingsDocument | None:
        return self._document

    def start(self) -> None:
        _LIVE_SENDERS.add(self)
        threading.Thread(target=self._run, name="baton-settings", daemon=True).start()

    def reset_in_child(self) -> None:
        """A forked child has no reading thread, and the pool's sockets and
        lock are the parent's."""
        if not self._stop.is_set():
            self._stop = threading.Event()
            self._pool = None
            self._pool_lock = threading.Lock()
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

    def _get(self, url: str, headers: dict[str, str], timeout_s: float) -> Any:
        urllib3 = _require_urllib3()
        with self._pool_lock:
            if self._pool is None:
                self._pool = self._make_pool()
            pool = self._pool
        return pool.urlopen(
            "GET",
            url,
            headers=headers,
            timeout=urllib3.Timeout(total=timeout_s),
            retries=False,
            redirect=False,
        )

    def _read(self) -> None:
        headers = dict(self._headers)
        if self._etag is not None:
            headers["If-None-Match"] = self._etag
        response = self._get(self._url, headers, REQUEST_TIMEOUT_S)
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

    def fetch_candidate(self, candidate: str, timeout_s: float) -> CandidateRead:
        """One read of change ``candidate``. Never raises."""
        query = urllib.parse.urlencode({"candidate": candidate})
        try:
            response = self._get(f"{self._url}&{query}", dict(self._headers), timeout_s)
        except Exception:
            logger.debug("baton: reading change %r failed", candidate, exc_info=True)
            return CandidateRead("failed")
        if response.status == 403:
            return CandidateRead("refused")
        if response.status == 404:
            return CandidateRead("unknown")
        if response.status == 200:
            document = parse_document(response.data, candidate)
            if document is not None:
                return CandidateRead("loaded", document)
        return CandidateRead("failed")


CandidateOutcome = Literal["loaded", "refused", "unknown", "failed"]


@dataclass(frozen=True)
class CandidateRead:
    outcome: CandidateOutcome
    document: SettingsDocument | None = None


_PENDING = object()


class CaptureSettings:
    """Per-call settings from the matched audience (SPEC §2.6), and the test
    changes a test key may fetch by id: ``BATON_CANDIDATE`` for the process,
    the ``Baton-Candidate`` header for one HTTP request, which wins."""

    def __init__(
        self,
        *,
        vendor_id: str,
        fetcher: SettingsFetcher,
        candidate: str | None = None,
        start_timeout_s: float = CANDIDATE_START_TIMEOUT_S,
    ) -> None:
        self._vendor_id = vendor_id
        self._fetcher = fetcher
        self._candidate = candidate
        self._start_timeout_s = start_timeout_s
        self._process_document: SettingsDocument | None = None
        # id -> its document, ``None`` for an unknown id, ``_PENDING``, or the
        # monotonic time a failed read may be tried again.
        self._held: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()
        # A 403 is about the key, so it answers every id for this process.
        self._refused = False
        self.retry_after_s = REQUEST_TIMEOUT_S

    def start(self) -> None:
        """Reads ``BATON_CANDIDATE``'s change first, blocking up to the start
        timeout, so it is in place before the first ``initialize``."""
        if self._candidate is not None and self._process_document is None:
            read = self._fetcher.fetch_candidate(self._candidate, self._start_timeout_s)
            self._record(self._candidate, read)
            if read.document is not None:
                self._process_document = read.document
            elif read.outcome != "refused":
                logger.error(
                    "baton: %s=%s could not be loaded (%s); today's settings apply.",
                    CANDIDATE_ENV,
                    self._candidate,
                    "unknown id" if read.outcome == "unknown" else "no answer",
                )
        _LIVE_SENDERS.add(self)
        self._fetcher.start()

    def reset_in_child(self) -> None:
        """A forked child has none of the parent's reading threads, and its
        lock may have been held at the fork."""
        self._lock = threading.Lock()
        for candidate in [k for k, v in self._held.items() if v is _PENDING]:
            del self._held[candidate]

    def stop(self) -> None:
        self._fetcher.stop()

    @property
    def document(self) -> SettingsDocument | None:
        """The document for a request with no header of its own."""
        if self._process_document is not None:
            return self._process_document
        return self._fetcher.document

    def for_caller(self, caller: Caller) -> CallSettings | None:
        """``None`` while no document is held, and this config's values apply."""
        if caller.candidate is None:
            document = self.document
        else:
            document = self._header_document(caller.candidate) or self._fetcher.document
        return None if document is None else document.settings_for(caller, self._vendor_id)

    def documents(self) -> list[SettingsDocument]:
        """Every document a request could be served under now."""
        document = self.document
        today = [] if document is None else [document]
        if not self._held:
            return today
        with self._lock:
            held = [d for d in self._held.values() if isinstance(d, SettingsDocument)]
        return today + [d for d in held if d is not document]

    def _header_document(self, candidate: str) -> SettingsDocument | None:
        """The header's change if it is held. The first sight of an id starts
        its read in the background, unless too many are already running."""
        with self._lock:
            if self._refused:
                return None
            held = self._held.get(candidate)
            if candidate in self._held and not (
                isinstance(held, float) and held <= time.monotonic()
            ):
                self._held.move_to_end(candidate)
                return held if isinstance(held, SettingsDocument) else None
            if sum(v is _PENDING for v in self._held.values()) >= CANDIDATE_READS_AT_ONCE:
                return None
            self._held[candidate] = _PENDING
            self._evict()
        try:
            threading.Thread(
                target=self._read_in_background,
                args=(candidate,),
                name="baton-candidate",
                daemon=True,
            ).start()
        except Exception:
            self._record(candidate, CandidateRead("failed"))
        return None

    def _read_in_background(self, candidate: str) -> None:
        self._record(candidate, self._fetcher.fetch_candidate(candidate, REQUEST_TIMEOUT_S))

    def _record(self, candidate: str, read: CandidateRead) -> None:
        with self._lock:
            if read.outcome == "refused" and not self._refused:
                logger.warning(
                    "baton: this server's key is not a test key, so test changes are not "
                    "fetched; today's settings apply."
                )
            if read.outcome == "refused":
                self._held.pop(candidate, None)
                self._refused = True
            elif read.outcome == "failed":
                self._held[candidate] = time.monotonic() + self.retry_after_s
            else:
                self._held[candidate] = read.document
                self._evict()

    def _evict(self) -> None:
        if len(self._held) <= CANDIDATES_HELD:
            return
        # A read in flight is never dropped, so it cannot be started twice.
        for key in [k for k, v in self._held.items() if v is not _PENDING]:
            if len(self._held) <= CANDIDATES_HELD:
                break
            del self._held[key]


def overlay_changes(settings: CaptureSettings | None, piece: str) -> bool:
    """Whether some audience's overlay changes ``piece`` of Baton's text."""
    return settings is not None and any(piece in d.audience_pieces for d in settings.documents())


def may_hide(settings: CaptureSettings | None, name: str) -> bool:
    return settings is not None and any(name in d.hidden_somewhere for d in settings.documents())
