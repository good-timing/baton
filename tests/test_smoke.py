"""Smoke test — verifies the package imports and exposes its version marker."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import textwrap
import tomllib

import baton


def test_version_present() -> None:
    assert baton.__version__.startswith("0.")


def test_httpx_is_a_core_dependency() -> None:
    """A DSN always builds an ``HttpSink``, and ``mcp`` 2.x and ``fastmcp`` 4.x
    depend on ``httpx2``, not ``httpx``. As an extra, httpx left a fresh
    ``pip install "baton-sdk[mcp]"`` unable to run the DSN quickstart.
    """
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text())["project"]["dependencies"]
    # ``\b`` so ``httpx2`` does not satisfy it.
    assert any(re.match(r"httpx\b", dep) for dep in deps), deps


def test_import_baton_needs_nothing_from_an_EXTRA() -> None:
    """``pip install baton-sdk`` with no extras must still ``import baton``.

    ``baton/__init__.py`` imports ``VendorConfig``, and so transitively half of
    ``integrations/``, on EVERY import — so a module-scope import of anything
    optional there breaks the LIBRARY-API install (``Client`` / ``AsyncClient``
    / ``Trace``) outright, in the one configuration that uses no MCP server at
    all. That happened on 2026-09-11 with ``anyio``, which is why anyio is now
    a CORE dependency rather than an extra; this test is the guard for the next
    one, which would arrive through ``mcp`` or ``fastmcp``.

    ⚠ **The subprocess and the blocker are the test.** Every dev venv resolves
    every extra, so both are always importable under ``make ci`` and an
    in-process assertion would pass straight through the break it is named for.
    """
    # Distribution name == module name for both; if that ever stops being
    # true, block the MODULE, since that is what an import statement asks for.
    blocked = ("mcp", "fastmcp")
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = textwrap.dedent(f"""
        import sys

        BLOCKED = {blocked!r}

        class Blocked:
            def find_spec(self, name, path=None, target=None):
                root = name.split(".")[0]
                if root in BLOCKED:
                    raise ModuleNotFoundError(f"No module named {{root!r}}")
                return None

        sys.meta_path.insert(0, Blocked())
        sys.path.insert(0, {src!r})
        import baton

        assert baton.Client is not None
        assert baton.AsyncClient is not None
        assert baton.Trace is not None
        leaked = [m for m in BLOCKED if m in sys.modules]
        assert not leaked, f"an extra was imported anyway: {{leaked}}"
        print("OK")
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, (
        f"`import baton` needs one of {blocked} — a core-only install is broken:\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
