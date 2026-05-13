"""Lint-test: every production MacroState(...) site must pass anchor_root.

Mirrors test_every_production_caller_passes_anchor_root in
test_compiler_identity_hash.py. The empty-string default on
MacroState.anchor_root was removed in v9.3.0; callers that drop the
kwarg silently re-introduce the gitroot-leak bug.
"""

import os
import re


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for fname in os.listdir(src_dir):
        if fname.endswith(".py") and not fname.startswith("test_"):
            yield os.path.join(src_dir, fname)


def _extract_call_args(text: str, open_paren_pos: int) -> str:
    """Return the full argument string inside matching parens, handling nesting."""
    i = open_paren_pos + 1
    depth = 1
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return text[open_paren_pos + 1 : i - 1]


def _is_in_comment(text: str, pos: int) -> bool:
    """Return True if the character at pos is inside a ``# …`` line comment."""
    # Find the start of the current line.
    line_start = text.rfind("\n", 0, pos) + 1
    line_prefix = text[line_start:pos]
    return "#" in line_prefix


def test_every_production_macrostate_caller_passes_anchor_root():
    # Match ``MacroState(`` but not inside a ``# …`` comment and not a class def.
    pattern = re.compile(r"(?<!class )MacroState\s*(\()")
    failures = []
    for path in _production_python_files():
        with open(path) as fh:
            text = fh.read()
        for m in pattern.finditer(text):
            if _is_in_comment(text, m.start()):
                continue
            open_paren = m.start(1)
            args_str = _extract_call_args(text, open_paren)
            if "anchor_root" not in args_str:
                line = text[: m.start()].count("\n") + 1
                failures.append(f"{os.path.basename(path)}:{line}")
    assert not failures, f"Production callers of MacroState must pass anchor_root=. Sites missing the kwarg: {failures}"
