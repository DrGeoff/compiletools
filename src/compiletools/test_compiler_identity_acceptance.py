"""Acceptance tests: every CAS cache-key site must canonicalise the
compiler binary path against the gitroot anchor.

Pairs with ``test_compiler_identity_hash.py`` (unit tests on the
helper) — this file is the integration / acceptance side: full
cache-key shapes, two-workspace property assertions drawn directly
from the upstream report's Part 1 reproducer.

Companion suite to ``test_cas_path_binding_acceptance``: that file
exercises path-bearing flag tokens and source paths; this file
exercises the *compiler binary* (and linker / archiver binary) path,
which is the leak vector identified in upstream report Part 1 and the
sister leaks of the same nature.

The leaks targeted here:
- ``compiler_identity``'s realpath segment (Part 1)
- ``_pch_command_hash`` ``cxx_command`` raw ``args.CXX``
- ``_pcm_command_hash`` ``cxx_command`` raw ``args.CXX``
- ``_link_key_hash`` ``ld_argv`` (incl. ``ld_argv[0]`` binary path)
- ``_lib_key_hash`` ``ar_argv_prefix`` (incl. ``ar_binary``)
- ``MacroState.build_context_hash`` raw ``compiler_path`` field

Each test pins mtime+atime on the wrapper script so the
``size|mtime_ns`` segment of ``compiler_identity`` matches across
workspaces — isolating the path-canonicalisation property from the
separate (and more invasive) mtime → content-hash refinement.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
and the upstream report at
``design/20260510-compiletools-cmd_hash-leaks-upstream-report.md``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from compiletools.apptools import compiler_identity

WRAPPER_CONTENT = '#!/bin/sh\nexec g++ "$@"\n'
FIXED_TIME = (1700000000, 1700000000)


def _make_workspace(root, name):
    """Create ``root/name/tools/cc-wrap.sh`` with pinned mtime so two
    workspaces produce identical (size, mtime_ns) for the wrapper."""
    ws = root / name / "workspace"
    (ws / "tools").mkdir(parents=True)
    binary = ws / "tools" / "cc-wrap.sh"
    binary.write_text(WRAPPER_CONTENT)
    binary.chmod(0o755)
    os.utime(binary, FIXED_TIME)
    return ws, binary


@pytest.fixture
def two_workspaces(tmp_path):
    """Two workspace fixtures with byte-identical wrapper scripts under
    different absolute paths. ``compiler_identity.cache_clear()`` runs
    before each call site invokes the helper to defeat lru_cache reuse
    across the two workspaces (which is what the production code would
    see across separate ct-cake processes anyway)."""
    ws_a, bin_a = _make_workspace(tmp_path, "run-A")
    ws_b, bin_b = _make_workspace(tmp_path, "run-B")
    compiler_identity.cache_clear()
    return (ws_a, bin_a), (ws_b, bin_b)


def _hash_both(two_workspaces, fn):
    """Run ``fn(prefix, binary)`` for both workspaces with
    ``compiler_identity.cache_clear()`` between them, defeating lru_cache
    reuse (mirrors separate ct-cake invocations)."""
    (ws_a, bin_a), (ws_b, bin_b) = two_workspaces
    compiler_identity.cache_clear()
    h_a = fn(ws_a, bin_a)
    compiler_identity.cache_clear()
    h_b = fn(ws_b, bin_b)
    return h_a, h_b


# ---------------------------------------------------------------------------
# _pch_command_hash — both compiler_identity AND raw cxx_command field
# ---------------------------------------------------------------------------


def test_pch_hash_stable_with_workspace_relative_compiler(two_workspaces):
    """A workspace-relative compiler wrapper must not pollute the PCH
    cache key. Reproducer: report Part 1 (``coverage-cc-wrapper.sh``
    inside the workspace, two CI checkouts under different attempts)."""
    from compiletools.build_backend import _pch_command_hash

    def _hash(prefix, binary):
        args = SimpleNamespace(CXX=str(binary))
        return _pch_command_hash(
            args=args,
            pch_header=f"{prefix}/lib/util/pch.h",
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            cxxflags_tokens=["-std=c++20"],
            scope_macro_hash="deadbeef" * 8,
            anchor_root=str(prefix),
        )

    h_a, h_b = _hash_both(two_workspaces, _hash)
    assert h_a == h_b, (
        f"PCH cache hash leaks workspace path through compiler_identity / cxx_command:\n  WS-A: {h_a}\n  WS-B: {h_b}"
    )


# ---------------------------------------------------------------------------
# _pcm_command_hash — same leak surface as PCH
# ---------------------------------------------------------------------------


def test_pcm_hash_stable_with_workspace_relative_compiler(two_workspaces):
    from compiletools.build_backend import _pcm_command_hash

    def _hash(prefix, binary):
        args = SimpleNamespace(CXX=str(binary))
        return _pcm_command_hash(
            args=args,
            source_path=f"{prefix}/lib/util/app.cppm",
            transitive_content_hash="aabb" * 16 + ":ccdd" * 16,
            cxxflags_tokens=["-std=c++20"],
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            extra_flags=[],
            stage="clang_module_interface",
            anchor_root=str(prefix),
        )

    h_a, h_b = _hash_both(two_workspaces, _hash)
    assert h_a == h_b, (
        f"PCM cache hash leaks workspace path through compiler_identity / cxx_command:\n  WS-A: {h_a}\n  WS-B: {h_b}"
    )


# ---------------------------------------------------------------------------
# MacroState.build_context_hash — raw compiler_path field leak
# ---------------------------------------------------------------------------


def test_macro_state_hash_stable_with_workspace_relative_compiler_path(two_workspaces):
    """MacroState.compiler_path is the user-visible CC string, embedded
    raw into the per-TU object cache key (preprocessing_cache.py:457).
    A workspace-relative compiler path makes the per-TU object cache
    cold across workspaces."""
    from compiletools.preprocessing_cache import MacroState

    def _hash(prefix, binary):
        identity = compiler_identity(str(binary), anchor_root=str(prefix))
        state = MacroState(
            core={b"__GNUC__": b"13"},
            variable={},
            compiler_path=str(binary),
            cppflags="-O2",
            cflags="-O2",
            cxxflags="-std=c++20",
            cmdline_origin=frozenset(),
            cppflags_tokens=["-O2"],
            cflags_tokens=["-O2"],
            cxxflags_tokens=["-std=c++20"],
            compiler_identity=identity,
            anchor_root=str(prefix),
        )
        return state.get_hash(include_core=True)

    h_a, h_b = _hash_both(two_workspaces, _hash)
    assert h_a == h_b, (
        f"Per-TU object cache hash leaks workspace path through MacroState.compiler_path:\n  WS-A: {h_a}\n  WS-B: {h_b}"
    )


# ---------------------------------------------------------------------------
# _link_key_hash / _lib_key_hash — workspace-relative linker / archiver
# ---------------------------------------------------------------------------


def _artefact_key(payload):
    """Mirror BuildBackend._compute_artefact_key_hash. Standalone so the
    link/ar key tests don't need to instantiate a BuildBackend."""
    import hashlib
    import json

    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_link_key_stable_with_workspace_relative_linker(two_workspaces):
    """The executable link key payload (build_backend.py:2411) must
    canonicalise both ``linker_identity`` and every ``ld_argv`` element.
    Builds the same payload shape the production code builds and asserts
    the resulting sha256 is workspace-portable."""
    from compiletools.apptools import canonicalize_paths_for_cache_key

    def _key(prefix, binary):
        anchor = str(prefix)
        ld_argv = [str(binary), "-pthread"]
        object_names = [f"{prefix}/build/foo.o", f"{prefix}/build/bar.o"]
        payload = {
            "linker_identity": compiler_identity(ld_argv[0], anchor_root=anchor),
            "ld_argv": canonicalize_paths_for_cache_key(ld_argv, anchor),
            "objects": sorted(canonicalize_paths_for_cache_key(object_names, anchor)),
        }
        return _artefact_key(payload)

    k_a, k_b = _hash_both(two_workspaces, _key)
    assert k_a == k_b, (
        f"Link cache key leaks workspace path through linker_identity / ld_argv:\n  WS-A: {k_a}\n  WS-B: {k_b}"
    )


def test_ar_key_stable_with_workspace_relative_archiver(two_workspaces):
    """Static-library key payload (build_backend.py:2519) must
    canonicalise both ``ar_identity`` and ``ar_argv_prefix``."""
    from compiletools.apptools import canonicalize_paths_for_cache_key

    def _key(prefix, binary):
        anchor = str(prefix)
        ar_argv_prefix = [str(binary), "-src"]
        object_names = [f"{prefix}/build/foo.o", f"{prefix}/build/bar.o"]
        payload = {
            "ar_argv_prefix": canonicalize_paths_for_cache_key(ar_argv_prefix, anchor),
            "ar_identity": compiler_identity(ar_argv_prefix[0], anchor_root=anchor),
            "objects": sorted(canonicalize_paths_for_cache_key(object_names, anchor)),
        }
        return _artefact_key(payload)

    k_a, k_b = _hash_both(two_workspaces, _key)
    assert k_a == k_b, (
        f"ar cache key leaks workspace path through ar_identity / ar_argv_prefix:\n  WS-A: {k_a}\n  WS-B: {k_b}"
    )
