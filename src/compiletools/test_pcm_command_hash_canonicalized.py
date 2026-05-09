"""Integration tests: _pcm_command_hash applies the canonicalizer.

Verifies the C++20 module BMI cache key is stable across workspace
path moves AND still distinguishes legitimately different sources/flags.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

from types import SimpleNamespace

from compiletools.build_backend import _pcm_command_hash


def _hash(prefix: str, *, source_name: str = "app.cppm", extra_cxx: str = "") -> str:
    """Build a _pcm_command_hash invocation whose flag tokens and source
    path live under `prefix`."""
    args = SimpleNamespace(CXX="/usr/bin/g++")
    cxx_extra = [extra_cxx] if extra_cxx else []
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20", *cxx_extra]
    source_path = f"{prefix}/lib/util/{source_name}"
    return _pcm_command_hash(
        args=args,
        source_path=source_path,
        transitive_content_hash="aabb" * 16 + ":ccdd" * 16,
        cxxflags_tokens=cxxflags_tokens,
        magic_cpp_flags=[],
        magic_cxx_flags=[],
        extra_flags=[],
        stage="clang_module_interface",
        anchor_root=prefix,
    )


def test_pcm_hash_stable_across_workspace_moves():
    """Same module under two workspace prefixes hashes identically.
    Covers BOTH flag tokens AND the standalone source_path."""
    assert _hash("/run-1/workspace") == _hash("/run-2/workspace")


def test_pcm_hash_distinguishes_different_sources():
    """Two different module sources under the same workspace hash differently."""
    a = _hash("/some/workspace", source_name="app.cppm")
    b = _hash("/some/workspace", source_name="widget.cppm")
    assert a != b, "Canonicalizer over-stripped: distinct module sources must yield distinct hashes"


def test_pcm_hash_distinguishes_real_flag_changes():
    """Adding -O3 to cxxflags must change the hash even when paths canonicalize."""
    baseline = _hash("/some/workspace")
    with_o3 = _hash("/some/workspace", extra_cxx="-O3")
    assert baseline != with_o3, "Canonicalizer over-stripped: -O3 addition must change the PCM hash"
