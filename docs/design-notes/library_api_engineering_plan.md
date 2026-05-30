# Library-API prototype (spike)

*Opened 2026-05-27. Engineering plan for prototyping the `baton.Client` library API per `docs/SKILLS_LIBRARY_API_DRAFT.md`.*

> **⚠️ Prerequisite (added 2026-05-28):** `docs/design-notes/integration_reorg.md` should land **before** this spike's phase 2 (Core `Client`) starts. The reorg moves existing MCP code to `baton.integrations.mcp.*` and sets up pip extras, so the library API code lands in `src/baton/client.py` at the core level (not nested under `integrations/`). If reorg lands after library API, we'd have to move things twice OR ship the library API with awkward placement that gets fixed later. Reorg is mechanical and low-risk; ~2-3 days; do it first.

---

## Why

Two reasons stack:

1. **Architectural direction.** Multiple independent signals across the agent ecosystem (Anthropic's own "code execution with MCP" research, public conversations at major cloud vendors, and design-partner-level patterns) point to **Skills as the architectural unit for app-like functionality inside agents.** Skills bundle the workflow + tools + context, and they often invoke vendor APIs via SDK-direct calls from agent-generated code — bypassing the MCP transport boundary entirely.
2. **The SDK-direct corner case is real.** For vendors whose customers reach the vendor API via Skill-generated code (not MCP tool calls), the existing MCP middleware path captures **zero** of the customer friction. The library API in `SKILLS_LIBRARY_API_DRAFT.md` is the only capture path that works for that ICP segment.

---

## Scope

**In:** the core library API surface from `SKILLS_LIBRARY_API_DRAFT.md` §"Public surface":

- `Client(...)` — entry point with config loading
- `client.trace(...)` — context manager for one logical call
- `trace.with_params(...)` — optional params capture
- `trace.observed(...)` — record outcome
- Exception path — automatic `tool_call_error` emit on exception inside the `with` block
- `client.annotate(...)` — reactive friction signal
- `SignalType` enum
- `client.close()` — graceful shutdown
- Async equivalents: `AsyncClient`, async `trace`, `aclose()`

**Out (explicitly deferred per the draft):**

- Decorator form (`@client.trace(...)`) — "v0.3 candidate — not committed"
- Auto-instrumentation (httpx hooks etc.) — "v0.3 candidate — not committed"

**Out (different work):**

- Any vendor-side Skill published into a vendor's Skills repository — that's partnership coordination work, not SDK work.

---

## Phase 1 — Design decisions (must resolve before code)

These shape the API surface and aren't worth coding-then-redesigning. Lock these before phase 2 starts.

| # | Decision | Recommended | Reasoning |
|---|---|---|---|
| 1 | Sync-only or sync + async from day one | **Both from day one** — `Client` and `AsyncClient`, same surface | Most modern vendor SDKs ship an async-first interface. Sync-only Baton library can't compose with `async def` Skill code naturally. Small additional cost; avoids painful migration later. |
| 2 | Config loading model | **Both explicit kwargs AND env-var fallback** | Explicit (`Client(api_key=..., ingest_url=..., vendor_id=...)`) wins for testability and production. Env-var fallback (`BATON_API_KEY`, `BATON_INGEST_URL`, `BATON_VENDOR_ID`) for ergonomic dev. Matches standard SDK conventions. |
| 3 | `consent_token` flow in library mode | **At `Client(...)` init (static per-process); per-trace optional override** | MCP path resolves consent_token from `VendorConfig` (per CHARTER OD-2 v0.1: static UUID per init). Library mode has no MCP session; init-time matches the existing v0.1 single-user UUID model. Per-trace override (`client.trace(..., consent_token=...)`) supports v0.2 per-end-user issuance per SPEC §2.3. |
| 4 | Scrubbing in library mode | **Reuse `src/baton/scrub.py`** (currently no-op identity function) with the same vendor-config rules | Architectural consistency — one scrub primitive controls both capture paths. When the real scrubber lands (Day 4+ per current scrub.py comment), both paths benefit. |

Effort: **~0.5 day discussion + commit decisions to this PLAN.md.**

---

## Phase 2 — Core `Client` (sync)

- New file: `src/baton/client.py`
- `Client` class: init, config loading (kwargs + env-var fallback), EventEmitter wiring
- Reuses: `src/baton/emitter.py` (`EventEmitter` + bounded buffer + retry), `src/baton/events.py` (event schema dataclasses), `src/baton/scrub.py` (identity scrubber for now)
- `client.close()` — graceful flush of pending events

Effort: **0.5 day.**

---

## Phase 3 — `Trace` context manager (sync)

- Same file as `Client`
- `trace = client.trace(intent="...", expected_outcome="...", workflow="...", tool_name="...")`
- `with trace:` enters → emit `tool_call_start` event
- `trace.with_params({...})` — optional, sets params on the start event (must be called inside the `with` block before exit)
- `trace.observed(result)` or `trace.observed(error_type=..., error_body=...)` — record outcome
- `with` block exits cleanly → emit `tool_call_end` event with the observed outcome
- `with` block exits via exception → emit `tool_call_error` event automatically with exception type + traceback

Edge cases:
- Multiple `observed()` calls in one trace — last wins, warn on subsequent
- Exit without `observed()` — emit `tool_call_end` with `outcome=null`, warn
- Exception path: capture exception → emit error event → re-raise (don't swallow)

Effort: **0.5 day.**

---

## Phase 4 — `client.annotate(...)`

- `client.annotate(signal_type=SignalType.DEAD_END, suggested_improvement="...", context={...})`
- Emits an annotation event (reuses `AnnotationEvent` from `events.py`)
- `SignalType` enum exported at package level (`from baton import SignalType`)

Effort: **0.5 day.**

---

## Phase 5 — Async equivalents

- New file: `src/baton/aclient.py` (or fold into `client.py` — decide during code review)
- `AsyncClient` with `async def __init__` equivalent (factory function `await AsyncClient.create(...)`)
- `async with client.trace(...) as trace:` — async context manager
- `await client.annotate(...)` — async annotation emit
- `await client.aclose()` — async graceful shutdown
- Mirror the sync surface exactly; same kwargs, same behavior, same event schema

Effort: **0.5 day.**

---

## Phase 6 — Tests

- New files: `tests/test_client.py`, `tests/test_aclient.py`
- Pytest with fake EventEmitter assertions (mirror existing test structure in `tests/test_emitter.py`)
- Coverage:
  - Happy path: `with trace: trace.observed(...)` emits 2 events
  - Exception path: exception inside `with` emits `tool_call_error` and re-raises
  - `client.annotate(...)` emits annotation event
  - Sync + async parity
  - Config loading: explicit kwargs win, env-var fallback works
  - `consent_token` resolution: init value, per-trace override

Effort: **1 day.**

---

## Phase 7 — E2e spike

- New file: `examples/library_api_smoke_test/smoke_test_library.py` — synthetic chat-completions-shaped script
- Pattern: import `baton.Client`, call a stub function that pretends to be `vendor.chat.completions.create(...)`, capture both happy and failing cases with `client.trace(...)`
- Run against a `local_ingest.py` HTTP capture server — proves both capture paths share the same Console ingest contract
- Add a `README.md` documenting what the spike validates

Output assertions:
- Events arrive at `local_ingest` with correct schema
- `tool_call_start` + `tool_call_end` shape matches MCP-path events (same fields, just different `agent_runtime` value like `"python-library"`)
- Annotation events emit cleanly

Effort: **0.5 day.**

---

## Total effort

**Phases 1-7: ~3.5-4 days of focused engineering.**

Recommendation: lock phase 1 decisions in a single morning; do phases 2-5 in two focused days; tests + spike on day 3-4.

---

## Dependencies

**Existing code to reuse (no changes needed):**
- `src/baton/emitter.py` — `EventEmitter` with bounded buffer + retry + backoff
- `src/baton/events.py` — event dataclasses + JSON serialization
- `src/baton/scrub.py` — identity scrubber (no-op; will replace later)
- `src/baton/_state.py` — `SessionCounter` for monotonic `sequence_number`

**Existing code untouched (paths assume `integration_reorg/PLAN.md` has landed first):**
- `src/baton/integrations/mcp/install.py` — MCP `install_baton(mcp, VendorConfig(...))` entry point. Library API is purely additive.
- `src/baton/integrations/mcp/middleware.py` — MCP middleware. Unchanged.
- `src/baton/integrations/mcp/annotation.py` — MCP-side annotation tool. The library API's `client.annotate(...)` is a separate code path.
- `src/baton/integrations/mcp/runtime_adapter.py` — MCP `_meta` adapter table. Library mode has no `_meta`; sets `agent_runtime` from the `Client(...)` config or env var.

**SPEC alignment:**
- Events emit per SPEC §11.4 envelope (`event_id`, `event_type`, `session_id`, `correlation_mode`, `tenant_id`, `sequence_number`, `captured_at`, `spec_version`, `sdk_version`, `agent_runtime`, `trace_context`, `payload`)
- `correlation_mode` defaults to `per-event` in library mode for v0.1 (library mode doesn't have a natural session boundary; `session-stitched` becomes optional v0.2+ work when the Skill caller passes an explicit session_id)
- `agent_runtime` value: lean `"python-library"` (parallels `"claude-code"`, `"cursor"`)

---

## Success criteria

- [ ] `from baton import Client, AsyncClient, SignalType` works
- [ ] `with client.trace(intent="...", expected_outcome="...") as trace: trace.observed(result)` emits exactly two events (`tool_call_start`, `tool_call_end`) to the configured ingest endpoint
- [ ] Async equivalent works: `async with client.trace(...) as trace: trace.observed(result)`
- [ ] Exception inside the `with` block emits `tool_call_error` automatically and re-raises the exception
- [ ] `client.annotate(signal_type=SignalType.DEAD_END, suggested_improvement="...", context={...})` emits an annotation event
- [ ] Tests cover the full surface (sync + async parity)
- [ ] E2e spike: synthetic chat-completions-shaped script sends events that land in `local_ingest.py` and match the expected SPEC §11.4 envelope shape
- [ ] No changes to existing MCP path; both paths coexist cleanly

---

## What this prototype proves (and what it doesn't)

**Proves:**
- The library API surface in `SKILLS_LIBRARY_API_DRAFT.md` is buildable on the existing SDK foundation
- Events from library-mode and MCP-mode share the same schema and land in the same Console ingest (architectural unification per CHARTER OD-7)
- Both capture paths share the same `POST /v0/events` ingest contract

**Does NOT prove:**
- That a real customer's agent will reliably call `client.trace(...)` in production Skills usage (this is the Skill-design + adherence question; partnership-shaped, not engineering-shaped)
- That session correlation under streamable HTTP works for library-mode (per SPEC §3.4, library mode defaults to `per-event` correlation_mode for v0.1; session-stitched is v0.2 work conditional on Skill-author cooperation passing explicit session_ids)
- That the consent_token model scales beyond v0.1's single-static-UUID (v0.2 `POST /v0/consent` work is separate)

---

## Next-step gate

Before phase 2 starts, lock the four phase-1 decisions either:
1. In this file (append `## Phase 1 — Decisions LOCKED 2026-MM-DD` section), OR
2. In a short Slack / async discussion documented in commit message of the first phase-2 commit

Either is fine; just don't skip the decision lock.

---

## Phase 1 — Decisions LOCKED 2026-05-28

All four phase-1 design decisions ratified per the recommendations in the table above. No deviations.

1. **Sync + async from day one.** `Client` and `AsyncClient`, same surface. Most modern vendor SDKs are async-first; sync-only Baton can't compose with `async def` Skill code naturally. Small additional cost up front; avoids painful migration later.
2. **Explicit kwargs + env-var fallback for config.** `Client(api_key=..., ingest_url=..., vendor_id=..., consent_token=...)` wins for testability. Env-var fallback (`BATON_API_KEY`, `BATON_INGEST_URL`, `BATON_VENDOR_ID`, `BATON_CONSENT_TOKEN`) for ergonomic dev. Matches standard SDK conventions.
3. **`consent_token` at `Client(...)` init; per-trace optional override.** Matches v0.1 single-user UUID model (CHARTER OD-2). Per-trace override (`client.trace(..., consent_token=...)`) supports v0.2 per-end-user issuance per SPEC §2.3.
4. **Reuse `src/baton/scrub.py`** (currently no-op identity function) with same vendor-config rules. One scrub primitive controls both capture paths.

Decisions locked → phase 2 starts.

---

## Cross-references

- `docs/design-notes/integration_reorg.md` — **prerequisite reorg** that lands before this spike's phase 2 starts
- `docs/SKILLS_LIBRARY_API_DRAFT.md` — full surface design (this PLAN.md scopes the build from that draft)
