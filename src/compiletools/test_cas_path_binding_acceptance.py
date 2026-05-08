"""Acceptance tests: CAS cache keys must be path-independent.

These tests assert the property that fixed-content TUs produce the SAME
cache-key hash even when the workspace they live in moves to a different
absolute path. Without this property, every CI re-run that lands in a
new workspace directory (e.g. a runner attempt-2 directory) is
effectively cold across all three CAS-keyed sites.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

from types import SimpleNamespace

from compiletools.preprocessing_cache import MacroState

WS1 = "/run-1/workspace"
WS2 = "/run-2/workspace"


def _macro_state_hash_for_prefix(prefix: str) -> str:
    """Build a MacroState whose flag tokens reference paths under `prefix`,
    then return its full include_core hash. Inputs are byte-identical
    across calls except for `prefix`."""
    cppflags_tokens = [f"-I{prefix}/lib/util", f"-I{prefix}/include/include", "-DAPP=app"]
    cflags_tokens = ["-O2"]
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20"]
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
        anchor_root=prefix,  # the canonicalizer anchors against the gitroot equivalent
    )
    return state.get_hash(include_core=True)


def _pch_hash_for_prefix(prefix: str) -> str:
    from compiletools.build_backend import _pch_command_hash

    args = SimpleNamespace(CXX="/usr/bin/g++", git_root=prefix)
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20"]
    magic_cpp_flags = [f"-I{prefix}/include/include"]
    magic_cxx_flags = []
    pch_header = f"{prefix}/lib/util/pch.h"
    return _pch_command_hash(
        args=args,
        pch_header=pch_header,
        magic_cpp_flags=magic_cpp_flags,
        magic_cxx_flags=magic_cxx_flags,
        cxxflags_tokens=cxxflags_tokens,
        scope_macro_hash="deadbeef" * 8,
    )


def _pcm_hash_for_prefix(prefix: str) -> str:
    from compiletools.build_backend import _pcm_command_hash

    args = SimpleNamespace(CXX="/usr/bin/g++", git_root=prefix)
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20"]
    source_path = f"{prefix}/lib/util/app.cppm"
    return _pcm_command_hash(
        args=args,
        source_path=source_path,
        transitive_content_hash="aabb" * 16 + ":ccdd" * 16,
        cxxflags_tokens=cxxflags_tokens,
        magic_cpp_flags=[],
        magic_cxx_flags=[],
        extra_flags=[],
        stage="clang_module_interface",
    )


def test_object_cache_path_independent():
    """MacroState.get_hash(include_core=True) must NOT depend on the
    absolute workspace path. Two TUs whose source/headers/flags match
    bit-for-bit modulo the workspace prefix MUST hash identically."""
    h1 = _macro_state_hash_for_prefix(WS1)
    h2 = _macro_state_hash_for_prefix(WS2)
    assert h1 == h2, f"object cache hash is workspace-bound: WS1={h1} != WS2={h2}. Every CI re-run becomes cache-cold."


def test_pch_cache_path_independent():
    """_pch_command_hash must be invariant under workspace path moves."""
    h1 = _pch_hash_for_prefix(WS1)
    h2 = _pch_hash_for_prefix(WS2)
    assert h1 == h2, f"PCH cache hash is workspace-bound: WS1={h1} != WS2={h2}. PCH cache cold across CI re-runs."


def test_pcm_cache_path_independent():
    """_pcm_command_hash must be invariant under workspace path moves."""
    h1 = _pcm_hash_for_prefix(WS1)
    h2 = _pcm_hash_for_prefix(WS2)
    assert h1 == h2, f"PCM cache hash is workspace-bound: WS1={h1} != WS2={h2}. PCM cache cold across CI re-runs."
