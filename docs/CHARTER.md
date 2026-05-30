# Baton — Project Charter

*v0.1, originally 2026-05-13. Public release sweep 2026-05-29. The load-bearing decisions, boundary disciplines, and open questions. This is the North Star doc — when in doubt, read this first.*

---

## 1. Purpose

Baton is the structured **signal** capture SDK that wraps a vendor's MCP server, captures the four things only an agent-using-a-tool has in one context (intent + tool call + observed outcome + expected outcome) plus friction signals, and ships signal handoffs to the vendor's Console. It is the application layer on top of MCP — specifically the product-quality signal surface. **The SDK + the spec are the product; the Console is the durable workflow surface.**

**The shape of the protocol — agent-to-agent, not agent-to-human:**

```
   customer  ↔  customer agent  ↔  Baton  ↔  vendor agent  ↔  vendor support
     ↑          (Claude / Cursor /    (the protocol     (vendor's AI         (humans, last
   human         ChatGPT / Codex)      layer)            assistant)           resort)
```

Five entities, not three. **Baton is the protocol layer connecting two agent layers**, with humans on both ends. The vendor side has its own agent (triage, deflection, structured-action routing) that consumes Baton signals BEFORE human support kicks in. Human support is the fallback when the vendor's agent can't resolve.

The signal surface is wider than outages: failures are one of eight signal types, alongside retry loops, dead-ends, parameter confusion, slow performance, silent abandonment, feature gaps, and "other." The wider framing de-risks Anthropic absorbing structured handoff into MCP itself, because product-quality signal capture is not what MCP's retry semantics address.

**v0.1 answered three load-bearing questions (all yes):**
1. **Can the SDK actually capture the four-things payload from a real MCP transport?** ✅ Validated against Claude Code, Cursor, and Claude Desktop across a multi-round spike. Full test suite green.
2. **Does the structured-signal loop close end-to-end?** ✅ Real Claude Code interactions producing real ticket entries across two unrelated vendor consumers.
3. **Is the spec buildable as a vendor-agnostic SDK, or does it leak per-vendor assumptions?** ✅ Two unrelated vendor consumers integrate via ~5 lines each + their own Channel adapter; no SDK code is vendor-specific.

Design-partner conversations are the next gate. One explicit job in those conversations is validating the wider framing.

---

## 2. Architecture

Baton ships as a standalone Python package (`baton-sdk`) installed as a dependency in a vendor's MCP server. The SDK is intentionally separable from any specific vendor — the integration contract is `BatonMiddleware(VendorConfig(...))`, ~5 lines in the vendor's MCP server startup code, and nothing about the SDK is special-cased per vendor.

The Console (hosted ingest + worker + storage + UI) lives in a separate sibling repository (`baton-console`) and is not required for the SDK to function. The SDK ships events to any HTTPS endpoint that accepts the wire format defined in `docs/SPEC.md` §11.4; the Console is one such consumer.

**Why a separate SDK package:** treating Baton as a separate package from day one enforces the SDK boundary. If Baton lived as a folder inside any one vendor's repo, it would stop being a portable SDK and become vendor glue. The premise of the product is that *any* vendor can wrap their MCP server in Baton; multiple unrelated vendor consumers have integrated via ~5 lines each, with no SDK code special-cased per vendor.

---

## 3. Wrapper-style: Option A (separate process, HTTP to vendor app)

The vendor's MCP server runs as its own process and talks to the vendor's app over HTTP. The Baton SDK wraps the MCP server, not the app.

**Why A over B (in-process FastAPI mount):**
- A matches what real-world vendors ship — production MCP servers commonly run as separate processes alongside the application backend.
- A forces us to confront identity/auth (OD-2), consent_token flow, and PII scrubbing boundaries early — these are the load-bearing thesis questions, not nice-to-haves.
- B would let the SDK reach into vendor app internals, hiding exactly the spec-design problems the prototype is supposed to surface (e.g., how does the agent's `expected_outcome` cross the MCP transport boundary).
- A's "5 lines to integrate" ergonomic check is the realistic acceptance criterion; B makes integration trivially easy in a way that doesn't generalize.

**Cost accepted:** ~1 week longer to first dogfood loop, less rich payloads in v0.1. Worth it.

---

## 4. SDK boundary rules

These are non-negotiable. They exist to keep Baton a real SDK.

1. **No vendor-specific imports anywhere in this repo.** Not in `src/baton/`, not in `tests/`, not in `examples/`. If anything ships in this repository that imports from a specific vendor module, the architecture is broken. CI enforces this. Examples use generic stubs that mirror common API shapes (e.g., OpenAI-compatible chat completions), not real vendor SDKs.
2. **Baton only sees what MCP transport carries.** Tool name, params, response, error, agent-supplied metadata (intent/expected, via a spec-defined mechanism). It does not have access to the vendor app's logs, request context, DB, or observability events. If we want richer signal, the *spec* defines a mechanism for vendors to provide it — the SDK does not reach across.
3. **The Baton spec lives in `docs/SPEC.md`, never in a vendor repo.** The JSON Schema for the signal and response payloads is the hero artifact. It must travel with the SDK, not with any one vendor.
4. **Public API is the contract.** Anything exported from `src/baton/__init__.py` is what vendors integrate against. Internal modules are off-limits to consumers. Changes to the public surface require a changelog entry and (eventually) a semver bump.
5. **Tests use fake-vendor fixtures only.** Baton's test suite uses synthetic in-process fixtures — `pytest-httpserver` for HTTP capture, FastMCP's in-process `Client` for MCP integration tests, generic stub vendor SDKs in examples. Tests prove the SDK works for *any* vendor. If a Baton test imports a real vendor module, it's wrong.
6. **Integration is ~5 lines or the SDK is failing.** The single load-bearing ergonomic check. A vendor wraps their FastMCP instance in `install_baton(mcp, VendorConfig(...))` and that's the whole story. If we ever see the integration code creeping past ~5 lines in a vendor's MCP server setup, that's a signal to refactor Baton, not paper over with more glue.
7. **Console doesn't bleed across.** The Console is what vendors see; the SDK is what end-user-side agent traffic flows through. They communicate only via the Baton wire format (signed payloads over HTTP). Code does not cross.

---

## 5. Spec-first, failing-test-first

Baton's spec doc is a public artifact, so spec-first matters double here.

**For any non-trivial change in this repo:**
1. **Spec.** Update `docs/SPEC.md` (the Baton protocol spec) and/or this charter first. For small changes, a comment in the PR is fine.
2. **Failing test.** Write the test against the fake-vendor fixture. Run it. Confirm RED.
3. **Implement.** Minimum code to pass.
4. **Green.** Run, confirm PASS.
5. **Refactor** (optional). Stay green.

---

## 6. Console — architecture posture

**v0 prototype (now superseded):** the v0 "Console" was a Notion database per vendor, written via a vendor-side Notion Channel adapter. Worked for dogfood; doesn't scale past a few hundred signals.

**Current posture (decided 2026-05-18):** Phase 1 ships a **hosted single-tenant Console per partner** as the canonical signal-storage + analytics + policy-authoring surface. Notion remains an optional Channel adapter for vendors who explicitly want it; it is no longer the default analytics destination.

**Why the change:**
- Phase 1 partner volumes overwhelm Notion within weeks. Notion's API + workspace UI break down past ~10K rows.
- The Sentry / PostHog / Plausible / Supabase playbook (per OD-1 + OD-5 resolution) is **hosted-first, not OSS-server-first**. Sentry shipped its hosted SaaS from day one; the OSS server came later as an enterprise/self-host option.
- "Vendor-self-hosts everything" sets partners up to roll their own analytics — and once they have homegrown infra, they have zero reason to migrate to our hosted Cloud later. **We permanently lose the upgrade path.**
- Recurring revenue requires us to operate something. Pure consulting is fundamentally not a SaaS business.

**Phase 1 → Phase 2 transition:**
- Phase 1 = single-tenant per partner (dedicated database per customer; one Console service per tenant or shared with subdomain routing). Simple data isolation; no cross-tenant leakage worries; no SOC2 commitment yet.
- Phase 2 = multi-tenant SaaS (shared database with tenant_id + row-level security; SOC2 Type II; self-serve signup; cross-vendor analytics). Same backend code; just the deployment model changes.

Build with multi-tenancy in mind from day one — every row tagged with `tenant_id` even in single-tenant — so the Phase 2 migration is "drop schema isolation, add tenant_id queries to the ORM," not a rewrite.

**OSS self-host path (parallel track):** Console code is OSS-eventually (per OD-1 leaning Apache 2.0, OD-5 hybrid). Vendors who explicitly want to self-host can clone the repo and deploy on their own infrastructure. Most won't bother — they'll pay for hosting. The OSS self-host option is a credibility signal + future enterprise-tier sell ("you can self-host if compliance demands").

The Console code lives in a separate `baton-console` repo; this repo (the SDK) does not depend on it.

---

## 7. Open decisions

These are unresolved or partially resolved. Flagged here so we don't drift.

### OD-1: Spec strategy — proprietary vs. open-source

**Resolved 2026-05-29: Apache 2.0 for the SDK + spec + reference Channel implementations + reference Console code.** Good Timing Cloud (hosted Console + premium features) is the proprietary product on top — the Sentry / PostHog / Plausible / Supabase hybrid playbook.

License chosen: Apache 2.0. Includes a patent grant (MIT does not), which is the right defensive posture for a protocol-shaped SDK; less adoption friction than AGPL (enterprises hesitate on AGPL); standard for the reference class.

### OD-2: Identity / auth model

Start with vendor-issued API key + per-end-user consent token, upgrade to OAuth/DID later. **SDK accepts `VendorConfig(api_key=..., consent_token=...)`. v0 consent_token can be a UUID granted at SDK init time (single-user dogfood); design the spec so OAuth/DID can replace it without breaking the wire format.** See SPEC §2.3.

### OD-3: Brand

"Baton" is the working code name; may revisit at Month 3. Package names use `baton` everywhere. If the brand changes, we rename then.

### OD-4: How does the agent supply `intent`, `expected_outcome`, and `signal_type`?

**RESOLVED in SPEC §5** (2026-05-13, refined by spike validation). v0.1 commits to a layered approach: agent-emitted values (intent, expected_outcome, agent-raised signal_type) ride the vendor-namespaced annotation tool (`<vendor_id>.annotate`); client-attached values (session_id, runtime_metadata) ride the standard MCP `_meta` field; reserved `_baton_*` param prefix is the escape hatch; nulls are the fallback.

**Critical refinement from the annotation-behavior spike:** the annotation tool alone is *insufficient* to trigger agent behavior. Server-level `instructions` (per the MCP spec's `initialize` response) are co-required and equally load-bearing. With tool description only, the agent did not call the annotation tool across multiple unprompted attempts. With server-level instructions added, the agent reliably called annotate proactively (with `intent` + `expected_outcome`) before vendor tool calls and reactively (with `signal_type=failure` + diagnostic context) after errors. SPEC §5.1.2 makes server instructions an SDK requirement, not optional.

### OD-5: Signal detection threshold

What counts as "signal-worthy"? v0 keeps SDK auto-detection conservative — fire on (a) explicit tool error/timeout (signal_type=failure) and (b) ≥3 retries of same tool with same params (signal_type=retry_loop). The remaining six signal types (dead_end, parameter_confusion, slow_performance, abandonment, feature_gap, other) are agent-raised via the annotation tool in v0.1. Auto-detection for slow_performance, abandonment, dead_end is on the v0.2+ roadmap (SPEC §6.4). Tune thresholds in dogfood.

### OD-6: Deployment posture — Library only vs SaaS vs Hybrid

**RESOLVED 2026-05-18: Hybrid, hosted-first.** OSS SDK + reference Channel implementations + OSS reference Console code (Apache 2.0). Good Timing Cloud as the proprietary product on top — hosted Console + premium Channels + cross-vendor analytics + compliance (SOC2/GDPR/BAA at scale).

**Phase 1 sequencing:** single-tenant hosted Console per partner from day one (NOT pure library + consulting; NOT Notion-as-stub forever). Each Phase 1 partner gets dedicated infra + subdomain. Real recurring revenue from day one. See §6.

**Phase 2 transition:** same backend code; deployment model shifts from single-tenant to multi-tenant SaaS (tenant_id column + row-level security + SOC2 Type II + self-serve signup). Build with multi-tenancy in mind from day one so the transition is "drop schema isolation + add tenant_id queries," not a rewrite.

**Why this beat the alternatives:**
- **Library-only consulting** (rejected): doesn't scale; partners build their own analytics + lose upgrade path; no recurring revenue.
- **Multi-tenant SaaS from day one** (rejected): premature compliance + multi-tenant data discipline before partner data validates the architecture.
- **Library + Notion forever** (rejected): Notion breaks down past ~10K rows; Phase 1 partners generate that in weeks.

**Why hosted-first beats OSS-server-first:**
- Sentry / PostHog / Plausible / Supabase playbook is hosted-first; the OSS server came later as an enterprise option.
- Self-host requires vendors to operate infrastructure — most won't. They'll roll their own or stay on Notion.
- We need operational learning on real production traffic before we have a credible SaaS to sell at Phase 2.

### OD-7: SDK shape — fat capture vs thin emit-only

**RESOLVED 2026-05-19: thin emit-only SDK + fat Console worker.** The SDK is a minimal event emitter; the Console worker is where interpretation, policy, correlation, detection, and dispatch happen. Sentry / Datadog / PostHog pattern exactly.

**What this means concretely:**
- **SDK** ships: middleware that emits `tool_call_start` / `tool_call_end` / `tool_call_error` events; annotation tool handler that emits `annotation` events; PII scrubbing at event-emit time; bounded local event buffer (~1000 events) with HTTPS POST + retry-with-backoff to the Console ingest endpoint; cheap stateless classification (e.g., `signal_type=failure` on tool exception). **No state. No policy. No Channels. No StateStore.**
- **Console worker** ships: event ingestion (`POST /v0/events`); session reconstruction from events; retry-loop detection by querying recent events; annotation correlation with tool calls by timestamp + sequence; SignalPayload assembly; policy evaluation; outbound action dispatch via Channels; response triage; return-channel notifications.

**Why this beat the fat-SDK alternative:**
- Hot-path SDK state was the original justification for StateStore. With thin SDK, state lives in the event log; SDK has nothing to remember. **StateStore design evaporates entirely.**
- Policy changes don't require SDK redeploy in vendor's MCP server — vendor updates policy in Console UI, worker hot-reloads. Operational massive win.
- Channels move to Console — vendor's MCP server has zero outbound credentials (third-party API keys, webhooks, etc. all live in Console secrets). Cleaner trust model.
- Event log is canonical; signals are derived. Enables replay, retroactive corrections, debugging, future analytics rework without breaking historical data.
- Matches Sentry / Datadog / PostHog industry pattern. Not inventing.
- OSS-only path is honest: install the SDK alone, point it at your own HTTPS endpoint, get a clean event stream. The rich product (signal stitching + policy + dispatch) is the Console paid tier.

**Thesis preservation (the load-bearing check):** the four-things-in-one-context payload (intent + tool_calls + observed_outcomes + expected_outcomes + friction signals) still reaches the vendor with full agent authorship. SDK emits each thing as events; worker stitches them into the canonical SignalPayload. Same rich content, same fidelity, same vendor-facing shape — assembly just happens at a different layer.

**Failure-mode posture:**
- Worker briefly restarts → events accumulate; no impact.
- Worker bug → events preserved; replay-able when fixed.
- Console DB briefly down → SDK buffers locally; eventually delivers.
- Console DB down for hours → SDK buffer fills; oldest events dropped with `UserWarning(events_dropped)`. **The one acceptable architectural data-loss case** — alternative is blocking vendor's hot path on remote service, which we won't do.
- Bad worker deployment → corrupted signals → bad tickets. Mitigated by idempotent worker design + replay capability.

**Required resilience properties:**
1. **Idempotent worker** — re-processing the same event produces the same signal (or in-place update); no duplicate signals.
2. **Replay capability** — worker can be instructed "reprocess everything since timestamp T." Append-only event log makes this trivial.
3. **Bounded SDK buffer with explicit drop policy** — default 1000 events, oldest-dropped-on-overflow, `UserWarning` emitted on drop. Documented loudly so vendors aren't surprised.

---

## 8. v0 shortcuts to revisit (technical debt log)

Things we're doing the fast way for the prototype. Each must be revisited before the gate noted.

| Shortcut | Gate to revisit | Why we're doing it |
|---|---|---|
| Notion direct-write from a vendor-side Channel adapter | Before Phase 2 (first paid logos) | Fastest dogfood loop; doesn't scale past single vendor + single user. |
| Single-user `consent_token` as UUID at init | Before second vendor onboards | Real consent flow is OD-2; v0 only has us as end users. |
| Editable install for vendor-side development | Before second vendor onboards | Loses versioned-contract discipline; fine for solo dogfood. |
| No SOC2 / no audit-log export / no RBAC | Before Phase 3 (mid-market) | Twelve weeks of compliance work; not the prototype's job. |
| Fake-vendor test fixtures hand-rolled | Before second vendor onboards | Library-ify into a reusable test kit if a real partner needs it. |
| Signal-type taxonomy is gut-pressed at 8 values | Before publishing the spec externally as v1.0 | Design partners may add/merge/split; treat enum as additive-only until v1.0 (per SPEC §13). |
| Auto-detection for `slow_performance`, `abandonment`, `dead_end` deferred to v0.2 | Before Phase 1 design-partner validation gate | Agent-raised path (SPEC §6.2) covers v0.1 dogfood; auto-detection is a v0.2 SDK enhancement. |
| Synchronous return channel (agent autopickup of vendor responses) deferred to v0.2 | First Phase 2 design-partner conversation that explicitly asks: "would agent autopickup matter more than out-of-band notifications?" | Building it now requires Console state machine + SDK polling + synthetic-injection semantics + stale-response handling — all benefit from real design-partner feedback on what response shape matters. v0.1 ships SPEC §8.4 async out-of-band notification (email/Slack/push to the end user, who re-engages the agent) as the human-loop pattern. Costs one extra human turn; gets us to dogfood without the bidirectional plumbing. |

Add to this table when we take a known shortcut. Don't let it grow silently.

**Completed migrations (kept here for changelog visibility):**

| Migration | Date | Notes |
|---|---|---|
| Vocabulary + schema sweep from thesis v0.2 → v0.3 | 2026-05-13 | `incident`→`signal`, `Resolution`→`Response`, added `signal_type` enum and `friction_signals`, widened status enum with `documented`/`feature_filed`/`educated`, added `doc_pointer`. No architectural change. |
| Annotation-behavior spike → SPEC §5.1.2 server-instructions requirement | 2026-05-13 | Spike against a real FastMCP server proved that the annotation tool alone is insufficient — the agent doesn't call it unprompted. Adding FastMCP `instructions=` (templated from `vendor_display_name`) flipped behavior: the agent reliably called annotate proactively + reactively with high-quality content. Spec now makes server instructions a SDK requirement. |
| `_meta` plumbing verified (Claude Code) | 2026-05-13 | Same spike confirmed Claude Code populates `_meta` end-to-end with `claudecode/toolUseId` (per-tool-invocation UUID) + `progressToken` (per-request sortable int). SPEC §5.2 updated with a per-runtime adapter table. Implementation note: FastMCP exposes `_meta` as a structured `Meta(...)` object, not a dict — real SDK must call `.model_dump()`. |
| Annotate signature consolidation (Round 2 + Round 3 spike → v0.2 fields) | 2026-05-13 | Free-form `context` spike showed two keys with 3/3 recurrence: `workflow` (proactive) and `suggested_improvement` (reactive — the moat field, agent-authored product feedback). Both promoted to top-level fields in the annotate signature and the signal payload (SPEC §3.1, §5.1.1). `note` field dropped — spike showed it was always subsumed by `context.*` keys. |
| Server-instructions framing locked: "MUST + (REQUIRED)" markers (Round 5 spike) | 2026-05-13 | Round 4 tried "do NOT duplicate" framing and backfired — agents stopped populating top-level fields entirely (0/3 on both workflow and suggested_improvement). Round 5 reframed with explicit MUST + (REQUIRED…) markers on each top-level field and positive "supplementary" framing for `context`. Result: 3/3 workflow (including feature_gap), 3/3 suggested_improvement, zero duplication. The validated text is now the SPEC §5.5 `server_instructions` default template. SPEC §5.5 also documents the two framing lessons (MUST/REQUIRED matters; anti-duplication framing backfires) and the workflow session-stability semantic the agent discovered. |
| Cross-runtime portability validated (Claude Desktop spike, Rounds 6+7) | 2026-05-13 | Wired the spike into Claude Desktop alongside Claude Code; ran the same three prompts. **Key finding: Claude Desktop does NOT surface MCP server `instructions` to the LLM** — the load-bearing motivation mechanism for §5.1.2 is Claude-Code-only. Unprompted Desktop made zero annotate calls. Explicit-prompt diagnostic (Round 7) confirmed Desktop CAN call annotate with high-quality content when told to: workflow populated and carried across proactive→reactive, signal_type=failure correct, rich context without duplication — BUT `suggested_improvement` stayed null (Desktop doesn't proactively add fields beyond what it was told to fill) and `_meta` was absent on all calls (Desktop populates no `_meta` keys, unlike Claude Code's `claudecode/toolUseId`). SPEC §5.1.3 added (per-runtime support matrix); SPEC §5.2 _meta adapter table updated. |
| Cursor validated as second wedge runtime (Round 8 spike) | 2026-05-13 | Same spike, wired into Cursor via `~/.cursor/mcp.json`. Result: **Cursor surfaces `instructions` to the LLM identically to Claude Code** — proactive + reactive annotation fired unprompted, `workflow` populated on both, `suggested_improvement` populated on reactive with concrete actionable content, zero duplication, rich diagnostic context. `_meta` carries only `progressToken` (no Cursor-specific correlation key like Claude Code's `claudecode/toolUseId`). SPEC §5.1.3 + §5.2 tables updated. **Both wedge developer runtimes (Claude Code + Cursor) validate the SPEC §5.1.2 design.** |

---

## 9. References

- **`docs/SPEC.md`** — the Baton wire protocol (canonical).
- **`docs/SKILLS_LIBRARY_API_DRAFT.md`** — the library-API surface for Skill-driven agent code (alongside the MCP middleware path).
- **`README.md`** — first-time-visitor entry point.
- **`docs/design-notes/`** — engineering memos and design-validation records (load-bearing for future spec discussions).
- **`examples/`** — runnable usage examples (the library API skill demo, the e2e smoke test).
- **Sibling repos:** `baton-console` (separate repository) for the Console worker + UI.

---

*Changes to this charter should be made deliberately and dated. The charter is meant to be stable across weeks; if it churns, the project is churning.*
