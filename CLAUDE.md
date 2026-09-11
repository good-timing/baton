# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical guidance

`AGENTS.md` is the canonical per-repo agent doc (follows the [agents.md](https://agents.md) convention). Read it first — it covers architectural posture, SDK boundary discipline, and the "what lives where" map. Strategic decisions live in `docs/CHARTER.md`; the wire protocol lives in `docs/SPEC.md`. **Read CHARTER before suggesting architectural changes.**

## Commands

```sh
make install        # uv sync --locked --extra dev  (fails if uv.lock is stale)
make ci             # canonical gate: lint + typecheck + test (matches GitHub Actions)

make lint           # ruff check src/ tests/
make format         # ruff format src/ tests/ (write changes)
make typecheck      # mypy --strict on src/baton
make test           # pytest -q
make test-cov       # pytest with coverage report (term + html)
make build          # python -m build
make clean          # remove caches, build artifacts, __pycache__
```

Run a single test:

```sh
.venv/bin/pytest tests/test_sinks.py::TestHttpSink::test_retry_on_500 -v
.venv/bin/pytest tests/integrations/official/ -v      # official mcp SDK adapter
.venv/bin/pytest tests/integrations/standalone/ -v   # standalone fastmcp adapter
.venv/bin/pytest -k "annotation" -v                # by keyword
```

CI uses `make ci`; if it's green locally it should be green on PR.

## Architecture in one screen

The SDK is a **thin event emitter** (CHARTER ADR-4). Three integration paths emit the same event envelope (`SPEC §11.4`) through a pluggable `Sink`:

```
Capture surface                                  Sink layer (baton/sinks.py)
─────────────────────────                        ──────────────────────────
MCP — official `mcp` SDK adapter         ───┐
  baton.integrations.official               │
  (tool-handler wrapping)                   │
                                            │
MCP — standalone `fastmcp` adapter       ───┤
  baton.integrations.standalone             ├─►  StdoutSink / FileSink / HttpSink / MultiSink
  (middleware chain)                        │      │
                                            │      ▼
Library API (vendor-side)                   │   (collector: any compatible HTTPS endpoint;
  baton.Client / AsyncClient                │    hosted Console is one such)
  with client.trace(...): ...               │
                                            │
Customer plugin (CC integration)         ───┘
  (planned; baton.integrations.claude_code)
```

The SDK does NOT keep session state beyond a bounded in-memory buffer, does NOT implement detection rules requiring multi-event correlation, does NOT implement policy or Channels. Those live downstream of the sink (Console worker).

## Source map (load-bearing files)

- `src/baton/__init__.py` — public top-level exports (`Client`, `AsyncClient`, `Trace`, `SignalType`, `__version__`). **The public contract.** Breaking changes need a `SPEC §13` changelog entry.
- `src/baton/sinks.py` — `Sink` ABC + `StdoutSink` / `FileSink` / `HttpSink` / `MultiSink`. The `Sink` protocol is `async write/flush/aclose`.
- `src/baton/events.py` — Pydantic `_EventEnvelope` + per-type payloads. The wire schema.
- `src/baton/client.py` — `Client` (sync, via background thread bridge) + `AsyncClient` + `Trace` / `AsyncTrace` context managers.
- `src/baton/scrub.py` — PII scrubber. **On by default** — `VendorConfig(scrubber=None)` resolves to `Scrubber()`, the same ruleset `baton-proxy` ships: email / bearer / `sk-*` / `AKIA*` / JWT / Luhn-checked card / NA phone, plus the field-name overrides in `REDACT_FIELD_NAMES`. Opt OUT with `VendorConfig(scrubber=identity_scrub)`. ⚠ **This line said "default is no-op identity" until 2026-09-11, which was backwards** — and it is the line a session reaches for when writing anything user-facing about what we collect. ⚠ It is PATTERN matching, not a guarantee: measured, `{"name": "Jane Doe", "address": "12 Elm St"}` passes through untouched while a card number does not. Never write "PII is scrubbed" on the strength of it.
- `src/baton/_dsn.py` — the packed connection string (`https://<key>@host/<tenant>/<server>`), parse only. Every raise goes through one `_fail` exit that sweeps the bearer out of the message; `Dsn.key` is `repr=False`. Nothing here reaches the wire.
- `src/baton/integrations/_config.py` — **where `VendorConfig` is actually defined**, plus `resolve_config` / `resolve_sink` / `build_config`. Both adapters' `install.py` re-export it; the class lives here, so a field change is one edit, not two.
- `src/baton/integrations/_llm_text.py` — **the server-instructions template (`SPEC §5.1.2`) and the annotation tool description**, shared by both adapters. Also the three injected params (`user_goal` / `expected_result` / `overall_task`) and `SIGNAL_TYPES`. ⚠ There is no `instructions.py` in either adapter folder; this map claimed one in both until 2026-09-11.
- `src/baton/integrations/official/` — **Official `mcp` SDK adapter.** Targets `mcp.server.fastmcp.FastMCP` on 1.x AND `mcp.server.mcpserver.MCPServer` on 2.x — `_compat.py` owns that two-branch import (upstream PR #1951 renamed the class, the `Context`, and `_mcp_server` → `_lowlevel_server`). `install.py` (entrypoint; re-exports `VendorConfig` from `_config.py`), `_tool_wrap.py` (wraps each registered tool's handler — no middleware in this library), `_registry.py` (resolver for `_tool_manager._tools`), `annotation.py` (registers `<vendor>_annotate`), `_auth.py` (the `get_access_token()` read behind `user_id`).
- `src/baton/integrations/standalone/` — **Standalone `fastmcp` adapter.** Targets `fastmcp.FastMCP` across the supported band, **2.x through 4.x** (`fastmcp>=2.14,<5` — the floor and the major cap are both deliberate, see `pyproject.toml`); this said "v2.x" until 2026-09-11. `install.py` (entrypoint; re-exports `VendorConfig`), `middleware.py` (`BatonMiddleware` — fastmcp's native middleware chain), `annotation.py`, `_session.py` (SPEC §3.4's session ladder), `_auth.py`.
- `src/baton/integrations/runtime_adapter.py` — **`_meta`-based agent-runtime detection, shared by BOTH adapters.** Lived under `standalone/` until 2026-09-09, which is why the official adapter never called it and shipped `agent_runtime: "unknown"` on every event. Call it on the RAW `_meta`, before the vendor's scrubber. ⚠ **The reason given here was wrong and the conclusion still holds.** This said "the default scrubber is a no-op"; it is not, and has not been. What is true is narrower and has the same consequence: measured 2026-09-11, the default `Scrubber()` leaves a realistic `_meta` (`claudecode/toolUseId`, `clientInfo`, `traceparent`) byte-identical, because none of those values match a pattern or a redacted field name. So a post-scrub call still passes every test here and still fails at the first vendor whose scrubber touches meta keys. **No override, but not therefore trusted.** The `io.baton/*` keys and `VendorConfig.default_agent_runtime` were both removed 2026-09-09, so nothing overrides the resolution — but the top two tiers report the client's own `clientInfo.name`, which is client text. Those tiers scrub (via `VendorConfig(scrubber=...)`) and cap at 128; the `claudecode/*` heuristic returns an SDK constant and does neither. A scrubber returning a non-string loses that tier rather than stringifying onto the wire.
- `src/baton/integrations/{mcp,fastmcp}.py` — **temporary silent aliases** at the pre-rename import paths. They exist because `baton-console` depends on `baton-sdk` by floor, not by pin. **Do not delete them on any event** — the console switching was the original trigger and it was wrong, and a hand-written replacement list was wrong too. The shim docstrings carry the precondition as a runnable grep across the sibling repos; run it, don't restate it here. Pinned by `tests/test_import_path_aliases.py`.

## Boundary rules that fail review (from CHARTER §3)

1. **No vendor-specific imports** in `src/baton/` or `tests/`. If a test imports a real vendor module, it's wrong.
2. **The SDK only sees what crosses MCP transport** (tool name, params, result, error). No reaching into vendor logs / request context / DB.
3. **No `print()` in `src/`.** The `T20` ruff rule guards this — `print()` would corrupt the MCP JSON-RPC stream under stdio transport. Use `logging` instead.
4. **Tests use fake-vendor fixtures only** (`pytest-httpserver`, FastMCP's in-process `Client`).
5. **Integration is ~5 lines or the SDK is failing.** If `install_baton(...)` grows past one call in a vendor repo, refactor Baton, not the vendor.

## Examples as runnable references

`examples/` contains the four-rung sink ladder (`01_stdout` → `02_local_file` → `03_local_https` → `04_hosted_console`) — the same demo flow against each sink. Use these as the canonical end-to-end shape when implementing new behavior. `examples/skill_demo/` exercises the library API path; `examples/library_api_smoke_test/` is a copy-paste-friendly integration-test starting point.

## When in doubt

`docs/CHARTER.md` is the North Star. `docs/SPEC.md` is the wire-format contract. Don't add state, policy, Channels, or vendor-specific code to this SDK without re-reading them first.
