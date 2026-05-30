# Per-repo guidance for AI coding agents

*Follows the [AGENTS.md](https://agents.md) convention — discovered automatically by Claude Code, Cursor, OpenAI Codex CLI, and other AI coding agents.*

This is the Baton SDK repo (pre-1.0, `0.1.x`). The canonical strategic frame lives in `docs/CHARTER.md` and the wire protocol in `docs/SPEC.md`. Read those before suggesting architectural changes.

## Architectural posture (CHARTER ADR-4)

The SDK is a **thin event emitter**. It captures events at the MCP transport boundary (tool_call_start / tool_call_end / tool_call_error / annotation) and ships them to the Console worker via HTTPS. **The SDK does NOT:**
- Maintain session state beyond a bounded local event buffer
- Implement detection rules that require multi-event correlation (retry_loop, dead_end pattern matching)
- Implement a policy layer
- Implement egress Channels (Pylon, Slack, Notion — those live Console-side in `baton-console/`)
- Block the vendor's hot path on Console availability

If you find yourself about to add state, a Channel, or policy logic to this codebase — stop. It belongs in `baton-console/`. See SPEC §11 for the capture/interpretation/egress separation.

## SDK boundary discipline (CHARTER §3)

1. **No vendor-specific imports.** Not in source, not in tests. If the SDK imports from any vendor module, the architecture is broken.
2. **Baton only sees what MCP transport carries.** No reaching into vendor app logs, request context, DB, or observability events.
3. **The spec lives in `docs/SPEC.md`, never in a vendor repo.**
4. **Public API is the contract.** Anything exported from `src/baton/__init__.py` is what vendors integrate against; breaking changes require a SPEC §13 changelog entry.
5. **Tests use fake-vendor fixtures only.** Tests prove the SDK works for any vendor.
6. **Integration is ~5 lines or the SDK is failing.** If `install_baton(...)` grows past 5 lines in a vendor repo, refactor Baton, not the vendor.
7. **No Baton-brand string leaks** to the calling agent or end user (whitelabel obligation per SPEC §5.4).

## Tooling

- Python 3.14+ (uses stdlib `uuid.uuid7()` per SPEC §3 + §11.4; 3.14 is current stable as of 2026)
- `uv` or `pip` for env management (Makefile uses `pip` for portability)
- `ruff` for lint + format
- `mypy --strict` for typing
- `pytest` + `pytest-asyncio` + `pytest-httpserver` for tests

Use `make ci` as the canonical gate (matches GitHub Actions).

## What lives where

- `src/baton/` — the SDK package (Python). Core substrate at the top level (`sinks.py`, `events.py`, `scrub.py`, `_state.py`, `client.py`). Integrations under `src/baton/integrations/*` — today: `mcp/` (`install_baton`, `VendorConfig`, middleware, annotation tool). Pattern: core SDK + opt-in integrations via pip extras.
- `docs/` — canonical strategic and protocol docs: `CHARTER.md`, `SPEC.md`.
- `examples/` — runnable usage examples (the four-rung sink ladder, the library-API skill demo, the e2e smoke test).
- `tests/` — test suite. Integration tests live under `tests/integrations/<name>/` mirroring the source layout.

**Public API and the contract:** anything exported from `src/baton/__init__.py` (core: `Client`, `AsyncClient`, `SignalType`, `__version__`), `src/baton/sinks.py` (the `Sink` ABC + implementations), or `src/baton/integrations/<name>/__init__.py` (per-integration; today: `install_baton`, `VendorConfig`, `BatonHandle` under `baton.integrations.mcp`) is what vendors integrate against. Breaking changes require a SPEC §13 changelog entry. MCP-side integrations import directly from `baton.integrations.mcp` — there is no top-level re-export.

## When in doubt

Read `docs/CHARTER.md` first. It's the North Star.
