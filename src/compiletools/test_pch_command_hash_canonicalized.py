"""Integration tests: _pch_command_hash applies the canonicalizer.

Verifies the PCH cache key is stable across workspace path moves AND
still distinguishes legitimately different headers/flags.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

from types import SimpleNamespace

from compiletools.build_backend import _pch_command_hash


def _hash(prefix: str, *, header_name: str = "pch.h", extra_cxx: str = "") -> str:
    """Build a _pch_command_hash invocation whose flag tokens, magic
    flags, and pch_header path all live under `prefix`. Optionally
    rename the header (for the distinguishes-headers test) or append
    an extra CXX flag (for the distinguishes-flags test)."""
    args = SimpleNamespace(CXX="/usr/bin/g++")
    cxx_extra = [extra_cxx] if extra_cxx else []
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20", *cxx_extra]
    magic_cpp_flags = [f"-I{prefix}/include/include"]
    magic_cxx_flags = []
    pch_header = f"{prefix}/lib/util/{header_name}"
    return _pch_command_hash(
        args=args,
        pch_header=pch_header,
        magic_cpp_flags=magic_cpp_flags,
        magic_cxx_flags=magic_cxx_flags,
        cxxflags_tokens=cxxflags_tokens,
        scope_macro_hash="deadbeef" * 8,
        anchor_root=prefix,
    )


def test_pch_hash_stable_across_workspace_moves():
    """Same PCH header + flags under two workspace prefixes hash identically.
    Covers BOTH the flag tokens AND the standalone pch_header path."""
    assert _hash("/run-1/workspace") == _hash("/run-2/workspace")


def test_pch_hash_distinguishes_different_headers():
    """Two different headers under the same workspace must hash differently."""
    a = _hash("/some/workspace", header_name="pch_a.h")
    b = _hash("/some/workspace", header_name="pch_b.h")
    assert a != b, "Canonicalizer over-stripped: distinct PCH headers must yield distinct hashes"


def test_pch_hash_distinguishes_real_flag_changes():
    """Adding -O3 to cxxflags must change the hash even when paths canonicalize."""
    baseline = _hash("/some/workspace")
    with_o3 = _hash("/some/workspace", extra_cxx="-O3")
    assert baseline != with_o3, "Canonicalizer over-stripped: -O3 addition must change the PCH hash"
