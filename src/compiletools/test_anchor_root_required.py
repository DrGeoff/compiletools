"""Lint-tests: every production caller of cache-key helpers must pass anchor_root=.

Mirrors test_every_production_caller_passes_anchor_root in
test_compiler_identity_hash.py. The defaults on MacroState.anchor_root
(empty-string) and on _pch_command_hash / _pcm_command_hash
(``None`` -> silent ``find_git_root()`` fallback) were removed; callers
that drop the kwarg silently re-introduce the gitroot-leak bug.
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


def _assert_every_production_call_passes_anchor_root(callee: str) -> None:
    """Grep-assert every production call to ``<callee>(...)`` passes ``anchor_root=``.

    Skips definitions (``def <callee>(``), comments, and string-literal
    occurrences via the same arg-extraction pass used by the MacroState
    sibling above. Used by both the PCH and PCM lint guards below.
    """
    # Negative lookbehind for ``def `` so the function definition itself
    # (which carries the parameter rather than passing it) doesn't count
    # as a missing-kwarg site. Word-boundary-anchored so the leading
    # underscore on ``_pch_command_hash`` / ``_pcm_command_hash`` matches.
    pattern = re.compile(rf"(?<!def )(?<![A-Za-z0-9_]){re.escape(callee)}\s*(\()")
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
    assert not failures, (
        f"Production callers of {callee} must pass anchor_root=. "
        f"Missing-anchor callers silently fell through to a fresh find_git_root() "
        f"lookup before the kwarg was made required. Offending sites: {failures}"
    )


def test_every_production_caller_passes_pch_command_hash_anchor_root():
    """``_pch_command_hash`` accepts ``anchor_root`` as a keyword-required
    ``str``. Production callers that drop the kwarg fail at runtime, but
    the failure mode used to be a silent ``find_git_root()`` fallback.
    This grep-guard catches a future refactor that loosens the signature
    back to ``str | None = None`` AND drops a call-site kwarg in the same
    change."""
    _assert_every_production_call_passes_anchor_root("_pch_command_hash")


def test_every_production_caller_passes_pcm_command_hash_anchor_root():
    """``_pcm_command_hash`` accepts ``anchor_root`` as a keyword-required
    ``str``. See the PCH counterpart above for rationale."""
    _assert_every_production_call_passes_anchor_root("_pcm_command_hash")
