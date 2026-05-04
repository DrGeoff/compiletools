"""End-to-end check that the Sphinx docs site builds without warnings."""
import subprocess
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
        ["sphinx-build", "-W", "-b", "html", str(docs_src), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sphinx-build failed:\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    assert (tmp_path / "index.html").is_file(), "rendered index.html missing"
