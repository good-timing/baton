"""The library API's session block and sequence numbers (SPEC §5.6)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from baton._uuid import uuid7
from baton.events import ClientObserved, PrincipalWire
from baton.integrations._session import SessionOrigin, clean_session_id
from baton.integrations.client_observed import (
    clean_runtime_meta,
    meta_to_dict,
    observe_client,
)
from baton.integrations.identity_adapter import wire_vendor_principal

logger = logging.getLogger(__name__)

TRANSPORT_LIBRARY = "library"
ORIGIN_OBSERVED: SessionOrigin = "observed"
ORIGIN_MINTED: SessionOrigin = "minted"
TURN_ID_META_KEY = "baton/turn_id"


@dataclass(frozen=True)
class LibrarySession:
    """What ``client.session(...)`` gives every call inside it."""

    session_id: str
    session_id_origin: SessionOrigin
    principal: PrincipalWire | None = None
    client_observed: ClientObserved | None = None
    runtime_meta: dict[str, Any] | None = None

    def envelope(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_id_origin": self.session_id_origin,
            "transport_observed": TRANSPORT_LIBRARY,
            "principal": self.principal,
            "client_observed": self.client_observed,
            "runtime_meta": self.runtime_meta,
        }


# The owning client is stored with the block so one client's block never
# reaches another client's events.
_current: ContextVar[tuple[object, LibrarySession] | None] = ContextVar(
    "baton_library_session", default=None
)


def _vendor_id(name: str, value: object) -> str | None:
    cleaned = clean_session_id(value)
    if cleaned is None and value is not None:
        logger.warning("baton: %s=%r is not a non-blank string — ignoring it.", name, value)
    return cleaned


def _minted() -> LibrarySession:
    return LibrarySession(str(uuid7()), ORIGIN_MINTED)


def _request_meta(meta: object) -> dict[str, Any] | None:
    try:
        as_dict = meta_to_dict(meta)
    except Exception:
        as_dict = None
    if as_dict is None and meta is not None:
        logger.warning("baton: meta=%r is not a mapping — ignoring it.", type(meta).__name__)
    return as_dict


def _runtime_meta(
    meta: dict[str, Any] | None, turn_id: str | None, scrubber: Callable[[Any], Any]
) -> dict[str, Any] | None:
    # A client could send its own ``baton/turn_id``; on a library event that
    # key is the vendor's (SPEC §11.4.1), so the client's copy never rides.
    client_keys = {k: v for k, v in (meta or {}).items() if k != TURN_ID_META_KEY}
    cleaned = clean_runtime_meta(scrubber, client_keys, logger) if client_keys else None
    out = dict(cleaned) if isinstance(cleaned, dict) else {}
    if turn_id is not None:
        out[TURN_ID_META_KEY] = turn_id
    return out or None


def new_session(
    *,
    session_id: object = None,
    principal: object = None,
    client_info: object = None,
    headers: Mapping[str, Any] | None = None,
    turn_id: object = None,
    meta: object = None,
    scrubber: Callable[[Any], Any],
    disabled: bool,
) -> LibrarySession:
    """A ``session()`` block's values. Never raises: a value that is not
    usable is logged and dropped."""
    vendor_id = _vendor_id("session_id", session_id)
    base = LibrarySession(vendor_id, ORIGIN_OBSERVED) if vendor_id is not None else _minted()
    if disabled:
        # The kill switch stops the work too: no scrubber runs on a value that
        # will never be sent.
        return base
    request_meta = _request_meta(meta)
    return LibrarySession(
        base.session_id,
        base.session_id_origin,
        principal=wire_vendor_principal(principal, logger=logger),
        client_observed=(
            observe_client(
                request_meta, client_info=client_info, headers=headers, scrubber=scrubber
            )
            if request_meta or client_info is not None or headers is not None
            else None
        ),
        runtime_meta=_runtime_meta(request_meta, _vendor_id("turn_id", turn_id), scrubber),
    )


def session_for_call(owner: object) -> LibrarySession:
    """``owner``'s open block, or a minted session for this call alone."""
    entry = _current.get()
    if entry is not None and entry[0] is owner:
        return entry[1]
    return _minted()


def enter_block(owner: object, session: LibrarySession) -> Any:
    return _current.set((owner, session))


def exit_block(token: Any) -> None:
    try:
        _current.reset(token)
    except ValueError:
        # The block closed in another context than it opened in, such as a
        # streamed response finished by another task. That context never saw
        # the block, so there is nothing to undo, and the vendor's code must
        # not fail over it.
        logger.debug("baton: session block closed in another context", exc_info=True)
