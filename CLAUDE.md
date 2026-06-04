# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical guidance

`AGENTS.md` is the canonical per-repo agent doc (follows the [agents.md](https://agents.md) convention). Read it first — it covers architectural posture, SDK boundary discipline, and the "what lives where" map. Strategic decisions live in `docs/CHARTER.md`; the wire protocol lives in `docs/SPEC.md`. **Read CHARTER before suggesting architectural changes.**

## Commands

```sh
make install        # creates .venv, installs in editable mode with [dev,mcp] extras
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
.venv/bin/pytest tests/integrations/mcp/ -v          # official mcp SDK adapter
.venv/bin/pytest tests/integrations/fastmcp/ -v      # standalone fastmcp adapter
.venv/bin/pytest -k "annotation" -v                # by keyword
```

CI uses `make ci`; if it's green locally it should be green on PR.

## Architecture in one screen

The SDK is a **thin event emitter** (CHARTER ADR-4). Three integration paths emit the same event envelope (`SPEC §11.4`) through a pluggable `Sink`:

```
Capture surface                                  Sink layer (baton/sinks.py)
─────────────────────────                        ──────────────────────────
MCP — official `mcp` SDK adapter         ───┐
  baton.integrations.mcp                    │
  (tool-handler wrapping)                   │
                                            │
MCP — standalone `fastmcp` adapter       ───┤
  baton.integrations.fastmcp                ├─►  StdoutSink / FileSink / HttpSink / MultiSink
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
- `src/baton/scrub.py` — PII scrubber interface. Default is no-op identity; vendors handling sensitive data MUST supply their own via `VendorConfig(scrubber=...)`.
- `src/baton/integrations/mcp/` — **Official `mcp` SDK adapter** (targets `mcp.server.fastmcp.FastMCP`): `install.py` (entrypoint + `VendorConfig`), `_tool_wrap.py` (wraps each registered tool's handler — no middleware in this library), `_registry.py` (resolver for `_tool_manager._tools`; single swap point for upstream rename PR #1951), `annotation.py` (registers `<vendor>_annotate` tool), `instructions.py` (server-instructions template per `SPEC §5.1.2`).
- `src/baton/integrations/fastmcp/` — **Standalone `fastmcp` adapter** (targets `fastmcp.FastMCP` v2.x): `install.py` (entrypoint + `VendorConfig`), `middleware.py` (`BatonMiddleware` — uses fastmcp's native middleware chain), `annotation.py`, `instructions.py`, `runtime_adapter.py` (`_meta`-based agent runtime detection).

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
