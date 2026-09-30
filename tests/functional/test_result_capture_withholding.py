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


def _read(path: Path) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _payloads(path: Path, event_type: str) -> list[dict[str, Any]]:
    return [e["payload"] for e in _read(path) if e["event_type"] == event_type]


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

    from baton.integrations._result_capture import returned_error_fields

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
    assert withheld == {"error_body": "", "result_capture": "off"}
    assert "result" not in withheld
    assert not rec.saw(SECRET), "neither error_text nor envelope_to_jsonable may run"

    kept = returned_error_fields(mode="full", scrubber=rec, result=result)
    assert kept["error_body"], "the control: this shape normally carries the reason"
    assert "result" in kept
    assert rec.saw(SECRET)


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


# =============================================================================
# 6 · A withheld event still conforms to the published schema
# =============================================================================


@pytest.mark.anyio
async def test_withheld_events_conform_to_the_shared_schema(tmp_path: Path) -> None:
    """The reason the schema was pushed BEFORE any producer could emit this.

    Both tool-call payload definitions are ``additionalProperties: false``, so a
    producer emitting ``result_capture`` against a schema that has not yet
    learned the member fails its own conformance suite.
    """
    import jsonschema

    schema_path = Path(__file__).resolve().parents[2] / "baton-spec" / "events.schema.json"
    if not schema_path.exists():
        pytest.skip(f"baton-spec submodule not checked out ({schema_path} missing)")
    schema = json.loads(schema_path.read_text())

    p = tmp_path / "off.jsonl"
    await _run_standalone(p, "off", _Recorder(), fail=False)
    events = _read(p)
    assert events
    for event in events:
        jsonschema.validate(event, schema)
