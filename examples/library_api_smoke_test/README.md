# Library-API smoke test

End-to-end smoke test for the `baton.Client` / `baton.AsyncClient` library API. Self-contained: spins up an in-process stdlib HTTP server (random localhost port) that mimics the Console's `POST /v0/events` endpoint, drives both sync and async paths through the SDK, and asserts on the captured event stream.

No external dependencies; no Console; no Postgres. Useful as:

- A regression check that the library API still emits the full SPEC §11.4 envelope correctly.
- A copyable starting point for your own integration tests when wiring Baton into a vendor SDK.

## Running it

```sh
cd <repo-root>
.venv/bin/python examples/library_api_smoke_test/smoke_test_library.py
# Should print: [spike] OK - library API e2e smoke test passed
```

## What it validates

| Concern | How |
|---|---|
| Sync `Client` emits via background thread bridge | Drives a synthetic chat-completion through `Client` + asserts events arrive |
| `AsyncClient` emits directly on the caller's async loop | Same shape, async path |
| Tool-call lifecycle: `start` → `end` with observed result | Sync + async; checks `duration_ms` populated |
| Proactive annotation when `intent`/`expected`/`workflow` set | Auto-emits inside `__enter__` with same `session_id` as start |
| Reactive standalone annotation via `client.annotate(...)` | Full payload incl. `signal_type` + `context` |
| Exception path emits `tool_call_error` and re-raises | Sync + async; `error_type` = exception class name |
| Both paths target the SAME `POST /v0/events` ingest contract | Bearer auth header verified; envelope matches SPEC §11.4 |
| Event envelope shape matches SPEC §11.4 | All required envelope fields present |
| `agent_runtime` set to `"python-library"` | All captured events |
| Sequence numbers monotonic per `session_id` | Per-event mode default |


