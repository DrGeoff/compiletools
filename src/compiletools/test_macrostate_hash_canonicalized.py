"""Integration tests: MacroState.get_hash applies the canonicalizer.

Verifies the object-cache key (macro_state_hash) is stable across
workspace path moves AND still distinguishes legitimately different
flag configurations.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

from compiletools.preprocessing_cache import MacroState


def _build(prefix: str, *, extra_cxx: str = "") -> str:
    """Build a MacroState whose flag tokens reference paths under `prefix`,
    optionally appending an extra CXX flag, and return its full hash."""
    cxx_extra = [extra_cxx] if extra_cxx else []
    cppflags_tokens = [f"-I{prefix}/lib/util", "-DAPP=app"]
    cflags_tokens = ["-O2"]
    cxxflags_tokens = [f"-I{prefix}/lib/util", "-std=c++20", *cxx_extra]
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
        anchor_root=prefix,
    )
    return state.get_hash(include_core=True)


def test_object_hash_stable_across_workspace_moves():
    """Same TU under two workspace prefixes hashes identically."""
    assert _build("/run-1/workspace") == _build("/run-2/workspace")


def test_object_hash_distinguishes_optimization_flag_change():
    """A genuine flag change (adding -O3) MUST still produce a different hash."""
    h_baseline = _build("/run-1/workspace")
    h_with_o3 = _build("/run-1/workspace", extra_cxx="-O3")
    assert h_baseline != h_with_o3, "Canonicalizer over-stripped: -O3 addition must change the hash"


def test_object_hash_distinguishes_in_workspace_vs_outside():
    """A path inside the workspace vs the same relative path outside it
    must hash differently — the anchor adds disambiguating context."""
    inside = _build("/some/workspace")
    # Same logical TU but headers come from a sibling repo outside the
    # workspace anchor; cppflags/cxxflags reference /elsewhere/... and
    # the anchor stays as the workspace.
    cppflags_tokens = ["-I/elsewhere/lib/util", "-DAPP=app"]
    cflags_tokens = ["-O2"]
    cxxflags_tokens = ["-I/elsewhere/lib/util", "-std=c++20"]
    elsewhere_state = MacroState(
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
        anchor_root="/some/workspace",
    )
    elsewhere_hash = elsewhere_state.get_hash(include_core=True)
    assert inside != elsewhere_hash, "Canonicalizer must distinguish in-anchor from out-of-anchor paths"


def test_object_hash_empty_anchor_is_graceful_noop():
    """anchor_root='' (the fallback when gitroot can't be resolved) must
    be a no-op canonicaliser: two identically-constructed MacroStates
    both passing anchor_root='' must hash identically."""
    cppflags_tokens = ["-I/foo/bar", "-DAPP=app"]
    cflags_tokens = ["-O2"]
    cxxflags_tokens = ["-I/foo/bar", "-std=c++20"]

    def _make() -> MacroState:
        return MacroState(
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
            anchor_root="",
        )

    assert _make().get_hash(include_core=True) == _make().get_hash(include_core=True)
