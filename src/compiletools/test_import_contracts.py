"""Drift-guard test: the import-linter architecture contracts stay GREEN.

``.importlinter`` (repo root) encodes the module layering documented in
``CLAUDE.md`` ("Architecture / Build flow") as machine-checkable contracts:
the build pipeline spine is a one-way stack, foundational/config modules
never import a backend, the dependency-analysis layer stays backend-agnostic,
core library modules never import the CLI/report tools, and the concrete
backends are independent of one another.

``lint-imports`` is the standalone way to run those contracts, but a
standalone tool is easy to forget in CI. This test pulls the same check into
the normal ``pytest -n auto`` gate so a layering regression fails loudly here
rather than rotting silently until someone happens to run the linter.

The check runs the real ``lint-imports`` CLI as a subprocess against the real
``.importlinter`` config, so the assertion exercises exactly what a developer
runs locally — no in-process re-implementation that could drift from the CLI.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
IMPORTLINTER_CONFIG = REPO_ROOT / ".importlinter"


def _lint_imports_argv() -> list[str]:
    """Return the argv that invokes import-linter's CLI.

    Prefer the installed ``lint-imports`` console script; fall back to
    ``python -m importlinter`` so the test still runs if the script shim
    is missing from PATH (e.g. odd venv layouts).
    """
    script = shutil.which("lint-imports")
    if script:
        return [script]
    return [sys.executable, "-m", "importlinter"]


def test_importlinter_config_exists() -> None:
    """The contracts file must be present at the repo root."""
    assert IMPORTLINTER_CONFIG.is_file(), (
        f"Expected import-linter contracts at {IMPORTLINTER_CONFIG}. This file "
        f"encodes the documented module architecture; without it the layering "
        f"guard is silently disabled."
    )


def test_import_contracts_are_kept() -> None:
    """``lint-imports`` must report every contract KEPT.

    A failure means a module now imports across a documented architecture
    boundary (a leaf reaching into a backend, config importing a backend, a
    backend coupling to a sibling, etc.). Read the ``Broken contracts``
    section of the output below: fix the offending import, or — only if the
    new edge is genuinely sound — adjust ``.importlinter`` with a comment
    explaining why.
    """
    try:
        result = subprocess.run(
            _lint_imports_argv(),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:  # pragma: no cover - import-linter is a dev dep
        pytest.skip("import-linter (lint-imports) is not installed")

    assert result.returncode == 0, (
        "import-linter reported a broken architecture contract.\n\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
