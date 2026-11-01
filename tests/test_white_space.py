"""Blank means Unicode ``White_Space`` exactly (SPEC §11.4), and a name pattern
matches the whole name.

U+001C to U+001F are what ``str.strip()`` removes and Go's ``TrimSpace``
keeps, so they are the values the SDKs could disagree on."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from baton._optout import capture_disabled
from baton._white_space import WHITE_SPACE
from baton.integrations._annotation_name import derive_annotation_tool_name
from baton.integrations._annotation_payload import is_report
from baton.integrations._config import VendorConfig, _validate_vendor_config
from baton.integrations._error_result import error_text
from baton.integrations._llm_text import _pop_text
from baton.integrations._session import clean_session_id

SEPARATORS = [pytest.param(chr(c), id=f"U+{c:04X}") for c in range(0x1C, 0x20)]


def test_the_set_is_unicode_white_space() -> None:
    assert sorted(map(ord, WHITE_SPACE)) == [
        *range(0x09, 0x0E),
        0x20,
        0x85,
        0xA0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
    ]


SITES = [
    pytest.param(lambda v: v if is_report(v) else None, False, id="annotation-report"),
    pytest.param(lambda v: _pop_text({"intent": v}, "intent"), False, id="injected-intent"),
    pytest.param(clean_session_id, True, id="session-id"),
    pytest.param(
        lambda v: error_text(SimpleNamespace(content=[SimpleNamespace(text=v)])),
        True,
        id="error-text",
    ),
]


@pytest.mark.parametrize(("site", "strips"), SITES)
@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_separator_is_not_blank_and_is_not_stripped(sep: str, site: Any, strips: bool) -> None:
    padded = f" {sep}\t"
    assert site(padded) == (sep if strips else padded)


@pytest.mark.parametrize("sep", SEPARATORS)
def test_a_zero_with_a_separator_is_not_a_zero_and_disables_capture(
    sep: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BATON_DISABLED", f"0{sep}")
    assert capture_disabled() == "BATON_DISABLED"


def test_a_vendor_id_with_a_trailing_newline_is_refused() -> None:
    config = VendorConfig(vendor_id="acme\n", vendor_display_name="V", consent_token="ct_test")
    with pytest.raises(ValueError, match="vendor_id"):
        _validate_vendor_config(config)


def test_a_tool_name_with_a_trailing_newline_is_refused() -> None:
    with pytest.raises(ValueError, match="Annotation tool name"):
        derive_annotation_tool_name("acme", override="acme_annotate\n")
