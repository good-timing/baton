# Baton SDK — thin event capture
# See docs/SPEC.md (the wire protocol) and docs/CHARTER.md (the load-bearing decisions).

PYTHON ?= $(shell command -v python3.14 >/dev/null 2>&1 && echo python3.14 || echo python3)
VENV ?= .venv
BIN = $(VENV)/bin

.PHONY: install test test-watch test-cov lint format typecheck ci clean build spec-check

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

test:
	$(BIN)/pytest -q

test-watch:
	$(BIN)/pytest-watch -q

test-cov:
	$(BIN)/pytest --cov=baton --cov-report=term-missing --cov-report=html

lint:
	$(BIN)/ruff check src/ tests/

format:
	$(BIN)/ruff format src/ tests/

typecheck:
	$(BIN)/mypy src/baton

# CI gate — lint + typecheck + test. GitHub Actions runs this directly.
ci: lint typecheck test

build:
	$(BIN)/python -m build

# Validate SPEC.md cross-references and section numbering.
# (Implementation lives in scripts/spec_check.py; placeholder until written.)
spec-check:
	@echo "spec-check not yet implemented; see scripts/spec_check.py"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
