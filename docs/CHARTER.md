# Baton — Project Charter

*The load-bearing decisions, boundary disciplines, and open questions. This is the North Star doc — when in doubt, read this first.*

---

## 1. Purpose

Baton is the structured **signal** capture SDK that instruments agent–tool interactions on the vendor side — either by wrapping a vendor's MCP server (`install_baton(mcp, ...)` middleware) or by direct library-API calls in vendor code (`baton.Client` / `AsyncClient`) — captures the four things only an agent-using-a-tool has in one context (intent + tool call + observed outcome + expected outcome) plus friction signals, and ships an event stream to a collector for signal assembly and dispatch. It is the application layer above the tool-transport surface (MCP or direct API) — specifically the product-quality signal surface. **The SDK + the spec are the product; the collector is the durable workflow surface.**

**The shape of the protocol — agent-to-agent, not agent-to-human:**

```
   customer  ↔  customer agent  ↔  Baton  ↔  vendor agent  ↔  vendor support
     ↑          (Claude / Cursor /    (the protocol     (vendor's AI         (humans, last
   human         ChatGPT / Codex)      layer)            assistant)           resort)
```

Five entities, not three. **Baton is the protocol layer connecting two agent layers**, with humans on both ends. The vendor side has its own agent (triage, deflection, structured-action routing) that consumes Baton signals BEFORE human support kicks in. Human support is the fallback when the vendor's agent can't resolve.

The signal surface is wider than outages: failures are one of eight signal types, alongside retry loops, dead-ends, parameter confusion, slow performance, silent abandonment, feature gaps, and "other." The wider framing de-risks Anthropic absorbing structured handoff into MCP itself, because product-quality signal capture is not what MCP's retry semantics address.

---

## 2. Architecture

Baton ships as a standalone Python package (`baton-sdk`) installed as a vendor-side dependency. The SDK is intentionally separable from any specific vendor and exposes two integration paths: `install_baton(mcp, VendorConfig(...))` for vendors with FastMCP servers, and `baton.Client` / `AsyncClient` for vendors whose customers reach the vendor API via agent-generated code (Skills pattern, no MCP transport). Nothing about the SDK is special-cased per vendor.

The Console (hosted ingest + worker + storage + UI) lives in a separate sibling repository (`baton-console`) and is not required for the SDK to function. The SDK ships events to any HTTPS endpoint that accepts the wire format defined in `docs/SPEC.md` §11.4; the Console is one such consumer.

**Why a separate SDK package:** treating Baton as a separate package enforces the SDK boundary. If Baton lived as a folder inside any one vendor's repo, it would stop being a portable SDK and become vendor glue. The architectural premise is that *any* vendor can integrate Baton — via MCP middleware or the library API — without per-vendor code paths in the SDK.

---

## 3. SDK boundary rules

These are non-negotiable. They exist to keep Baton a real SDK.

1. **No vendor-specific imports anywhere in this repo.** Not in `src/baton/`, not in `tests/`, not in `examples/`. If anything ships in this repository that imports from a specific vendor module, the architecture is broken. CI enforces this. Examples use generic stubs that mirror common API shapes (e.g., OpenAI-compatible chat completions), not real vendor SDKs.
2. **Baton only sees what MCP transport carries.** Tool name, params, response, error, agent-supplied metadata (intent/expected, via a spec-defined mechanism). It does not have access to the vendor app's logs, request context, DB, or observability events. For richer signal, the *spec* defines a mechanism for vendors to provide it — the SDK does not reach across.
3. **The Baton spec lives in `docs/SPEC.md`, never in a vendor repo.** The JSON Schema for the signal and response payloads is the hero artifact. It travels with the SDK, not with any one vendor.
4. **Public API is the contract.** Anything exported from `src/baton/__init__.py` (or `src/baton/integrations/<name>/__init__.py`) is what vendors integrate against. Internal modules are off-limits to consumers. Changes to the public surface require a `CHANGELOG.md` entry; wire-format changes require a `SPEC.md §13` entry.
5. **Tests use fake-vendor fixtures only.** The test suite uses synthetic in-process fixtures — `pytest-httpserver` for HTTP capture, FastMCP's in-process `Client` for MCP integration tests, generic stub vendor SDKs in examples. Tests prove the SDK works for *any* vendor. If a Baton test imports a real vendor module, the test is wrong.
6. **Integration is minimal or the SDK is failing.** The single load-bearing ergonomic check. For the MCP path: one call to `install_baton(mcp, VendorConfig(...))` and that's the whole story. For the library API: a `Client(...)` constructor plus `with client.trace(...)` wrapping each call. If integration code grows substantially beyond these shapes, refactor Baton, not the vendor.
7. **Console doesn't bleed across.** The Console is what vendors see; the SDK is what end-user-side agent traffic flows through. They communicate only via the Baton wire format over HTTP. Code does not cross.
8. **PII scrubbing is non-negotiable.** All event payloads pass through a PII scrubber before emit. Vendors MUST configure scrub rules per SPEC §7 when handling sensitive end-user data; the SDK MUST NOT log unscrubbed payloads anywhere (stderr, stdout, files, exception messages). The current default scrubber is identity (no-op) — flagged loudly in §5 — so vendors handling sensitive data MUST supply their own via `VendorConfig(scrubber=...)` until the default scrubber lands with real rules.

---

## 4. Architectural decisions

Numbered architectural-decision records (ADR-style). Each entry captures the decision, the resolution status, and the rationale. Resolved entries are kept as historical record — they explain *why* the codebase is the way it is. Entries still in flux are marked accordingly.

### ADR-1: Identity / auth model

Start with vendor-issued API key + per-end-user consent token; upgrade to OAuth/DID later. The SDK accepts `VendorConfig(api_key=..., consent_token=...)`. v0 `consent_token` is a UUID granted at SDK init time; the wire format is designed so OAuth/DID can replace it without a breaking change. See SPEC §2.3.

### ADR-2: How does the agent supply `intent`, `expected_outcome`, and `signal_type`? — **RESOLVED in SPEC §5**

Layered approach (full mechanism in SPEC §5): agent-emitted values ride a vendor-namespaced annotation tool; client-attached values ride the MCP `_meta` field; nulls fall back. **Server-level `instructions` are an SDK requirement, not optional** (SPEC §5.1.2) — empirically validated: the annotation tool alone is insufficient to trigger agent behavior on the runtimes that honor server instructions (Claude Code, Cursor). Per-runtime support matrix in SPEC §5.1.3.

### ADR-3: Signal detection threshold — **RESOLVED in SPEC §6**

The SDK auto-detects only conservative cases (`signal_type=failure` on tool exception; `signal_type=retry_loop` on ≥3 same-params retries). The other six signal types are agent-raised via the annotation tool. Mechanism + roadmap items (auto-detection for `slow_performance` / `abandonment` / `dead_end`) in SPEC §6.

### ADR-4: SDK shape — fat capture vs thin emit-only — **RESOLVED: thin emit-only SDK + fat Console worker**

The SDK is a minimal event emitter; the Console worker is where interpretation, policy, correlation, detection, and dispatch happen. Sentry / Datadog / PostHog pattern. Full normative split (what the SDK MUST do vs what the Console MUST do, plus failure-mode posture + resilience properties) lives in SPEC §11; this entry captures the *rationale* for the choice.

**Why this beat the fat-SDK alternative:**

- Hot-path SDK state was the original justification for an SDK-side StateStore. With thin SDK, state lives in the event log; the SDK has nothing to remember.
- Policy changes don't require SDK redeploy in the vendor's MCP server — the vendor updates policy in the Console UI; the worker hot-reloads. Operational win.
- Channels move to the Console — the vendor's MCP server holds no outbound credentials (third-party API keys, webhooks, etc. all live in Console secrets). Cleaner trust model.
- Event log is canonical; signals are derived. Enables replay, retroactive corrections, debugging, future analytics rework without breaking historical data.
- Matches the Sentry / Datadog / PostHog industry pattern. Not inventing.
- OSS-only path is honest: install the SDK alone, point it at any HTTPS endpoint, get a clean event stream. The rich product (signal stitching + policy + dispatch) is the Console paid tier.

**What the thin SDK preserves:** the four-things-in-one-context payload (intent + tool_calls + observed_outcomes + expected_outcomes + friction signals) still reaches the vendor with full agent authorship. The SDK emits each thing as events; the Console worker stitches them into the canonical SignalPayload. Same rich content, same fidelity, same vendor-facing shape — assembly just happens at a different layer.

---

## 5. Known limitations + deferred work

SDK-package-level limitations deliberately deferred from the current release. Each has a documented gate for revisiting. (Wire-format / spec-level open questions live in [SPEC §14](SPEC.md).)

| Limitation / shortcut | When to revisit | Why deferred |
|---|---|---|
| `consent_token` is a single UUID per SDK init (single-end-user model) | Before multi-end-user deployment; real consent flow is ADR-1 | Current design assumes a single end user per SDK process; per-end-user OAuth/DID is wire-format-compatible. |
| Editable install for vendor-side development | Before integrators are external | Loses versioned-contract discipline; fine for in-house integration. |
| No SOC2 / no audit-log export / no RBAC | Before mid-market deployment | ~12 weeks of compliance work; not load-bearing for the SDK package. |
| Synchronous return channel (agent autopickup of vendor responses) deferred | When real integrator feedback shows it matters more than out-of-band notifications | Building it requires Console state machine + SDK polling + synthetic-injection semantics + stale-response handling — all benefit from real usage data on what response shape matters. The current release ships SPEC §8.1 async out-of-band notification (email/Slack/push to the end user, who re-engages the agent). Costs one extra human turn; works today. |
| Default PII scrubber is identity (no-op) — the *enforcement* is non-negotiable per §3 rule 8, but the *default behavior* is a passthrough | Before any integrator handles sensitive end-user data | Real default scrub rules land in a subsequent release. Vendors handling sensitive data MUST supply a custom scrubber via `VendorConfig(scrubber=...)` today; the SDK enforces it on every event but doesn't ship sensible defaults yet. |

---

## 6. Spec evolution log

Architectural decisions baked into the current SPEC, dated for traceability.

| Decision | Date | Notes |
|---|---|---|
| Vocabulary + schema sweep to "signal" terminology | 2026-05-13 | `incident`→`signal`, `Resolution`→`Response`; added `signal_type` enum (8 values) and `friction_signals`; widened status enum with `documented`/`feature_filed`/`educated`; added `doc_pointer`. |
| Server `instructions` made an SDK requirement (SPEC §5.1.2) | 2026-05-13 | Empirical finding: the annotation tool alone is insufficient to trigger agent behavior. Adding FastMCP `instructions=` (templated from `vendor_display_name`) flipped behavior on runtimes that honor instructions — agents reliably call annotate proactively + reactively with high-quality content. |
| `_meta` plumbing verified | 2026-05-13 | Claude Code populates `_meta` end-to-end with `claudecode/toolUseId` (per-tool-invocation UUID) + `progressToken` (per-request sortable int). SPEC §5.2 has the per-runtime adapter table. Implementation note: FastMCP exposes `_meta` as a structured `Meta(...)` object, not a dict; SDK must call `.model_dump()`. |
| `workflow` + `suggested_improvement` promoted to top-level annotation fields | 2026-05-13 | Free-form `context` testing showed two keys with consistent recurrence: `workflow` (proactive — session-stable broader-task label) and `suggested_improvement` (reactive — agent-authored product feedback; the moat field). Both promoted to top-level in the annotation signature and signal payload (SPEC §3.1, §5.1.1). |
| Server-instructions framing: explicit MUST + (REQUIRED) markers | 2026-05-13 | Empirical iteration: anti-duplication framing ("do NOT duplicate") backfired (agents stopped populating top-level fields). Explicit MUST + (REQUIRED…) markers on each top-level field plus positive "supplementary" framing for `context` produced reliable structured output without duplication. The validated text is the SPEC §5.4 `server_instructions` default template. |
| Per-runtime support matrix added (SPEC §5.1.3) | 2026-05-13 | **Key finding: Claude Desktop does NOT surface MCP server `instructions` to the LLM** — the load-bearing motivation mechanism for §5.1.2 is honored by Claude Code and Cursor but not Desktop. Desktop CAN call annotate when explicitly prompted (workflow + signal_type populate correctly; suggested_improvement stays null unless told). `_meta` is absent on Desktop entirely. Documented for honest cross-runtime expectations. |
| Cursor validated as equivalent to Claude Code | 2026-05-13 | Cursor surfaces `instructions` to the LLM identically to Claude Code — proactive + reactive annotation fires unprompted with all top-level fields populated. `_meta` carries only `progressToken` (no Cursor-specific stable correlation key like Claude Code's `claudecode/toolUseId`). Both runtimes validate the SPEC §5.1.2 design. |

---

## 7. References

- **`docs/SPEC.md`** — the Baton wire protocol (canonical).
- **`README.md`** — first-time-visitor entry point.
- **`CHANGELOG.md`** — SDK package changelog (wire-format changes are recorded in `SPEC.md §13`).
- **`examples/`** — runnable usage examples (the library API skill demo, the e2e smoke test).
- **Sibling repository:** `baton-console` (separate repo) for the Console worker + UI.

---

*Changes to this charter should be made deliberately. The charter is meant to be stable across weeks; if it churns, the project is churning.*
