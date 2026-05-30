# Contributing to Baton

Thanks for your interest in contributing. Baton is a thin, opinionated SDK; the contribution surface is deliberately small. This doc covers what we expect from contributions, how to set up a dev environment, and where to start.

## TL;DR

- **Bug fix or small enhancement?** Open a PR. CI + reviewer feedback will guide it home.
- **Architectural change, new public API, or anything touching the wire format?** **Open a SPEC or CHARTER discussion first** (see *Spec-first, failing-test-first*). Changes to `docs/SPEC.md` need a §13 changelog entry.
- **Security disclosure?** See [SECURITY.md](SECURITY.md) — do not open a public issue.

## Project disciplines

Before writing code, read:

1. `docs/CHARTER.md` — load-bearing decisions and SDK boundary rules. **§4 is non-negotiable** — no vendor-specific imports in `src/baton/` or `tests/`; the SDK must work for any vendor.
2. `docs/SPEC.md` — the canonical wire protocol. Changes here are coordinated and versioned; you cannot patch the schema in a hotfix.
3. `README.md` — the "Library API" section covers the `baton.Client` / `AsyncClient` surface alongside the MCP middleware path.

## Development setup

```sh
# Clone, then:
make install        # creates .venv, installs in editable mode with [dev,mcp]
make ci             # runs the full canonical gate locally (lint + typecheck + tests)
```

The `make ci` target matches the GitHub Actions CI gate, so if it's green locally it should be green on PR.

Individual commands:

```sh
make lint           # ruff check + format check
make format         # ruff format (write changes)
make typecheck      # mypy --strict
make test           # pytest -q
```

## Code style

- **Python 3.14+.** We use `uuid.uuid7()` from stdlib (per SPEC §3 + §11.4).
- **`ruff` for lint + format.** Configured in `pyproject.toml`; CI fails on any violation.
- **`mypy --strict` for typing.** Every public function has type annotations.
- **No `print()` in `src/`.** The `T20` ruff rule guards this; logging goes through Python's `logging` module.
- **Default to no comments.** Only add one when the *why* is non-obvious (a hidden constraint, a subtle invariant, a workaround for a specific bug). Don't explain *what* the code does — well-named identifiers already do that.

## Tests

- **Fake-vendor fixtures only.** The test suite uses synthetic vendor stubs. If a test imports a real vendor module, it's wrong (per CHARTER §3 rule 5).
- **`pytest` + `pytest-asyncio` + `pytest-httpserver`.** Async tests use `asyncio_mode = "auto"`.
- **Coverage is a guideline, not a gate.** We don't enforce a coverage percentage; we do require that load-bearing behaviors have tests.

## Public API discipline

Anything exported from `src/baton/__init__.py` (or `src/baton/integrations/<name>/__init__.py` for integrations) is the public contract.

- **Adding to the public surface:** OK in a minor version (`0.x → 0.(x+1)`).
- **Changing existing public surface:** requires a SPEC §13 changelog entry + a deprecation cycle (typically one minor version of `DeprecationWarning` before removal).
- **Internal modules** (anything not exported) are off-limits to consumers; we may rename, move, or break them in any release.

## PRs

- One logical change per PR. Big PRs that mix refactoring + behavior change are hard to review; please split.
- Reference the SPEC/CHARTER section your change relates to (e.g., "implements SPEC §11.4 envelope additions").
- If your change updates `docs/SPEC.md`, also update `docs/SPEC.md §13` (changelog) in the same PR.
- Tests are required for behavior changes; they're optional for pure refactors that don't change observable behavior.

## What we won't accept

- Vendor-specific code in `src/baton/` or `tests/`. The SDK is vendor-agnostic by charter.
- Changes that introduce stateful logic in the SDK beyond the bounded event buffer. Per CHARTER ADR-4, the SDK is thin-emit-only; correlation, policy, and dispatch live in the Console worker.
- Backward-compatibility shims for code paths that don't exist yet. We don't speculatively shim.
- Features that require breaking the public API without a deprecation cycle.

## Where to start

If you'd like to contribute but aren't sure where:

- Issues labeled `good first issue` (when we have them) are small, scoped, and have clear acceptance criteria.
- Documentation improvements are always welcome — typo fixes, clearer examples, missing context.
- Reviewing open PRs and leaving thoughtful feedback is high-leverage; we always want more eyes.

## Code of Conduct

Participation in this project is governed by the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions will be licensed under the same Apache 2.0 license that covers this project (see [LICENSE](LICENSE)).
