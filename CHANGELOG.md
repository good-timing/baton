# Changelog

All notable changes to the Baton SDK will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it leaves pre-release.

**Spec changes** (anything affecting the wire format) are also recorded in `docs/SPEC.md §13`. This file is for the SDK package's user-facing changelog; SPEC §13 is the canonical wire-format change log.

---

## Unreleased

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
