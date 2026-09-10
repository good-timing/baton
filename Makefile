# Baton SDK — thin event capture
# See docs/SPEC.md (the wire protocol) and docs/CHARTER.md (the load-bearing decisions).

# PYTHON is gone: `uv sync` selects the interpreter from PYTHON_VERSION below,
# so a second way to name one could only disagree with it.
PYTHON_VERSION ?= 3.14
VENV ?= .venv
BIN = $(VENV)/bin

.PHONY: install test test-fast test-perf test-functional soak test-watch test-cov lint format format-check typecheck ci clean build spec-check

# `--locked` is the point: it FAILS if uv.lock and pyproject.toml disagree,
# rather than quietly re-resolving. That is what makes this venv the same one
# CI builds — the property three separate bugs (09-07, 09-08, 09-10) turned on.
# Run `uv lock` after changing a dependency; the failure tells you to.
#
# UV_PROJECT_ENVIRONMENT honours $(VENV). Without it uv always writes .venv
# while $(BIN) still points at $(VENV), so `make install VENV=x` built one
# venv and every other target then ran out of another. PYTHON_VERSION is what
# lets you reproduce a single matrix leg locally:
#   make install PYTHON_VERSION=3.11 VENV=.venv311 && make test VENV=.venv311
install:
	UV_PROJECT_ENVIRONMENT=$(VENV) uv sync --locked --extra dev --python $(PYTHON_VERSION)

# Excludes only `perf` (wall-clock-timing tests — see perf-timing below for
# that marker's own job/target) — mirrors the `core` CI job's Test step
# exactly. `functional`-marked tests (tests/functional/, plus
# TestFailOpenOnBufferOverflow in tests/perf/test_nonblocking_hotpath.py)
# are NOT excluded: they're deterministic, not timing-flaky, so they belong
# in the regular merge-blocking run like any other test.
test:
	$(BIN)/pytest -q -m "not perf"

# Fast subset for tight iteration loops — skips tests marked `slow`,
# `integration`, or `perf` (see pyproject.toml's [tool.pytest.ini_options]
# markers list).
test-fast:
	$(BIN)/pytest -q -m "not slow and not integration and not perf"

# The wall-clock-timing subset only (mirrors ci.yml's continue-on-error
# perf-timing job) — run explicitly since `make test`/`make ci` exclude it.
# For the deterministic functional suite, use `make test-functional`
# (already included in plain `make test`/`make ci`, so this is just a
# scoped-directory convenience for local iteration).
test-perf:
	$(BIN)/pytest tests/perf tests/functional -q -m perf

test-functional:
	$(BIN)/pytest tests/perf tests/functional -q -m functional

# Standalone soak/load run — not a pytest test, run manually before a PLG
# launch and periodically after. See scripts/soak.py's module docstring.
soak:
	$(BIN)/python scripts/soak.py

test-watch:
	$(BIN)/pytest-watch -q

test-cov:
	$(BIN)/pytest --cov=baton --cov-report=term-missing --cov-report=html

lint:
	$(BIN)/ruff check src/ tests/

format:
	$(BIN)/ruff format src/ tests/

format-check:
	$(BIN)/ruff format --check src/ tests/

typecheck:
	$(BIN)/mypy src/baton

# CI gate — lint + format-check + typecheck + test. Mirrors the `core`
# .github/workflows/ci.yml job so a green local `make ci` predicts a green
# PR — this already includes the functional suite (see `test` above). Does
# NOT include `make test-perf` — that mirrors the separate, continue-on-error
# `perf-timing` job; run it explicitly.
ci: lint format-check typecheck test

# `--with build` rather than $(BIN)/python: `uv sync` PRUNES anything not in the
# lock, unlike the additive pip install this replaced, so a hand-installed
# `build` now disappears on the next `make install`. `build` is a packaging
# tool, not a dependency of the SDK, so it belongs in neither the lock nor an
# extra — uv supplies it for the one command that needs it.
build:
	uv run --with build python -m build

# Validate SPEC.md cross-references and section numbering.
# (Implementation lives in scripts/spec_check.py; placeholder until written.)
spec-check:
	@echo "spec-check not yet implemented; see scripts/spec_check.py"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
