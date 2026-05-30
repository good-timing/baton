"""Dogfood demo: Baton library API instrumenting a Skill-pattern code path.

What this demo is
-----------------

A customer (or an agent following a vendor-published chat-completions Skill)
writes Python that calls ``client.chat.completions.create(...)``. This file
is exactly that — **plus** a thin Baton wrap (3 patterns: ``client.trace()``
context manager, ``trace.observed(...)``, and ``trace.annotate(...)`` for
the reactive friction signal that becomes a Console "ticket").

The vendor SDK is stubbed (``fake_vendor``) so this runs offline. The bug we
reproduce is the kind of capability-mismatch failure that surfaces across
multiple inference vendors today: a small model returns a 400 ("Grammar must
have a 'properties' field") when called with
``response_format={"type": "json_schema", ...}`` even though the vendor's
docs and Skill imply json_schema is broadly supported.

What we're trying to learn
--------------------------

This is dogfood, not demo theatre. We're using our own SDK to:

1. Confirm the ``baton.Client`` / ``trace`` / ``annotate`` surface holds up
   when someone follows a real-world Skill code pattern.
2. Surface every rough edge — awkward call, redundant ceremony, surprising
   default — into ``ROUGH_EDGES.md``. Those become library API fixes before
   the SDK gets used by external integrators.

Run
---

In one terminal::

    cd <repo-root>
    .venv/bin/python examples/skill_demo/local_ingest.py
    # Listens on http://127.0.0.1:8000, bearer = "dev-key", logs to events.jsonl

In another::

    cd <repo-root>
    .venv/bin/python examples/skill_demo/demo.py

Watch the ingest emulator's stderr for the live event tail.
"""

from __future__ import annotations

import logging
import sys

from baton import Client, SignalType

# Stubbed vendor SDK with OpenAI-compatible chat completions. Real customer
# code would import the actual vendor's client class instead.
from fake_vendor import BadRequestError, VendorClient  # noqa: E402


logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
log = logging.getLogger("demo")


# The user prompt the agent is responding to. Pinned at module level so the
# storyline reads top-down.
USER_PROMPT = (
    "Extract product attributes from this messy text — return name, price, "
    "and category as a strict JSON object. Use the cheapest fast small model. "
    "The text is:\n\n"
    "'Got the new Acme Pro Toaster for $129.99 last week, kitchen gadget, "
    "love the bagel setting.'"
)


def main() -> None:
    # Init the Baton client. Explicit kwargs; in production you'd let
    # BATON_API_KEY / BATON_INGEST_URL / BATON_VENDOR_ID env vars feed these.
    baton = Client(
        api_key="dev-key",
        ingest_url="http://127.0.0.1:8765",
        vendor_id="acme",
        consent_token="demo-consent-token",
    )

    vendor = VendorClient(api_key="not-used-in-demo")

    try:
        _run_demo(baton, vendor)
    finally:
        # close() flushes pending events, stops the sync bridge, and is safe
        # to call multiple times.
        baton.close()


def _run_demo(baton: Client, vendor: VendorClient) -> None:
    # =========================================================================
    # Step 1 — preflight: pick a model. Plain chat completion, succeeds.
    # The agent (or customer) uses chat.completions to reason about which
    # model to use. Regular vendor call.
    # =========================================================================

    log.info("[demo] Step 1 — preflight chat completion")
    preflight_messages = [
        {"role": "system", "content": "You help an agent pick a vendor model."},
        {
            "role": "user",
            "content": "Which model is cheapest+fastest for short JSON extraction?",
        },
    ]
    with baton.trace(
        tool_name="vendor.chat.completions.create",
        intent="pick a vendor model for cheap structured-output extraction",
        expected_outcome="a model id string the agent can use for the actual extraction",
        workflow="model-selection-preflight",
        params={"model": "vendor/llm-8b-instruct", "messages": preflight_messages},
    ) as trace:
        preflight = vendor.chat.completions.create(
            model="vendor/llm-8b-instruct",
            messages=preflight_messages,
        )
        trace.observed(result=preflight.model_dump())
    log.info("[demo]   ok — preflight returned %s", preflight.choices[0].message.content[:80])

    # =========================================================================
    # Step 2 — the actual extraction. The agent follows the Skill's
    # json_schema pattern. vendor/llm-3b-instruct is the "cheapest fast" one.
    # This fails with a cryptic 400 — the documented capability-mismatch
    # pattern.
    # =========================================================================

    log.info("[demo] Step 2 — json_schema extraction on vendor/llm-3b-instruct")
    extraction_messages = [
        {"role": "system", "content": "Extract product attributes. Return strict JSON."},
        {"role": "user", "content": USER_PROMPT},
    ]
    extraction_schema = {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "price": {"type": "number"},
                "category": {"type": "string"},
            },
            "required": ["name", "price", "category"],
        },
    }

    failed_with: BadRequestError | None = None
    with baton.trace(
        tool_name="vendor.chat.completions.create",
        intent="extract product attributes as strict JSON per user prompt",
        expected_outcome="json object with name, price, category fields populated",
        workflow="structured-output-extraction",
        params={
            "model": "vendor/llm-3b-instruct",
            "messages": extraction_messages,
            "response_format": extraction_schema,
        },
    ) as extraction_trace:
        try:
            result = vendor.chat.completions.create(
                model="vendor/llm-3b-instruct",
                messages=extraction_messages,
                response_format=extraction_schema,
            )
            extraction_trace.observed(result=result.model_dump())
        except BadRequestError as exc:
            # Catch the error so the demo can continue and emit the reactive
            # annotation. trace.observed(error=exc) marks the trace as failed
            # without re-raising — the trace derives error_type + error_body
            # from the exception object. (Alternative: let it propagate;
            # __exit__ auto-emits tool_call_error and re-raises.)
            failed_with = exc
            extraction_trace.observed(error=exc)

    if failed_with is not None:
        log.info("[demo]   error — %s: %s", type(failed_with).__name__, failed_with)

        # =====================================================================
        # Step 3 — the "ticket." The agent recognizes the failure shape as
        # a capability mismatch (the small model doesn't support json_schema
        # despite the Skill implying broad model support) and raises a
        # reactive friction signal via extraction_trace.annotate(), which
        # binds the signal to the same session_id as the failed trace —
        # so the Console can correlate this ticket to its cause without
        # timestamp heuristics. The canonical Baton hero moment.
        # =====================================================================

        log.info("[demo] Step 3 — agent raises dead_end signal (this becomes the ticket)")
        log.info("[demo]   binding to extraction trace session_id=%s", extraction_trace.session_id)
        extraction_trace.annotate(
            signal_type=SignalType.DEAD_END,
            intent="strict-json extraction with json_schema response_format",
            expected_outcome="successful 200 with parseable JSON matching the schema",
            workflow="structured-output-extraction",
            suggested_improvement=(
                "vendor/llm-3b-instruct returns 400 'Grammar must have a properties field' "
                "when called with response_format=json_schema, but the chat-completions Skill "
                "implies json_schema is broadly supported. Fix options: "
                "(a) expose a supports_json_schema flag on list_models() so agents can pre-filter; "
                "(b) update SKILL.md with the model capability matrix; "
                "(c) return a more actionable 400 ('model X does not support json_schema; use Y instead')."
            ),
            context={
                "model_attempted": "vendor/llm-3b-instruct",
                "feature_attempted": "response_format=json_schema",
                "error_class": type(failed_with).__name__,
                "error_message": str(failed_with),
                "skill_followed": "vendor-chat-completions",
                "user_prompt_summary": "extract product attributes as strict JSON",
            },
        )
        log.info("[demo]   ticket emitted (signal_type=dead_end)")

    log.info("[demo] done — flushing and closing")


if __name__ == "__main__":
    main()
