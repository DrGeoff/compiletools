"""build_state must stay pure: no ambient-authority imports.

The import-linter contract in .importlinter enforces this at prek
time; this AST test enforces it in every pytest run.
"""

import ast
import pathlib

import compiletools.build_state

_FORBIDDEN = {"os", "sys", "subprocess", "shutil", "pathlib"}


def test_build_state_imports_no_ambient_authority_modules():
    source = pathlib.Path(compiletools.build_state.__file__).read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    offending = imported & _FORBIDDEN
    assert not offending, (
        f"build_state.py imports ambient-authority module(s) {sorted(offending)}; "
        f"gather owns effects, build_state must stay pure."
    )
