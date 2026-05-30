# Integration reorganization (spike)

*Opened 2026-05-28. Status: scoped, not started. Reorganize `src/baton/` to follow the Sentry / Datadog / OpenTelemetry integration pattern: core SDK + optional integrations under `baton.integrations.*` with pip extras for opt-in dependencies. **Prerequisite to `docs/design-notes/library_api_engineering_plan.md`** — best landed before the library API code lands.*

---

## Why

Three reasons stack:

1. **Architectural alignment with CHARTER OD-7.** The thin-emit substrate (EventEmitter, scrub, events) is mode-agnostic. The MCP middleware, the upcoming library API (Skills), the future managed-agents integration, the future A2A integration — all feed the same substrate from different surfaces. Today's layout (`src/baton/install.py` + `middleware.py` + `annotation.py` + `runtime_adapter.py` + `instructions.py` at the top level) implies MCP is the SDK. It isn't — MCP is one integration among future N.

2. **Avoids dependency hell.** MCP integration requires `fastmcp`. Managed-agents integration will require `anthropic[managed-agents]`. A2A will require an A2A SDK. A vendor running only the library API (e.g., a Skills-pattern consumer) shouldn't be forced to install `fastmcp`. Optional pip extras solve this cleanly.

3. **Pattern is well-trodden.** Sentry, Datadog, OpenTelemetry, Langfuse all use core + optional-integrations packaging. Pressure-tested. Toggle-pattern alternatives ("`baton.install(mode='mcp')`") consistently fail in production with surprising `ImportError`s and config drift. See "Why the toggle pattern fails" in the conversation log that triggered this plan.

---

## Target layout

```
src/baton/
  __init__.py                       # exports: Client, AsyncClient, SignalType, EventEmitter
  client.py                         # library API entry point (new, per library_e2e/PLAN.md)
  aclient.py                        # async library API (new)
  emitter.py                        # core, unchanged
  events.py                         # core, unchanged
  scrub.py                          # core, unchanged
  _state.py                         # core, unchanged
  integrations/
    __init__.py
    mcp/
      __init__.py                   # exports: install_baton
      install.py                    # moved from src/baton/install.py
      middleware.py                 # moved
      annotation.py                 # moved
      runtime_adapter.py            # moved
      instructions.py               # moved
    # Future integrations (placeholder, not built yet):
    # managed_agents/
    #   __init__.py                 # exports: attach
    # a2a/
    #   __init__.py                 # exports: instrument
```

**Top-level `baton.__init__.py` keeps re-exports for backward compatibility:**

```python
# baton/__init__.py
from baton.client import Client, AsyncClient, SignalType
from baton.emitter import EventEmitter

# Backward-compat re-export with deprecation warning
import warnings as _warnings

def __getattr__(name):
    if name == "install_baton":
        _warnings.warn(
            "Importing `install_baton` from `baton` is deprecated. "
            "Use `from baton.integrations.mcp import install_baton` instead. "
            "The top-level re-export will be removed in v0.4.",
            DeprecationWarning,
            stacklevel=2,
        )
        from baton.integrations.mcp import install_baton as _install_baton
        return _install_baton
    raise AttributeError(f"module 'baton' has no attribute {name!r}")
```

Same pattern for any other historically-top-level exports (`VendorConfig`, etc. if they exist).

---

## `pyproject.toml` changes

Optional dependencies via pip extras:

```toml
[project]
dependencies = [
    "httpx>=0.27",
    # any other always-needed deps
]

[project.optional-dependencies]
mcp = ["fastmcp>=2.0"]
managed-agents = ["anthropic[managed-agents]>=0.30"]  # placeholder; verify version when this integration lands
a2a = ["a2a-sdk>=...."]  # placeholder; not yet built
all = [
    "baton[mcp]",
    "baton[managed-agents]",
    "baton[a2a]",
]
```

Installation patterns vendors will use:

```bash
pip install baton                       # core + library API (Skills-shaped vendors, no MCP)
pip install baton[mcp]                  # MCP-wrapping vendors
pip install baton[managed-agents]       # vendors building products on Anthropic Managed Agents API
pip install baton[all]                  # everything (kitchen-sink dev installs)
```

---

## Versioning model

**Shared versioning.** One Baton release covers all integrations. Vendors pin a single version of `baton` and get the matching integration code automatically.

Alternative considered: independent per-integration versioning (each integration ships its own version). Rejected because:
- Higher mental load for vendors ("which version of `baton.integrations.mcp` works with `baton` 0.3?")
- Matches Sentry / Datadog convention, not OpenTelemetry convention. Sentry/Datadog are closer reference classes for our SDK scope.

---

## Phases

Mechanical work; low-risk; no behavior change.

### Phase 1 — File moves + import path updates

- `git mv src/baton/install.py src/baton/integrations/mcp/install.py`
- `git mv src/baton/middleware.py src/baton/integrations/mcp/middleware.py`
- `git mv src/baton/annotation.py src/baton/integrations/mcp/annotation.py`
- `git mv src/baton/runtime_adapter.py src/baton/integrations/mcp/runtime_adapter.py`
- `git mv src/baton/instructions.py src/baton/integrations/mcp/instructions.py`
- Create `src/baton/integrations/__init__.py` (empty marker)
- Create `src/baton/integrations/mcp/__init__.py` with re-exports of `install_baton` and any other public surface
- Update internal imports within the moved files to use new paths
- Update `src/baton/__init__.py` to drop top-level re-exports of MCP-specific names, add deprecation `__getattr__` shim

Effort: **~0.5 day.**

### Phase 2 — `pyproject.toml` extras

- Move `fastmcp` from `dependencies` to `[project.optional-dependencies].mcp`
- Add placeholder entries for `managed-agents` and `a2a` (commented out until those integrations land)
- Add `all` extra that pulls everything
- Update README and any install docs to reflect new install patterns

Effort: **~0.5 day.**

### Phase 3 — Test reorganization

- Move MCP-specific tests from `tests/test_install.py`, `tests/test_middleware.py` to `tests/integrations/mcp/test_install.py`, `tests/integrations/mcp/test_middleware.py`
- Update test imports
- Add a CI matrix entry that runs core tests with ONLY core deps installed (catches accidental cross-imports — e.g., if `baton.client` accidentally imports something from `baton.integrations.mcp`, this fails)
- CI matrix: `[core, core+mcp, core+all]` for clean isolation guarantee

Effort: **~0.5 day.**

### Phase 4 — Update spike infrastructure

- Existing spike infrastructure — update imports to `from baton.integrations.mcp import install_baton`
- Any vendor repos consuming Baton — update imports (separate PRs in those repos)

Effort: **~0.5 day** (mostly in this repo; vendor-repo updates are separate PRs).

### Phase 5 — Documentation refresh

- Update `README.md` install instructions
- Update `CLAUDE.md` to reflect the new layout (the "What lives where" section needs updates)
- Update `docs/SPEC.md` if it references SDK file paths
- Update `docs/CHARTER.md` §4 (SDK boundary discipline) — the layout change is the practical expression of OD-7

Effort: **~0.5 day.**

---

## Total effort

**Phases 1-5: ~2-3 days of careful, mechanical work.** Low-risk because it's structural reorganization, not behavior change. Each phase is independently commit-able.

---

## Backward compatibility strategy

**The only public surface that breaks today is `from baton import install_baton`** (the MCP entry point at top level).

Strategy:
1. **v0.3 release with this reorg:** top-level `install_baton` keeps working via `__getattr__` shim; emits `DeprecationWarning` on import telling users to switch to `from baton.integrations.mcp import install_baton`.
2. **v0.4 release** (next major): drop the shim. `from baton import install_baton` becomes `ImportError`. Users have one minor version of warning.

Coordination needed:
- Any internal vendor repos using `install_baton` — update their install path before v0.4.
- New integrations — use the new path from day one.

No external users to coordinate with — Baton SDK is early-access at the time of this reorg; no public adopters yet.

---

## Coordination with `library_e2e/PLAN.md`

**Reorg should land first.** Reasons:

1. The library API (`Client`, `AsyncClient`) lives in `src/baton/client.py` at the core level, NOT under `integrations/`. If the reorg lands after the library API code lands, we have to move things twice OR the library API ships with awkward placement that gets fixed later.
2. The `pyproject.toml` extras structure needs to exist BEFORE we add managed-agents / A2A integrations, and we know managed-agents is on the near horizon (per the Skills shift thread).
3. Reorg is mechanical and low-risk; doing it first sets the foundation cleanly.

Sequence:
1. **Reorg lands** (this plan, phases 1-5, ~2-3 days)
2. **Library API lands** (`library_e2e/PLAN.md`, phases 1-7, ~3.5-4 days)
3. Subsequent integrations (`managed-agents`, `a2a`) follow the same pattern when scoped.

---

## Success criteria

- [ ] `from baton import Client, AsyncClient, SignalType` works (core surface)
- [ ] `from baton.integrations.mcp import install_baton` works
- [ ] `from baton import install_baton` works with `DeprecationWarning` (backward compat)
- [ ] `pip install baton` does NOT install `fastmcp`
- [ ] `pip install baton[mcp]` installs `fastmcp`
- [ ] CI matrix passes: core-only tests + core+mcp tests both green
- [ ] Existing MCP smoke-test infrastructure runs unchanged behaviorally (imports updated)
- [ ] Any internal vendor repos updated (separate PRs)
- [ ] README + CLAUDE.md + CHARTER §4 reflect the new layout

---

## Open questions to lock before starting

1. **Single `baton` package or split into multiple packages?** Stay with **one package, optional extras** (recommended). Multi-package (e.g., `baton-core` + `baton-mcp` as separate PyPI packages) is more ceremony for marginal benefit at this scale.
2. **When does the v0.4 deprecation shim come out?** Lean: **v0.4.** One minor version of `DeprecationWarning` is sufficient for early-access scope.
3. **Should we adopt the integration pattern for VENDOR-SPECIFIC config too (e.g., `baton.integrations.<vendor>` with vendor-specific scrub defaults)?** Lean: **no.** Per CHARTER §4.1 (no vendor-specific imports in SDK). Vendor-specific config lives in the vendor's own repo, not in Baton's tree. Integrations under `baton.integrations.*` are for protocol / runtime surfaces (MCP, A2A, Managed Agents), not for individual partners.

---

## Cross-references

- `docs/design-notes/library_api_engineering_plan.md` — the library API spike that depends on this reorg landing first
- `docs/CHARTER.md` §4 (SDK boundary discipline), OD-7 (thin SDK / fat Console worker) — the architectural rationale this reorg expresses physically
- `docs/EXPLORATIONS.md` "Skills shift" thread — strategic context (multiple integration surfaces ahead: library + managed-agents + A2A)
- Conversation log 2026-05-28 — "support managed agents, skills and MCP as part of one repo" architecture discussion that triggered this plan
