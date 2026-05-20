"""Tests for the compiler_identity helper and its inclusion in MacroState's
build-context hash (TOKEN-4).

Symmetric to ``_pch_command_hash`` (which already folds in compiler identity
via ``build_backend._compiler_identity``): the per-TU object cache key now
also captures the compiler binary's realpath/size/mtime so that an in-place
toolchain swap that does not change the user-visible command name (``g++``)
still invalidates stale objects.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import stringzilla as sz

import compiletools.test_base as tb
from compiletools.apptools import compiler_identity
from compiletools.build_context import BuildContext
from compiletools.preprocessing_cache import (
    MacroState,
    get_or_compute_preprocessing,
)


@pytest.fixture(autouse=True)
def _clear_compiler_identity_cache():
    """Reset the lru_cache on compiler_identity so each test sees a fresh probe."""
    compiler_identity.cache_clear()


def _make_wrapper_script(path: Path, content: str = '#!/bin/sh\nexec g++ "$@"\n') -> Path:
    """Create an executable wrapper script at ``path``, making parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path

# ---------------------------------------------------------------------------
# Tests 1-3: apptools.compiler_identity helper
# ---------------------------------------------------------------------------


def test_compiler_identity_module_helper_returns_realpath_size_mtime():
    """When ``cxx`` resolves to a real binary, the result has the
    ``realpath|size|mtime_ns`` shape. ``sys.executable`` is a guaranteed-
    to-exist binary, used as a stand-in for a real compiler."""
    result = compiler_identity(sys.executable)
    parts = result.split("|")
    assert len(parts) == 3, f"expected 3 |-separated parts, got {parts!r}"
    realpath_part, size_part, mtime_part = parts
    assert os.path.isabs(realpath_part)
    assert size_part.isdigit()
    assert int(size_part) > 0
    assert mtime_part.isdigit()
    assert int(mtime_part) > 0


def test_compiler_identity_falls_back_to_string_when_unstatable():
    """A nonexistent compiler name returns the raw string without raising."""
    result = compiler_identity("nonexistent_compiler_xyz")
    assert result == "nonexistent_compiler_xyz"


def test_compiler_identity_handles_ccache_g_plus_plus():
    """Multi-token commands like ``ccache g++`` cannot be ``shutil.which``'d
    as a single token; the helper must fall back to the raw string instead
    of crashing."""
    result = compiler_identity("ccache g++")
    assert result == "ccache g++"


# ---------------------------------------------------------------------------
# Anchor-aware canonicalisation: the realpath segment must be canonicalised
# against the gitroot anchor when the binary lives under the workspace
# (e.g. an in-repo coverage / sccache / distcc wrapper script). Otherwise
# every CI checkout under a fresh attempt directory produces a distinct
# compiler_identity → distinct PCH/PCM/object/link cache key → no reuse.
# Cross-references the upstream report's Part 1 leak.
# ---------------------------------------------------------------------------


def test_compiler_identity_canonicalises_in_workspace_binary(tmp_path):
    """When the resolved compiler binary lives under the supplied
    anchor_root, the realpath portion is rewritten to ``<GITROOT>/...``
    so two workspaces sharing a CAS see identical identity strings."""
    workspace = tmp_path / "workspace"
    binary = _make_wrapper_script(workspace / "tools" / "cc-wrap.sh")

    result = compiler_identity(str(binary), anchor_root=str(workspace))
    realpath_part = result.split("|", 1)[0]
    assert realpath_part == "<GITROOT>/tools/cc-wrap.sh", f"compiler_identity leaked workspace prefix: {result!r}"


def test_compiler_identity_outside_anchor_unchanged(tmp_path):
    """A system / out-of-workspace compiler is left alone — only paths
    actually under the anchor get rewritten."""
    binary = _make_wrapper_script(tmp_path / "external" / "g++-fake", "#!/bin/sh\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = compiler_identity(str(binary), anchor_root=str(workspace))
    realpath_part = result.split("|", 1)[0]
    assert realpath_part == str(binary)


def test_compiler_identity_empty_anchor_is_identity(tmp_path):
    """anchor_root="" must be a graceful no-op (no canonicalisation)."""
    binary = _make_wrapper_script(tmp_path / "workspace" / "tools" / "cc.sh", "#!/bin/sh\n")

    result = compiler_identity(str(binary), anchor_root="")
    realpath_part = result.split("|", 1)[0]
    assert realpath_part == str(binary)


def test_compiler_identity_fallback_string_canonicalised_when_under_anchor(tmp_path):
    """If shutil.which / os.stat fail (non-existent binary, ``ccache g++``
    multi-token), the helper falls back to the raw string. That fallback
    must ALSO canonicalise when the raw string is an absolute path under
    the anchor — otherwise the leak survives the fallback path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bogus = str(workspace / "tools" / "does-not-exist")

    result = compiler_identity(bogus, anchor_root=str(workspace))
    assert result == "<GITROOT>/tools/does-not-exist", f"compiler_identity fallback leaked workspace prefix: {result!r}"


def test_every_production_caller_passes_anchor_root():
    """Convention guard: every non-test call to ``compiler_identity`` /
    ``_compiler_identity`` must pass ``anchor_root=`` (the parameter is
    keyword-only at the function level; this test catches anyone who
    deletes the keyword-only marker AND adds a new positional caller).

    Production code paths whose result feeds into a cache key must
    canonicalise — a missing ``anchor_root`` would silently re-introduce
    the workspace-prefix leak this fix exists to prevent. Test files
    are exempt; they use the helper for both anchor-aware and
    anchor-agnostic assertions."""
    import pathlib
    import re

    src_dir = pathlib.Path(__file__).parent
    pattern = re.compile(r"\b_?compiler_identity\s*\(")
    offenders: list[str] = []
    for py in src_dir.glob("*.py"):
        if py.name.startswith("test_") or py.name == "conftest.py":
            continue
        text = py.read_text()
        for match in pattern.finditer(text):
            # Slice from the call's opening paren forward to find its
            # matching close. Allow nested parens because ld_argv[0]
            # contains brackets but no parens.
            i = match.end()
            depth = 1
            while i < len(text) and depth > 0:
                c = text[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                i += 1
            call = text[match.start() : i]
            # Skip definition (``def compiler_identity(``) and the alias
            # assignment (``_compiler_identity = compiletools...``).
            head = text[max(0, match.start() - 4) : match.start()]
            if head.endswith("def "):
                continue
            # Single-arg form (no comma in the args) is allowed only for
            # the ``args.CXX``-less / ``cxx``-less compatibility callers
            # that don't go into a cache key. There aren't any in
            # production, so flag any 1-arg call.
            args_str = call[call.index("(") + 1 : -1]
            if "anchor_root" not in args_str:
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{py.name}:{line_no}: {call.strip()}")

    assert not offenders, (
        "Production callers of compiler_identity must pass anchor_root=. "
        "Missing-anchor callers silently emit per-workspace cache keys "
        "(see upstream report Part 1). Offenders:\n  " + "\n  ".join(offenders)
    )


def test_compiler_identity_two_workspaces_canonicalise_to_same_realpath(tmp_path):
    """**Upstream-report acceptance test for Part 1.**

    The earlier tests in this section unit-test the helper one
    invocation at a time; this one exercises the property end-to-end
    with two real fixture workspaces and pinned mtimes, mirroring the
    report's wrapper-script reproducer. Distinct from the unit tests
    because it proves the *cross-workspace* equality the leak
    actually broke."""
    ws_a = tmp_path / "run-A" / "workspace"
    bin_a = _make_wrapper_script(ws_a / "tools" / "cc-wrap.sh")

    ws_b = tmp_path / "run-B" / "workspace"
    bin_b = _make_wrapper_script(ws_b / "tools" / "cc-wrap.sh")

    # Pin mtime+atime so the size|mtime portions match — the realpath
    # canonicalisation is what we're proving, not the (separate, more
    # invasive) mtime → content-hash refinement.
    fixed = (1700000000, 1700000000)
    os.utime(bin_a, fixed)
    os.utime(bin_b, fixed)

    id_a = compiler_identity(str(bin_a), anchor_root=str(ws_a))
    compiler_identity.cache_clear()
    id_b = compiler_identity(str(bin_b), anchor_root=str(ws_b))
    assert id_a == id_b, (
        f"compiler_identity must canonicalise the realpath across workspaces.\n  WS-A: {id_a}\n  WS-B: {id_b}"
    )


# ---------------------------------------------------------------------------
# Tests 4-5: MacroState build-context hash includes compiler_identity
# ---------------------------------------------------------------------------


def _make_state(**kwargs):
    """Construct a MacroState with sane defaults; only set new fields when
    explicitly passed so we exercise both the explicit and the default
    paths."""
    base = {
        "core": {sz.Str("__GNUC__"): sz.Str("13")},
        "variable": {},
        "compiler_path": "g++",
        "cppflags": "-I/usr/include",
        "cflags": "-O2",
        "cxxflags": "-std=c++17",
    }
    base.setdefault("anchor_root", "")
    base.update(kwargs)
    return MacroState(**base)


def test_macro_state_hash_changes_with_compiler_identity_change():
    """Two MacroStates that differ only in compiler_identity must produce
    different full hashes — the whole point of TOKEN-4."""
    a = _make_state(compiler_identity="/path/to/gcc-13|12345|99")
    b = _make_state(compiler_identity="/path/to/gcc-14|54321|100")
    assert a.get_hash(include_core=True) != b.get_hash(include_core=True)


def test_macro_state_hash_unchanged_with_compiler_identity_default():
    """Two MacroStates constructed without compiler_identity (default ``""``)
    must produce equal hashes — backward compatibility for every existing
    test that doesn't set the new field."""
    a = _make_state()
    b = _make_state()
    assert a.get_hash(include_core=True) == b.get_hash(include_core=True)


# ---------------------------------------------------------------------------
# Test 6: magicflags._initialize_macro_state populates compiler_identity
# ---------------------------------------------------------------------------


class TestInitialMacroStatePopulatesCompilerIdentity(tb.BaseCompileToolsTestCase):
    """Site 1: DirectMagicFlags._initialize_macro_state must populate
    compiler_identity from args.CXX."""

    def test_initial_macro_state_populates_compiler_identity(self):
        ctx = BuildContext()
        parser = tb.create_magic_parser(["--magic", "direct"], tempdir=self._tmpdir, context=ctx)
        expected = compiler_identity(parser._args.CXX)
        assert parser._initial_macro_state.compiler_identity == expected


# ---------------------------------------------------------------------------
# Tests 7-8: compiler_identity propagates through with_updates / preprocessing
# ---------------------------------------------------------------------------


def test_compiler_identity_propagates_through_with_updates():
    """``with_updates`` must carry compiler_identity through to the new
    MacroState, identically to how it propagates the other build-context
    fields."""
    state = _make_state(compiler_identity="foo")
    updated = state.with_updates({sz.Str("X"): sz.Str("1")})
    assert updated.compiler_identity == "foo"


def test_compiler_identity_propagates_through_get_or_compute_preprocessing():
    """The MacroState returned in ``ProcessingResult.updated_macros`` must
    keep compiler_identity. Mirrors Task C's regression test for the other
    build-context fields."""
    from compiletools.file_analyzer import FileAnalysisResult

    # Minimal FileAnalysisResult: no conditionals, no includes, no defines —
    # the file is permanently invariant so we exercise the simple cache path
    # in ``get_or_compute_preprocessing``.
    file_result = FileAnalysisResult(
        line_count=0,
        line_byte_offsets=[],
        include_positions=[],
        magic_positions=[],
        directive_positions={},
        directives=[],
        directive_by_line={},
        bytes_analyzed=0,
        was_truncated=False,
        content_hash="dummy_hash_for_compiler_identity_test",
    )

    initial = _make_state(compiler_identity="my-identity-string")
    ctx = BuildContext()
    # SimplePreprocessor reverse-resolves the content hash to a path on disk;
    # mock it since we synthesized a dummy hash above.
    with patch("compiletools.global_hash_registry.get_filepath_by_hash") as mock_lookup:
        mock_lookup.return_value = "<test-file>"
        result = get_or_compute_preprocessing(file_result, initial, verbose=0, context=ctx)
    assert result.updated_macros.compiler_identity == "my-identity-string"
