"""The envelope's ``client_observed`` (SPEC §11.4).

What the client said about itself, sent uninterpreted. The consumer names the
client; this SDK does not, and its adapters leave ``agent_runtime`` at
``"unknown"``.

Lives beside the adapters, not under one, because both call it and the
standalone adapter may not import the official one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from baton.events import ClientInfoObserved, ClientObserved
from baton.scrub import scrub_or_none

logger = logging.getLogger(__name__)

#: The per-request carrier for the client's declaration, reserved by MCP
#: 2026-07-28.
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"

# The only headers ever copied. A request also carries credentials, so this is
# a list in the SDK and not a config option: ``test_client_observed.py``,
# ``test_never_copies_a_header_that_is_not_registered``.
OBSERVED_HEADERS = ("user-agent", "x-anthropic-client")

CLIENT_INFO_MAX_LEN = 128
HEADER_VALUE_MAX_LEN = 256


def meta_to_dict(meta: Any) -> dict[str, Any] | None:
    """Normalize an MCP ``_meta`` value to a plain dict.

    Accepts a dict, an MCP ``RequestParams.Meta`` model, or ``None``. Uses
    ``by_alias=True`` so namespaced keys like ``claudecode/toolUseId`` survive
    the dump (they are model extras whose JSON form is the alias).
    """
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta
    if hasattr(meta, "model_dump"):
        return meta.model_dump(by_alias=True)  # type: ignore[no-any-return]
    return None


def _clean(value: Any, scrubber: Callable[[Any], Any] | None, max_len: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if scrubber is not None:
        value = scrub_or_none(scrubber, value, "client_observed", logger)
        if not isinstance(value, str) or not value:
            return None
    return value[:max_len]


def _field(info: Any, name: str) -> Any:
    # An adapter may hand over a model where a dict is expected.
    return info.get(name) if isinstance(info, dict) else getattr(info, name, None)


def _usable(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _declared(info: Any) -> tuple[Any, Any] | None:
    """The pair one source declared, or ``None`` when it holds no text at all,
    so a malformed declaration on the request does not hide the handshake's."""
    if info is None:
        return None
    pair = (_field(info, "name"), _field(info, "version"))
    return pair if _usable(pair[0]) or _usable(pair[1]) else None


def _declared_on_request(meta: Mapping[str, Any] | None) -> tuple[Any, Any] | None:
    return _declared(meta.get(CLIENT_INFO_META_KEY)) if meta else None


def _declared_on_connection(context: Any) -> tuple[Any, Any] | None:
    params = getattr(getattr(context, "session", None), "client_params", None)
    # mcp 1.x names the attribute ``clientInfo``; 2.x renamed it ``client_info``.
    return _declared(getattr(params, "client_info", None) or getattr(params, "clientInfo", None))


def _observed_info(
    meta: Mapping[str, Any] | None, context: Any, scrubber: Callable[[Any], Any] | None
) -> ClientInfoObserved | None:
    # Both fields come from one source, so the pair is one the client sent.
    declared = _declared_on_request(meta) or _declared_on_connection(context)
    if declared is None:
        return None
    name = _clean(declared[0], scrubber, CLIENT_INFO_MAX_LEN)
    version = _clean(declared[1], scrubber, CLIENT_INFO_MAX_LEN)
    if name is None and version is None:
        return None
    return ClientInfoObserved(name=name, version=version)


def _observed_headers(
    headers: Mapping[str, Any] | None, scrubber: Callable[[Any], Any] | None
) -> dict[str, str] | None:
    if not headers:
        return None
    # The two adapters hand over differently-cased keys, and a multi-line
    # header arrives as one item per line where the adapter can see each line.
    lines: dict[str, list[str]] = {}
    for key, value in headers.items():
        if isinstance(value, str) and (name := str(key).lower()) in OBSERVED_HEADERS:
            lines.setdefault(name, []).append(value)
    observed = {
        name: value
        for name in OBSERVED_HEADERS
        if (value := _clean(", ".join(lines.get(name, ())), scrubber, HEADER_VALUE_MAX_LEN))
        is not None
    }
    return observed or None


def _or_none(read: Callable[[], Any]) -> Any:
    # These read two third-party libraries' objects, which raise what they like
    # outside a live request. One failing read costs its own member, never the
    # other or the vendor's call.
    try:
        return read()
    except Exception:
        logger.debug("client_observed: a read raised", exc_info=True)
        return None


def observe_client(
    meta: Mapping[str, Any] | None,
    *,
    context: Any = None,
    headers: Mapping[str, Any] | None = None,
    scrubber: Callable[[Any], Any] | None = None,
) -> ClientObserved | None:
    """What the client declared and the registered headers it sent, or ``None``.

    ``meta`` is the request's ``_meta`` as :func:`meta_to_dict` returns it,
    ``context`` the adapter's MCP context, read only for the cached handshake.
    """
    info = _or_none(lambda: _observed_info(meta, context, scrubber))
    observed_headers = _or_none(lambda: _observed_headers(headers, scrubber))
    if info is None and observed_headers is None:
        return None
    return ClientObserved(info=info, headers=observed_headers)
