# skill_demo

## Why

A worked example of the library API (`baton.Client` / `AsyncClient`) in the shape an agent would actually write following a vendor-published Skill — the chat-completions code pattern with a Baton `trace(...)` wrap and a reactive `annotate(...)` on failure.

Useful as a copy-paste starting point for vendors writing their own Baton-aware Skill content, or as a worked end-to-end of the trace + observe + annotate surface.

## What's in the box

| File | Purpose |
|---|---|
| `demo.py` | The worked example. Mirrors the prescribed pattern from a typical chat-completions Skill (vendor-agnostic). Two calls: one succeeds, one fails with a real-world capability-mismatch pattern. The failure triggers an agent-raised `annotate(signal_type=DEAD_END, ...)` — the "ticket." |
| `fake_vendor.py` | Minimal stub of a vendor SDK client (`client.chat.completions.create` surface — a common chat-completions API shape). Reproduces a 400 ("Grammar must have a 'properties' field") when `response_format={"type": "json_schema"}` is sent to a small model that doesn't support it. Real customer code would import from the vendor's SDK instead — that's the only swap. |
| `local_ingest.py` | Tiny stdlib HTTP collector that prints received events; lets `demo.py` ship over a real HTTP target without standing up infrastructure. |

## Storyline

**The capability mismatch.**

The user prompt:

> Extract product attributes from this messy text — return name, price, and category as a strict JSON object. Use the cheapest fast small model.

The agent (following the chat-completions Skill) writes idiomatic Python: pick a model, pass `response_format=json_schema`. The call returns 400 with a cryptic grammar error. The agent recognizes this as a capability gap and emits a structured `dead_end` signal back to the vendor's PM team via Baton.

Why this storyline:
- Reproducible against a documented real-world failure pattern that surfaces across multiple inference vendors today.
- The signal Baton emits maps cleanly to the SPEC §3.1 `dead_end` semantics.
- The "ticket" content writes itself — naming the missing capability matrix + suggesting the fix is the canonical Baton hero moment.

## Out of scope

- A real vendor API. We use a stub; this is library-mechanics validation, not vendor-integration validation.
- A Baton-aware Skill that an agent could load via `npx skills add`. That's vendor-side content separate from this library example.
- The MCP integration. Library-mode only.

## Running it

Two terminals:

```sh
# Terminal 1 — ingest emulator (stdlib HTTP server that mimics the Console's POST /v0/events)
cd <repo-root>
.venv/bin/python examples/skill_demo/local_ingest.py

# Terminal 2 — demo
cd <repo-root>
.venv/bin/python examples/skill_demo/demo.py
```

Expected event sequence in `events.jsonl`:

1. `tool_call_start` — preflight `vendor.chat.completions.create`
2. `annotation` — proactive intent capture (auto-emitted because `intent`/`expected_outcome`/`workflow` were set on the trace)
3. `tool_call_end` — preflight succeeds
4. `tool_call_start` — extraction call
5. `annotation` — proactive intent capture for the extraction
6. `tool_call_error` — the 400
7. `annotation` — agent-raised `dead_end` signal (the ticket)
