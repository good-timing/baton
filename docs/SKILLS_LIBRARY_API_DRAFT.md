# Baton library API for Skill-instrumented agent code — draft

*v0 draft, 2026-05-19. For vendors whose customers reach the vendor API via agent-generated code (Skills pattern), not via MCP tool calls. The Baton SDK exposes a parallel library API alongside the FastMCP middleware so events emit cleanly from either surface.*

**Status: v0.3 candidate (2026-05-27).** Direction is validated by multiple independent signals converging on Skills as the architectural unit for app-like functionality inside agents; the library API is the capture path for vendors whose customers reach the vendor API via Skill-generated code (no MCP transport). Architecture compatible with CHARTER OD-7 (thin-emit SDK) — uses the same EventEmitter; same event schema (SPEC §11.4); same Console worker.

---

## Why a library API alongside MCP middleware

The MCP-wrapping path (`install_baton(mcp, ...)` + middleware) works for vendors who expose their API as MCP tools.

For vendors using the **Skills pattern**: customers' agents generate code that calls the vendor's HTTP API directly. No MCP transport involved. The vendor publishes Skills (markdown patterns) teaching the agent how to write correct code. The library API is the only capture path that works in this shape — middleware on an MCP server can't see calls that never go through MCP.

To instrument that code with Baton, the agent's generated code needs to **import and call the SDK directly** — not register middleware. Hence the library API surface.

Both paths feed the same Console: same `events` table, same correlation rules (SPEC §11.5), same SignalPayload shape (SPEC §3), same policy and dispatch.

---

## Design principles

1. **Minimal cognitive overhead.** Agent-generated code already wraps the vendor API call; adding Baton instrumentation should feel like adding a `with` block around it, not restructuring the code.
2. **Same EventEmitter underneath.** No fork in event-handling logic. The library calls and the MCP middleware both emit the same event types into the same buffer.
3. **Faithful to the four-things-in-one-context payload.** `intent` + `expected_outcome` arrive on the trace; tool call + outcome are observed inside the trace; reactive annotations layer on top. Same as MCP path.
4. **Graceful degradation.** If the Console is unreachable, agent code still works (buffer fills → drops oldest → UserWarning per CHARTER OD-7). The agent doesn't see Baton failures.
5. **Idiomatic Python.** Context managers for trace lifetimes; async-first; type-hinted; works in either sync or async code.

---

## Public surface

```python
from baton import Client, SignalType
```

### `Client(...)` — entry point

```python
client = Client(
    vendor="together",                    # vendor_id; matches the Console tenant
    console_url="https://...",            # ingest endpoint
    api_key="...",                        # bearer token for ingest auth
    session_id=None,                      # optional; auto-generated if None
    agent_runtime="unknown",              # caller's best guess; "claude-code" / "cursor" / ...
    buffer_size=1000,                     # bounded local event buffer (CHARTER OD-7)
    timeout_seconds=1.0,                  # per-request POST timeout
)
```

Constructed once at the top of the agent's code. Holds the EventEmitter, the per-session sequence counter, and the connection to the Console ingest endpoint.

### `client.trace(...)` — context manager for one tool call

```python
with client.trace(
    tool="chat.completions.create",
    intent="summarize this PR comment thread for the maintainer",
    expected_outcome="a 2-3 sentence paragraph capturing the decision and any open questions",
    workflow="code-review-summary",        # optional; session-stable
) as trace:
    response = together.chat.completions.create(
        model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        messages=[...],
    )
    trace.observed(response)
```

Emits, in order:
1. `annotation` event with `intent` + `expected_outcome` + `workflow` (proactive — same shape as MCP-side proactive annotation)
2. `tool_call_start` event with `tool` name + (optionally) params if `trace.with_params(...)` was called
3. Inside-context body executes; agent calls vendor API
4. `trace.observed(response)` records the outcome (params: response, optional `error_type`, optional `duration_ms` — auto-computed if not provided)
5. On context exit: `tool_call_end` event (or `tool_call_error` if an exception propagated)

If the agent never calls `trace.observed(...)` and exits cleanly: emit `tool_call_end` with empty outcome (defensive — better incomplete than missing).

### `trace.with_params(...)` — optional params capture

```python
with client.trace(tool="chat.completions.create", intent=..., expected_outcome=...) as trace:
    trace.with_params(model="...", messages=[...])  # captured in tool_call_start payload
    response = together.chat.completions.create(...)
    trace.observed(response)
```

Optional because agent-generated code may not want to mirror its params verbatim (PII / size concerns). PII scrubbing applies if configured.

### `trace.observed(...)` — record outcome

Three modes:

```python
# Success path
trace.observed(response)

# Failure path (preferred) — pass the exception object
try:
    response = together.chat.completions.create(...)
    trace.observed(response)
except TogetherTimeoutError as exc:
    trace.observed(error=exc)   # trace derives error_type + error_body

# Failure path (advanced) — when you only have stringified info
trace.observed(error_type="TogetherTimeoutError", error_body="...")
```

Records the outcome into the trace. On context exit:
- Success → `tool_call_end` event with the buffered result + `duration_ms`.
- Failure → `tool_call_error` event with `error_type` + `error_body`.

Use `error=exc` (not the explicit string form) inside an `except` block when you want to record the failure and **continue** the function (e.g., to emit a reactive `trace.annotate(...)` ticket). If you want the exception to propagate normally instead, don't catch it — `__exit__` auto-emits `tool_call_error` and re-raises (see "Exception path — automatic" below).

If both `error=` and explicit `error_type`/`error_body` are passed, the explicit values win — useful for re-classifying (`HTTPError` → `RateLimitExceeded`) or scrubbing the body before storage.

### Exception path — automatic

```python
with client.trace(tool="chat.completions.create", intent=..., expected_outcome=...) as trace:
    response = together.chat.completions.create(...)   # raises TogetherTimeoutError
    trace.observed(response)
# context __exit__:
#   - sees exception
#   - emits tool_call_error event with error_type="TogetherTimeoutError" + error_body
#   - re-raises so calling code handles it normally
```

The agent doesn't need to catch the exception just to emit Baton events; the context manager handles it. If the agent DOES catch and recover, it can call `trace.error(exc)` explicitly before continuing.

### `client.annotate(...)` — reactive friction signal (standalone)

```python
client.annotate(
    signal_type=SignalType.DEAD_END,
    suggested_improvement="the response was empty; the prompt should have produced content",
    context={"prompt_length": 500, "response_length": 0, "likely_cause": "content_filter"},
)
```

Emits an `annotation` event with the friction signal. **Standalone** — not tied to any trace. Each standalone call mints a fresh `session_id` (library mode defaults to per-event correlation per SPEC §3.4).

Use when no preceding trace is the referent — e.g., an agent flags a friction that didn't come from a specific tool call. For the common "this ticket is about *that* failed trace" pattern, prefer `trace.annotate(...)` (below), which binds the session.

### `trace.annotate(...)` — reactive friction signal (bound to a trace)

```python
with client.trace(...) as trace:
    try:
        ...
    except SomeError as exc:
        trace.observed(error_type=type(exc).__name__, error_body=str(exc))

# After the with block — trace var stays in scope per Python's with semantics.
trace.annotate(
    signal_type=SignalType.DEAD_END,
    suggested_improvement="...",
    context={...},
)
```

Equivalent to `client.annotate(session_id=trace.session_id, ...)`. The emitted annotation shares the trace's `session_id` and continues its sequence counter — so the Console worker can trivially join the ticket back to the trace it's about, without timestamp heuristics.

This is the canonical Baton hero pattern: tool call fails → trace records the error → agent decides "this is friction" → `trace.annotate(...)` ships the structured ticket bound to the failure.

### `trace.session_id` — public correlation handle

```python
with client.trace(...) as trace:
    ...
print(trace.session_id)  # uuid7 — stable across the trace's events
```

Exposed for callers who want to wire correlation themselves (cross-thread, cross-process, etc.). Prefer `trace.annotate(...)` for the common case.

### `SignalType` enum

```python
SignalType.FAILURE
SignalType.RETRY_LOOP
SignalType.DEAD_END
SignalType.PARAMETER_CONFUSION
SignalType.SLOW_PERFORMANCE
SignalType.ABANDONMENT
SignalType.FEATURE_GAP
SignalType.OTHER
```

Matches SPEC §3.1 vocabulary. Agents pass these by string in code; the enum exists for type-safety in libraries that consume the SDK.

### `client.aclose()` / `client.close()` — graceful shutdown

```python
# sync
client.close()

# async
await client.aclose()
```

Flushes the local buffer; cancels in-flight retries cleanly. Should be called at the end of an agent's code-execution context (some agent runtimes tear it down automatically; local code calls it explicitly).

---

## Async equivalents

Everything has an async counterpart with the same names:

```python
async with client.trace(
    tool="chat.completions.create",
    intent="...",
    expected_outcome="...",
) as trace:
    response = await together_async.chat.completions.create(...)
    trace.observed(response)

await client.annotate(signal_type="dead_end", suggested_improvement="...")
await client.aclose()
```

The EventEmitter is already async under the hood (uses `httpx.AsyncClient`); both sync and async user-facing APIs share the same emitter.

---

## Worked example: Together inference call with Baton

Agent-generated code following a hypothetical Baton-aware Together Skill:

```python
import os
from baton import Client, SignalType
from together import Together

client = Client(
    vendor="together",
    console_url=os.environ["BATON_CONSOLE_URL"],
    api_key=os.environ["BATON_API_KEY"],
    agent_runtime="claude-code",   # passed by the Skill content
)

together = Together()

def summarize_pr_comments(comments: list[str]) -> str:
    with client.trace(
        tool="chat.completions.create",
        intent="summarize a PR comment thread for the maintainer",
        expected_outcome="2-3 sentence paragraph capturing the decision",
        workflow="code-review-summary",
    ) as trace:
        try:
            response = together.chat.completions.create(
                model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
                messages=[
                    {"role": "system", "content": "Summarize PR comments..."},
                    {"role": "user", "content": "\n".join(comments)},
                ],
            )
            trace.observed(response)
            summary = response.choices[0].message.content

            # agent notices the summary is empty
            if not summary.strip():
                client.annotate(
                    signal_type=SignalType.DEAD_END,
                    suggested_improvement="content filter likely triggered; surface a clearer error so the agent can adjust the prompt",
                    context={"prompt_length": sum(len(c) for c in comments), "response_length": 0},
                )
                return "[summary unavailable]"

            return summary
        except Exception as exc:
            # tool_call_error already emitted by trace context; just propagate
            raise

# main:
result = summarize_pr_comments(["...", "..."])
client.close()
```

What the Console sees (events):
1. `annotation` (proactive: intent + expected_outcome + workflow)
2. `tool_call_start` (tool=`chat.completions.create`, params if captured)
3. `tool_call_end` (response observed)
4. `annotation` (reactive: signal_type=dead_end + suggested_improvement + context) — if the summary was empty
5. Console worker stitches these into a SignalPayload per SPEC §11.5; vendor policy decides action(s) per SPEC §11.6

---

## Decorator form (v0.3 candidate — not committed)

For wrapping whole functions without restructuring:

```python
@client.observe(
    tool="chat.completions.create",
    intent_from_arg="user_prompt",       # pull intent from a function argument
    expected_outcome="a relevant response",
)
def call_together(user_prompt: str):
    return together.chat.completions.create(...)
```

Decorator wraps the function in an implicit `trace()` context. Useful for code that doesn't naturally have inline `with` blocks (e.g., function pipelines).

Deferred until usage patterns from real partners show whether this is needed.

---

## Contextvar-based annotation auto-binding (v0.3 candidate — not committed)

The v0.2 surface for the failure → ticket pattern is `trace.annotate(...)` (explicit binding via the trace variable). It works, but the call-site still requires the caller to keep the trace var in scope across the with-block boundary. A future polish is to add an implicit "current trace" using `contextvars.ContextVar`:

```python
with client.trace(...) as trace:
    try:
        ...
    except SomeError:
        ...  # trace.observed(error_*)

# trace var no longer needed — client.annotate finds the most-recent trace
# in this async/task context automatically.
client.annotate(signal_type=SignalType.DEAD_END, ...)
```

Tradeoffs:

- **For:** cleaner call sites; matches how OpenTelemetry's `tracer.start_as_current_span` works; lets `client.annotate(...)` be the single API.
- **Against:** introduces implicit state the caller can't see; ambiguous semantics around nested traces and post-close calls (does `annotate` after several traces bind to the *last entered* or *last exited*?); harder to reason about in async fan-out patterns.

**Decision rule:** ship if real-world Skill-author feedback shows the explicit `trace.annotate(...)` is noisy enough to discourage the reactive-ticket pattern. Until then, keep the surface explicit — implicit binding is harder to remove than to add.

Origin of the trade-off: spike `examples/skill_demo/` (2026-05-28). The dogfood pass surfaced that orphaned reactive annotations (RE-02 in the spike's `ROUGH_EDGES.md`) were a P0 — `trace.annotate(...)` + `trace.session_id` shipped as the v0.2 fix; contextvar deferred here.

---

## Auto-instrumentation (v0.3 candidate — not committed)

OpenTelemetry-style auto-instrumentation that detects `together.*` calls and wraps them automatically:

```python
from baton.instrumentations.together import auto_instrument
auto_instrument(client=client)

# Now all subsequent together.* calls emit Baton events without explicit trace() blocks.
# Agent code is unmodified.
```

This is much more invasive — has to monkey-patch the Together client library. Defer to v0.3 unless a partner explicitly asks. Adopts the standard OTel auto-instrumentation pattern; could ship adapters for Anthropic, OpenAI, Together, etc.

---

## Comparison: MCP middleware path vs library API path

| Concern | MCP middleware (`install_baton(mcp, ...)`) | Library API (`Client(...)`) |
|---|---|---|
| Setup | Vendor's MCP server adds 5 lines | Vendor publishes a Skill teaching agents the pattern |
| Where instrumentation lives | Vendor side (in MCP server runtime) | Agent side (in agent-generated code) |
| Reliability | Deterministic — middleware runs on every tool call | Soft — depends on agent following the Skill |
| Annotation surface | MCP tool (`vendor_annotate`) with MUST/REQUIRED framing | Python function calls (`client.annotate(...)`) |
| Auto-detection (failure / retry_loop) | Worker-side queries events table (per OD-7) | Same — worker-side, no SDK difference |
| Vendor API call captured? | Yes (vendor controls MCP server) | Yes (agent calls vendor API from inside trace context) |
| Cost overhead per call | ~5ms middleware overhead | ~1ms (in-process function call + async event emit) + agent tokens to write the code |
| PII scrubbing | At MCP middleware (vendor's process) | At library call (agent's execution environment) — same scrubbing layer |
| Where partner has to invest | Wire SDK into their MCP server | Author + maintain a Baton-aware Skill |
| Best fit | Vendors with rich MCP servers as their primary surface | Vendors using Skills as their primary distribution |

**Both paths emit identical events to the same Console.** The Console worker doesn't care about the source; correlation + policy + dispatch are unchanged.

---

## Skill content (what a vendor publishes)

A Baton-aware vendor Skill would be a markdown file in the vendor's Skills repository (or registry entry). Conceptual structure:

```markdown
# Baton-instrumented vendor API usage

When writing code that calls the vendor API, instrument it with Baton so
the vendor's support team can see structured signals from your agent's work.

## Setup

`pip install baton-sdk <vendor-sdk>`

## Pattern

Wrap each vendor API call in a `client.trace(...)` block that includes
the user's intent and your expected outcome. On friction, call
`client.annotate(signal_type=..., suggested_improvement=...)`.

## Examples

[Three or four canonical examples covering the vendor's main API surfaces.]

## Why this matters

Your `intent` and `expected_outcome` are the four-things-in-one-context
payload only YOU have access to. When the tool produces unexpected results,
the vendor's team sees what you were trying to do — not just the failure mode.
```

The Skill is the carrier; the SDK is the library. The vendor owns the Skill content; we maintain the SDK.

---

## Cross-thread connections

- **OD-7 thin-emit architecture** — directly compatible. Library API uses the same EventEmitter as MCP middleware; same buffer + retry semantics + idempotency.
- **Annotation cost knobs (EXPLORATIONS thread)** — library API has the same LLM-turn cost concerns. Agent's code has to include `trace()` + `annotate()` calls; each costs tokens to write. The "annotation-on-signal-only" knob translates: Skill teaches reactive annotation but skips proactive `intent`/`expected_outcome` for cost-sensitive deployments.
- **Governance + auditability (EXPLORATIONS thread)** — Console-pushed scrub policy applies to library-emitted events too; SDK enforces locally before emit. Same mechanism.
- **Developer hero cases (EXPLORATIONS thread)** — observability + cost visibility ALSO compose with library mode: any code calling `client.trace(...)` produces the event stream the observability product needs.

---

## Open design questions

1. **Sync vs async API.** Both. The EventEmitter is already async; sync `Client` is a small adapter on top.
2. **PII scrubbing surface.** Library-side scrub config — `Client(scrub_rules=...)` — same shape as `VendorConfig` in MCP path? Or push from Console at session start? Lean: push from Console (matches the governance thread's design).
3. **What about agent-supplied `_meta` fields?** MCP path gets agent_runtime + session_id from MCP `_meta`. Library path must supply these explicitly. Defaults to "unknown" + auto-generated UUIDv7 session_id; Skill content can teach the agent to populate them.
4. **Stateless library helper functions vs class-based?** Lean class-based (`Client` instance) — cleaner state management for the buffer + session counter. Function-style (`baton.trace(...)`) is tempting but loses statefulness.
5. **What does the Skill's `install` command actually do?** Bare minimum: documents the import + env-var setup. v0.3 candidate: a `baton init --vendor=<vendor-id> --skill` CLI that generates a starter file.
6. **Does this conflict with the MCP-side annotation tool's CHARTER §4 boundary discipline?** No — the SDK is still vendor-agnostic. The agent's code is the consumer; the agent's runtime decides what to do with the SDK.

---

## When to commit this to scope

**Not v0.1 / v0.2 default scope.** The MCP path is the validated integration shape. The library API graduates from speculative to committed after:

1. **Design-partner validation** that the vendor's actual customer friction lives in Skill-driven code execution (not exclusively in MCP transport or in the support funnel prior to MCP).
2. **Vendor DevRel review** of the Baton-instrumentation Skill design — adherence is softer for Skills than MCP; we need vendor confidence that customers will follow the pattern.
3. **A second vendor** with Skills-pattern usage corroborates the integration shape.

If all three: ship the library API in v0.3 alongside the MCP middleware. Same Console; same EventEmitter; minimal architecture growth.
