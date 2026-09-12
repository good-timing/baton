"""``VendorConfig.resolve_user`` — the ASSERTED provenance (N11).

Unit-level. The end-to-end halves live in
``tests/functional/test_user_id_parity.py``, which drives both adapters
through both emit paths and is where precedence and the ``v1:`` tag are
pinned on real events.

What this file covers is the normalizer: every way a vendor's hook can hand
back something that is not a usable ``Principal``. Each of those is a MISS
that falls through to the verified token, never a raise and never a
half-built principal — because the next thing that happens to a hook's return
value is an HMAC over ``principal.user_id``, and ``user_id`` is additive
analytics that may not fail a vendor's tool call (SPEC §11.2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import pytest

from baton.identity import HASH_SCHEME, VENDOR_HASH_SCHEME, Principal, hash_user_id
from baton.integrations._config import SessionResolutionContext, _validate_vendor_config
from baton.integrations.identity_adapter import (
    USER_ID_MODE_HASHED,
    resolve_call_user_id,
    resolve_principal_via_hook,
)

KEY = b"unit-test-key"
TENANT = "tenant-a"
CTX = SessionResolutionContext(headers=None, meta=None, tool_name="lookup", arguments={})


@dataclass
class _Token:
    """An ``AccessToken`` carrying a usable subject — the attested fallback."""

    token: str = "jwt"
    client_id: str = "acme-app"
    scopes: list[str] = field(default_factory=list)
    claims: dict[str, str] = field(
        default_factory=lambda: {"sub": "attested@acme.example", "iss": "https://idp.example"}
    )


def _attested() -> str:
    return hash_user_id(
        "attested@acme.example", tenant_id=TENANT, key=KEY, issuer="https://idp.example"
    )


async def _resolve_with_timeout(
    hook: Any, timeout: float, token: Any = None, **kw: Any
) -> str | None:
    """``_resolve`` with the hook budget shortened, so the test does not have
    to wait out the real five-second default."""
    import baton.integrations._hooks as hooks_mod

    original = hooks_mod.HOOK_TIMEOUT_SECONDS
    hooks_mod.HOOK_TIMEOUT_SECONDS = timeout
    try:
        return await _resolve(hook, token=token, **kw)
    finally:
        hooks_mod.HOOK_TIMEOUT_SECONDS = original


async def _resolve(hook: Any, token: Any = None, **kw: Any) -> str | None:
    params: dict[str, Any] = {
        "hook": hook,
        "hook_context": CTX,
        "mode": USER_ID_MODE_HASHED,
        "tenant_id": TENANT,
        "hmac_key": KEY,
        "logger": logging.getLogger("test"),
        "warned": set(),
    }
    params.update(kw)
    return await resolve_call_user_id(token, **params)


async def test_a_principal_is_hashed_under_the_vendor_scheme() -> None:
    got = await _resolve(lambda _c: Principal(user_id="employee-1"))
    assert got == hash_user_id("employee-1", tenant_id=TENANT, key=KEY, scheme=VENDOR_HASH_SCHEME)
    assert got is not None and got.startswith("v1:")


async def test_the_tag_is_the_only_difference_from_the_attested_derivation() -> None:
    """One person reached by two provenances is the same hex under two tags.

    Deliberate, and worth pinning: the scheme is not part of the HMAC message,
    so a consumer that later decides to unify the two provenances CAN, and one
    that must keep them apart still can. Two unrelated digests would have
    foreclosed the first option silently.
    """
    asserted = await _resolve(lambda _c: Principal(user_id="same-person"))
    attested = hash_user_id("same-person", tenant_id=TENANT, key=KEY)
    assert asserted is not None
    assert asserted.split(":", 1)[1] == attested.split(":", 1)[1]
    assert asserted.startswith(f"{VENDOR_HASH_SCHEME}:")
    assert attested.startswith(f"{HASH_SCHEME}:")


async def test_the_hook_beats_a_usable_token() -> None:
    got = await _resolve(lambda _c: Principal(user_id="employee-1"), token=_Token())
    assert got != _attested()
    assert got is not None and got.startswith("v1:")


async def test_returning_none_falls_through_to_the_token() -> None:
    assert await _resolve(lambda _c: None, token=_Token()) == _attested()


async def test_no_hook_configured_is_exactly_todays_behaviour() -> None:
    assert await _resolve(None, token=_Token()) == _attested()
    assert await _resolve(None, token=None) is None


async def test_a_configured_hook_with_no_context_degrades_rather_than_dropping_identity() -> None:
    """A caller that forgets to build the context gets today's behaviour, not
    silence. The context is built only where a hook exists, so this is a
    reachable wiring mistake rather than a hypothetical one."""
    called = False

    def hook(_c: Any) -> Principal:
        nonlocal called
        called = True
        return Principal(user_id="employee-1")

    got = await _resolve(hook, token=_Token(), hook_context=None)
    assert got == _attested()
    assert not called


class _DuckPrincipal(NamedTuple):
    """The shape a vendor reaches for first — AgentCat's ``identify()`` returns
    a dict, and a namedtuple with the right field names is the near miss."""

    user_id: str
    issuer: str | None = None


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param({"user_id": "employee-1"}, id="dict"),
        pytest.param("employee-1", id="bare-string"),
        pytest.param(_DuckPrincipal(user_id="employee-1"), id="duck-typed-namedtuple"),
        pytest.param(object(), id="arbitrary-object"),
        pytest.param(Principal, id="the-class-not-an-instance"),
    ],
)
async def test_a_wrong_return_type_is_a_miss_not_a_value(returned: Any) -> None:
    """Duck-typing these would put an unvalidated value one line from an HMAC.

    The namedtuple case is the one that matters: it has ``.user_id`` and
    ``.issuer``, so ``getattr``-based code would accept it and hash it happily.
    """
    assert await _resolve(lambda _c: returned, token=_Token()) == _attested()


@pytest.mark.parametrize(
    "bad_user_id",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(" ", id="single-space"),
        pytest.param("\t\n", id="tab-newline"),
        pytest.param("   ", id="padded-CHAR-column"),
    ],
)
async def test_a_principal_with_no_usable_user_id_is_a_miss(bad_user_id: Any) -> None:
    """An empty subject hashes to a real, stable digest that names nobody —
    every such caller merged into one actor, which is the exact collapse
    ``user_id`` exists to undo. It must not reach the HMAC.

    ⚠ The whitespace cases are NOT padding on the empty one. ``hash_user_id``
    canonicalizes NFC → strip → lower, so `" "` and `"\t\n"` hash to the SAME
    digest — measured ``h1:14fa5f91…`` for both — and a truthiness guard waves
    them through. A blank header value and a padded ``CHAR(n)`` column are the
    reachable shapes, and they are what makes the phantom actor a real merge
    rather than a theoretical one.
    """
    got = await _resolve(lambda _c: Principal(user_id=bad_user_id), token=_Token())
    assert got == _attested()


async def test_a_hook_that_raises_cannot_fail_the_call() -> None:
    def boom(_c: Any) -> Principal:
        raise RuntimeError("directory service down")

    assert await _resolve(boom, token=_Token()) == _attested()


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

    assert await _resolve(boom, token=_Token()) == _attested()


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
        return Principal(user_id="employee-1")

    task = asyncio.create_task(_resolve(blocks, token=_Token()))
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

    assert await _resolve(self_cancel, token=_Token()) == _attested()


async def test_a_slow_hook_loses_its_result_rather_than_the_loop() -> None:
    """The timeout is only meaningful because the call is off the loop.

    Measured before containment existed: an enclosing
    ``asyncio.wait_for(timeout=2)`` could NOT interrupt a 30-second sync hook,
    because the blocking call held the loop the timer needed. Here the hook
    outlives its budget, the call falls through to the token, and the event
    still ships.
    """

    def slow(_c: Any) -> Principal:
        time.sleep(2.0)
        return Principal(user_id="employee-1")

    t0 = time.monotonic()
    got = await _resolve_with_timeout(slow, 0.2, token=_Token())
    assert got == _attested()
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
        return Principal(user_id="employee-1")

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.02)
    await _resolve(blocks, token=_Token())
    stop.set()
    await beat
    assert ticks > 10, f"the event loop was blocked during the hook: {ticks} ticks"


# --- the normalizer's issuer rules (found by /code-review, 2026-09-11) -------


async def test_an_empty_issuer_hashes_as_no_issuer() -> None:
    """``Principal(user_id=sub, issuer=claims.get("iss", ""))`` is the natural
    thing for a vendor to write. Without coercion it appends an extra
    separator to the HMAC message, so one person gets a stable-but-wrong
    pseudonym and a later switch to ``None`` silently renames every user."""
    absent = await _resolve(lambda _c: Principal(user_id="same-person"))
    empty = await _resolve(lambda _c: Principal(user_id="same-person", issuer=""))
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
    downstream — but the cost was the whole event's ``user_id``, including the
    attested one the token could still have produced, so a configured-but-buggy
    hook was strictly worse than no hook. It is coerced before it gets there."""
    got = await _resolve(
        lambda _c: Principal(user_id="employee-1", issuer=bad_issuer), token=_Token()
    )
    assert got == await _resolve(lambda _c: Principal(user_id="employee-1"))
    assert got is not None and got.startswith("v1:")


async def test_a_principal_that_cannot_be_hashed_emits_nothing_rather_than_the_token() -> None:
    """**The one case that deliberately does not fall through.**

    A hook returning ``None`` has no opinion about the request, so the token is
    the best available answer. A hook that NAMED a person and then failed to
    render them has told us the token names somebody else — which is the whole
    reason it sits above the token. Falling back would file the call under the
    gateway's service account: plausible, wrong, and heavily merged. Losing the
    join beats inventing one.

    An unpaired surrogate is the reachable shape once ``issuer`` is coerced.
    """
    got = await _resolve(lambda _c: Principal(user_id="a\ud800b"), token=_Token())
    assert got is None, "a failed hash substituted a different actor"
    assert got != _attested()


async def test_the_hook_receives_the_context_it_was_given() -> None:
    seen: list[SessionResolutionContext] = []

    def hook(c: SessionResolutionContext) -> Principal:
        seen.append(c)
        return Principal(user_id="employee-1")

    await _resolve(hook)
    assert seen == [CTX]
    assert seen[0].tool_name == "lookup"


async def test_an_async_hook_is_awaited() -> None:
    async def hook(_c: Any) -> Principal:
        return Principal(user_id="employee-1")

    got = await resolve_principal_via_hook(hook, CTX, logger=logging.getLogger("test"))
    assert got == Principal(user_id="employee-1")


async def test_hashed_mode_with_no_key_drops_the_field_rather_than_leaking_it() -> None:
    """The asserted path goes through the same chokepoint as the attested one,
    so it inherits the missing-key rule instead of restating it. The principal
    must not appear in the output on that branch."""
    got = await _resolve(lambda _c: Principal(user_id="employee-1"), hmac_key=None)
    assert got is None


def test_a_non_callable_resolve_user_is_refused_at_install() -> None:
    """Unvalidated it would fail inside the hook's own fail-open guard —
    logged, identity silently absent for the life of the process, in the one
    deployment shape (stdio + out-of-band auth) the field exists for."""
    from baton.integrations._config import VendorConfig

    with pytest.raises(ValueError, match="resolve_user must be callable"):
        _validate_vendor_config(
            VendorConfig(
                vendor_id="acme",
                vendor_display_name="Acme",
                resolve_user="baton.identity.resolve",  # type: ignore[arg-type]
            )
        )


def test_the_vendor_scheme_sits_outside_the_rotation_family() -> None:
    """``h2:`` is reserved for HMAC key rotation, so a vendor scheme must not
    eat a letter rotation may later want (SPEC §11.4)."""
    assert not VENDOR_HASH_SCHEME.startswith("h")
    assert VENDOR_HASH_SCHEME != HASH_SCHEME
