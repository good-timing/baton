# Per-repo guidance for Claude Code sessions

This is the Baton SDK repo (v0.2 active development). The canonical strategic frame lives in `docs/CHARTER.md` and the wire protocol in `docs/SPEC.md`. Read those before suggesting architectural changes.

## Architectural posture (CHARTER OD-7)

The SDK is a **thin event emitter**. It captures events at the MCP transport boundary (tool_call_start / tool_call_end / tool_call_error / annotation) and ships them to the Console worker via HTTPS. **The SDK does NOT:**
- Maintain session state beyond a bounded local event buffer
- Implement detection rules that require multi-event correlation (retry_loop, dead_end pattern matching)
- Implement a policy layer
- Implement egress Channels (Pylon, Slack, Notion — those live Console-side in `baton-console/`)
- Block the vendor's hot path on Console availability

If you find yourself about to add state, a Channel, or policy logic to this codebase — stop. It belongs in `baton-console/`. See SPEC §11 for the capture/interpretation/egress separation.

## SDK boundary discipline (CHARTER §4)

1. **No vendor-specific imports.** Not in source, not in tests. If the SDK imports from any vendor module, the architecture is broken.
2. **Baton only sees what MCP transport carries.** No reaching into vendor app logs, request context, DB, or observability events.
3. **The spec lives in `docs/SPEC.md`, never in a vendor repo.**
4. **Public API is the contract.** Anything exported from `src/baton/__init__.py` is what vendors integrate against; breaking changes require a SPEC §13 changelog entry.
5. **Tests use fake-vendor fixtures only.** Tests prove the SDK works for any vendor.
6. **Integration is ~5 lines or the SDK is failing.** If `install_baton(...)` grows past 5 lines in a vendor repo, refactor Baton, not the vendor.
7. **No Baton-brand string leaks** to the calling agent or end user (whitelabel obligation per SPEC §5.5).

## Spec-first, failing-test-first

For any non-trivial change:
1. Update `docs/SPEC.md` and/or `docs/CHARTER.md` first.
2. Write a failing test against fake-vendor fixtures.
3. Run, confirm RED.
4. Implement minimum code to pass.
5. Refactor (optional). Stay green.

## What's archived

The v0.1 SDK implementation was a prototype to validate the spec; v0.2 (this codebase) is a clean rewrite per CHARTER OD-7. Validated learnings live in `docs/` (including `docs/design-notes/`) — don't reference v0.1 code as the basis for v0.2 changes.

## Tooling

- Python 3.14+ (uses stdlib `uuid.uuid7()` per SPEC §3 + §11.4; 3.14 is current stable as of 2026)
- `uv` or `pip` for env management (Makefile uses `pip` for portability)
- `ruff` for lint + format
- `mypy --strict` for typing
- `pytest` + `pytest-asyncio` + `pytest-httpserver` for tests

Use `make ci` as the canonical gate (matches GitHub Actions).

## What lives where

- `src/baton/` — the SDK package (Python). Core substrate at the top level (`emitter.py`, `events.py`, `scrub.py`, `_state.py`; `client.py` + `aclient.py` forthcoming per `docs/design-notes/library_api_engineering_plan.md`). Integrations under `src/baton/integrations/*` — today: `mcp/` (`install_baton`, `VendorConfig`, middleware, annotation tool). Future: `managed_agents/`, `a2a/`. See `docs/design-notes/integration_reorg.md` for the rationale (Sentry/Datadog/OTel pattern: core + opt-in integrations via pip extras).
- `docs/` — canonical strategic and protocol docs (don't move; cross-referenced from CHARTER + memory)
- `docs/design-notes/` — engineering memos and design-validation records (load-bearing for future spec discussions; don't delete)
- `examples/` — runnable usage examples (the library API skill demo, the e2e smoke test)
- `tests/` — test suite. Integration tests live under `tests/integrations/<name>/` mirroring the source layout.
- The v0.1 archive does not live in this repo. v0.1 was a prototype; v0.2 is the canonical codebase.

**Public API and the contract:** anything exported from `src/baton/__init__.py` (core: `Client`, `AsyncClient`, `SignalType`, version markers) or `src/baton/integrations/<name>/__init__.py` (per-integration; today: `install_baton`, `VendorConfig`, `BatonHandle` under `baton.integrations.mcp`) is what vendors integrate against. Breaking changes require a SPEC §13 changelog entry. MCP-side integrations import directly from `baton.integrations.mcp` — there is no top-level re-export.

## When in doubt

Read `docs/CHARTER.md` first. It's the North Star.
