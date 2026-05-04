"""Sphinx configuration for compiletools docs site.

Project metadata is pulled from pyproject.toml so the version is never
duplicated. A small build-time hook generates per-tool stub .rst files
into docs/_generated/ before the build runs (see Task 5).
"""
from datetime import date
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found]  # Python 3.10 backport

DOCS_DIR = Path(__file__).resolve().parent
REPO_ROOT = DOCS_DIR.parent

with (REPO_ROOT / "pyproject.toml").open("rb") as f:
    _pyproject = tomllib.load(f)

project = _pyproject["project"]["name"]
release = _pyproject["project"]["version"]
version = release
author = ", ".join(a["name"] for a in _pyproject["project"]["authors"])
copyright = f"{date.today().year}, {author}"  # noqa: A001

extensions = [
    "sphinx.ext.autosectionlabel",
    "sphinx_copybutton",
]

# Prefix auto-generated section labels with the document name so identical
# section titles in different READMEs (e.g. "DESCRIPTION") don't collide.
autosectionlabel_prefix_document = True

exclude_patterns = ["_build"]

html_theme = "furo"
html_title = f"{project} {release}"
html_static_path = []  # add "_static" here later if custom CSS appears


def _generate_tool_stubs(app):
    """Write one .rst stub per src/compiletools/README.ct-*.rst.

    Each stub is a single ``.. include::`` directive pointing at the
    canonical README. The README's own title becomes the document title,
    avoiding duplicate-label warnings from autosectionlabel and
    heading-hierarchy mismatches from a synthetic wrapper title.
    The Tools toctree picks them up via ``:glob:``.
    """
    src_dir = REPO_ROOT / "src" / "compiletools"
    out_dir = DOCS_DIR / "_generated"
    out_dir.mkdir(exist_ok=True)

    for readme in sorted(src_dir.glob("README.ct-*.rst")):
        # README.ct-cake.rst -> ct-cake
        tool_name = readme.stem[len("README."):]
        stub = out_dir / f"{tool_name}.rst"
        rel = Path("..") / ".." / "src" / "compiletools" / readme.name
        # No synthetic title: the README's own heading is the document title.
        # This avoids autosectionlabel duplicate-label warnings and
        # heading-hierarchy errors from included RST files whose sections
        # use the same underline character as the wrapper would.
        stub.write_text(f".. include:: {rel.as_posix()}\n")


def setup(app):
    _generate_tool_stubs(app)
    return {"version": release, "parallel_read_safe": True}
