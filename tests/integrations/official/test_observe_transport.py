"""``observe_transport`` — the official adapter's four answers, asserted HERE.

**Why this file sits in ``tests/integrations/official/`` and not beside the
parity test.** ``mcp-matrix`` in ``ci.yml`` is the only job that pins an ``mcp``
version — 1.20, 1.25, 1.27.2, 2.0.0 — and its last step runs
``pytest tests/integrations/official/`` and nothing else. Until this file
existed that directory held **no assertion about the transport read at all**,
so none of those four legs exercised it.

⚠ **It was not that the read never RAN there.** ``_fake_context`` attaches a
real request object, so ``observe_transport`` was already being called through
the install tests on every leg — and nothing looked at what it returned.
Measured 2026-09-22: inverting the terminal return in ``_tool_wrap.py`` left
``tests/integrations/official/`` at 97 passed, exit 0, while
``tests/functional/test_transport_observed_parity.py`` reddened. A fixture that
exercises a read without asserting on it is indistinguishable from no coverage.

⚠ **The parity test cannot be moved or pointed at.** ``tests/functional/``
covers BOTH adapters, so every file in it needs ``fastmcp`` — and this job
resolves ``[mcp,test]`` and **fails on purpose if fastmcp is installed**
(``ci.yml``: "must not have it", and the leg prints ``no fastmcp``). Adding that
directory to the job's run line would break the job rather than close the gap.
So the assertion is duplicated here in the one shape this leg can hold: the
official adapter alone, no fastmcp, no server.

The four answers are ``SPEC §11.4``'s and the function's docstring states the
reasoning for each; this file pins them, it does not restate them.
"""

from __future__ import annotations

from typing import Any

import pytest

from baton.integrations.official._tool_wrap import observe_transport

from ._fake_context import _FakeContextV1, _FakeContextV2


class _RequestContextRaises:
    """A context whose ``request_context`` raises — the library's documented
    answer to "is there a live request?" (``ValueError``) and an unexpected
    failure (anything else)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def request_context(self) -> Any:
        raise self._exc


class _RequestRaises:
    """``request_context`` is fine; reading ``.request`` off it is not."""

    class _RC:
        @property
        def request(self) -> Any:
            raise RuntimeError("the request read blew up")

    def __init__(self) -> None:
        self.request_context = _RequestRaises._RC()


@pytest.mark.parametrize("ctx_class", [_FakeContextV2, _FakeContextV1])
def test_a_live_request_reads_as_http(ctx_class: Any) -> None:
    """A request object is there → ``"http"``.

    Both context shapes, because mcp 1.x has no ``.headers`` on ``Context`` and
    2.x does — the read must key on the REQUEST either way, never on headers.
    """
    assert observe_transport(ctx_class({"x-forwarded-for": "203.0.113.7"})) == "http"


@pytest.mark.parametrize("ctx_class", [_FakeContextV2, _FakeContextV1])
def test_no_request_behind_a_live_call_reads_as_no_http_request(ctx_class: Any) -> None:
    """``rc.request is None`` → ``"no-http-request"``. Stdio or in-memory.

    ⚠ This is the value SPEC §3.4 licenses a consumer to group a process-wide
    fallback ``session_id`` on. It must never be the answer to a FAILED read.
    """
    assert observe_transport(ctx_class(None)) == "no-http-request"


def test_http_and_no_http_request_are_not_interchangeable() -> None:
    """The two live answers must differ.

    Without this, inverting the terminal return is invisible to this directory
    — which is exactly what was measured on 2026-09-22.
    """
    with_request = observe_transport(_FakeContextV2({"host": "example.test"}))
    without_request = observe_transport(_FakeContextV2(None))
    assert with_request == "http"
    assert without_request == "no-http-request"
    assert with_request != without_request


def test_no_context_at_all_reads_as_none() -> None:
    """Nothing to look at → ``None``, not a claim about the deployment."""
    assert observe_transport(None) is None


def test_a_programmatic_call_reads_as_none_not_no_http_request() -> None:
    """``request_context`` raising ``ValueError`` is ``mcp.call_tool()``.

    Deliberately NOT ``no-http-request``: there is no live MCP request, so
    saying so would assert a fact about a deployment that is not running. And
    NOT ``read-failed``: nothing failed.
    """
    assert observe_transport(_RequestContextRaises(ValueError("no active request"))) is None


def test_an_unexpected_context_failure_reads_as_read_failed() -> None:
    """Any other raise from ``request_context`` → ``"read-failed"``.

    The whole point of the value: our own instrument failing must be countable,
    and must not be dressed up as a fact about the customer's transport.
    """
    assert (
        observe_transport(_RequestContextRaises(AttributeError("no such attribute")))
        == "read-failed"
    )


def test_a_failing_request_read_reads_as_read_failed() -> None:
    """The second read can fail on its own, after ``request_context`` succeeded."""
    assert observe_transport(_RequestRaises()) == "read-failed"


def test_read_failed_is_never_confused_with_absence() -> None:
    """The A6 direction: a failed read must not land on ``no-http-request``.

    ``_extract_headers_from_context`` folds ``AttributeError`` into the same
    ``None`` a genuine absence returns, and register A6 records that as a live
    defect. ``observe_transport`` must not inherit it — this is the assertion
    that would red if someone rebuilt it on that helper.
    """
    failed = observe_transport(_RequestContextRaises(AttributeError("boom")))
    absent = observe_transport(_FakeContextV2(None))
    assert failed == "read-failed"
    assert absent == "no-http-request"
    assert failed != absent
