"""Unit tests for ``baton.integrations.runtime_adapter``.

The module had NO tests of its own before this file — not a gap in coverage
of an edge case, but zero. Its two functions were exercised only incidentally,
through one adapter's middleware suite, which is part of why the other adapter
never calling them went unnoticed through a rename and a release. The
end-to-end pair is ``tests/functional/test_agent_runtime_parity.py``; this
file pins the function's own contract, including the shapes an adapter can
hand it that are not dicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.integrations.runtime_adapter import (
    AGENT_RUNTIME_MAX_LEN,
    AGENT_RUNTIME_META_KEY,
    detect_agent_runtime,
    meta_to_dict,
)


class _FakeMetaModel:
    """Stands in for mcp's ``RequestParams.Meta``: a pydantic-ish object whose
    namespaced keys are ALIASES, so a dump without ``by_alias=True`` loses
    them. That is the whole reason ``meta_to_dict`` exists."""

    def __init__(self, by_alias_payload: dict[str, Any], plain_payload: dict[str, Any]) -> None:
        self._by_alias = by_alias_payload
        self._plain = plain_payload

    def model_dump(self, by_alias: bool = False) -> dict[str, Any]:
        return self._by_alias if by_alias else self._plain


class TestMetaToDict:
    def test_none_stays_none(self) -> None:
        assert meta_to_dict(None) is None

    def test_a_dict_passes_through_unchanged(self) -> None:
        meta = {"claudecode/toolUseId": "tu_1"}
        assert meta_to_dict(meta) is meta

    def test_a_model_is_dumped_BY_ALIAS(self) -> None:
        """The namespaced key only survives the alias dump.

        Asserted by giving the fake two different payloads rather than by
        trusting the call: a test that dumped either way would pass on a
        ``model_dump()`` with no ``by_alias``, and the namespaced keys every
        heuristic below keys on would be silently gone at runtime.
        """
        model = _FakeMetaModel(
            by_alias_payload={"claudecode/toolUseId": "tu_1"},
            plain_payload={"claudecode_toolUseId": "tu_1"},
        )
        assert meta_to_dict(model) == {"claudecode/toolUseId": "tu_1"}

    def test_an_object_that_is_neither_is_none(self) -> None:
        assert meta_to_dict(object()) is None


class TestDetectAgentRuntime:
    def test_no_meta_is_no_signal(self) -> None:
        assert detect_agent_runtime(None) is None

    def test_empty_meta_is_no_signal(self) -> None:
        assert detect_agent_runtime({}) is None

    def test_claudecode_prefix_is_the_heuristic(self) -> None:
        assert detect_agent_runtime({"claudecode/toolUseId": "tu_1"}) == "claude-code"

    def test_progress_token_alone_is_no_signal(self) -> None:
        """Cursor's shape per SPEC §5.2 — and Claude Code sends it too, so
        matching on it would attribute every Cursor call to Claude Code."""
        assert detect_agent_runtime({"progressToken": 7}) is None

    def test_explicit_override_wins(self) -> None:
        assert detect_agent_runtime({AGENT_RUNTIME_META_KEY: "acme-plugin"}) == "acme-plugin"

    def test_the_override_key_is_the_reverse_dns_one(self) -> None:
        """Pins the literal string, not just the constant.

        Both sides of an ``AGENT_RUNTIME_META_KEY == AGENT_RUNTIME_META_KEY``
        comparison move together when someone edits the constant, so the wire
        key needs pinning as a literal — it is what a vendor reading SPEC §5.2
        types into their client, and changing it is a wire change.
        """
        assert AGENT_RUNTIME_META_KEY == "io.baton/agent_runtime"

    def test_override_beats_a_matching_heuristic(self) -> None:
        assert (
            detect_agent_runtime(
                {AGENT_RUNTIME_META_KEY: "acme-plugin", "claudecode/toolUseId": "tu_1"}
            )
            == "acme-plugin"
        )

    def test_the_pre_B5_nested_form_is_not_read(self) -> None:
        """``_meta["baton"]["agent_runtime"]`` is dead — see the module's own
        note on why it is a clean break rather than an accept-both."""
        assert detect_agent_runtime({"baton": {"agent_runtime": "acme-plugin"}}) is None

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param("", id="empty-string"),
            pytest.param(None, id="null"),
            pytest.param(123, id="not-a-string"),
            pytest.param({"agent_runtime": "x"}, id="a-dict"),
        ],
    )
    def test_a_junk_override_falls_through_to_the_heuristic(self, override: Any) -> None:
        """A client sending a malformed override must not be able to force
        ``None`` where a heuristic would have answered — the override is a
        client-supplied value and the fallback has to survive it."""
        meta = {AGENT_RUNTIME_META_KEY: override, "claudecode/toolUseId": "tu_1"}
        assert detect_agent_runtime(meta) == "claude-code"

    def test_a_non_string_key_does_not_crash_the_prefix_scan(self) -> None:
        """``_meta`` arrives as JSON so keys are strings, but it is also handed
        to us as a plain dict by callers we do not control."""
        assert detect_agent_runtime({7: "x", "claudecode/toolUseId": "tu_1"}) == "claude-code"

    def test_it_accepts_a_model_not_only_a_dict(self) -> None:
        """The adapters pass the RAW ``_meta`` object straight in."""
        model = _FakeMetaModel(
            by_alias_payload={"claudecode/toolUseId": "tu_1"},
            plain_payload={},
        )
        assert detect_agent_runtime(model) == "claude-code"


class TestTheOverrideValueIsUntrustedInput:
    """The override is an arbitrary string from the client, copied onto every
    event of the call. The detection INPUT stays raw; the emitted VALUE does
    not."""

    def test_a_long_override_is_capped(self) -> None:
        meta = {AGENT_RUNTIME_META_KEY: "x" * 5000}
        detected = detect_agent_runtime(meta)
        assert detected is not None
        assert len(detected) == AGENT_RUNTIME_MAX_LEN

    def test_the_vendor_scrubber_is_applied_to_the_override(self) -> None:
        """A vendor whose scrubber redacts identifiers expects it to cover this
        field — otherwise a client can put an email here and ship it raw."""
        meta = {AGENT_RUNTIME_META_KEY: "user@example.com"}
        assert detect_agent_runtime(meta, lambda v: "[REDACTED]") == "[REDACTED]"

    def test_the_scrubber_is_NOT_applied_to_a_derived_value(self) -> None:
        """Scrubbing our own constant would be the opposite mistake: the
        heuristic's answer is a value we control, not client input."""
        meta = {"claudecode/toolUseId": "tu_1"}
        assert detect_agent_runtime(meta, lambda v: "[REDACTED]") == "claude-code"

    def test_the_scrubber_does_NOT_see_the_detection_input(self) -> None:
        """The whole reason detection reads the RAW meta: a scrubber that
        touches meta KEYS must not be able to switch detection off."""
        seen: list[Any] = []

        def _scrubber(value: Any) -> Any:
            seen.append(value)
            return value

        assert detect_agent_runtime({"claudecode/toolUseId": "tu_1"}, _scrubber) == "claude-code"
        assert seen == [], f"the scrubber was handed the detection input: {seen}"

    def test_an_override_scrubbed_to_empty_falls_through(self) -> None:
        """A scrubber returning "" must not make the reported runtime empty —
        it falls through to the heuristic, then to the caller's default."""
        meta = {AGENT_RUNTIME_META_KEY: "user@example.com", "claudecode/toolUseId": "tu_1"}
        assert detect_agent_runtime(meta, lambda v: "") == "claude-code"
