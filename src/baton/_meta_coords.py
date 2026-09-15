"""Coordinate rounding for ``_meta`` before it becomes ``runtime_meta``.

ChatGPT sends ``openai/userLocation`` in every request's ``_meta`` with
``latitude`` and ``longitude`` at metre precision (measured in prod 2026-09-15:
two values resolved to a storefront and a home). ``_meta`` is what the CLIENT
reveals about the person, so every adapter rounds it here before it is emitted:
to 1 decimal, roughly 10 km, keeping city, region, country and timezone.

**``_meta`` only, never tool params or results.** A vendor's own tool that takes
or returns coordinates is the vendor's data and is captured at full precision.
That is why this is not a Scrubber rule: the Scrubber walks params and results
too.

**After runtime detection, before the vendor's scrubber, regardless of it.**
Each adapter calls this once ``detect_agent_runtime`` has read the raw meta and
before ``VendorConfig(scrubber=...)`` runs, so a vendor who supplies their own
scrubber, or opts out with ``identity_scrub``, still gets the rounding.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from baton.scrub import DEPTH_LIMIT

#: Keys whose value is rounded. Case-insensitive exact match, like the
#: Scrubber's field names, so ``lat`` / ``lng`` are not matched.
COORD_KEYS: frozenset[str] = frozenset({"latitude", "longitude"})

# "Parses as a decimal number" means plain decimal notation. ``float()`` alone
# would also take ``nan``, ``inf``, ``1e2`` and ``3_7``; those are left alone.
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def round_meta_coordinates(meta: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``meta`` with every ``latitude`` / ``longitude`` value rounded
    to 1 decimal in its own type, at any depth within ``DEPTH_LIMIT`` (the
    Scrubber's cut-off, counted the same way). ``meta`` is not modified."""
    return {k: _walk(v, depth=1, coord=_is_coord_key(k)) for k, v in meta.items()}


def _is_coord_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in COORD_KEYS


def _walk(value: Any, *, depth: int, coord: bool) -> Any:
    if depth >= DEPTH_LIMIT:
        return value
    if coord:
        rounded = _round(value)
        if rounded is not None:
            return rounded
    if isinstance(value, Mapping):
        return {k: _walk(v, depth=depth + 1, coord=_is_coord_key(k)) for k, v in value.items()}
    if isinstance(value, list):
        # A list under ``latitude`` does not inherit the key; dicts inside any
        # list are still walked for their own.
        return [_walk(item, depth=depth + 1, coord=False) for item in value]
    return value


def _round(value: Any) -> float | str | None:
    """``value`` rounded to 1 decimal in its own type, or ``None`` when the rule
    does not apply: an int, a bool, ``None``, a non-decimal string, or a
    non-finite number. A bool never reaches the float branch; it subclasses
    ``int``, not ``float``."""
    if isinstance(value, float):
        return round(value, 1) if math.isfinite(value) else None
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        number = float(value)
        return f"{number:.1f}" if math.isfinite(number) else None
    return None
