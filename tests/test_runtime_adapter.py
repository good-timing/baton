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
    CLIENT_NAME_MAX_LEN,
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

    @pytest.mark.parametrize(
        "meta",
        [
            pytest.param({"io.baton/agent_runtime": "acme-plugin"}, id="reverse-dns-form"),
            pytest.param({"baton": {"agent_runtime": "acme-plugin"}}, id="pre-B5-nested-form"),
        ],
    )
    def test_no_client_override_is_honoured_in_any_form(self, meta: dict[str, Any]) -> None:
        """**The override was REMOVED 2026-09-09** — both spellings are inert.

        This is the point of the removal, so it is pinned rather than left to
        absence: a client asserting its own runtime is now ignored, and
        detection answers only from signals the SDK derives itself. The nested
        form died at B5 and the reverse-DNS form died here; a test that only
        covered one of them would let the other be quietly restored.
        """
        assert detect_agent_runtime(meta) is None

    def test_an_override_cannot_suppress_the_heuristic(self) -> None:
        """The removal must not have left a half-read key that can still lose
        us a detection — the heuristic answers regardless of what else is in
        ``_meta``."""
        meta = {"io.baton/agent_runtime": "acme-plugin", "claudecode/toolUseId": "tu_1"}
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


class TestEverythingReturnedIsAValueWeControl:
    """No tier reads client text any more, so nothing here is scrubbed or
    capped. Pinned because that invariant is what licenses the absence of a
    cap — the next tier to read a client-supplied value (``clientInfo.name``)
    must bring its own."""

    def test_the_only_answer_is_the_sdks_own_constant(self) -> None:
        meta = {"claudecode/toolUseId": "tu_1", "io.baton/agent_runtime": "x" * 5000}
        assert detect_agent_runtime(meta) == "claude-code"

    def test_no_client_string_can_reach_the_return_value(self) -> None:
        """Any answer must be one of the SDK's own literals, whatever the
        client sent."""
        meta = {"claudecode/toolUseId": "tu_1", "attacker": "y" * 5000}
        assert detect_agent_runtime(meta) in {None, "claude-code"}


class _Info:
    def __init__(self, name: Any) -> None:
        self.name = name


class _Params1x:
    """mcp 1.x spells the attribute ``clientInfo``."""

    def __init__(self, name: Any) -> None:
        self.clientInfo = _Info(name)  # mirrors mcp 1.x exactly


class _Params2x:
    """mcp 2.x renamed it to ``client_info`` (wire aliases unchanged)."""

    def __init__(self, name: Any) -> None:
        self.client_info = _Info(name)


class _Ctx:
    def __init__(self, params: Any) -> None:
        self.session = type("S", (), {"client_params": params})()


class _RaisingCtx:
    """``ctx.session`` raises outside a live request on both mcp majors."""

    @property
    def session(self) -> Any:
        raise ValueError("Context is not available outside of a request")


class TestTheDeclaredTiers:
    """``clientInfo`` — what the client called itself, which is the whole
    point of B1-R: identity that does not depend on a vendor's name appearing
    in a key prefix."""

    @pytest.mark.parametrize(
        ("params", "id_"),
        [
            pytest.param(_Params1x("claude-ai"), "mcp-1.x", id="mcp-1x-clientInfo"),
            pytest.param(_Params2x("claude-ai"), "mcp-2.x", id="mcp-2x-client_info"),
        ],
    )
    def test_BOTH_attribute_spellings_are_read(self, params: Any, id_: str) -> None:
        """The trap this file exists to hold shut.

        mcp 2.x renamed ``clientInfo`` to ``client_info``. Reading one spelling
        returns ``None`` on the other major — which is indistinguishable from
        "this client is anonymous" and would report ``unknown`` across an
        entire supported version band. Measured on fastmcp 4, where asking for
        the 1.x name on a 2.x object looked exactly like no data.
        """
        assert detect_agent_runtime(None, context=_Ctx(params)) == "claude-ai"

    def test_a_context_outside_a_live_request_is_not_an_error(self) -> None:
        """``getattr(ctx, "session", None)`` does NOT save you here: its
        default swallows AttributeError only, and this raises ValueError."""
        assert detect_agent_runtime({"claudecode/toolUseId": "tu_1"}, context=_RaisingCtx()) == (
            "claude-code"
        )
        assert detect_agent_runtime(None, context=_RaisingCtx()) is None

    def test_the_per_request_key_outranks_the_connection(self) -> None:
        """New-spec clients declare on every request; that is fresher than a
        handshake cached at connect time."""
        meta = {"io.modelcontextprotocol/clientInfo": {"name": "zed"}}
        assert detect_agent_runtime(meta, context=_Ctx(_Params2x("gateway"))) == "zed"

    def test_a_declaration_outranks_the_prefix_heuristic(self) -> None:
        """Declared beats inferred, and an earlier draft had this backwards.

        That draft argued ``_meta`` survives a proxy hop while ``clientInfo``
        does not, so the prefix would name the agent and the declaration the
        middlebox. ``baton-proxy`` forwards ``initialize`` UNCHANGED, so the
        server behind it sees the agent's own ``clientInfo`` — and a proxy
        forwards ``_meta`` too, which makes ``claudecode/*`` evidence about
        where the METADATA came from, not about who is calling.
        """
        meta = {"claudecode/toolUseId": "tu_1"}
        got = detect_agent_runtime(meta, context=_Ctx(_Params2x("some-other-client")))
        assert got == "some-other-client"

    def test_the_heuristic_still_answers_when_nobody_declared(self) -> None:
        """Demoted, not dropped — it is proven coverage for the one client it
        knows, and discarding proven coverage needs evidence nobody relies on
        it."""
        assert detect_agent_runtime({"claudecode/toolUseId": "tu_1"}) == "claude-code"

    def test_the_connection_answers_when_the_call_says_nothing(self) -> None:
        """Claude Desktop's shape: no ``_meta`` at all. Unattributable before
        this tier existed, on both adapters."""
        assert detect_agent_runtime(None, context=_Ctx(_Params2x("claude-ai"))) == "claude-ai"


class TestDeclaredNamesAreUntrustedInput:
    """A client picks its own ``clientInfo``, so both declared tiers carry
    arbitrary client text onto every event of the call. The heuristic's answer
    is a constant we own and stays untouched."""

    def test_a_long_declared_name_is_capped(self) -> None:
        got = detect_agent_runtime(None, context=_Ctx(_Params2x("x" * 5000)))
        assert got is not None
        assert len(got) == CLIENT_NAME_MAX_LEN

    def test_the_vendor_scrubber_is_applied_to_a_declared_name(self) -> None:
        got = detect_agent_runtime(
            None, context=_Ctx(_Params2x("user@example.com")), scrubber=lambda v: "[REDACTED]"
        )
        assert got == "[REDACTED]"

    def test_the_scrubber_is_NOT_applied_to_the_heuristics_own_constant(self) -> None:
        """Mangling ``claude-code`` would be the opposite mistake — it is a
        value this module derived, not one a client sent."""
        got = detect_agent_runtime(
            {"claudecode/toolUseId": "tu_1"}, scrubber=lambda v: "[REDACTED]"
        )
        assert got == "claude-code"

    def test_the_scrubber_never_sees_the_detection_INPUT(self) -> None:
        """Detection reads the RAW meta: a scrubber that touches meta keys must
        not be able to switch detection off."""
        seen: list[Any] = []

        def _scrubber(value: Any) -> Any:
            seen.append(value)
            return value

        detect_agent_runtime({"claudecode/toolUseId": "tu_1"}, scrubber=_scrubber)
        assert seen == [], f"the scrubber was handed the detection input: {seen}"

    def test_a_name_scrubbed_to_empty_falls_through_to_the_next_tier(self) -> None:
        """Losing a tier to a scrubber must not lose the whole ladder."""
        meta = {"io.modelcontextprotocol/clientInfo": {"name": "a"}, "claudecode/toolUseId": "t"}
        assert detect_agent_runtime(meta, scrubber=lambda v: "") == "claude-code"

    @pytest.mark.parametrize(
        "name",
        [pytest.param("", id="empty"), pytest.param(None, id="null"), pytest.param(7, id="int")],
    )
    def test_a_junk_declared_name_falls_through(self, name: Any) -> None:
        meta = {"claudecode/toolUseId": "tu_1"}
        assert detect_agent_runtime(meta, context=_Ctx(_Params2x(name))) == "claude-code"

    @pytest.mark.parametrize(
        "scrubbed",
        [
            pytest.param(None, id="redacts-by-returning-None"),
            pytest.param(object(), id="returns-some-object"),
            pytest.param(b"bytes", id="returns-bytes"),
        ],
    )
    def test_a_scrubber_returning_a_non_string_loses_the_TIER(self, scrubbed: Any) -> None:
        """Not the whole ladder, and NOT a stringified value on the wire.

        ``str(None)`` is ``"None"`` — truthy, and it would have shipped as the
        reported runtime on every event of every call for any vendor whose
        scrubber redacts that way. An arbitrary object would have shipped its
        ``repr``. Falling through is the documented contract for a tier a
        scrubber took away.
        """
        meta = {"io.modelcontextprotocol/clientInfo": {"name": "a"}, "claudecode/toolUseId": "t"}
        assert detect_agent_runtime(meta, scrubber=lambda v: scrubbed) == "claude-code"


@pytest.mark.parametrize(
    "exc",
    [RuntimeError, ValueError, AttributeError, TypeError, KeyError],
    ids=lambda e: e.__name__,
)
def test_a_context_whose_session_raises_does_not_escape(exc: type[Exception]) -> None:
    """fastmcp's ``Context.session`` raises **RuntimeError** outside a live
    session — not ``ValueError``, which is what the official SDK raises and
    what this module's except tuple originally enumerated.

    The consequence was not a lost tier: the ``RuntimeError`` escaped
    ``detect_agent_runtime`` into ``BatonMiddleware._on_call_tool``, which does
    not guard it, and **failed the vendor's tool call** — the one thing SPEC
    §11.2 forbids capture from doing. Reproduced against fastmcp 3.4.2 through
    the public ``mcp.call_tool(...)``; the end-to-end version of this lives in
    ``tests/integrations/standalone/test_fail_open_boundary.py``, and it is the
    one that would actually have caught it, since the guard's original unit
    test fed it a context raising the exception the guard already handled.

    Parameterised over what the two libraries really raise plus a few they
    might: enumerating what a third-party property raises has been wrong once
    here already.
    """

    class _RaisingSessionCtx:
        @property
        def session(self) -> Any:
            raise exc("outside a live request")

    assert detect_agent_runtime({}, context=_RaisingSessionCtx()) is None
    # A raising context must cost ONE tier, not the whole ladder.
    assert (
        detect_agent_runtime({"claudecode/toolUseId": "t"}, context=_RaisingSessionCtx())
        == "claude-code"
    )
