"""Sphinx configuration for compiletools docs site.

Project metadata is pulled from pyproject.toml so the version is never
duplicated. A small build-time hook generates per-tool stub .rst files
into docs/_generated/ before the build runs (see Task 5).
"""
import tomllib
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent

with (REPO_ROOT / "pyproject.toml").open("rb") as f:
    _pyproject = tomllib.load(f)

project = _pyproject["project"]["name"]
release = _pyproject["project"]["version"]
version = release
author = ", ".join(a["name"] for a in _pyproject["project"]["authors"])
copyright = author  # noqa: A001

extensions = [
    "sphinx.ext.autosectionlabel",
    "sphinx_copybutton",
]

# Prefix auto-generated section labels with the document name so identical
# section titles in different READMEs (e.g. "DESCRIPTION") don't collide.
autosectionlabel_prefix_document = True

templates_path = ["_templates"]
exclude_patterns = ["_build", "_generated/.gitkeep"]

html_theme = "furo"
html_title = f"{project} {release}"
html_static_path = []  # add "_static" here later if custom CSS appears
