"""``round_meta_coordinates`` (``baton/_meta_coords.py``): the rule on its own.

Where it runs (every adapter site that emits ``runtime_meta``, after detection
and before the vendor's scrubber) is pinned by the adapter tests:
``tests/integrations/official/test_agent_runtime.py`` and
``tests/integrations/standalone/test_meta_coords.py``.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from baton._meta_coords import round_meta_coordinates
from baton.scrub import DEPTH_LIMIT, Scrubber
from tests._chatgpt_meta import CHATGPT_IPHONE_META, CHATGPT_MAC_META


@pytest.mark.parametrize(
    ("sample", "before"),
    [
        pytest.param(CHATGPT_MAC_META, ("37.79535", "-122.39366"), id="chatgpt-mac"),
        pytest.param(
            CHATGPT_IPHONE_META,
            ("37.79535123456789", "-122.39366123456789"),
            id="chatgpt-iphone",
        ),
    ],
)
def test_chatgpt_meta_rounds_the_coordinates_and_nothing_else(
    sample: dict[str, Any], before: tuple[str, str]
) -> None:
    """Whole-dict equality is the "nothing else" half: city, region, country,
    timezone, the user agent and every id must come back unchanged."""
    location = sample["openai/userLocation"]
    assert (location["latitude"], location["longitude"]) == before
    expected = copy.deepcopy(sample)
    expected["openai/userLocation"].update(latitude="37.8", longitude="-122.4")
    assert round_meta_coordinates(sample) == expected


def test_the_input_is_not_modified() -> None:
    """The adapters read the raw meta for detection and the session ladder;
    rounding must not reach back into it."""
    sample = copy.deepcopy(CHATGPT_IPHONE_META)
    round_meta_coordinates(sample)
    assert sample == CHATGPT_IPHONE_META


def test_float_coordinate_rounds_to_a_float() -> None:
    out = round_meta_coordinates({"latitude": 37.79535, "longitude": -122.39366})
    assert out == {"latitude": 37.8, "longitude": -122.4}
    assert type(out["latitude"]) is float
    assert type(out["longitude"]) is float


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(37, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="none"),
        pytest.param("unknown", id="non-numeric-string"),
        # float() parses these; none is plain decimal notation.
        pytest.param("nan", id="nan-string"),
        pytest.param("inf", id="inf-string"),
        pytest.param("1e2", id="exponent-string"),
        pytest.param("3_7", id="underscore-string"),
    ],
)
def test_non_decimal_values_are_left_alone(value: Any) -> None:
    """``type(...) is`` as well as ``==``: ``37 == 37.0`` and ``True == 1``,
    so equality alone would pass a build that turned an int into a float."""
    out = round_meta_coordinates({"latitude": value})
    assert out == {"latitude": value}
    assert type(out["latitude"]) is type(value)


def test_key_match_is_case_insensitive() -> None:
    out = round_meta_coordinates({"Latitude": "37.79535", "LONGITUDE": "-122.39366"})
    assert out == {"Latitude": "37.8", "LONGITUDE": "-122.4"}


def test_lat_and_lng_are_not_coordinate_keys() -> None:
    """Exact match only, like the Scrubber's field names."""
    raw = {"lat": "37.79535", "lng": "-122.39366", "lon": -122.39366}
    assert round_meta_coordinates(raw) == raw


def test_coordinates_inside_a_list_are_rounded() -> None:
    out = round_meta_coordinates({"points": [{"latitude": "37.79535"}, "37.79535"]})
    assert out == {"points": [{"latitude": "37.8"}, "37.79535"]}


def _nest(leaf: dict[str, Any], levels: int) -> dict[str, Any]:
    for _ in range(levels):
        leaf = {"next": leaf}
    return leaf


def _dig(tree: dict[str, Any], levels: int) -> dict[str, Any]:
    for _ in range(levels):
        tree = tree["next"]
    return tree


def test_rounding_reaches_the_depth_limit_and_stops_there() -> None:
    """The latitude VALUE sits one level below its dict, and DEPTH_LIMIT is
    exclusive: the deepest reachable one rounds, one level further is left
    raw, the same cut-off the Scrubber has."""
    inside = round_meta_coordinates(_nest({"latitude": "37.79535"}, DEPTH_LIMIT - 2))
    past = round_meta_coordinates(_nest({"latitude": "37.79535"}, DEPTH_LIMIT - 1))
    assert _dig(inside, DEPTH_LIMIT - 2) == {"latitude": "37.8"}
    assert _dig(past, DEPTH_LIMIT - 1) == {"latitude": "37.79535"}


def test_the_default_scrubber_does_not_round_coordinates() -> None:
    """The scope decision, at the unit level: the rounding is for ``_meta``
    only, and the Scrubber also walks tool params and results."""
    raw = {"latitude": "37.79535123456789", "longitude": -122.39366123456789}
    assert Scrubber()(raw) == raw
