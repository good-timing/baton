# Changelog

All notable changes to the Baton SDK will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it leaves pre-release.

**Spec changes** (anything affecting the wire format) are also recorded in `docs/SPEC.md §13`. This file is for the SDK package's user-facing changelog; SPEC §13 is the canonical wire-format change log.

---

## Unreleased

Initial public release pending.

---

## 0.1.0 — initial public release

First public release. The codebase has internal-history lineage as a v0.1-then-v0.2 rewrite (preserved in `docs/CHARTER.md` and `docs/design-notes/`), but the public version line starts at `0.1.0` — pre-1.0 signals "no API stability promise yet; expect breaking changes until v1.0," consistent with the surrounding-OSS convention.

Public surface:

### Core

- `baton.Client` — sync library API for Skill-instrumented agent code (per `docs/SKILLS_LIBRARY_API_DRAFT.md`).
- `baton.AsyncClient` — async equivalent.
- `baton.Trace`, `baton.AsyncTrace` — context managers returned by `client.trace(...)`; emit `tool_call_start` / `tool_call_end` / `tool_call_error` events around a wrapped call.
- `baton.SignalType` — `StrEnum` mirroring SPEC §3.1 signal types (`failure`, `retry_loop`, `dead_end`, `parameter_confusion`, `slow_performance`, `abandonment`, `feature_gap`, `other`).
- `baton.SPEC_VERSION`, `baton.__version__` — version markers embedded in every emitted event.
- `consent_token` is required on every `Client` / `AsyncClient` construction and on every emitted event (per SPEC §2.3 + §11.4); missing consent raises `ValueError` at init.
- `trace.session_id` — public correlation handle.
- `trace.annotate(...)` — reactive friction-signal helper that binds the trace's `session_id` automatically.
- `trace.observed(error=...)` — exception-object shorthand for the failure path (derives `error_type` + `error_body` from the exception).

### Integrations

- `baton.integrations.mcp.install_baton(mcp, VendorConfig(...))` — FastMCP middleware path (~5-line integration in a vendor's MCP server). Opt-in via `pip install baton-sdk[mcp]`.
- `baton.integrations.mcp.VendorConfig` — required: `vendor_id`, `vendor_display_name`, `console_url`, `api_key`, `consent_token`.
- `baton.integrations.mcp.BatonHandle` — returned from `install_baton`; exposes `flush()` and `aclose()`.

### Wire format (SPEC §11.4)

- Event envelope: `event_id`, `event_type`, `tenant_id`, `session_id`, `sequence_number`, `captured_at`, `consent_token`, `spec_version`, `sdk_version`, `agent_runtime`, `payload`.
- Event types: `tool_call_start`, `tool_call_end`, `tool_call_error`, `annotation`.
- Annotation events are discriminated proactive vs reactive via the `signal_type` field's presence (SPEC §11.4 sub-section + §11.5 correlation rules).

### Documentation

- `docs/SPEC.md` — canonical wire-protocol specification.
- `docs/CHARTER.md` — load-bearing decisions, SDK boundary rules, open decisions log.
- `docs/SKILLS_LIBRARY_API_DRAFT.md` — library API surface design.

### Known limitations (pre-release)

- Public API is **not yet stable**. Breaking changes are expected before v1.0; SPEC §13 will record them.
- PII scrubbing is currently a no-op identity function (`src/baton/scrub.py`); real scrub rules land in a subsequent release.
- Auto-detection for `slow_performance`, `abandonment`, `dead_end` is deferred to v0.2+; v0.1 only auto-detects `failure` (on exception) and relies on agent-raised annotation for the rest (per SPEC §6.4).
- Synchronous return channel (agent autopickup of vendor responses) deferred; v0.1 uses out-of-band notification (SPEC §8.4).
- Single-static-UUID consent model; per-end-user `POST /v0/consent` issuance is v0.2 work (SPEC §2.3).
