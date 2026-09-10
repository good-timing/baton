"""The ``tool_call_end`` result body must be READABLE on every supported fastmcp.

N10: on fastmcp 2.14.7 — the floor, and the version a pinned vendor actually
runs — ``ToolResult`` is a plain object, so ``_result_to_jsonable``'s
``hasattr(result, "model_dump")`` branch misses and the payload carries
``"<fastmcp.tools.tool.ToolResult object at 0x…>"``: a memory address that
changes every run, with the tool's actual output nowhere on the wire. On 3.x
and 4.x the same class is a pydantic model and a real dict goes out.

Every other result assertion in this suite is ``result is not None`` (see
``test_middleware.py::test_result_in_end_event``), which a repr satisfies —
which is why this shipped through a release and a version matrix unseen.

The assertions here are POSITIVE — the value the tool returned has to be
findable in the payload — because a negative check ("no ``object at 0x``")
passes vacuously the moment the extractor looks in the wrong place.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

try:  # fastmcp 3.x/4.x
    from fastmcp.tools import ToolResult
except ImportError:  # fastmcp 2.14.7 — the floor exports it only from the leaf module
    from fastmcp.tools.tool import ToolResult

from baton.integrations.standalone.middleware import BatonMiddleware
from baton.scrub import Scrubber
from baton.sinks import HttpSink, Sink

# A string no version label, class path or memory address can produce.
MARKER = "n10-readable-marker"


@pytest.fixture
async def captured() -> list[dict[str, Any]]:
    return []


@pytest.fixture
async def sink(
    httpserver: HTTPServer,
    captured: list[dict[str, Any]],
) -> Sink:
    def handler(request: Any) -> Response:
        captured.append(request.get_json())
        return Response("", status=201)

    httpserver.expect_request("/v0/events", method="POST").respond_with_handler(handler)
    s = HttpSink(url=httpserver.url_for(""), api_key="k")
    yield s
    await s.aclose()


def _build_mcp(sink: Sink, **mw_kwargs: Any) -> FastMCP:
    mcp = FastMCP("test-vendor")
    mcp.add_middleware(
        BatonMiddleware(
            tenant_id="ten_test",
            vendor_id="ten_test",
            consent_token="ct_test",
            sink=sink,
            **mw_kwargs,
        )
    )
    return mcp


async def _call_and_capture(
    sink: Sink,
    captured: list[dict[str, Any]],
    fn: Any,
    args: dict[str, Any],
    **mw_kwargs: Any,
) -> dict[str, Any]:
    """Drive one real tool call and return its ``tool_call_end`` payload."""
    mcp = _build_mcp(sink, **mw_kwargs)
    mcp.tool()(fn)

    async with Client(mcp) as client:
        await client.call_tool(fn.__name__, args)

    await sink.flush()
    return next(ev for ev in captured if ev["event_type"] == "tool_call_end")["payload"]


def _content_texts(result: Any) -> list[str]:
    """Text blocks of a serialised ToolResult, or [] if it is not that shape."""
    if not isinstance(result, dict):
        return []
    blocks = result.get("content")
    if not isinstance(blocks, list):
        return []
    return [b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)]


class TestResultBodyIsReadable:
    async def test_string_result_reaches_the_wire(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        def report(tag: str) -> str:
            return f"{MARKER}:{tag}"

        payload = await _call_and_capture(sink, captured, report, {"tag": "ok"})
        result = payload["result"]

        assert isinstance(result, dict), (
            f"result body is not a structured object: {result!r} — a vendor on this "
            "fastmcp ships an unreadable result (N10)"
        )
        texts = _content_texts(result)
        assert texts, f"no readable content blocks were checked in {result!r}"
        assert any(f"{MARKER}:ok" in t for t in texts), (
            f"the tool's own output is not in the payload: {texts!r}"
        )

    async def test_structured_result_reaches_the_wire(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        def measure(n: int) -> dict[str, Any]:
            return {"marker": MARKER, "doubled": n * 2}

        payload = await _call_and_capture(sink, captured, measure, {"n": 21})
        result = payload["result"]

        assert isinstance(result, dict), f"result body is not a structured object: {result!r}"
        structured = result.get("structured_content")
        assert isinstance(structured, dict), (
            f"structured_content did not survive serialisation: {result!r}"
        )
        assert structured.get("doubled") == 42, f"wrong structured body: {structured!r}"
        assert structured.get("marker") == MARKER

    async def test_no_object_repr_anywhere_in_the_payload(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        """The failure mode by name — a memory address is not data.

        Secondary to the positive assertions above, and deliberately so: this
        one alone would pass on an empty payload.
        """

        def echo(text: str) -> str:
            return text

        payload = await _call_and_capture(sink, captured, echo, {"text": MARKER})
        blob = json.dumps(payload["result"])

        assert MARKER in blob, f"nothing was checked — the marker is absent: {blob!r}"
        assert "object at 0x" not in blob, f"result body is an object repr: {blob!r}"


class TestResultBodyIsScrubbed:
    """The serialised body must be WALKABLE by the scrubber, not merely present.

    ``scrub.py`` recurses through ``dict`` / ``list`` / ``str`` and returns
    anything else untouched, so a result whose content blocks are still
    library objects sails past an active ``Scrubber()`` — and pydantic's
    envelope dump then puts that unscrubbed text on the wire anyway. The
    emitted body would look fine and be unredacted, which is worse than the
    repr it replaced. ``install_baton`` wires a real ``Scrubber()`` by
    default (``standalone/install.py:134``), so this is the shipped path, not
    an opt-in one.
    """

    async def test_pii_in_the_result_is_redacted_on_every_version(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        def lookup(who: str) -> str:
            return f"contact {who} at alice@example.com"

        payload = await _call_and_capture(
            sink, captured, lookup, {"who": "alice"}, scrubber=Scrubber()
        )
        blob = json.dumps(payload["result"])

        assert "contact alice at" in blob, f"nothing was checked — no result text in {blob!r}"
        assert "alice@example.com" not in blob, (
            f"the scrubber could not reach the result text: {blob!r}"
        )
        assert "[REDACTED:email]" in blob, f"expected a redaction marker in {blob!r}"


class _Contact(BaseModel):
    """A vendor model nested inside ``meta`` — the scrubber cannot walk one."""

    email: str


class TestNestedModelsAreScrubbable:
    """Deep, not shallow: ``meta`` is a free-form dict a vendor fills.

    Copying it by reference (the first cut of this fix) put
    ``alice@example.com`` on the wire in the clear on 2.14.7 while 3.4.2
    redacted it — because ``model_dump(mode="json")`` converts the whole tree
    and a shallow copy converts nothing. Found in review, not by this suite,
    which is why the case is pinned here rather than trusted to the docstring.
    """

    async def test_pii_nested_in_result_meta_is_redacted(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        def leaky() -> ToolResult:
            return ToolResult(
                content="ok",
                meta={"owner": _Contact(email="alice@example.com")},
            )

        payload = await _call_and_capture(sink, captured, leaky, {}, scrubber=Scrubber())
        blob = json.dumps(payload["result"])

        assert "owner" in blob, f"nothing was checked — meta is absent from {blob!r}"
        assert "alice@example.com" not in blob, (
            f"a model nested in meta carried PII past the scrubber: {blob!r}"
        )
        assert "[REDACTED:field-email]" in blob, f"expected a redaction marker in {blob!r}"


class TestIsErrorIsNotFabricated:
    """The ``is_error`` conditional, pinned in BOTH directions.

    No matrix leg reaches it: 2.14.7 has no such attribute, and 3.x/4.x take
    the ``model_dump`` branch above. So it is exercised here directly, against
    stand-ins rather than a real ``ToolResult`` — deliberately, because the
    point is the branch's own shape, not a claim about any library version.
    The asymmetry it preserves (3.x/4.x emit ``is_error``, the floor does not)
    is inherited from ``model_dump``, not introduced here: a consumer must
    ``.get`` it rather than index it.
    """

    def test_absent_attribute_emits_no_key(self) -> None:
        class Floorish:
            content: ClassVar[list[Any]] = []
            structured_content = None
            meta = None

        body = BatonMiddleware._result_to_jsonable(Floorish())
        assert isinstance(body, dict)
        assert "is_error" not in body

    def test_present_attribute_is_carried(self) -> None:
        class Erroring:
            content: ClassVar[list[Any]] = []
            structured_content = None
            meta = None
            is_error = True

        body = BatonMiddleware._result_to_jsonable(Erroring())
        assert isinstance(body, dict)
        assert body["is_error"] is True


class TestContentBlockKeysAreUnaliased:
    """The floor's rebuilt body must use the SAME key names as ``model_dump``.

    ``to_jsonable_python`` defaults to ``by_alias=True`` where
    ``model_dump(mode="json")`` defaults to ``False``, so the rebuild silently
    renamed a content block's ``meta`` to ``_meta`` on 2.x only — caught by
    diffing real payloads across versions, invisible to every assertion here,
    which read the text. A consumer indexing ``block["meta"]`` would have seen
    it and nothing else would.
    """

    async def test_block_uses_meta_not_the_wire_alias(
        self, sink: Sink, captured: list[dict[str, Any]]
    ) -> None:
        def echo(text: str) -> str:
            return text

        payload = await _call_and_capture(sink, captured, echo, {"text": MARKER})
        blocks = payload["result"]["content"]
        assert blocks, f"nothing was checked — no content blocks in {payload['result']!r}"
        for block in blocks:
            assert "meta" in block, f"un-aliased key missing from {block!r}"
            assert "_meta" not in block, (
                f"content block carries the WIRE ALIAS, diverging from the "
                f"model_dump path 3.x/4.x take: {block!r}"
            )


class TestSerialisationIsFailOpen:
    """A value nothing can serialise must degrade, never raise.

    ``_result_to_jsonable`` runs while BUILDING the event, so it is outside
    ``safe_write``'s guard: a raise here propagates into the middleware and can
    fail the vendor's tool call, which is the failure `8b4356d` exists to
    prevent. ``TypeAdapter(Any).dump_python`` raises on an unknown type;
    ``to_jsonable_python(serialize_unknown=True)`` degrades that ONE value to
    its repr and serialises the rest, which is why it is the one used.
    """

    def test_unserialisable_value_degrades_instead_of_raising(self) -> None:
        class Unserialisable:
            pass

        class FakeResult:
            content: ClassVar[list[Any]] = []
            structured_content = None
            meta: ClassVar[dict[str, Any]] = {"odd": Unserialisable()}

        body = BatonMiddleware._result_to_jsonable(FakeResult())
        assert isinstance(body, dict)
        assert "Unserialisable" in str(body["meta"]["odd"]), body
