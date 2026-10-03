"""`result_capture_mode="off"` withholds result-derived data on all three surfaces.

SPEC §11.4 (the wire member), §11.2.6 (what a conforming SDK must do), §11.4.3
(which of the two failure shapes withholds what) and §7 (the scrubber MUST NOT
be invoked on a withheld result).

**Every pre-existing fixture in this repo captures a response**, so `"off"` is a
branch no corpus here has ever entered — the shape
`feedback_mode_switched_blind_spot` describes, where a green suite and a working
server are blind for the same reason. So the mode is synthesised here, and each
test is written to be able to fail:

* **The discriminator is "was the scrubber CALLED", not "is `result` absent".**
  A payload with no `result` key is produced by BOTH a correct short-circuit and
  a wrong one that scrubs the body and then drops it — and the second one has
  already run the vendor's code over the data we promised never to touch, which
  is the whole failure `feedback_a_guard_after_the_write_undoes_the_write`
  names. `_Recorder` counts invocations, so the two are distinguishable.
* **The control is the SAME fixture under `"full"`**, which must record a call.
  Without it, a scrubber that is never invoked for an unrelated reason (a broken
  fixture, a tool that never ran) would pass the `"off"` assertion vacuously.

⚠ A raising scrubber was considered as the discriminator and rejected: on the
MCP surfaces it would pass today only because the scrubber runs OUTSIDE
`safe_write` — the unguarded-scrubber defect recorded in `backlog.md` §Code. A
test whose control depends on an open bug turns green into red the day the bug
is fixed, and for the wrong reason. Counting is invariant to that fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._event_helpers import read_events

pytestmark = pytest.mark.functional

TENANT = "tenant-withhold"
SECRET = "row-42-social-security-000-00-0000"


class _Recorder:
    """A scrubber that remembers everything it was asked to scrub.

    Identity, so `"full"` behaves exactly as an un-configured SDK does and the
    control leg is not testing this class instead of the code under test.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, value: Any) -> Any:
        self.calls.append(value)
        return value

    def saw(self, needle: str) -> bool:
        """Whether the secret ever reached the scrubber, at any depth."""
        return any(needle in json.dumps(c, default=str) for c in self.calls)


def _payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in read_events(path) if e["event_type"] == event_type]


# =============================================================================
# Surface drivers — one per capture surface, same tools, same assertions
# =============================================================================


async def _run_standalone(events_path: Path, mode: str, scrubber: Any, *, fail: bool) -> None:
    from fastmcp import Client, FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.sinks import FileSink

    mcp: Any = FastMCP("withhold-standalone")

    @mcp.tool
    def fetch(row: str) -> dict[str, Any]:
        if fail:
            raise ValueError(f"no such row {row}")
        return {"record": SECRET}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="withhold",
            vendor_display_name="Withhold Vendor",
            consent_token="ct_withhold",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
            scrubber=scrubber,
            result_capture_mode=mode,
        ),
    )
    try:
        async with Client(mcp) as client:
            try:
                await client.call_tool("fetch", {"row": "42"})
            except Exception:
                # A raising tool is one of the two failure shapes under test;
                # the client re-raises it and that is not this test's subject.
                pass
    finally:
        await handle.aclose()


async def _run_official(events_path: Path, mode: str, scrubber: Any, *, fail: bool) -> None:
    from baton.integrations.official import VendorConfig, install_baton
    from baton.integrations.official._compat import MCPServerClass as FastMCP
    from baton.sinks import FileSink
    from tests._mcp_session import connected_session

    mcp = FastMCP("withhold-official")

    @mcp.tool()
    def fetch(row: str) -> dict[str, Any]:
        if fail:
            raise ValueError(f"no such row {row}")
        return {"record": SECRET}

    handle = install_baton(
        mcp,
        VendorConfig(
            vendor_id="withhold",
            vendor_display_name="Withhold Vendor",
            consent_token="ct_withhold",
            sink=FileSink(str(events_path)),
            tenant_id=TENANT,
            scrubber=scrubber,
            result_capture_mode=mode,
        ),
    )
    try:
        async with connected_session(mcp) as client:
            try:
                await client.call_tool("fetch", {"row": "42"})
            except Exception:
                pass
    finally:
        await handle.aclose()


def _run_library(events_path: Path, mode: str, scrubber: Any) -> None:
    from baton import Client
    from baton.sinks import FileSink

    client = Client(
        sink=FileSink(str(events_path)),
        vendor_id="withhold",
        tenant_id=TENANT,
        consent_token="ct_withhold",
        scrubber=scrubber,
        result_capture_mode=mode,
    )
    try:
        with client.trace(tool_name="fetch", params={"row": "42"}) as t:
            t.observed(result={"record": SECRET})
    finally:
        client.close()


# =============================================================================
# 1 · The short-circuit — the scrubber is never invoked on a withheld result
# =============================================================================


@pytest.mark.anyio
async def test_standalone_off_never_scrubs_the_result(tmp_path: Path) -> None:
    rec = _Recorder()
    await _run_standalone(tmp_path / "off.jsonl", "off", rec, fail=False)
    assert not rec.saw(SECRET), "SPEC §7: the scrubber MUST NOT be invoked on a withheld result"


@pytest.mark.anyio
async def test_standalone_full_DOES_scrub_the_result(tmp_path: Path) -> None:
    """The control. If this cannot see the body, the test above proves nothing."""
    rec = _Recorder()
    await _run_standalone(tmp_path / "full.jsonl", "full", rec, fail=False)
    assert rec.saw(SECRET)


@pytest.mark.anyio
async def test_official_off_never_scrubs_the_result(tmp_path: Path) -> None:
    rec = _Recorder()
    await _run_official(tmp_path / "off.jsonl", "off", rec, fail=False)
    assert not rec.saw(SECRET)


@pytest.mark.anyio
async def test_official_full_DOES_scrub_the_result(tmp_path: Path) -> None:
    rec = _Recorder()
    await _run_official(tmp_path / "full.jsonl", "full", rec, fail=False)
    assert rec.saw(SECRET)


def test_library_off_never_scrubs_the_result(tmp_path: Path) -> None:
    """The library path's gate is in ``observed()``, not at payload construction.

    This is the test that distinguishes the two placements. By the time a
    payload is built, ``Trace.observed`` has already run the vendor's scrubber
    over the body and stored the result on the trace — a gate there would emit
    a correct-looking event about data we had promised not to touch.
    """
    rec = _Recorder()
    _run_library(tmp_path / "off.jsonl", "off", rec)
    assert not rec.saw(SECRET)


def test_library_full_DOES_scrub_the_result(tmp_path: Path) -> None:
    rec = _Recorder()
    _run_library(tmp_path / "full.jsonl", "full", rec)
    assert rec.saw(SECRET)


# =============================================================================
# 2 · The wire — the marker is present and the body is not
# =============================================================================


@pytest.mark.anyio
async def test_standalone_end_carries_the_marker_and_no_body(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    await _run_standalone(p, "off", _Recorder(), fail=False)
    ends = _payloads(p, "tool_call_end")
    assert ends, "no tool_call_end emitted — the assertion below would be vacuous"
    for payload in ends:
        assert payload["result_capture"] == "off"
        assert payload.get("result") is None
        # SPEC §11.2.6: classification, pairing and timing all still work.
        assert payload["tool_name"] == "fetch"
        assert payload["duration_ms"] is not None


@pytest.mark.anyio
async def test_official_end_carries_the_marker_and_no_body(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    await _run_official(p, "off", _Recorder(), fail=False)
    ends = _payloads(p, "tool_call_end")
    assert ends
    for payload in ends:
        assert payload["result_capture"] == "off"
        assert payload.get("result") is None


def test_library_end_carries_the_marker_and_no_body(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _run_library(p, "off", _Recorder())
    ends = _payloads(p, "tool_call_end")
    assert ends
    for payload in ends:
        assert payload["result_capture"] == "off"
        assert payload.get("result") is None


# =============================================================================
# 3 · Absent means captured — and the reference SDK spells absent as ``null``
# =============================================================================


@pytest.mark.anyio
async def test_captured_events_carry_a_NULL_marker_not_a_missing_key(tmp_path: Path) -> None:
    """Pins the fact SPEC §11.4 now states, because a consumer depends on it.

    ``model_dump(mode="json")`` carries no ``exclude_none``, so a defaulted
    member reaches the wire as ``null`` rather than being omitted. A consumer
    testing ``"result_capture" in payload`` therefore reads EVERY captured call
    as withheld — the exact inversion of the member's meaning. The Console
    reads the VALUE (``is not None``), which is why it composes; this test is
    what stops the serialization changing under it silently.
    """
    p = tmp_path / "full.jsonl"
    await _run_standalone(p, "full", _Recorder(), fail=False)
    ends = _payloads(p, "tool_call_end")
    assert ends
    for payload in ends:
        assert "result_capture" in payload, "the key IS emitted — see the docstring"
        assert payload["result_capture"] is None, "and its value is what means 'captured'"


# =============================================================================
# 4 · The two failure shapes sort differently — SPEC §11.4.3
# =============================================================================


@pytest.mark.anyio
async def test_the_RAISE_shape_is_UNCHANGED_under_off(tmp_path: Path) -> None:
    """§11.4.3(1): a raised exception's message is the vendor's own code speaking.

    Nothing was withheld from this event, so it carries NO marker — and the
    diagnostic that makes "write the fix" specificity possible survives the
    strictest capture mode. A change that blanket-dropped ``error_body`` would
    pass every other test in this file and fail here.
    """
    off = tmp_path / "raise-off.jsonl"
    full = tmp_path / "raise-full.jsonl"
    await _run_standalone(off, "off", _Recorder(), fail=True)
    await _run_standalone(full, "full", _Recorder(), fail=True)

    off_errors = _payloads(off, "tool_call_error")
    full_errors = _payloads(full, "tool_call_error")
    assert off_errors, "the tool did not raise — the assertions below would be vacuous"
    assert len(off_errors) == len(full_errors)

    for withheld_run, captured_run in zip(off_errors, full_errors, strict=True):
        # The assertion is EQUALITY across the two modes, not a match against a
        # literal message: fastmcp wraps the vendor's ``ValueError`` in its own
        # ``ToolError`` before this adapter sees it, so the class name here is
        # the library's and pinning it would be testing fastmcp. What §11.4.3
        # actually promises is that ``"off"`` changes this leg NOT AT ALL.
        assert withheld_run["error_body"] == captured_run["error_body"]
        assert withheld_run["error_type"] == captured_run["error_type"]
        # ...and the control for THAT: there has to be something to preserve.
        assert withheld_run["error_body"], "a blank body would satisfy equality vacuously"
        assert withheld_run.get("result_capture") is None


def test_the_RETURN_shape_withholds_BOTH_members(tmp_path: Path) -> None:
    """§11.4.3(2): on a returned failure, ``error_body`` is unwrapped FROM the
    result, so it goes too — as ``""``, because it is a required member.

    Driven through the helper rather than a live server: the returned-flag shape
    needs a result object carrying MCP's error flag, and its spelling varies by
    library version (``isError`` vs ``is_error``), which is a different test's
    subject.
    """
    from types import SimpleNamespace

    from baton.integrations._error_result import returned_error_fields

    rec = _Recorder()
    # ⚠ ATTRIBUTES, not a dict. ``error_text`` reads ``result.content[i].text``
    # via ``getattr`` — the MCP libraries hand it pydantic models, and a dict
    # stand-in silently yields ``""``, which is exactly what withholding
    # produces. A dict fixture here would make the control unable to fail.
    result = SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(type="text", text=SECRET)],
    )

    withheld = returned_error_fields(mode="off", scrubber=rec, result=result)
    assert withheld.error_body == ""
    assert withheld.result_capture == "off"
    # The VALUE, not the key set: SPEC §11.4 says absent and null are the same
    # answer on the wire, so asserting "no result key" would pin a distinction
    # the wire does not have — and the one consumers are told never to test.
    assert withheld.result is None
    assert not rec.saw(SECRET), "neither error_text nor envelope_to_jsonable may run"

    kept = returned_error_fields(mode="full", scrubber=rec, result=result)
    assert kept.error_body == SECRET, "the control: this shape carries the reason"
    assert kept.result_capture is None
    assert rec.saw(SECRET), "and the scrubber IS invoked on the capturing path"
    # ⚠ NOT `kept.result is not None`: `envelope_to_jsonable` answers None for
    # anything it cannot make JSON-safe, and a `SimpleNamespace` has no
    # `model_dump`. SPEC §11.4.3 names that explicitly — "baton-sdk's envelope
    # serializer answers None when an envelope cannot be made JSON-safe, so a
    # RETURN-shape failure can legitimately carry result: null" — which is also
    # why `result` is not a discriminator. ⚠ The mode's effect on this member
    # is NOT covered by the live-server tests above — this file has no
    # RETURN-shape live-server test, for the reason its own docstring gives.
    # It is `test_returned_error_keeps_the_body`, in BOTH
    # `tests/integrations/official/test_iserror_reclassify.py` and its
    # standalone twin, that a mutation check shows reddens when the body is
    # dropped under `"full"`.
    assert kept.result is None


# =============================================================================
# 5 · An unregistered value is refused where it is set, not at emit
# =============================================================================


def test_vendor_config_refuses_an_unregistered_mode() -> None:
    from fastmcp import FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton

    mcp: Any = FastMCP("withhold-invalid")
    with pytest.raises(ValueError, match="result_capture_mode"):
        install_baton(
            mcp,
            VendorConfig(
                vendor_id="withhold",
                vendor_display_name="Withhold Vendor",
                consent_token="ct_withhold",
                tenant_id=TENANT,
                result_capture_mode="none",
            ),
        )


def test_client_refuses_an_unregistered_mode(tmp_path: Path) -> None:
    """``"none"``, ``"disabled"`` and a stray-space ``"off "`` are the plausible
    wrong guesses, and each must raise rather than be read as "not off" and
    capture everything — the one direction that cannot be undone once sent."""
    from baton import Client
    from baton.sinks import FileSink

    for wrong in ("none", "disabled", "off "):
        with pytest.raises(ValueError, match="result_capture_mode"):
            Client(
                sink=FileSink(str(tmp_path / "unused.jsonl")),
                vendor_id="withhold",
                tenant_id=TENANT,
                consent_token="ct_withhold",
                result_capture_mode=wrong,
            )


def test_the_mode_is_validated_at_the_DOORS_and_not_re_checked_inside() -> None:
    """Two doors, two checks — and deliberately no third or fourth.

    ⚠ This test replaced one that asserted `BatonMiddleware` and
    `install_wraps` ALSO refuse an unregistered mode. They did, for one commit,
    and the justification was circular: the seams were guarded because the test
    suite constructs them directly, which is not a vendor. The two sibling
    modes (`intent_param_mode`, `principal_id_mode`) are threaded through those
    same seams and validated at the config door only; adding seam checks here
    made the official path validate twice on every real install, and would have
    made the reserved partial rung a four-place edit.

    ⚠ This read "mypy-strict over `src/baton` covers the chain between the door
    and the seam" until 2026-10-02. It does not — `ResultCaptureMode` is a bare
    `str` alias. See `baton._result_capture.validate_mode`, which carries the
    same correction.

    So what is pinned is the DOOR count: a vendor going through `VendorConfig`
    or `Client`/`AsyncClient` is refused. ⚠ A vendor importing
    `BatonMiddleware` directly is NOT — that path is public and unvalidated,
    and this test does not claim otherwise.
    """
    from fastmcp import FastMCP

    from baton.integrations.standalone import VendorConfig, install_baton
    from baton.integrations.standalone.middleware import BatonMiddleware
    from baton.sinks import StdoutSink

    mcp: Any = FastMCP("withhold-doors")
    with pytest.raises(ValueError, match="result_capture_mode"):
        install_baton(
            mcp,
            VendorConfig(
                vendor_id="withhold",
                vendor_display_name="Withhold Vendor",
                consent_token="ct_withhold",
                tenant_id=TENANT,
                result_capture_mode="OFF",
            ),
        )

    # The adapter-internal seam takes it unchecked, and that is the intended
    # shape — not an oversight. Pinned positively so a future reader does not
    # "fix" it back into a four-place invariant.
    assert (
        BatonMiddleware(
            tenant_id=TENANT,
            vendor_id="withhold",
            consent_token="ct_withhold",
            sink=StdoutSink(),
            result_capture_mode="OFF",
        )._result_capture_mode
        == "OFF"
    )


# =============================================================================
# 6 · A withheld event still conforms to the published schema
# =============================================================================


@pytest.mark.anyio
async def test_withheld_events_conform_to_the_shared_schema(
    event_schema: dict[str, Any], tmp_path: Path
) -> None:
    """The reason the schema was pushed BEFORE any producer could emit this.

    Both tool-call payload definitions are ``additionalProperties: false``, so a
    producer emitting ``result_capture`` against a schema that has not yet
    learned the member fails its own conformance suite.
    """
    import jsonschema

    p = tmp_path / "off.jsonl"
    await _run_standalone(p, "off", _Recorder(), fail=False)
    events = read_events(p)
    assert events
    for event in events:
        jsonschema.validate(event, event_schema)


# =============================================================================
# 6 · `failure_kind` — declared on the wire, not yet emitted (SPEC §11.4.3)
# =============================================================================


def test_failure_kind_is_PERMITTED_on_the_error_payload_and_absent_elsewhere() -> None:
    """The schema half of F3, landing before any producer emits it.

    `ToolCallErrorPayload` is ``extra="forbid"``, so a producer cannot send
    this member until the model and the generated schema carry it — which is
    why the declaration ships on its own, ahead of the capture seam that will
    populate it. The mirror assertion matters as much: it is NOT on the success
    payload, because a failure filed as ``tool_call_end`` is the false success
    this member exists to correct.
    """
    from pydantic import ValidationError

    from baton.events import ToolCallEndPayload, ToolCallErrorPayload

    err = ToolCallErrorPayload(
        tool_name="lookup", error_type="ProtocolError", error_body="Tool lookup not found"
    )
    assert err.failure_kind is None, "absent means the vendor's handler spoke for itself"

    for value in ("unknown_tool", "tool_disabled", "invalid_argument", "output_schema_mismatch"):
        assert (
            ToolCallErrorPayload(
                tool_name="lookup", error_type="ProtocolError", error_body="x", failure_kind=value
            ).failure_kind
            == value
        )

    # A str, not a Literal: a value SPEC registers LATER must not raise on a
    # path §11.2 requires to fail open. Same rule `result_capture` follows.
    assert (
        ToolCallErrorPayload(
            tool_name="t", error_type="E", error_body="x", failure_kind="not_yet_registered"
        ).failure_kind
        == "not_yet_registered"
    )

    with pytest.raises(ValidationError):
        ToolCallEndPayload(tool_name="lookup", failure_kind="unknown_tool")  # type: ignore[call-arg]


def test_failure_kind_reaches_the_wire_as_NULL_when_unset() -> None:
    """Same trap as `result_capture`, one member over — see section 3.

    No ``exclude_none``, so the key IS emitted with a null value. A consumer
    testing ``"failure_kind" in payload`` would read every vendor-authored
    failure as producer-manufactured.
    """
    from baton.events import ToolCallErrorPayload

    wire = ToolCallErrorPayload(
        tool_name="lookup", error_type="ValueError", error_body="boom"
    ).model_dump(mode="json")
    assert "failure_kind" in wire, "the key IS emitted — read the VALUE, never the key"
    assert wire["failure_kind"] is None
