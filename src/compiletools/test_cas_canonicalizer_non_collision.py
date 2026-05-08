"""Layer 4 non-collision guards for the CAS path canonicalizer.

These tests guard against a too-aggressive canonicalizer producing
hash collisions that would silently merge cache entries that should
be distinct. Each test names a property the canonicalizer must NOT
violate.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

from types import SimpleNamespace

from compiletools.build_backend import _pch_command_hash, _pcm_command_hash
from compiletools.preprocessing_cache import MacroState

WORKSPACE = "/some/workspace"


def _macro_state_hash(*, include_path: str, anchor: str = WORKSPACE) -> str:
    """Build a MacroState referencing a single -I path and hash it."""
    cppflags_tokens = [f"-I{include_path}"]
    cflags_tokens = ["-O2"]
    cxxflags_tokens = [f"-I{include_path}", "-std=c++20"]
    state = MacroState(
        core={b"__GNUC__": b"13"},
        variable={},
        compiler_path="/usr/bin/g++",
        cppflags=" ".join(cppflags_tokens),
        cflags=" ".join(cflags_tokens),
        cxxflags=" ".join(cxxflags_tokens),
        cmdline_origin=frozenset(),
        cppflags_tokens=cppflags_tokens,
        cflags_tokens=cflags_tokens,
        cxxflags_tokens=cxxflags_tokens,
        compiler_identity="/usr/bin/g++|123456|1700000000",
        anchor_root=anchor,
    )
    return state.get_hash(include_core=True)


# ---------------------------------------------------------------------------
# Object cache (MacroState) non-collision guards
# ---------------------------------------------------------------------------


def test_object_distinct_in_workspace_paths_distinct_hashes():
    """Two different -I paths under the SAME workspace must hash differently.
    A canonicalizer that collapsed them would silently merge cache
    entries for compilations with genuinely different include search
    paths."""
    a = _macro_state_hash(include_path=f"{WORKSPACE}/lib/util")
    b = _macro_state_hash(include_path=f"{WORKSPACE}/include/util")
    assert a != b, "Distinct in-workspace -I paths must produce distinct hashes"


def test_object_in_vs_out_of_workspace_distinct_hashes():
    """An -I under the workspace vs the same relative tail outside it must
    produce different hashes — the anchor adds disambiguating context."""
    inside = _macro_state_hash(include_path=f"{WORKSPACE}/lib/util")
    outside = _macro_state_hash(include_path="/elsewhere/lib/util")
    assert inside != outside, "In-workspace vs out-of-workspace -I paths must produce distinct hashes"


def test_object_empty_anchor_matches_today_for_outside_paths():
    """When the anchor is empty AND all -I paths are outside any workspace,
    the hash must match what we'd get with the canonicalizer disabled —
    i.e., today's behavior is preserved bit-for-bit when there's nothing
    to canonicalize."""
    h_anchored = _macro_state_hash(include_path="/usr/include", anchor=WORKSPACE)
    h_no_anchor = _macro_state_hash(include_path="/usr/include", anchor="")
    assert h_anchored == h_no_anchor, "Out-of-anchor paths must hash the same regardless of anchor presence"


# ---------------------------------------------------------------------------
# PCH cache non-collision guards
# ---------------------------------------------------------------------------


def _pch(prefix: str, *, include: str, header: str) -> str:
    args = SimpleNamespace(CXX="/usr/bin/g++")
    return _pch_command_hash(
        args=args,
        pch_header=header,
        magic_cpp_flags=[],
        magic_cxx_flags=[],
        cxxflags_tokens=[f"-I{include}", "-std=c++20"],
        scope_macro_hash="deadbeef" * 8,
        anchor_root=prefix,
    )


def test_pch_distinct_in_workspace_includes_distinct_hashes():
    a = _pch(WORKSPACE, include=f"{WORKSPACE}/lib/util", header=f"{WORKSPACE}/pch.h")
    b = _pch(WORKSPACE, include=f"{WORKSPACE}/include/util", header=f"{WORKSPACE}/pch.h")
    assert a != b


def test_pch_in_vs_out_of_workspace_header_distinct_hashes():
    inside = _pch(WORKSPACE, include=f"{WORKSPACE}/lib/util", header=f"{WORKSPACE}/lib/util/pch.h")
    outside = _pch(WORKSPACE, include=f"{WORKSPACE}/lib/util", header="/elsewhere/lib/util/pch.h")
    assert inside != outside


# ---------------------------------------------------------------------------
# PCM cache non-collision guards
# ---------------------------------------------------------------------------


def _pcm(prefix: str, *, include: str, source: str) -> str:
    args = SimpleNamespace(CXX="/usr/bin/g++")
    return _pcm_command_hash(
        args=args,
        source_path=source,
        transitive_content_hash="aabb" * 16 + ":ccdd" * 16,
        cxxflags_tokens=[f"-I{include}", "-std=c++20"],
        magic_cpp_flags=[],
        magic_cxx_flags=[],
        extra_flags=[],
        stage="clang_module_interface",
        anchor_root=prefix,
    )


def test_pcm_distinct_in_workspace_includes_distinct_hashes():
    a = _pcm(WORKSPACE, include=f"{WORKSPACE}/lib/util", source=f"{WORKSPACE}/app.cppm")
    b = _pcm(WORKSPACE, include=f"{WORKSPACE}/include/util", source=f"{WORKSPACE}/app.cppm")
    assert a != b


def test_pcm_in_vs_out_of_workspace_source_distinct_hashes():
    inside = _pcm(WORKSPACE, include=f"{WORKSPACE}/lib/util", source=f"{WORKSPACE}/lib/app.cppm")
    outside = _pcm(WORKSPACE, include=f"{WORKSPACE}/lib/util", source="/elsewhere/lib/app.cppm")
    assert inside != outside
