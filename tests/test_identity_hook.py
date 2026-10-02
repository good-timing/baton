"""``VendorConfig.resolve_principal`` — the ASSERTED provenance (N11).

Unit-level. The end-to-end halves live in
``tests/functional/test_principal_id_parity.py``, which drives both adapters
through both emit paths and is where precedence and the ``v1:`` tag are
pinned on real events.

What this file covers is the normalizer: every way a vendor's hook can hand
back something that is not a usable ``Principal``. Each of those is a MISS —
the event ships with no ``principal`` — never a raise and never a
half-built principal — because the next thing that happens to a hook's return
value is an HMAC over ``principal.principal_id``, and ``principal_id`` is additive
analytics that may not fail a vendor's tool call (SPEC §11.2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import pytest

from baton.events import PrincipalWire
from baton.identity import Principal, hash_principal_id
from baton.integrations._config import (
    CaseInsensitiveHeaders,
    SessionResolutionContext,
    _validate_vendor_config,
)
from baton.integrations.identity_adapter import (
    PRINCIPAL_ID_MODE_HASHED,
    resolve_call_principal,
    resolve_principal_via_hook,
)

KEY = b"unit-test-key"
TENANT = "tenant-a"
CTX = SessionResolutionContext(headers=None, meta=None, tool_name="lookup", arguments={})


@dataclass
class _Token:
    """An ``AccessToken`` carrying a usable subject. Until the token rung was
    deleted this was the fallback every miss below landed on; it now proves a
    usable token is NOT used unless a hook reads it."""

    token: str = "jwt"
    client_id: str = "acme-app"
    scopes: list[str] = field(default_factory=list)
    claims: dict[str, str] = field(
        default_factory=lambda: {"sub": "token-sub@acme.example", "iss": "https://idp.example"}
    )


#: The context every miss below runs under: it CARRIES a usable token's
#: claims, so a miss that came back non-``None`` would mean something read
#: them behind the hook's back — the deleted fallback returning.
CTX_WITH_TOKEN = SessionResolutionContext(
    headers=None, meta=None, tool_name="lookup", arguments={}, claims=_Token().claims
)


async def _resolve_with_timeout(hook: Any, timeout: float, **kw: Any) -> PrincipalWire | None:
    """``_resolve`` with the hook budget shortened, so the test does not have
    to wait out the real five-second default."""
    import baton.integrations._hooks as hooks_mod

    original = hooks_mod.HOOK_TIMEOUT_SECONDS
    hooks_mod.HOOK_TIMEOUT_SECONDS = timeout
    try:
        return await _resolve(hook, **kw)
    finally:
        hooks_mod.HOOK_TIMEOUT_SECONDS = original


async def _resolve(hook: Any, **kw: Any) -> PrincipalWire | None:
    params: dict[str, Any] = {
        "hook": hook,
        "hook_context": CTX_WITH_TOKEN,
        "mode": PRINCIPAL_ID_MODE_HASHED,
        "tenant_id": TENANT,
        "hmac_key": KEY,
        "logger": logging.getLogger("test"),
        "warned": set(),
    }
    params.update(kw)
    return await resolve_call_principal(**params)


async def test_an_asserted_principal_hashes_under_the_KEY_GENERATION_tag() -> None:
    """``v1:`` is retired: the hook's principal is the bare digest, and
    ``source`` is ``"asserted"`` — the only value this SDK emits."""
    got = await _resolve(lambda _c: Principal(principal_id="employee-1"))
    assert got == PrincipalWire(
        id=hash_principal_id("employee-1", tenant_id=TENANT, key=KEY),
        source="asserted",
        form="hashed",
    )


async def test_a_usable_token_with_NO_hook_yields_no_principal() -> None:
    """The deletion itself. Until this release the SDK read ``claims["sub"]``
    off the verified token whenever no hook answered and stamped it
    ``"attested"``. Identity is the vendor's choice now: no hook, no
    ``principal``, however usable the token."""
    assert await _resolve(None) is None
    assert await _resolve(lambda _c: None) is None


async def test_attested_is_never_emitted() -> None:
    """Even a hook that reads the verified token is the vendor's claim as far
    as the wire can tell, so it is ``"asserted"``."""
    from baton import principal_from_oauth_sub

    got = await _resolve(principal_from_oauth_sub)
    assert got is not None
    assert got.source == "asserted"
    import baton.integrations.identity_adapter as adapter

    assert not hasattr(adapter, "PRINCIPAL_SOURCE_ATTESTED")
    assert not hasattr(adapter, "resolve_attested_principal")


async def test_a_configured_hook_with_no_context_is_treated_as_no_hook() -> None:
    """The context is built only where a hook exists, so a caller that forgets
    it is a reachable wiring mistake; it costs the principal, never a raise."""
    called = False

    def hook(_c: Any) -> Principal:
        nonlocal called
        called = True
        return Principal(principal_id="employee-1")

    assert await _resolve(hook, hook_context=None) is None
    assert not called


class _DuckPrincipal(NamedTuple):
    """The shape a vendor reaches for first — AgentCat's ``identify()`` returns
    a dict, and a namedtuple with the right field names is the near miss."""

    principal_id: str
    issuer: str | None = None


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param({"principal_id": "employee-1"}, id="dict"),
        pytest.param("employee-1", id="bare-string"),
        pytest.param(_DuckPrincipal(principal_id="employee-1"), id="duck-typed-namedtuple"),
        pytest.param(object(), id="arbitrary-object"),
        pytest.param(Principal, id="the-class-not-an-instance"),
    ],
)
async def test_a_wrong_return_type_is_a_miss_not_a_value(returned: Any) -> None:
    """Duck-typing these would put an unvalidated value one line from an HMAC.

    The namedtuple case is the one that matters: it has ``.principal_id`` and
    ``.issuer``, so ``getattr``-based code would accept it and hash it happily.
    """
    assert await _resolve(lambda _c: returned) is None


@pytest.mark.parametrize(
    "bad_principal_id",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(" ", id="single-space"),
        pytest.param("\t\n", id="tab-newline"),
        pytest.param("   ", id="padded-CHAR-column"),
    ],
)
async def test_a_principal_with_no_usable_principal_id_is_a_miss(bad_principal_id: Any) -> None:
    """An empty subject hashes to a real, stable digest that names nobody —
    every such caller merged into one actor, which is the exact collapse
    ``principal_id`` exists to undo. It must not reach the HMAC.

    ⚠ The whitespace cases are NOT padding on the empty one. ``hash_principal_id``
    canonicalizes NFC → strip → lower, so `" "` and `"\t\n"` hash to the SAME
    digest — measured ``h1:14fa5f91…`` for both — and a truthiness guard waves
    them through. A blank header value and a padded ``CHAR(n)`` column are the
    reachable shapes, and they are what makes the phantom actor a real merge
    rather than a theoretical one.
    """
    got = await _resolve(lambda _c: Principal(principal_id=bad_principal_id))
    assert got is None


async def test_a_hook_that_raises_cannot_fail_the_call() -> None:
    def boom(_c: Any) -> Principal:
        raise RuntimeError("directory service down")

    assert await _resolve(boom) is None


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(KeyError("a dict lookup in the vendor's resolver"), id="KeyError"),
        pytest.param(TypeError("wrong arity"), id="TypeError"),
        pytest.param(AttributeError("no such field"), id="AttributeError"),
    ],
)
async def test_the_except_is_broad_rather_than_an_enumerated_tuple(exc: Exception) -> None:
    """An enumerated tuple is a guess about somebody else's code, and this repo
    has already paid for one: fastmcp's ``Context.session`` raises
    ``RuntimeError`` against a docstring promising ``ValueError``, and the stub
    unit test passed the whole time. None of these classes would appear in a
    tuple a reader wrote by imagining what a resolver throws."""

    def boom(_c: Any) -> Principal:
        raise exc

    assert await _resolve(boom) is None


async def test_a_genuine_task_cancellation_propagates() -> None:
    """A capture hook may not swallow a cancellation.

    If it did, a cancelled request would keep running on the vendor's server.
    This is the case that MUST get through: the enclosing task is being
    cancelled from outside, and containment has to let that pass while still
    containing everything the hook does to itself.
    """
    started = asyncio.Event()

    def blocks(_c: Any) -> Principal:
        started.set()
        time.sleep(5)
        return Principal(principal_id="employee-1")

    task = asyncio.create_task(_resolve(blocks))
    await started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_hook_cancelling_ITSELF_is_contained() -> None:
    """The other side of the same coin, and the reason the thread hands its
    outcome back as a VALUE rather than letting it propagate.

    A hook raising ``CancelledError`` for its own private reasons is a hook
    failing, not the request being cancelled. Propagating it would let a
    vendor's control flow kill the tool call the hook was only meant to
    annotate — the fail-open contract broken by the one exception class that
    is not an ``Exception``.
    """

    async def self_cancel(_c: Any) -> Principal:
        raise asyncio.CancelledError

    assert await _resolve(self_cancel) is None


async def test_a_slow_hook_loses_its_result_rather_than_the_loop() -> None:
    """The timeout is only meaningful because the call is off the loop.

    Measured before containment existed: an enclosing
    ``asyncio.wait_for(timeout=2)`` could NOT interrupt a 30-second sync hook,
    because the blocking call held the loop the timer needed. Here the hook
    outlives its budget, the event ships without a ``principal``, and it
    still ships.
    """

    def slow(_c: Any) -> Principal:
        time.sleep(2.0)
        return Principal(principal_id="employee-1")

    t0 = time.monotonic()
    got = await _resolve_with_timeout(slow, 0.2)
    assert got is None
    assert time.monotonic() - t0 < 1.5, "the hook was not abandoned at its deadline"


async def test_a_blocking_hook_does_not_stall_concurrent_calls() -> None:
    """The headline: a vendor's plain ``def`` doing I/O must suspend its OWN
    request and nothing else. Measured pre-fix at 1 tick where ~25 were due."""
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    def blocks(_c: Any) -> Principal:
        time.sleep(0.3)
        return Principal(principal_id="employee-1")

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    await _resolve(blocks)
    stop.set()
    await beat
    assert ticks > 10, f"the event loop was blocked during the hook: {ticks} ticks"


# --- the normalizer's issuer rules (found by /code-review, 2026-09-11) -------


async def test_an_empty_issuer_hashes_as_no_issuer() -> None:
    """``Principal(principal_id=sub, issuer=claims.get("iss", ""))`` is the natural
    thing for a vendor to write. Without coercion it appends an extra
    separator to the HMAC message, so one person gets a stable-but-wrong
    pseudonym and a later switch to ``None`` silently renames every user."""
    absent = await _resolve(lambda _c: Principal(principal_id="same-person"))
    empty = await _resolve(lambda _c: Principal(principal_id="same-person", issuer=""))
    assert absent == empty


@pytest.mark.parametrize(
    "bad_issuer",
    [
        pytest.param(42, id="int"),
        pytest.param(object(), id="object"),
        pytest.param(b"x", id="bytes"),
    ],
)
async def test_a_junk_issuer_costs_the_issuer_not_the_identity(bad_issuer: Any) -> None:
    """A non-string issuer reaches ``unicodedata.normalize`` and raises. Caught
    downstream — but the cost was the whole event's ``principal``. It is
    coerced before it gets there, so a junk issuer costs the issuer only."""
    got = await _resolve(lambda _c: Principal(principal_id="employee-1", issuer=bad_issuer))
    assert got == await _resolve(lambda _c: Principal(principal_id="employee-1"))
    assert got is not None and got.source == "asserted"


async def test_a_principal_that_cannot_be_hashed_emits_nothing() -> None:
    """An unpaired surrogate is the reachable shape once ``issuer`` is coerced.
    The context carries a usable token, and it must not be substituted: a hook
    that named a person and failed to render them has not asked for the
    token's subject."""
    got = await _resolve(lambda _c: Principal(principal_id="a\ud800b"))
    assert got is None, "a failed hash substituted a different actor"


async def test_the_hook_receives_the_context_it_was_given() -> None:
    seen: list[SessionResolutionContext] = []

    def hook(c: SessionResolutionContext) -> Principal:
        seen.append(c)
        return Principal(principal_id="employee-1")

    await _resolve(hook)
    assert seen == [CTX_WITH_TOKEN]
    assert seen[0].tool_name == "lookup"


async def test_an_async_hook_is_awaited() -> None:
    async def hook(_c: Any) -> Principal:
        return Principal(principal_id="employee-1")

    got = await resolve_principal_via_hook(hook, CTX, logger=logging.getLogger("test"))
    assert got == Principal(principal_id="employee-1")


async def test_hashed_mode_with_no_key_drops_the_field_rather_than_leaking_it() -> None:
    """The principal must not appear in the output on the missing-key branch."""
    got = await _resolve(lambda _c: Principal(principal_id="employee-1"), hmac_key=None)
    assert got is None


def test_a_non_callable_resolve_principal_is_refused_at_install() -> None:
    """Unvalidated it would fail inside the hook's own fail-open guard —
    logged, identity silently absent for the life of the process, in the one
    deployment shape (stdio + out-of-band auth) the field exists for."""
    from baton.integrations._config import VendorConfig

    with pytest.raises(ValueError, match="resolve_principal must be callable"):
        _validate_vendor_config(
            VendorConfig(
                vendor_id="acme",
                vendor_display_name="Acme",
                resolve_principal="baton.identity.resolve",  # type: ignore[arg-type]
            )
        )


def test_NO_scheme_tag_comes_back_and_neither_constant_returns() -> None:
    """Both retired tags stay retired, and so does the machinery for one.

    This test has been rewritten twice, each time narrowing what may appear in
    a hashed value, and the history is the point:

    * originally it guarded that the vendor tag ``v1:`` sat OUTSIDE the ``h*``
      family, so a key rotation could not eat its letter;
    * at 0.8.10 ``v1:`` was retired — provenance became ``principal.source``,
      which survives ``"raw"`` mode where a tag cannot — and this asserted that
      one family remained and meant only the key generation;
    * at 0.8.11 that family went too, so **nothing may prefix the digest**.

    ⚠ **The assertions are written NEGATIVELY and generically on purpose.**
    Naming ``h1:`` would let ``h2:`` or a newly invented letter through, which
    is exactly how the thing being guarded came back the first time. No colon,
    and no constant to select one.
    """
    import baton.identity

    assert not hasattr(baton.identity, "VENDOR_HASH_SCHEME")
    assert not hasattr(baton.identity, "HASH_SCHEME")
    digest = hash_principal_id("x", tenant_id=TENANT, key=KEY)
    assert ":" not in digest
    assert len(digest) == 64 and digest == digest.lower()

    # And the parameter that selected a tag is gone from the signature, not
    # merely unused: a keyword left in place is an invitation to pass one.
    import inspect

    assert "scheme" not in inspect.signature(hash_principal_id).parameters


# =============================================================================
# ``CaseInsensitiveHeaders`` — register A8.
#
# The context's ``headers`` is the one field whose declared type normalized
# less than it appeared to. ``Mapping[str, str]`` is true of a case-insensitive
# Starlette ``Headers`` and of a lowercased plain ``dict`` alike, so the
# divergence was invisible to mypy AND to every test in this file, whose only
# context until now was ``headers=None``.
# =============================================================================


class TestCaseInsensitiveHeaders:
    def test_lookup_ignores_case_in_both_directions(self) -> None:
        headers = CaseInsensitiveHeaders({"x-forwarded-user": "employee-4417"})

        assert headers["X-Forwarded-User"] == "employee-4417"
        assert headers["x-forwarded-user"] == "employee-4417"
        assert headers.get("X-FORWARDED-USER") == "employee-4417"
        assert "X-Forwarded-User" in headers
        # Construction folds too, so a vendor building one by hand from
        # canonical-case keys gets the same object the adapter would deliver.
        assert CaseInsensitiveHeaders({"X-Forwarded-User": "e"})["x-forwarded-user"] == "e"

    def test_a_miss_is_still_a_miss(self) -> None:
        """The wrap must not turn an absent header into a present one — that
        would manufacture a principal out of nothing, the direction every tie
        in this design breaks against."""
        headers = CaseInsensitiveHeaders({"x-forwarded-user": "employee-4417"})

        assert headers.get("x-api-key") is None
        assert "x-api-key" not in headers
        with pytest.raises(KeyError):
            headers["X-Api-Key"]

    def test_every_dict_behaviour_a_hook_had_is_preserved(self) -> None:
        """The regression this class could have introduced in the OTHER
        direction. Standalone hooks have been handed a real ``dict`` since the
        field existed, so a plain ``Mapping`` wrapper would break
        ``isinstance``, ``json.dumps``, ``.copy()`` and ``|`` — and because
        ``resolve_principal_via_hook`` catches bare ``Exception``, a hook that
        merely logged its headers before reading a key would have started
        nulling ``principal_id`` silently. Same fail-open, opposite direction.
        """
        import json

        headers = CaseInsensitiveHeaders({"x-forwarded-user": "employee-4417"})

        assert isinstance(headers, dict)
        assert json.loads(json.dumps(headers)) == {"x-forwarded-user": "employee-4417"}
        assert headers.copy() == {"x-forwarded-user": "employee-4417"}
        assert headers | {"x-api-key": "k"} == {
            "x-forwarded-user": "employee-4417",
            "x-api-key": "k",
        }
        assert headers == {"x-forwarded-user": "employee-4417"}
        # Iteration yields the WIRE form, one spelling per header — folding on
        # read must not add a second key or change what enumeration reports.
        assert set(headers) == {"x-forwarded-user"}
        assert len(headers) == 1

    def test_writes_fold_too_so_a_key_you_just_set_is_readable(self) -> None:
        """The half this class shipped without, and it was strictly worse than
        the plain ``dict`` it replaced.

        Folding reads alone broke the invariant that every stored key is
        lowercase: ``h["X-B"] = "2"`` stored ``X-B`` verbatim, and ``h["X-B"]``
        then raised ``KeyError`` for a key ``list(h)`` plainly showed. A hook
        that normalizes before reading — ``setdefault`` a fallback, then read
        it back — worked on the old dict and raised here, and the raise is
        swallowed into a null ``principal_id`` on every event.
        """
        headers = CaseInsensitiveHeaders({"x-a": "1"})

        headers["X-B"] = "2"
        assert headers["X-B"] == "2"
        assert headers.get("x-b") == "2"
        assert "X-B" in headers

        assert headers.setdefault("X-C", "v") == "v"
        assert headers["x-c"] == "v"
        # An EXISTING key is found through the fold, so a default cannot
        # overwrite a header the caller already has under another spelling.
        assert headers.setdefault("X-A", "other") == "1"

        headers.update({"X-D": "4"})
        headers.update([("X-E", "5")])
        assert (headers["x-d"], headers["x-e"]) == ("4", "5")

        assert headers.pop("X-B") == "2"
        assert "x-b" not in headers
        del headers["X-A"]
        assert "x-a" not in headers

        # The invariant the whole class rests on.
        assert all(key == key.lower() for key in headers)

    def test_a_non_string_key_behaves_as_it_did_on_a_plain_dict(self) -> None:
        """``.lower()`` on an unconditional path would raise ``AttributeError``
        where a dict answered. The odd key is still a key."""
        headers = CaseInsensitiveHeaders({"x-forwarded-user": "employee-4417"})

        assert headers.get(None) is None  # type: ignore[arg-type]
        assert None not in headers
        with pytest.raises(KeyError):
            headers[None]  # type: ignore[index]


async def test_a_hand_built_context_folds_case_the_way_production_does() -> None:
    """Register A8, at the seam a VENDOR touches when testing their own hook.

    This is the case no adapter-side fix could reach. A vendor unit-testing
    their hook constructs this object by hand, so if the class did not fold,
    a hook written for production (``ctx.headers["X-Forwarded-User"]``) would
    fail their own test while working when deployed — and one written to pass
    their test would fail in production. The public type is the thing vendors
    construct, so the public type is the thing that has to behave.

    Replaces a test that asserted the opposite: it fed the raw wire dict in and
    pinned the fall-through to ``None``, which documented the bug's shape rather
    than the contract, and would have frozen the fix at the depth of one
    adapter's extractor.
    """

    def vendor_hook(ctx: Any) -> Any:
        assert ctx.headers is not None
        return Principal(principal_id=ctx.headers["X-Forwarded-User"])

    resolved = await resolve_principal_via_hook(
        vendor_hook,
        SessionResolutionContext(
            headers={"x-forwarded-user": "employee-4417"},
            meta=None,
            tool_name="lookup",
            arguments={},
        ),
        logger=logging.getLogger("test"),
    )
    assert resolved == Principal(principal_id="employee-4417")


def test_any_other_mapping_is_folded_not_just_a_dict() -> None:
    """The gate is "fold by default", not "fold dicts".

    It gated on ``isinstance(dict)`` first, which covered the two shapes the
    adapters ship and missed every other mapping — and holed the exact case
    that argued for folding in this class at all, a vendor hand-building the
    context in their own unit tests.
    """
    from types import MappingProxyType

    ctx = SessionResolutionContext(
        headers=MappingProxyType({"x-forwarded-user": "employee-4417"}),
        meta=None,
        tool_name="lookup",
        arguments={},
    )

    assert ctx.headers is not None
    assert ctx.headers["X-Forwarded-User"] == "employee-4417"


def test_a_starlette_headers_is_passed_through_untouched() -> None:
    """The direction constraint, enforced structurally rather than by comment.

    Starlette ``Headers`` is not a ``dict`` subclass, which is what lets
    ``__post_init__`` tell "needs folding" from "already folded, and carries
    more than a dict can". If it were ever converted, official vendors would
    keep case-insensitive lookup and silently lose ``getlist()``.
    """
    from tests._asgi import starlette_headers

    original = starlette_headers({"X-Forwarded-User": "employee-4417"})
    ctx = SessionResolutionContext(headers=original, meta=None, tool_name="lookup", arguments={})

    assert ctx.headers is original
    assert ctx.headers.getlist("x-forwarded-user") == ["employee-4417"]  # type: ignore[union-attr]


async def test_the_shipped_hooks_never_touch_the_hook_thread_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_vendor_hook``'s inline fast path, proven by taking the thread pool
    away: a shipped hook still resolves, and a vendor's sync hook — which must
    still go to a thread — now cannot."""
    import anyio.to_thread

    from baton import principal_from_oauth_sub

    async def no_threads(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a hook went to a worker thread")

    monkeypatch.setattr(anyio.to_thread, "run_sync", no_threads)

    got = await _resolve(principal_from_oauth_sub)
    assert got is not None and got.source == "asserted"
    # The control: without it, a pool that was never consulted at all would
    # pass the line above for the wrong reason.
    assert await _resolve(lambda _c: Principal(principal_id="employee-1")) is None
