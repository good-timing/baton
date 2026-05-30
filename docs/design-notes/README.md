# Design notes

Engineering memos and design-validation records for the Baton SDK. These document **how decisions were made** — not just what the current design is.

The canonical wire protocol lives in [`../SPEC.md`](../SPEC.md); the load-bearing decisions live in [`../CHARTER.md`](../CHARTER.md). These notes are the *trail* that led to the current state — useful for understanding rationale, but not authoritative for what the SDK does today.

## What's here

| File | What it covers |
|---|---|
| [`integration_reorg.md`](integration_reorg.md) | Reorganization of `src/baton/` from a flat MCP-shaped layout to `core + integrations/<name>` (Sentry / Datadog / OpenTelemetry-style). Rationale, target layout, phased plan, backward-compat strategy. |
| [`library_api_engineering_plan.md`](library_api_engineering_plan.md) | Engineering plan for prototyping the `baton.Client` library API alongside the MCP middleware path. 7 phases, design decisions, success criteria, what the prototype proves vs doesn't. |
| [`library_api_validation.md`](library_api_validation.md) | What the library-API e2e smoke test validated (and didn't) — architecture under test, captured event shapes, out-of-scope items. |
| [`library_api_dogfood_rough_edges.md`](library_api_dogfood_rough_edges.md) | Punch list from dogfooding the library API in `examples/skill_demo/`. Every API friction we hit, with severity and resolution status. All P0/P1/P2 items closed. |
| [`claude_desktop_description_loading.md`](claude_desktop_description_loading.md) | Spike result: packing MUST/REQUIRED framing into the annotation tool's *description* (vs. server `instructions`) does NOT recover annotation behavior on Claude Desktop. Negative result with concrete data for the MCP Agents WG conversation. |
| [`extension_matrix_pr_draft.md`](extension_matrix_pr_draft.md) | Draft PR text for the cross-runtime extension matrix — which MCP clients honor which protocol features. Reference material for the description-loading spike. |

## How these relate to `examples/`

Several design notes pair with runnable examples:

- `library_api_engineering_plan.md` + `library_api_validation.md` ↔ [`examples/library_api_smoke_test/`](../../examples/library_api_smoke_test/)
- `library_api_dogfood_rough_edges.md` ↔ [`examples/skill_demo/`](../../examples/skill_demo/)

If you want to *run* the validated pattern, see `examples/`. If you want to understand *why* it ended up that shape, read the design notes here.

## Status

Most notes are dated within the design-validation window of the SDK's v0.1–v0.2 period (2026-05-13 → 2026-05-29). They reflect the state of the world at the time of writing. The SPEC and CHARTER are kept current; design notes are kept as historical record and are not maintained in lockstep with code changes.
