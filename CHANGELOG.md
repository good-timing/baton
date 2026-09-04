# Changelog

All notable changes to the Baton SDK will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it leaves pre-release.

**Spec changes** (anything affecting the wire format) are also recorded in `docs/SPEC.md §13`. This file is for the SDK package's user-facing changelog; SPEC §13 is the canonical wire-format change log.

---

## 0.6.1 — refuse before mutating; cap fastmcp below 4

### Fixed

- **`install_baton` now refuses a server it can't install into, up front, instead of half-installing into it.** The check runs first — before any validation or mutation — and probes the tool registry, the seam install genuinely cannot proceed without. Previously the refusal came too late to be one: the low-level-server lookup that would have caught it is deliberately best-effort, so its message was swallowed into a log line; `set_server_instructions` then *succeeded*, because on a bare `mcp.server.Server` `instructions` is a plain settable attribute rather than the read-only property it is on FastMCP/MCPServer; and install died several steps later in the wrap layer on a raw `'Server' object has no attribute '_tool_manager'` — leaving the server advertising an annotation tool that was never registered. Two shapes hit this and each gets its own diagnosis, because a shared one is wrong for at least one of them: the **standalone `fastmcp` library's FastMCP** (detected by module path, no import) is told to use `baton.integrations.fastmcp.install_baton` — the likeliest mixup, since both libraries name the class `FastMCP` and both adapters' `install_baton` take the same arguments; anything else is told it looks like a bare low-level `Server` (the API the reference servers — git, time, fetch — are written against), not supported yet, with the version pin as the fallback branch. Relatedly, `get_lowlevel_server`'s message now leads with the **version** cause and keeps the shape as its second branch — the reverse of the guard's ordering, and correct for it: with the guard running first, anything still reaching that error has a tool registry, so it really is a FastMCP/MCPServer on an unrecognised layout. **Behavior change** for anyone passing either shape: previously a late `AttributeError` on a mutated server, now an immediate `TypeError` on an untouched one. Callers passing the official FastMCP/MCPServer are unaffected. Low-level-`Server` support remains separate work — the capture seam is the tool registry, which a low-level server has none of, so it needs the `list_tools`/`call_tool` handlers wrapped instead. **Both adapters guard now**, symmetrically: the reverse mixup (official `FastMCP` → `baton.integrations.fastmcp.install_baton`) used to half-install for the same reason in mirror image — on mcp 1.x the official server has `_mcp_server`, so the surface capture succeeded and the read-only-property fallback wrote the instructions onto it, and install died at `add_middleware`. (On mcp 2.x that backing is renamed `_lowlevel_server`, which this adapter doesn't route through a compat shim, so the instructions write raised and nothing was mutated — an earlier and less damaging failure, replaced by the same clean refusal.) That adapter now refuses first too, on its own seam (`add_middleware`), and names `baton.integrations.mcp.install_baton` as the one to use.

- **`fastmcp` is capped below 4.0 — the published `baton-sdk[fastmcp]` currently installs a broken adapter.** The extra declared `fastmcp>=2.10` with no upper bound, so a fresh `pip install baton-sdk[fastmcp]` resolves fastmcp 4.0.1 today, and on 4.x six adapter behaviours fail: sequence numbers repeat within a session, the once-per-session synthesized proactive fires twice, a vendor's own `user_goal` is no longer forwarded to their handler, and a `tools/list` response comes back with no `inputSchema`. Unlike the `mcp<3` cap, which guards an untested future major, this one is tested and known broken. **Anyone who installed with the `fastmcp` extra since fastmcp 4.0 shipped is affected and should pin `fastmcp<4` until this release lands.** Porting the adapter to 4.x is not done and is tracked separately. 4.x also lifted fastmcp's own `mcp<2` pin, which is how mcp 2.x reached the typecheck job and turned this into a CI failure that masked the test failures underneath it.

- **The injected `user_goal` described itself as `OPTIONAL.` while the schema advertised it as required.** `VendorConfig.intent_param_mode="required"` appends `user_goal` to a tool's advertised `required` list, but the description shipping inside that same schema still opened `OPTIONAL.` — the model reads both, and whichever it believed, the other was noise. The leading label now tracks the mode; the sentence after it is byte-identical across modes, because that text is measured and the mode is not a licence to reword it. `expected_result` and `overall_task` are never added to `required` in any mode, so their `OPTIONAL.` was true and is unchanged. Both adapters were affected and both are fixed. Text-only: same fields, same events, no consumer change — the mode was already an advertisement that nothing validates.

### Added

- **`VendorConfig.proactive_mode` — gates the pre-call annotation request. Defaults to `"off"`, which is a behavior change for every existing install.** The injected params superseded the proactive annotation: `user_goal`/`expected_result`/`overall_task` ride every `tool_call_start` as `call_intent`/`call_expected`/`call_workflow`, so a pre-call annotation carries no field the call event doesn't — while costing a full extra inference turn per tool call (measured 2×: 4 annotation calls serving 4 tool calls in one session) and capturing worse (R=0.135 coverage vs the params' 1.000, and conversation-scoped umbrella task labels where the param gives per-task ones). `"on"` restores the previous behavior verbatim. **Reactive annotation is untouched in both modes** — the tool stays registered, the AFTER/IF friction clauses of the instructions are byte-identical (pinned by test), and the SDK's own synthesized proactive still fires from the injected params with no agent turn, so `intent`/`expected_outcome`/`workflow` stay populated on the wire. Only the agent-initiated pre-call annotation goes away. Setting `intent_param_mode="off"` *and* `proactive_mode="off"` now raises at construction rather than installing a capture no-op: the two are alternative intent channels, and running both also produces two competing `workflow` labels a consumer has to arbitrate. **Enforced, not merely requested:** alongside the instruction and description changes, the annotation handler itself rejects a call that arrives with no `signal_type` while the mode is off, returning an explanatory `ok: false` and emitting no event. Text alone is only a request, and one stray proactive is enough to do damage — its umbrella `workflow` label outranks the per-call `call_workflow` in the reference consumer, merging distinct tasks. Enforcement via the handler rather than by escalating `signal_type` to required in the schema is deliberate: a required enum would let an agent satisfy it with a fabricated `failure`, corrupting the reactive signal in order to suppress a redundant one. Verified end-to-end against the toybox fixture — zero agent-initiated proactives across a 3-turn session, the synthesized proactive still firing and agreeing with `call_workflow`, and the `feature_gap` reactive still landing on a dead end.

### Changed

- **The `workflow` annotation field now asks for the user's *current task*, not "the broader task"; the `overall_task` param keeps its shipped wording.** The annotation field gains a repeat-verbatim-until-they-switch stability contract for the first time (it previously had none). Text-only: same fields, same events, no consumer change. The two surfaces are deliberately worded differently now, and the reason is measurement, not oversight — see below.

- **The same rewording was applied to the `overall_task` param and then reverted — measured, and it lost a real trade.** The candidate ("the specific task the user is working on right now — not the overall theme of the conversation") was scored against the shipped text over 40 paired live-agent sessions on 2026-08-11, scripted multi-turn conversations with known ground truth, each run served from one build (`baton-internal/spikes/overall_task_a5/`, §A5b and §A5c). The two wordings have **complementary** failure modes, and the shipped text is kept because its failure is the survivable one:

  | | misses a real task boundary | splits one task apart |
  |---|---|---|
  | shipped | yes, when the user switches topic without announcing it (boundary 0.700) | never (0.000 over-split, both corpora) |
  | candidate | never (1.000, both corpora) | yes, and inconsistently (0.200 then 0.400 on identical scripts) |

  The candidate's gain (+0.300 boundary) is smaller than its cost (0.400 over-split), and shattering a task is the failure that destroys downstream trust — so the param text is unchanged and this release carries no `overall_task` semantics change. **Do not reword without scoring against both corpora.** A v3 has a concrete target: the candidate's boundary behaviour with the shipped text's within-task stability. The two failures are independent, so this is not a granularity dial — it needs the repeat-verbatim contract hardened against *step-level* rewording.

- **Known weakness, stated for consumers.** The shipped wording under-splits when the user changes subject without saying so — agents carry the first task's label forward (one session labelled a rice lookup, a chickpea restock and a waste check all "cook dal tonight"). Boundary detection is 1.000 when the user announces the switch and 0.700 when they don't, so **`call_workflow` should not be the sole merge signal on conversational traffic**; correlators keying on it want a time or semantic band alongside. Unchanged and reconfirmed: the injected param still groups more reliably than the annotation field, which is why SPEC §11.5.2 and §13 tell consumers to treat annotation-sourced `workflow` as the weaker evidence.

---

## 0.6.0 — overall_task grouping key + vendor-neutral goal params

### Added

- **`overall_task` — a third injected param: a stable task-label grouping key, riding `tool_call_start.payload.call_workflow`.** Unlike `user_goal`/`expected_result` — call-scoped diagnostics that legitimately reword on every call — `overall_task`'s param description carries an explicit string-stability contract: the agent is asked to repeat the exact same string across every call serving one task, which makes it usable as a grouping key by a console (it feeds the existing workflow-continuity input, so task grouping no longer depends on the annotation path). Diagnostics and grouping keys deliberately do not share a field. The session's synthesised proactive annotation carries the label as `AnnotationPayload.workflow`; `seam_augmentations.intent_param.names` gains `overall_task`. The agent-facing name avoids `workflow` because a vendor tool plausibly declares a param of that name, and per-param disposition would then mark it native and never strip it — the vendor's own argument would be swallowed into capture. Same injection surface on both adapters (mcp adapter + fastmcp middleware). Additive wire change — `call_workflow` + `call_expected` on `tool_call_start`; baton-spec schema regenerated; SPEC §11.4/§11.4.2/§13.
- **Per-call `expected_result` capture: `tool_call_start.payload.call_expected`.** Previously the injected `expected_result` survived only as the session's first synthesised proactive's `expected_outcome` and was dropped on every later call; it now also rides every `tool_call_start` as `call_expected`, matching how `user_goal` rides `call_intent`.
- **`surface_snapshot` event — the SDK now captures and emits its wrapped server's tool surface, matching baton-proxy/baton-extmcp.** Previously the SDK had zero surface-capture support: no event type, no emission, even though it already had the `tools` result in hand on both adapters. Mirrors baton-proxy's `_capture_surface`: snapshots `server_info`/`capabilities`/`instructions` (captured once at `install_baton(...)` time, before Baton's own instructions suffix is applied) plus the full `tools` list with schemas (captured before Baton's `user_goal`/`expected_result` injection), hashes it (canonical JSON), and emits at most once per observed `surface_hash` per process — repeat observations of an unchanged surface are deduped. The hash deliberately excludes anything Baton itself adds (the annotation tool, the injected goal params) so it stays stable across e.g. an `intent_param_mode` change — that's the identity change specs and recipes are authored against. New shared helper module `baton.integrations._surface` (`surface_hash`, `build_server_meta`, `build_seam_augmentations`) backs both adapters. Capture mechanism differs by adapter: `baton.integrations.fastmcp` has a real `on_list_tools` middleware hook, so it captures every `tools/list`; `baton.integrations.mcp` (official SDK) has no such hook, so it builds the snapshot from data already captured during tool registration and lazily emits on the first tool call (a server that's listed but never called won't get a snapshot — a narrower version of the same limitation proxy already has). Additive wire change — see `docs/SPEC.md §11.4.2` / `§13`.
- **`VendorConfig.resolve_session_id` — a vendor-supplied session-id resolver hook, checked before each adapter's own session resolution (rung 0).** A vendor who already has their own session/auth concept can now hand Baton a real correlation key directly — the only mechanism that works on new-spec MCP HTTP (SEP-2567, which drops the `mcp-session-id` header from the wire on negotiated connections) and true-stateless HTTP, where nothing MCP-native is observable by protocol design. The hook receives a `SessionResolutionContext` (`headers`, `meta`, `tool_name`, `arguments`) — the same normalized shape on both the mcp-adapter and fastmcp-adapter paths, exported from both `baton.integrations.mcp` and `baton.integrations.fastmcp`. Sync or async; a non-empty string return wins outright, `None`/empty or a raised exception (logged, never propagated) falls through to each adapter's existing resolution unchanged. Returned values are passed through raw, not hashed — hashing/derivation is the vendor's responsibility if the raw value is sensitive. Wired into: the mcp-adapter's tool-call wrap layer, the fastmcp-adapter's middleware, and the fastmcp-adapter's annotation tool. **Known gap:** the mcp-adapter's annotation tool doesn't check the hook (or any session context at all — it's always the process-wide fallback id, a pre-existing limitation predating this change; see the comment in `mcp/annotation.py`), so on that adapter a vendor's explicit reactive annotations won't stitch to the hook-resolved session id their tool calls get. Synthesised proactives are unaffected. See `docs/design-notes/session_resolver_hook.md` for the full design (including why the schema-injected mint-back alternative was rejected).

### Fixed

- **The official-mcp-SDK adapter no longer misreports a paused multi-round-trip (MRTR) tool call as finished.** mcp>=2.0 lets a tool handler pause mid-flight and return `InputRequiredResult` to ask the client for more input before the call actually completes; the client then retries the same logical call, carrying its answers via `Context.input_responses`/`request_state`. Previously every round — paused or not — fired a `tool_call_start`/`tool_call_end` pair, so a 3-round exchange misreported as three separate completed calls. Now a paused round emits no `tool_call_end` (`_is_mrtr_pause`, detected by duck-typing the wire discriminator `result_type == "input_required"`), and a round continuing a prior pause emits no new `tool_call_start` (`_is_mrtr_continuation`) — whichever round eventually returns a real result or errors gets the one true end/error event. The injected `user_goal`/`expected_result` strip still runs unconditionally on every round regardless, so a continuation that resends the original arguments can't leak them to the vendor handler. Detection is duck-typed, not an `mcp_types` import, so it's inert (always `False`) on mcp<2.0 and the standalone `fastmcp` adapter is unaffected (that library pins `mcp<2.0` and has no `InputRequiredResult` concept — its similarly-named `"input_required"` task status is an unrelated feature of its own background-tasks/elicitation extension).
- **The official-mcp-SDK adapter now resolves a real per-call `session_id` instead of one process-wide UUID shared by every caller.** On a hosted deployment (one server process, many end users — the documented shape for vendors serving remote MCP clients), every event previously carried the same install-time fallback id, so the Console had no way to tell different users' calls apart. `_tool_wrap.py` now implements SPEC §3.4's layered fallback, in priority order: (1) `_meta.traceparent` (W3C trace context, SEP-414), (2) `_meta["io.baton/session_id"]` (vendor-supplied app-level handle), (4) the `mcp-session-id` HTTP header on stateful streamable HTTP (rung 3, a future runtime-specific `_meta` key, isn't defined for any runtime yet). Rungs 1-2 read data already extracted for `runtime_meta`, so they're free, and unlike the header they don't depend on which MCP protocol version a client negotiated — per SPEC §5.2 no runtime Baton has validated (Claude Code, Claude Desktop, Cursor) populates either key yet, but both resolve automatically the moment one does, no further SDK change needed. Rung 4 covers the documented hosted-deployment default (`stateless_http` is `False` by default on both mcp 1.x and 2.0) but is a genuine no-op on MCP protocol 2026-07-28+ (SEP-2567), which removes the session header from the wire entirely when a client negotiates that version — confirmed against the mcp 2.0.0 SDK source, not inferred. stdio is unaffected (one process really is one user there, so the fallback was always correct); true stateless HTTP (`stateless_http=True`, opt-in, no current vendor on it) still has no session-bearing signal to read by protocol design and needs a vendor-configurable resolver hook, tracked separately on the sdk-hardening thread. `SessionCounter` sequencing and the proactive-annotation dedup (`ProactiveTracker`) now key on the resolved id too, so two users' sequence numbers no longer share one counter.

### Changed

- **Intent-param injection renamed to vendor-neutral names, and now also captures an expected result.** The injected params on both adapters are now `user_goal` + `expected_result` (was the single `baton_intent`). Names are vendor-neutral because anything an instrumented customer's agent can see must speak the vendor's voice, not Baton's (white-label rule) — matches baton-extmcp's spike-proven naming, which diverged from baton-proxy's `baton_intent` for the same reason. `user_goal` still rides `tool_call_start.payload.call_intent`; `expected_result` is new and rides only the session's first synthesised proactive annotation, as `AnnotationPayload.expected_outcome` (a field that already existed in the wire schema, previously reachable only via a real annotation-tool call). `required` mode escalates only `user_goal` to the tool's `required` list — `expected_result` stays optional regardless, since forcing it on every tool is a bigger surface mutation than the signal warrants. Disposition tracking (`injected`/`native`, skip-if-the-tool-already-declares-the-name) is now per param, not per tool, so a vendor tool can own one name natively while the other is still injected. **Breaking, no dual-support**: `baton_intent` is no longer recognized by either adapter; a tool that declared it as its own param is unaffected (it was never Baton's to strip), but any caller relying on the old injected name to carry intent will stop being captured. baton-proxy and baton-extmcp are unaffected — proxy still injects `baton_intent`, a collision-safety call from when it sat in front of upstream tools it doesn't own (a constraint that doesn't apply to an SDK-wrapped vendor server); porting proxy is a separate follow-up, not done here.

---

## 0.5.0 — mcp 2.0 support

### Added

- **The official `mcp` adapter now supports mcp 2.0.** mcp 2.0 renamed the
  server class `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`
  and moved the instructions backing `_mcp_server` → `_lowlevel_server`. A new
  `baton.integrations.mcp._compat` resolves the class across both, and
  `install_baton` routes instruction-setting through it — so
  `from mcp.server.mcpserver import MCPServer` servers wrap identically to 1.x.
  The tool-registry internals the adapter depends on (`_tool_manager._tools`,
  `Tool.run`/`parameters`/`fn`/`is_async`, `add_tool`, `list_tools`) are
  preserved byte-for-byte across the rename, so injection + wrapping are
  unchanged. The `tool_call_end.result` unwrap also handles 2.0's
  `CallToolResult` return object (1.x returned a `(content, structured)` tuple).
  CI's `mcp-matrix` now tests `2.0.0` alongside 1.20/1.25/1.27. The standalone
  `fastmcp` adapter is unaffected (that library still pins `mcp<2`).

### Packaging

- **`[mcp]` extra pinned `mcp>=1.20,<3`** (was unbounded `>=1.20`). 2.x is now
  supported and CI-tested; the `<3` cap guards against an untested future major.
  This also fixes clean `pip install baton-sdk[mcp]` resolving to mcp 2.0.0
  against the pre-2.0 adapter.

---

## 0.4.0 — intent-param injection on both MCP adapters + near-zero-dep

Kept in lockstep with baton-proxy 0.3.0's intent-injection design (D1–D6).

### Added

- **Per-tool intent-param injection (both adapters).** Both adapters now
  inject a `baton_intent` string parameter into every wrapped tool's input
  schema at `tools/list` and strip it at `tools/call` before the vendor handler
  runs — so intent is captured even on runtimes that drop
  `InitializeResult.instructions` (notably Claude Desktop), where the annotation
  tool alone yields nothing. The session's first injected intent also
  synthesises one proactive annotation (deduped against a real annotation-tool
  proactive via a shared `ProactiveTracker`); every call's intent rides
  `tool_call_start.payload.call_intent` with `intent_source="injected_param"`.
  Mode via `VendorConfig.intent_param_mode`: `optional` (default) | `required` |
  `off`. Tools that already declare `baton_intent` are left untouched (`native`
  disposition — never stripped). Ports baton-proxy 0.3.0's design (D1–D6) to the
  SDK. The FastMCP adapter (`BatonMiddleware`) injects per-request in
  `on_list_tools`; the official `mcp` adapter — which has no middleware hook —
  mutates each `Tool.parameters` schema once at install and strips in the wrapped
  `Tool.run`, reaching the same wire output. New module `baton._uuid`; new
  `baton._state.ProactiveTracker`.

### Wire format

- **`call_intent` + `intent_source` on `ToolCallStartPayload`** and
  **`intent_source` + `tool_name` on `AnnotationPayload`** — additive, nullable,
  omitted when unset (output byte-identical when the injected param is unused).
  Matches baton-proxy's emitter output; the Console already reads
  `payload.call_intent` / `intent_source`. Recorded in SPEC §13.

### Packaging

- **Dropped the `uuid6` runtime dependency.** UUIDv7 is now generated in-tree by
  `baton._uuid` (stdlib `uuid.uuid7` on 3.14+, a monotonic RFC-9562 fallback
  below), preserving same-millisecond monotonicity. One fewer dep inherited by a
  wrapped vendor.
- **`httpx` moved to an optional `[http]` extra.** Only `HttpSink` needs it;
  the stdlib `StdoutSink`/`FileSink` demo path installs nothing. Base runtime
  deps are now just `pydantic`. Both `pydantic` and `httpx` are already required
  by `mcp`/`fastmcp`, so wrapping a real MCP server adds **zero** marginal
  dependencies. `pip install baton-sdk[http]` for the Console path.

---

## 0.3.0 — end-user identity (user_id) on the wire

Wire-schema addition, kept in lockstep with baton-proxy 0.5.0.

- **`user_id` on the event envelope** (`_EventEnvelope`) — a hashed end-user
  actor (HMAC-SHA256, per-tenant, hashed at the capture edge; the raw principal
  is never transmitted). Additive and nullable, so pre-`user_id` consumers are
  unaffected. Lets the Console group by `(tenant_id, vendor_id, user_id)`.
  Recorded in SPEC §11.4 / §13.
- **`baton.identity`** module — `hash_user_id()` + `Principal` /
  `IdentityResolver`, mirroring `baton_proxy.identity` (kept as parallel copies
  until the shared package lands). Population from FastMCP context / per-trace
  kwargs is staged for a follow-up; this release lands the schema + util.
- **Scrubber** redacts the `user_name` field name (not `name`).

---

## 0.2.8 — vendor_id + scrubber-on + intent-required + trigger discipline

One tightening pass on the wire contract and the annotation surface, landing
ahead of the first real SDK consumer. Four load-bearing changes that hang
together: `vendor_id` makes every event self-attributing on the wire; the
scrubber default-on makes payloads safe to ship; `intent` becomes required
so proactives can't land empty; and the trigger discipline matches the
mechanical-trigger correction the proxy shipped in 0.1.3.

### Added

- **`vendor_id` field on `_EventEnvelope` (REQUIRED).** Every emitted event now carries the wrapped vendor's identifier in a dedicated envelope field, mirroring `baton-proxy`'s `BATON_VENDOR_ID` envelope stamping (proxy commit `ba7af35`). The Console's `IncomingEvent` ingest schema requires it — events from 0.2.7 and earlier are rejected with 422 on the upgraded Console. In SDK-mode (a vendor wrapping their own MCP server) `vendor_id == tenant_id`; the additive field exists so customer-mode proxy emitters can carry both — `tenant_id` identifying the customer paying for the dashboard, `vendor_id` identifying which wrapped vendor's events these are. Wired at every event construction site in `client.Client` / `client.AsyncClient`, `integrations.fastmcp.{middleware,annotation}`, and `integrations.mcp.{_tool_wrap,annotation}`. `BatonMiddleware.__init__` and `install_wraps` gain a required `vendor_id: str` keyword; both adapter `install.py` modules already had `VendorConfig.vendor_id` in scope and pass it through.
- **PII scrubber on by default.** `baton.scrub.Scrubber` ported from `baton-proxy` (same regex set: email / Bearer / `sk-*` / `AKIA*` / JWT / Luhn-validated CC / NA phone, plus field-name overrides on `email/phone/ssn/api_key/token/secret/password`; recursive walker with 10-level depth cap; per-instance redaction counter). `Client`, `AsyncClient`, `install_baton` (both adapters) now default to a fresh `Scrubber()` per construction site. `VendorConfig.scrubber=None` resolves to `Scrubber()`; pass `baton.scrub.identity_scrub` to opt out. Mirrors `baton-proxy/src/baton_proxy/scrub.py` so the two surfaces stay rule-equivalent until the shared package extraction lands (Persona B P2). 22 new tests in `tests/test_scrub.py` mirror the proxy's matrix.
- **Three mechanical IF triggers in the rendered server instructions** (`_llm_text.py`): in addition to "lacks a structured field", the prompt now surfaces "intent satisfied via workaround because no tool matched" and "user asked for something this server can't do". All three are observable states Claude can check at the end of a tool call — vigilance triggers ("notice X") lose to task completion. Ported from baton-proxy 0.1.3's 2026-06-12 live-Claude discipline correction.
- **`SIGNAL_TYPES` constant** in `_llm_text` so adapter schemas and rendered prose key off the same source of truth.

### Changed

- **`intent` is now required on the annotation tool schema** (both `mcp` and `fastmcp` adapters). Mirrors `baton-proxy/src/baton_proxy/proxy.py:93`'s explicit `required: ["intent"]`. Was previously optional (`intent: str | None = None`) which let agents emit payloadless annotations. The Console worker's proactive-bounded turn segmenter (per SPEC §11.5.1 step 2, since Claude Code's `runtime_meta.claudecode/sessionId` is per-session not per-turn) relies on proactives carrying intent — without this every SDK-instrumented vendor's session rendered as a single no-intent trailing turn on the dashboard.
- **`signal_type` and `suggested_improvement` marked reactive-only in the annotation tool description.** Agents were populating them on proactives just because the fields existed, inflating friction counts. Ported from baton-proxy 0.1.3.

### Wire format

Breaking for any consumer at the JSON envelope level — `vendor_id` is required. The Console rejected pre-0.2.8 envelopes from a 6afe0d4 deploy onward, so the migration is fail-loud rather than carrying a soft-shim. Per the 2026-06-14 cross-repo decision: zero live SDK consumers means a tight contract is cleaner than a multi-version optional-then-required ladder.

### Tool surface

The annotation tool's JSON Schema gains `required: ["intent"]` (was `[]`). MCP clients that previously called the tool without intent will get a `missing_argument` validation error. No live consumers known.

---

## 0.2.7 — fail-open at the capture boundary

### Fixed

- **`safe_write` makes sink failures non-fatal at the capture boundary (SPEC §11.2).** Previously every `await self._sink.write(...)` propagated up out of the middleware on a sink-side failure (closed sink, transport error, warnings-as-errors promoting a buffer-overflow `UserWarning`, `BrokenPipeError` from `StdoutSink`, etc.) — taking down the vendor's tool call so the bug looked like a vendor outage rather than Baton instrumentation. `baton.sinks.safe_write(sink, event, logger)` now wraps the seven `sink.write` call sites across both adapters (fastmcp middleware/annotation + mcp `_tool_wrap`/annotation). Catches `Exception`, not `BaseException` — `KeyboardInterrupt` / `SystemExit` still propagate for clean shutdown. Surfaced by reviewing the SDK under the same trust lens as baton-proxy (which structurally fails open).
- **`__version__` now derives from package metadata** (`importlib.metadata.version("baton-sdk")`) instead of a hand-bumped string. The hardcoded constant in `src/baton/__init__.py` had silently drifted — it was missed in both the 0.2.5 and 0.2.6 release commits, so events emitted by those releases carry a stale `sdk_version="0.2.4"` payload. After this release `pyproject.toml` is the single source of truth; the SDK version, the PyPI version, and the `sdk_version` field on every emitted event are guaranteed to agree. Any user upgrading from 0.2.5/0.2.6 will see the field jump to `"0.2.7"`.

### Changed

- **`BatonHandle` and `VendorConfig` extracted to shared modules.** Both adapter `install.py` files held identical definitions; moved to `baton.integrations._handle` (`BatonHandle` + `escalate()`) and `baton.integrations._config` (`VendorConfig` + validation). Public APIs (`from baton.integrations.{mcp,fastmcp} import VendorConfig, ...`) are preserved via re-export; no breaking changes.

### Wire format

No changes (event schema unchanged; the `sdk_version` field value corrects as noted above).

---

## 0.2.6 — fix escalate() session_id mismatch

### Fixed

- **`handle.escalate()` sent wrong `session_id` to Console.** The FastMCP
  adapter resolves the runtime MCP session UUID from `fastmcp_context` for
  every emitted event, but `handle.session_id` always held the SDK-generated
  fallback UUID. `escalate()` was therefore sending a session_id that the
  Console had no events for, resulting in 401 on every call from a live
  Claude session. Fix: `escalate()` now accepts an optional `session_id`
  keyword arg. FastMCP vendor tools should pass `ctx.session_id` from their
  tool handler's `Context`; the mcp adapter and dev mode can omit it and
  continue using the fallback.

---

## 0.2.5 — handle.escalate() + instructions shrink + session_id

### Added

- **`BatonHandle.escalate(annotation_seq=None)` (S3).** Calls `POST {console_url}/v0/escalate` synchronously and returns `{"ticket_id": ..., "ticket_url": ...}` in-turn, so vendor tools can surface the ticket URL to the user in the same response. Extracts Console URL and API key from `HttpSink` automatically. Falls back to `{"ticket_id": "queued", "ticket_url": None}` in dev mode (StdoutSink / FileSink) with a logged warning.
- **`BatonHandle.session_id` (#89).** The process-lifetime session ID is now a public attribute on the handle returned by `install_baton`. Vendor tools that need to correlate external artifacts with the Baton event stream can read it directly from the handle instead of extracting it through internal closures.
- **`HttpSink.url` and `HttpSink.api_key` properties.** Public read-only accessors for the Console URL and bearer token configured on the sink.

### Changed

- **Server-instructions template shrunk from ~2.3K chars to ~1.1K chars (#85).** Claude Code empirically truncates `InitializeResult.instructions` at ~2087 chars. Verbose field-by-field guidance and the consent-gated ticket flow scaffolding were dropped; the BEFORE/AFTER MUST/REQUIRED behavioral framing, full 8-value signal_type enum, and "annotation doesn't replace answering" guardrail remain. Headroom for vendor extensions is now ~960 chars.
- **Annotation tool description expanded** to absorb the field-level reference (intent / expected_outcome / workflow / signal_type / suggested_improvement / context keys). Tool descriptions are loaded at call time — the right place for the just-in-time field dictionary.
- **`build_server_instructions` raises `ValueError`** if the rendered output exceeds the 1500-char safety cap. Vendors with very long `vendor_display_name` values get a clear error at install time rather than silent truncation in production.
- **Canonical templates moved to `baton.integrations._llm_text`** (internal). Both adapter modules import from this shared module; per-adapter re-export shims removed.

### Wire format

No changes.

---

## 0.2.4 — Python compatibility fix (0.2.3 effectively uninstallable)

### Fixed

- **`requires-python` lowered from `>=3.14` to `>=3.11`.** The 3.14 pin was an authoring-host artifact, not a real dependency — the SDK is just `pydantic + httpx + the MCP wrapper`. 0.2.3 was effectively uninstallable in practice: 3.13 and earlier were excluded by metadata, and 3.14 itself hit native-build failures across the pyo3 ecosystem (`rpds-py`, `pydantic-core`, `jiter` — all depended on by transitive jsonschema/pydantic and didn't yet ship wheels or build against 3.14's C ABI even with `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1`). 0.2.4 ships clean install on 3.11–3.14.
- `except AttributeError, ValueError:` rewritten to the standard `except (AttributeError, ValueError):` form. The unparenthesized PEP-758 syntax was added in 3.14 — accidentally adopted; not load-bearing.
- `uuid.uuid7` (stdlib-only on 3.14+) replaced with a dependency on `uuid6>=2025.0` (pure-Python, MIT, RFC 9562). An inline polyfill was considered and rejected: it would have missed within-millisecond monotonicity (stdlib uuid7 provides this via clock-sequence; a naive bit-packed polyfill does not), and rolling UUID logic in an event-capture SDK trades a clear domain boundary for a tiny LOC win.

### CI

- **Python version matrix added to the `core` job** — was only running on 3.14, which is how 0.2.3 shipped broken. Now runs lint + typecheck + tests across 3.11 / 3.12 / 3.13 / 3.14 on every PR.

### Wire format

No changes. Drop-in replacement for 0.2.3.

---

## 0.2.3 — re-cut of 0.2.2 (CI format-check fix)

v0.2.2 was tagged but never published — GitHub Actions `core` job failed
at `ruff format --check src/ tests/` because local `make ci` wasn't
running format-check (only `ruff check`). v0.2.3 ships the formatter
fix + a Makefile correction so future `make ci` mirrors the workflow's
gate exactly.

No functional changes from what 0.2.2 should have been.

### Errata for the 0.2.2 mcp-adapter commit (see git log `9ff1bb5`)

The commit message stated that "Claude Code's tools/call requests do not include `_meta` on the wire, so the server receives None and our adapter correctly propagates that." **That observation was wrong.** It was caused by a vendor MCP server fork's venv holding a stale wheel of the SDK, not the local-source-with-new-code that the test was assumed to use. With the published 0.2.3 wheel correctly installed and the Console persisting `runtime_meta` to Postgres, Claude Code's `_meta` lands end-to-end on every `tool_call_*` event: `{"progressToken": <int>, "claudecode/toolUseId": "<toolu_...>"}`. Each tool call has a unique `toolUseId` — the per-call correlation primitive SPEC §11.5.1 calls for.

Annotation events still receive null `runtime_meta` in the mcp adapter — the annotation tool's handler doesn't take a `Context` kwarg (avoided earlier to dodge an mcp <1.20 `issubclass` bug; mcp >=1.20 is now required, so this is straightforward follow-up).

---

## 0.2.2 — runtime_meta on event envelope + mcp adapter refactor

### Added

- **Event envelope `runtime_meta: dict | None` field** per SPEC §11.4.1. Carries the raw MCP `_meta` dict from the request (PII-scrubbed via vendor's scrubber). The Console worker uses this to derive per-turn / per-cycle correlation that's more precise than `session_id` alone (which is only the SDK-process lifetime, not a conversation turn). Examples of meaningful keys captured: `claudecode/toolUseId`, `claudecode/sessionId`, `cursor/conversationId`, `progressToken`. Null when the host runtime didn't surface a meta or the adapter can't access it.
- `baton.integrations.fastmcp` (middleware + annotation): wires `runtime_meta` from `MiddlewareContext.fastmcp_context.request_context.meta` into every emitted event. Backwards-compatible: existing events that don't read the field are unaffected.

### Wire format

Additive — null default preserves backward compatibility with 0.2.x consumers that don't know about the field.

### Spec additions (informative for Console implementors)

- **SPEC §11.4.1** — `runtime_meta` field documentation + correlation hierarchy.
- **SPEC §11.5.1-3** — cycle-vs-session distinction, in-cycle annotation correlation (proactive must come from same cycle as reactive — the bug pattern that breaks ticketing Channels when they work off raw event windows), and the normative "Channels MUST consume Signals, not events" rule. Migration guidance for v0.2 Console implementations doing correlation in Channels.

---

## 0.2.1 — re-cut of 0.2.0 (yanked)

`0.2.0` was published from a stale commit due to a release-pipeline race: an
in-flight workflow run on the original `v0.2.0` tag was approved after we'd
re-tagged on the fix commit, so the original (pre-fix) wheel landed on PyPI.
`0.2.0` is yanked; `0.2.1` ships the intended 0.2.0 content (the rename,
the official-mcp-SDK adapter, the extras split, the mcp>=1.20 requirement)
with no functional changes from what 0.2.0 should have been.

Lesson and SOP follow-up: when a release tag needs to be re-cut on a fix
commit, cancel pending publish-approval workflow runs **first** — re-tagging
alone doesn't invalidate a paused run on the old tag.

---

## 0.2.0 — official `mcp` SDK adapter + rename (yanked)

### Breaking changes (pre-1.0; allowed per SPEC §13)

- **Renamed** `baton.integrations.mcp` → `baton.integrations.fastmcp`. The 0.1.x module adapted the standalone `fastmcp` library (jlowin/fastmcp); the name was misleading. It now lives at `baton.integrations.fastmcp` to match the PyPI package it targets.
- **The name `baton.integrations.mcp` is now reused** for a new adapter targeting the official Anthropic `mcp` package's `mcp.server.fastmcp.FastMCP` (see below). Vendors must update imports based on which FastMCP library they actually use.
- **Renamed pip extras**: `baton-sdk[mcp]` now installs `mcp>=1.10` (the official Anthropic library); `baton-sdk[fastmcp]` installs `fastmcp>=2.10` (the standalone library). Previously `[mcp]` installed the standalone fastmcp.

### Added

- `baton.integrations.mcp` — new adapter for the **official Anthropic `mcp` package's `FastMCP`** (`mcp.server.fastmcp.FastMCP`). The dominant production Python MCP library has no middleware system, so this adapter wraps each registered tool's handler in place. Tools added after `install_baton(...)` are also wrapped via a monkey-patched `add_tool`. Same vendor surface as the standalone-fastmcp adapter: `install_baton(mcp, VendorConfig(...))` returns a `BatonHandle`; sinks, events, scrubbing layer unchanged. Requires `mcp>=1.20` (earlier versions crash on stringified annotations from `from __future__ import annotations` due to an upstream `Tool.from_function` bug). Internal struct verified bit-stable across the supported range via CI matrix.
- `baton.integrations.mcp._registry` — single resolver for `_tool_manager._tools`. When upstream `mcp` PR #1951 lands (`FastMCP` → `MCPServer`, module path `mcp.server.fastmcp.*` → `mcp.server.mcpserver.*`), only this file needs updating.

### Fixed

- `baton.integrations.fastmcp.install_baton` falls back to `mcp._mcp_server.instructions = ...` when the public `instructions` setter raises `AttributeError`. Newer FastMCP versions (>=1.10) made `instructions` a read-only property; the fallback writes to the backing `MCPServer` instance directly so the server-instructions template still ships.
- `baton.__version__` now matches the released package version. The hardcoded value was stuck at `"0.1.0"` through the `0.1.1` release, mislabeling the `sdk_version` field on every emitted event (SPEC §11.4). Permanent fix (read from package metadata dynamically) is a follow-up.

---

## 0.1.1 — doc fixes

- Fix `pip install baton[...]` strings in `baton.integrations` and `baton.integrations.mcp` package docstrings to the correct `pip install baton-sdk[...]` form. No behavioral change; `baton` on PyPI is an unrelated project (the iRODS wrapper) and copy-pasting the old strings would install the wrong package.
- Release automation: GitHub Actions workflow (`.github/workflows/release.yml`) now publishes via PyPI Trusted Publishing (OIDC) on `v*` tag push. No API token in repo secrets.

---

## 0.1.0 — initial public release

First public release. Pre-1.0 — no API stability promise yet; expect breaking changes until v1.0, consistent with the surrounding-OSS convention.

Public surface:

### Core

- `baton.Client` — sync library API for Skill-instrumented agent code (see the "Library API" section in `README.md`).
- `baton.AsyncClient` — async equivalent.
- `baton.Trace`, `baton.AsyncTrace` — context managers returned by `client.trace(...)`; emit `tool_call_start` / `tool_call_end` / `tool_call_error` events around a wrapped call.
- `baton.SignalType` — `StrEnum` mirroring SPEC §3.1 signal types (`failure`, `retry_loop`, `dead_end`, `parameter_confusion`, `slow_performance`, `abandonment`, `feature_gap`, `other`).
- `baton.__version__` — embedded in every emitted event's `sdk_version` field.
- `consent_token` is required on every `Client` / `AsyncClient` construction and on every emitted event (per SPEC §2.3 + §11.4); missing consent raises `ValueError` at init.
- `trace.session_id` — public correlation handle.
- `trace.annotate(...)` — reactive friction-signal helper that binds the trace's `session_id` automatically.
- `trace.observed(error=...)` — exception-object shorthand for the failure path (derives `error_type` + `error_body` from the exception).

### Sinks (`baton.sinks`)

- `Sink` — async ABC. All sinks implement `write` / `flush` / `aclose`.
- `StdoutSink(stream=sys.stderr)` — zero-config JSONL to stderr.
- `FileSink(path)` — JSONL append to a file.
- `HttpSink(url, api_key=...)` — bounded buffer + retry + circuit breaker; POSTs to `{url}/v0/events` with bearer auth.
- `MultiSink([...])` — fan out to multiple sinks; failures aggregated via `ExceptionGroup`.

### Integrations

- `baton.integrations.mcp.install_baton(mcp, VendorConfig(...))` — FastMCP middleware path. Opt-in via `pip install baton-sdk[mcp]`.
- `baton.integrations.mcp.VendorConfig` — required: `vendor_id`, `vendor_display_name`, `consent_token`. Optional: `sink` (defaults to `StdoutSink()`).
- `baton.integrations.mcp.BatonHandle` — returned from `install_baton`; exposes `flush()` and `aclose()`.

### Wire format (SPEC §11.4)

- Event envelope: `event_id`, `event_type`, `tenant_id`, `session_id`, `sequence_number`, `captured_at`, `consent_token`, `sdk_version`, `agent_runtime`, `payload`.
- Event types: `tool_call_start`, `tool_call_end`, `tool_call_error`, `annotation`.
- Annotation events are discriminated proactive vs reactive via the `signal_type` field's presence (SPEC §11.4 sub-section + §11.5 correlation rules).

### Documentation

- `docs/SPEC.md` — canonical wire-protocol specification.
- `docs/CHARTER.md` — load-bearing decisions, SDK boundary rules.

### Known limitations (pre-release)

- Public API is **not yet stable**. Pre-1.0 means breaking changes can land at any minor bump. This CHANGELOG records SDK package changes (Python API, package layout, behavior); `docs/SPEC.md §13` records wire-format changes.
- PII scrubbing is currently a no-op identity function (`src/baton/scrub.py`); real scrub rules land in a subsequent release.
- Auto-detection only fires for `failure` (on exception) and `retry_loop`; the other signal types must be agent-raised via the annotation tool (per SPEC §6.4).
- Synchronous return channel (agent autopickup of vendor responses) deferred; current release uses out-of-band notification (SPEC §8.1).
- Single-static-UUID consent model; per-end-user `POST /v0/consent` issuance is deferred (SPEC §2.3).
