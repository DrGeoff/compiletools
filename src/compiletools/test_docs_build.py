"""End-to-end check that the Sphinx docs site builds without warnings.

This test serves two distinct purposes:

1. Build verification — proves the published site at
   drgeoff.github.io/compiletools/ can actually be produced from the
   sources in this checkout. Caught by ``.github/workflows/docs.yml``
   running on tag push, but we want failures locally before tag time.

2. RST validity gate — the ``-W`` flag turns every Sphinx warning into
   an error, so malformed RST in any ``src/compiletools/README.ct-*.rst``
   (broken cross-reference, malformed table, duplicate label, bad
   directive) fails this test. Without this gate the per-tool READMEs
   could rot silently between releases.

A missing ``docs/conf.py`` is ``pytest.fail``, not ``pytest.skip``: an
accidental delete of the Sphinx sources should be loud, not silently
mark the test green. ``importorskip`` on the Sphinx packages IS the
right gate for "developer hasn't installed the [docs] extras".
"""

import subprocess
import sys
from pathlib import Path

import pytest


def test_docs_build_clean(tmp_path):
    pytest.importorskip("sphinx")
    pytest.importorskip("furo")
    pytest.importorskip("sphinx_copybutton")

    repo_root = Path(__file__).resolve().parents[2]
    docs_src = repo_root / "docs"
    if not (docs_src / "conf.py").exists():
        pytest.fail(f"docs/conf.py not found at {docs_src}")

    result = subprocess.run(
        [sys.executable, "-m", "sphinx", "-W", "-b", "html", str(docs_src), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sphinx-build failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert (tmp_path / "index.html").is_file(), "rendered index.html missing"
