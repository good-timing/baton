"""Unit tests for ``baton.integrations.client_observed``.

The adapter-level pairs are ``tests/functional/test_client_observed_parity.py``
and each adapter's own ``test_observed_client.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.integrations.client_observed import (
    CLIENT_INFO_META_KEY,
    meta_to_dict,
    observe_client,
)
from tests._asgi import fake_http_request


class _FakeMetaModel:
    """Stands in for mcp's ``RequestParams.Meta``, whose namespaced keys are
    aliases: a dump without ``by_alias=True`` loses them."""

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

    def test_a_model_is_dumped_by_alias(self) -> None:
        model = _FakeMetaModel(
            by_alias_payload={"claudecode/toolUseId": "tu_1"},
            plain_payload={"claudecode_toolUseId": "tu_1"},
        )
        assert meta_to_dict(model) == {"claudecode/toolUseId": "tu_1"}

    def test_an_object_that_is_neither_is_none(self) -> None:
        assert meta_to_dict(object()) is None


class _Info:
    def __init__(self, name: Any = None, version: Any = None) -> None:
        self.name = name
        self.version = version


class _Params1x:
    def __init__(self, info: _Info) -> None:
        self.clientInfo = info


class _Params2x:
    def __init__(self, info: _Info) -> None:
        self.client_info = info


class _Ctx:
    def __init__(self, params: Any) -> None:
        self.session = type("S", (), {"client_params": params})()


def _handshake(name: Any = None, version: Any = None) -> _Ctx:
    return _Ctx(_Params2x(_Info(name, version)))


def _raising_ctx(exc: type[Exception]) -> Any:
    class _RaisingSessionCtx:
        @property
        def session(self) -> Any:
            raise exc("outside a live request")

    return _RaisingSessionCtx()


def _wire(observed: Any) -> Any:
    return None if observed is None else observed.model_dump(mode="json")


class TestNothingObserved:
    @pytest.mark.parametrize("meta", [None, {}, {"progressToken": 7}])
    def test_no_declaration_and_no_headers_is_none(self, meta: Any) -> None:
        assert observe_client(meta) is None

    def test_a_claudecode_key_alone_is_none(self) -> None:
        assert observe_client({"claudecode/toolUseId": "tu_1"}) is None

    def test_a_claudecode_key_never_fills_info(self) -> None:
        observed = observe_client(
            {"claudecode/toolUseId": "tu_1"}, headers={"user-agent": "agent/1.0"}
        )
        assert _wire(observed) == {"headers": {"user-agent": "agent/1.0"}}

    def test_a_handshake_that_declared_nothing_is_none(self) -> None:
        assert observe_client(None, context=_handshake()) is None
        assert observe_client(None, context=_Ctx(None)) is None

    def test_headers_with_no_registered_name_are_none(self) -> None:
        assert observe_client(None, headers={"accept": "*/*"}) is None
        assert observe_client(None, headers={}) is None


class TestInfo:
    @pytest.mark.parametrize(
        "params",
        [
            pytest.param(_Params1x(_Info("claude-ai", "1.2.3")), id="mcp-1x-clientInfo"),
            pytest.param(_Params2x(_Info("claude-ai", "1.2.3")), id="mcp-2x-client_info"),
        ],
    )
    def test_both_handshake_attribute_spellings_are_read(self, params: Any) -> None:
        observed = observe_client(None, context=_Ctx(params))
        assert _wire(observed) == {"info": {"name": "claude-ai", "version": "1.2.3"}}

    def test_the_request_declaration_is_preferred_over_the_handshake(self) -> None:
        meta = {CLIENT_INFO_META_KEY: {"name": "zed", "version": "0.9"}}
        observed = observe_client(meta, context=_handshake("gateway", "4.0"))
        assert _wire(observed) == {"info": {"name": "zed", "version": "0.9"}}

    def test_a_request_declaration_is_not_mixed_with_the_handshake(self) -> None:
        meta = {CLIENT_INFO_META_KEY: {"version": "0.9"}}
        observed = observe_client(meta, context=_handshake("gateway", "4.0"))
        assert _wire(observed) == {"info": {"version": "0.9"}}

    @pytest.mark.parametrize("declared", [{}, {"name": ""}, {"name": 5, "version": None}])
    def test_a_request_declaration_with_no_text_yields_to_the_handshake(
        self, declared: dict[str, Any]
    ) -> None:
        meta = {CLIENT_INFO_META_KEY: declared}
        observed = observe_client(meta, context=_handshake("gateway", "4.0"))
        assert _wire(observed) == {"info": {"name": "gateway", "version": "4.0"}}

    def test_a_request_declaration_may_be_a_model(self) -> None:
        observed = observe_client({CLIENT_INFO_META_KEY: _Info("zed", "0.9")})
        assert _wire(observed) == {"info": {"name": "zed", "version": "0.9"}}

    def test_a_dumped_meta_model_keeps_the_declaration(self) -> None:
        model = _FakeMetaModel(
            by_alias_payload={CLIENT_INFO_META_KEY: {"name": "zed"}}, plain_payload={}
        )
        assert _wire(observe_client(meta_to_dict(model))) == {"info": {"name": "zed"}}

    @pytest.mark.parametrize("junk", ["", None, 7])
    def test_a_field_that_is_not_a_non_empty_string_is_omitted(self, junk: Any) -> None:
        assert _wire(observe_client(None, context=_handshake("a", junk))) == {"info": {"name": "a"}}
        assert _wire(observe_client(None, context=_handshake(junk, "1"))) == {
            "info": {"version": "1"}
        }


class TestHeaders:
    def test_never_copies_a_header_that_is_not_registered(self) -> None:
        observed = observe_client(
            None,
            headers={
                "user-agent": "agent/1.0",
                "x-anthropic-client": "desktop",
                "authorization": "Bearer s3cret",
                "cookie": "sid=s3cret",
                "x-forwarded-user": "jane",
            },
        )
        assert _wire(observed) == {
            "headers": {"user-agent": "agent/1.0", "x-anthropic-client": "desktop"}
        }

    def test_mixed_case_keys_are_matched_and_lower_cased(self) -> None:
        observed = observe_client(
            None, headers={"User-Agent": "agent/1.0", "X-Anthropic-Client": "desktop"}
        )
        assert _wire(observed) == {
            "headers": {"user-agent": "agent/1.0", "x-anthropic-client": "desktop"}
        }

    def test_a_header_sent_on_several_lines_is_joined(self) -> None:
        request = fake_http_request(
            [(b"user-agent", b"agent/1.0"), (b"cookie", b"x"), (b"user-agent", b"proxy/2")]
        )
        observed = observe_client(None, headers=request.headers)
        assert _wire(observed) == {"headers": {"user-agent": "agent/1.0, proxy/2"}}

    def test_a_key_that_is_not_a_string_is_skipped(self) -> None:
        headers: Any = {7: "x", b"user-agent": "bytes-key", "user-agent": "agent/1.0"}
        assert _wire(observe_client(None, headers=headers)) == {
            "headers": {"user-agent": "agent/1.0"}
        }

    @pytest.mark.parametrize("junk", ["", None, 7])
    def test_a_value_that_is_not_a_non_empty_string_is_omitted(self, junk: Any) -> None:
        observed = observe_client(None, headers={"user-agent": junk, "x-anthropic-client": "d"})
        assert _wire(observed) == {"headers": {"x-anthropic-client": "d"}}


class TestEveryValueIsUntrustedText:
    def test_the_scrubber_sees_each_value_and_nothing_else(self) -> None:
        seen: list[Any] = []

        def scrubber(value: Any) -> Any:
            seen.append(value)
            return f"<{value}>"

        observed = observe_client(
            {CLIENT_INFO_META_KEY: {"name": "zed", "version": "0.9"}, "claudecode/toolUseId": "t"},
            headers={"user-agent": "agent/1.0", "authorization": "Bearer s3cret"},
            scrubber=scrubber,
        )
        assert _wire(observed) == {
            "info": {"name": "<zed>", "version": "<0.9>"},
            "headers": {"user-agent": "<agent/1.0>"},
        }
        assert sorted(seen) == ["0.9", "agent/1.0", "zed"]

    @pytest.mark.parametrize(
        "scrubbed",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty"),
            pytest.param(object(), id="object"),
            pytest.param(b"bytes", id="bytes"),
        ],
    )
    def test_a_scrubber_returning_no_string_drops_the_value(self, scrubbed: Any) -> None:
        def scrubber(value: Any) -> Any:
            return scrubbed if value in {"zed", "agent/1.0"} else value

        observed = observe_client(
            {CLIENT_INFO_META_KEY: {"name": "zed", "version": "0.9"}},
            headers={"user-agent": "agent/1.0", "x-anthropic-client": "desktop"},
            scrubber=scrubber,
        )
        assert _wire(observed) == {
            "info": {"version": "0.9"},
            "headers": {"x-anthropic-client": "desktop"},
        }

    def test_a_scrubber_that_drops_everything_leaves_none(self) -> None:
        observed = observe_client(
            None,
            context=_handshake("zed", "0.9"),
            headers={"user-agent": "agent/1.0"},
            scrubber=lambda _: None,
        )
        assert observed is None

    def test_a_scrubber_raising_on_one_value_drops_that_value_alone(self) -> None:
        def scrubber(value: Any) -> Any:
            if value == "zed":
                raise RuntimeError("vendor scrubber bug")
            return value

        observed = observe_client(
            None,
            context=_handshake("zed", "0.9"),
            headers={"user-agent": "agent/1.0"},
            scrubber=scrubber,
        )
        assert _wire(observed) == {
            "info": {"version": "0.9"},
            "headers": {"user-agent": "agent/1.0"},
        }

    # Literal lengths: SPEC §11.4 states them, so a changed constant must fail.
    @pytest.mark.parametrize(("sent", "kept"), [(127, 127), (128, 128), (129, 128), (5000, 128)])
    def test_info_fields_are_cut_at_128(self, sent: int, kept: int) -> None:
        observed = observe_client(None, context=_handshake("n" * sent, "v" * sent))
        assert _wire(observed) == {"info": {"name": "n" * kept, "version": "v" * kept}}

    @pytest.mark.parametrize(("sent", "kept"), [(255, 255), (256, 256), (257, 256), (5000, 256)])
    def test_each_header_is_cut_at_256(self, sent: int, kept: int) -> None:
        observed = observe_client(
            None, headers={"user-agent": "u" * sent, "x-anthropic-client": "x" * sent}
        )
        assert _wire(observed) == {
            "headers": {"user-agent": "u" * kept, "x-anthropic-client": "x" * kept}
        }

    def test_the_cap_applies_to_what_the_scrubber_returned(self) -> None:
        observed = observe_client(None, context=_handshake("zed"), scrubber=lambda _: "r" * 5000)
        assert _wire(observed) == {"info": {"name": "r" * 128}}


class TestAFailedReadCostsItsOwnMember:
    # RuntimeError is what fastmcp's ``Context.session`` raises outside a live
    # session and ValueError what mcp's does; the rest guard against a library
    # choosing another.
    @pytest.mark.parametrize(
        "exc",
        [RuntimeError, ValueError, AttributeError, TypeError, KeyError],
        ids=lambda e: e.__name__,
    )
    def test_a_raising_session_loses_info_and_keeps_headers(self, exc: type[Exception]) -> None:
        ctx = _raising_ctx(exc)
        assert observe_client({}, context=ctx) is None
        observed = observe_client({}, context=ctx, headers={"user-agent": "agent/1.0"})
        assert _wire(observed) == {"headers": {"user-agent": "agent/1.0"}}

    def test_a_raising_session_is_not_reached_when_the_request_declared(self) -> None:
        observed = observe_client(
            {CLIENT_INFO_META_KEY: {"name": "zed"}}, context=_raising_ctx(RuntimeError)
        )
        assert _wire(observed) == {"info": {"name": "zed"}}

    def test_raising_headers_lose_headers_and_keep_info(self) -> None:
        class _RaisingHeaders(dict[str, str]):
            def items(self) -> Any:
                raise RuntimeError("request went away")

        observed = observe_client(
            None, context=_handshake("zed", "0.9"), headers=_RaisingHeaders({"user-agent": "a"})
        )
        assert _wire(observed) == {"info": {"name": "zed", "version": "0.9"}}


class TestWireForm:
    def test_unset_members_are_omitted_not_null(self) -> None:
        only_name = observe_client(None, context=_handshake("zed"))
        only_headers = observe_client(None, headers={"user-agent": "agent/1.0"})
        assert _wire(only_name) == {"info": {"name": "zed"}}
        assert _wire(only_headers) == {"headers": {"user-agent": "agent/1.0"}}

    def test_an_event_carries_the_same_omitting_form(self) -> None:
        from datetime import UTC, datetime

        from baton.events import AnnotationEvent, AnnotationPayload

        event = AnnotationEvent(
            tenant_id="t",
            vendor_id="v",
            consent_token="ct",
            session_id="s",
            sequence_number=1,
            captured_at=datetime.now(UTC),
            client_observed=observe_client(None, context=_handshake("zed")),
            payload=AnnotationPayload(intent="g"),
        )
        dumped = event.model_dump(mode="json")
        assert dumped["client_observed"] == {"info": {"name": "zed"}}
        assert dumped["agent_runtime"] == "unknown"

        # SPEC §11.4 lets the key be absent or null, never an empty object.
        unobserved = event.model_copy(update={"client_observed": observe_client(None)})
        assert '"client_observed":null' in unobserved.model_dump_json()
