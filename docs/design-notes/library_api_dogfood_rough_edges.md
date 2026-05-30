# Rough edges — library API dogfood findings

*Dogfood log from `examples/skill_demo/` (opened 2026-05-28, closed 2026-05-29). Each entry: what we hit, severity, recommended fix, resolution.*

Severity scale:
- **P0** — blocks real-integrator use / would embarrass the SDK in front of a first-time consumer.
- **P1** — noticeable friction; should fix before v0.2 final.
- **P2** — minor; nice-to-have polish.

---

## Found while writing `demo.py` (before the run)

### RE-01 — `trace.observed(error_type=, error_body=)` requires manual class-name lookup ✅ FIXED 2026-05-29

**Severity:** P1. **Status:** resolved — `Trace.observed()` and `AsyncTrace.observed()` now accept `error: BaseException | None = None`. When passed, the trace derives `error_type = type(error).__name__` and `error_body = str(error)` automatically. Explicit `error_type`/`error_body` still win if both are passed (useful for re-classifying or pre-scrubbing).

**Before (demo, original):**

```python
except BadRequestError as exc:
    extraction_trace.observed(error_type=type(exc).__name__, error_body=str(exc))
```

**After (demo, current):**

```python
except BadRequestError as exc:
    extraction_trace.observed(error=exc)
```

Three sync + one async test added covering the derivation, the explicit override precedence, and async parity.

**Original finding below for context:**

**What:** When a tool call inside a `with client.trace(...) as trace:` block raises, the user has two paths:

- Let the exception propagate — `__exit__` auto-emits `tool_call_error` and re-raises. Clean, but the demo wants to **continue** after the failure (to emit the reactive `annotate()` ticket).
- Catch, then manually call `trace.observed(error_type=type(exc).__name__, error_body=str(exc))`. Works, but the user has to know the kwargs, know to use `__name__`, and stringify the exception themselves.

**Recommended fix:** add an `observed(error=exc)` overload that accepts the exception object directly:

```python
except BadRequestError as exc:
    trace.observed(error=exc)
```

Alternative: a `trace.swallow(exc)` method that's explicit about "record this failure, but don't propagate."

---

### RE-02 — no public `trace.session_id` for cross-event correlation ✅ FIXED 2026-05-28

**Severity:** P0. **Status:** resolved in `src/baton/client.py` — added `Trace.session_id` public property + `Trace.annotate(...)` method (mirrored on `AsyncTrace`). Deeper contextvar-based auto-binding tracked as a v0.3 candidate in `docs/SKILLS_LIBRARY_API_DRAFT.md`.

**Evidence (re-run, same script):**

```
 # event_type         seq  session_id
 1 tool_call_start      1  019e70e7-5c61-…   ← preflight trace
 2 annotation           2  019e70e7-5c61-…
 3 tool_call_end        3  019e70e7-5c61-…
 4 tool_call_start      1  019e70e7-5c64-…   ← failed trace
 5 annotation           2  019e70e7-5c64-…
 6 tool_call_error      3  019e70e7-5c64-…
 7 annotation           4  019e70e7-5c64-…   ← ticket NOW BOUND, seq=4 continues counter
```

Before the fix: 3 unique `session_id`s (preflight, failed trace, orphan ticket). After: 2 unique (preflight, failed trace + bound ticket). Console-side correlation is now a single `WHERE session_id = ?` query.

**Original finding below for context:**

**What:** The reactive `annotate()` call after the failed trace conceptually **belongs to the same logical session as the trace**. Today, `baton.annotate(...)` mints a fresh `session_id` (per-event mode default) unless the caller passes one explicitly. To pass the trace's `session_id`, the caller would have to reach into `trace._session_id` — a private attribute.

**Why this matters:** any vendor consuming Baton's events will want to ask "which trace did this `dead_end` signal correspond to?" If we can't answer that without forcing the caller to manage session_ids themselves, the Console-side correlation story breaks.

**Recommended fix:** expose `trace.session_id` as a public property; document the pattern:

```python
with baton.trace(...) as trace:
    ...
baton.annotate(session_id=trace.session_id, signal_type=..., ...)
```

Better: `trace.annotate(...)` as a method on the trace itself, auto-binding the session_id:

```python
with baton.trace(...) as trace:
    try:
        ...
    except BadRequestError as exc:
        trace.observed(error=exc)
        trace.annotate(signal_type=SignalType.DEAD_END, ...)  # auto-uses trace.session_id
```

---

### RE-03 — `params={...}` on `trace()` duplicates the request shape

**Severity:** P2.

**What:** The caller passes the request params to Baton **and** to the vendor SDK:

```python
with baton.trace(
    tool_name="vendor.chat.completions.create",
    params={"model": "...", "messages": [...], "response_format": {...}},
) as trace:
    result = vendor.chat.completions.create(model="...", messages=[...], response_format={...})
```

The dict literal repeats. For long requests this is tedious and a copy-paste risk (params drift from what's actually sent).

**Recommended fix:** decorator form or auto-instrumentation (already listed as "v0.3 candidate" in SKILLS_LIBRARY_API_DRAFT.md). Defer; the explicit shape is fine for v0.2 but document the duplication pain in the draft.

Alternative for now: a `trace.with_params(**kwargs)` that the caller writes once, then passes `kwargs` to the SDK. Slightly less duplication.

---

### RE-04 — no warning if `consent_token` is missing

**Severity:** P1 (privacy posture).

**What:** `_resolve_config_value("consent_token", required=False)` — silently accepts `None`. Given consent is the privacy primitive, the default should at least emit a one-time `UserWarning` so the integrator knows they're emitting events without one.

**Recommended fix:** if `consent_token` resolves to `None` and `baton_disable_consent_warning=False`, emit a `UserWarning` on `Client(...)` construction.

---

### RE-05 — `SignalType` enum is verbose at call sites; consider accepting strings

**Severity:** P2.

**What:**

```python
baton.annotate(signal_type=SignalType.DEAD_END, ...)
```

vs.

```python
baton.annotate(signal_type="dead_end", ...)
```

Strings already pass through (`signal_type_str = signal_type.value if isinstance(signal_type, SignalType) else signal_type`). So strings work — but it's not advertised, and there's no validation against typos. A typo silently ships as `signal_type="dead-end"` and the Console side has to handle the noise.

**Recommended fix:** if a string is passed, validate it against `SignalType.__members__.values()` and raise `ValueError` on miss. Document both forms in the docstring. (Doesn't break anything; just guards.)

---

## Found during the run

Demo ran clean first-try: 7 events captured, all `201 Created`, envelopes well-formed (`spec_version=0.2`, `sdk_version=0.1.0`, `agent_runtime=python-library` on every one). The library mechanics are solid. The rough edges below are about **ergonomics and signal-correlation**, not correctness.

### RE-02 *(P0)* — confirmed by event sequence

Run output (pre-fix):

```
Event 4: tool_call_start    session=019e70d9-f043-709a-b659-c8aef7f615fc  (the failed trace)
Event 5: annotation         session=019e70d9-f043-709a-b659-c8aef7f615fc  (proactive intent)
Event 6: tool_call_error    session=019e70d9-f043-709a-b659-c8aef7f615fc  (the 400)
Event 7: annotation         session=019e70d9-f044-716e-9950-daca302ea3c8  ← orphan ticket
                           ↑ DIFFERENT session_id — Console can't correlate
```

Event 7 is the "ticket" — but its `session_id` is fresh, disconnected from the failed trace it's *about*. On the Console side, surfacing this ticket means joining across `session_id`s with timestamp heuristics or hoping the caller stuffed enough into `context.error_message` for a fuzzy match. That's brittle.

**This was the single most important fix.** Without it, every reactive `dead_end` signal is a floating note with no provable link to the failure that caused it. Fix landed via `trace.session_id` public property + `trace.annotate(...)` method that auto-binds.

### RE-04 *(P1 + uncovered P0)* — ✅ FIXED 2026-05-29

**Original (P1):** `Client(...)` with no `consent_token` silently accepted; emitted events with no consent record.

**Uncovered while probing (P0):** even when `consent_token` *was* passed, it was **never reaching the wire** — `events.py`'s `_EventEnvelope` had no field for it, and neither MCP middleware (`integrations/mcp/install.py` + `middleware.py` + `annotation.py`) nor the library path threaded it through. The Console literally could not validate consent for any received event. SPEC §2.3 + §3.1 mandate `consent_token` as a required body field on every event; the SDK was out of compliance across both transports.

**Fix shipped:**

1. `_EventEnvelope.consent_token: str` (required) added in `src/baton/events.py`.
2. Library API: `Client.__init__` / `AsyncClient.__init__` flipped `consent_token` from `required=False` → `required=True` — raises `ValueError` if missing. All 12 event-construction sites in `src/baton/client.py` now thread `self._consent_token` through. `Client.annotate(...)` / `AsyncClient.annotate(...)` gained a `consent_token` kwarg for per-trace overrides; `Trace.annotate(...)` forwards `self._consent_token` automatically.
3. MCP integration: `VendorConfig.consent_token: str` added; `install_baton(...)` raises if missing; `BatonMiddleware.__init__` + `register_annotation_tool(...)` accept it and wire it through 4 event-construction sites in `middleware.py` + `annotation.py`.
4. Vendor demo updated to read `BATON_CONSENT_TOKEN` env and pass it.
5. Test suite updated; all 98 tests pass.

**Evidence (re-run, same demo script):**

```
Events captured: 7
All have consent_token field: True
Unique consent_tokens: {'demo-consent-token'}
```

Missing-consent probe:

```
$ Client(api_key='x', ingest_url='...', vendor_id='v')  # no consent_token
ValueError: consent_token must be supplied explicitly or via the BATON_CONSENT_TOKEN environment variable
```

### RE-05 *(P2)* — confirmed by probe ✅ FIXED 2026-05-29

Ran `client.annotate(signal_type='dead-end', ...)` (hyphen instead of underscore). Silently accepted; shipped a `signal_type="dead-end"` payload that didn't match any enum value.

**Fix:** added `_resolve_signal_type(...)` helper in `client.py` — validates string inputs against `{m.value for m in SignalType}` and raises `ValueError` on miss with a message listing valid values. Both `Client.annotate` and `AsyncClient.annotate` now route through it. Test `test_annotate_rejects_typo_signal_type` covers it.

### RE-06 *(P2)* — `from baton import` doesn't surface `Trace` / `AsyncTrace` ✅ FIXED 2026-05-29

**Fix:** re-exported `Trace` and `AsyncTrace` from `src/baton/__init__.py`'s `__all__` + import line. Typed callers can now write `def f(t: baton.Trace) -> ...` without reaching into `baton.client`. Probe: `from baton import Trace, AsyncTrace` works.

### RE-07 *(P2)* — proactive `annotation` event vs reactive `annotation` event are indistinguishable on the wire ✅ FIXED 2026-05-29 (doc-only)

**Fix decision:** doc-only — the implicit `signal_type` discriminator works in practice (spike validated this) and a wire-format change (e.g., new `event_type=intent_capture`) would be breaking with no current upside. Worker-side dispatch on `signal_type`'s presence is already specified in SPEC §11.5.

**SPEC §11.4 updated** with an "Annotation event sub-types" subsection that names the proactive/reactive split explicitly and points to §11.5 for the correlation rules. The wire format stays unified; the semantic split is now load-bearing documentation that worker implementations can rely on.

Promoting to a wire-format change tracked as a v1.0 option if real-world friction emerges; until then, the discriminator is documented and stable.

---

## Verdict (post-run)

**The library API works.** Three Skills patterns map cleanly: opening a trace, observing an outcome, and raising a reactive signal. The bridge thread + emitter machinery shipped 7 events with no flakes.

**P0 RE-02 — fixed 2026-05-28** in the same spike (added `Trace.session_id` + `Trace.annotate(...)`; deeper contextvar variant tracked as v0.3 candidate).

**RE-01 — fixed 2026-05-29** (added `observed(error=exc)` overload).

**RE-04 — fixed 2026-05-29** (and uncovered a deeper P0 — `consent_token` was dead code across both SDK transports; full SPEC §2.3 + §3.1 compliance shipped).

**RE-05 / RE-06 / RE-07 — fixed 2026-05-29.** Three P2s closed: RE-05 (typo validation), RE-06 (top-level Trace re-export), RE-07 (SPEC §11.4 documents the annotation sub-type discriminator).

**All P0s, P1s, P2s closed.** Library API is integrator-ready from a rough-edge standpoint. Spike complete.
