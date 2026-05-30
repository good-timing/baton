# library_e2e spike

End-to-end validation that the Baton library API (`baton.Client`, `baton.AsyncClient`, `baton.SignalType`) works against the SPEC §11.4 event ingest contract — short of an actual Console behind the ingest URL.

This is the library counterpart to the MCP integration spike (which validates the FastMCP middleware path). Both paths feed the same event-ingest contract (`POST /v0/events` with Bearer auth, SPEC §11.4 envelope), so a Console wired for one path automatically works for the other.

## What this spike validated

| Concern | Result |
|---|---|
| **Sync `Client` emits via background thread bridge cleanly** | ✅ `_SyncBridge` daemon thread + persistent loop pattern works |
| **`AsyncClient` emits directly on the caller's async loop** | ✅ No thread bridge; same `EventEmitter` underneath |
| **Tool-call lifecycle: `start` → `end` with observed result** | ✅ Both sync + async; correct `duration_ms` populated |
| **Proactive annotation when `intent`/`expected`/`workflow` set** | ✅ Emits inside `__enter__` in same `session_id` as start |
| **Reactive standalone annotation via `client.annotate(...)`** | ✅ Emits with full payload incl. `signal_type` + `context` |
| **Exception path emits `tool_call_error` and re-raises** | ✅ Both sync + async; `error_type` = exception class name |
| **Both paths target the SAME `POST /v0/events` ingest contract** | ✅ Bearer auth header verified; envelope matches SPEC §11.4 |
| **Event envelope shape matches SPEC §11.4** | ✅ All required fields present; `spec_version=0.2` |
| **`agent_runtime` correctly set to `"python-library"`** | ✅ All 12 captured events |
| **Sequence numbers monotonic per `session_id`** | ✅ Per-event mode: each `Trace` mints a fresh `session_id` |

## What this spike did NOT validate (out-of-scope per `PLAN.md`)

1. **Real customer agent adherence** — that a real customer's agent following a vendor-published Skill will reliably call `client.trace(...)`. This is a Skill-design + adherence question, not engineering.
2. **Session correlation under streamable HTTP** — per SPEC §3.4, library mode defaults to `correlation_mode=per-event` for v0.1. Session-stitched mode requires Skill-author cooperation to pass explicit `session_id`s.
3. **`consent_token` scaling beyond v0.1 single-static-UUID** — `POST /v0/consent` per-end-user issuance is v0.2 work; see SPEC §2.3.
4. **Real vendor SDK behavior** — we use a synthetic stub shaped like a typical chat-completions API response, but no real vendor API calls are made.

## Architecture under test

```
   Test driver (smoke_test_library.py)
            │
            │  starts stdlib HTTP capture server (random localhost port)
            ▼
   localhost:<port>  ── (POST /v0/events) ──┐
                                            │
            │  drives sync via Client       │
            │  drives async via AsyncClient │
            ▼                                │
   baton.Client / baton.AsyncClient ────────┘
      ├ Trace / AsyncTrace context managers
      ├ EventEmitter (bounded buffer + retry + circuit breaker)
      └ _SyncBridge (daemon thread + persistent asyncio loop, sync path only)
```

No Console required — the capture server replaces it for the test. Real ingest payloads land in `_captured` (list of dicts); the script asserts on shape, ordering, and counts.

## Files

- `PLAN.md` — engineering plan that scoped this spike (7 phases, decisions, success criteria)
- `smoke_test_library.py` — the actual e2e driver
- `README.md` — this file

## Running the spike

```sh
cd <repo-root>
.venv/bin/python examples/library_api_smoke_test/smoke_test_library.py
# Should print "[spike] OK - library API e2e smoke test passed"
```

No external dependencies, no Console, no Postgres. Self-contained.

## What this unblocks

- **Skills-pattern partnerships** — the library API substrate now exists; the next step in any partnership is coordinating a vendor-published Baton-aware Skill that drives this API from agent-generated code.
- **Future managed-agents integration** (`baton.integrations.managed_agents`) — the core `EventEmitter` + event schema work for the library API works for any future async-shaped integration too; managed-agents integration is the same pattern with an additional surface adapter.
