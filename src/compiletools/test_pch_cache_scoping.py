"""Integration + unit tests for the PCH cache-key scope-filter (PCH-A).

The PCH cache key (``_pch_command_hash``) historically hashed
``args.CXXFLAGS`` raw, so two builds that differ only in an irrelevant
``-DAPP_NAME=...`` value produced distinct PCH cache directories --
wasting tens-of-MB-per-entry of disk and defeating cross-app PCH reuse.
This is the same pollution pattern as the per-TU object hash; this test
file mirrors :mod:`test_hunter_cache_scoping` for the PCH side.

The load-bearing reproducer is
``test_pch_cache_key_unchanged_when_unused_cmdline_macro_changes``.
"""

from types import SimpleNamespace

import configargparse
import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_backend import _pch_command_hash, _pch_scope_macro_hash
from compiletools.build_context import BuildContext


def _make_hunter(extra_args, temp_config):
    """Build a fresh Hunter wired up with its own BuildContext."""
    argv = ["-c", temp_config, "--include", uth.ctdir()] + list(extra_args)
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.hunter.add_arguments(cap)
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)
    return hntr


def _process(hunter, sample_path):
    """Drive the hunter's magicflags pipeline for ``sample_path``."""
    try:
        hunter.magicflags(sample_path)
    except RuntimeError as e:
        if "No functional C++ compiler detected" in str(e):
            pytest.skip("No functional C++ compiler detected")
        raise


def _sample(rel):
    return uth.example_file(f"cache_scoping/{rel}")


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe global configargparse parser cache around every test, and
    construct a throwaway ArgumentParser to mirror the long-standing
    TestHunterModule.setup_method pattern."""
    uth.reset()
    configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    yield
    uth.reset()


@pytest.fixture
def temp_config():
    """Provide a temp config path plus a fresh isolated tmp dir (no cwd change)."""
    with uth.TempDirContextNoChange(), uth.TempConfigContext() as cfg:
        yield cfg


def _hash_pch_with_app_name(value, sample_rel, temp_config):
    """Build a Hunter with ``-DAPP_NAME=<value>`` and compute the PCH
    command hash for ``sample_rel`` (used as a PCH header)."""
    hntr = _make_hunter(
        [f"--append-CPPFLAGS=-DAPP_NAME={value}"],
        temp_config,
    )
    sample = _sample(sample_rel)
    _process(hntr, sample)
    # Sanity: cmdline_origin actually contains APP_NAME.
    assert sz.Str("APP_NAME") in hntr.magicparser._initial_macro_state.cmdline_origin

    cxxflags_tokens = compiletools.apptools.tokenize_compile_flags("", "", hntr.args.CXXFLAGS)[2]
    scope_macro_hash = _pch_scope_macro_hash(hntr, sample)
    return _pch_command_hash(
        hntr.args,
        sample,
        magic_cpp_flags=[],
        magic_cxx_flags=[],
        cxxflags_tokens=cxxflags_tokens,
        scope_macro_hash=scope_macro_hash,
        anchor_root="",
    )


class TestPchCacheKeyScopeFilter:
    """The PCH-side mirror of TestMacroStateHashScopeFilter."""

    def test_pch_cache_key_unchanged_when_unused_cmdline_macro_changes(self, temp_config):
        """Load-bearing reproducer: ``no_ref.cpp`` does not reference
        ``APP_NAME``, so two PCH builds that differ only in
        ``-DAPP_NAME=A`` vs ``-DAPP_NAME=B`` must produce IDENTICAL PCH
        cache keys. Pre-fix this assertion fails (the cmdline -D value
        leaks into the hash)."""
        h_a = _hash_pch_with_app_name("A", "no_ref.cpp", temp_config)
        h_b = _hash_pch_with_app_name("B", "no_ref.cpp", temp_config)
        assert h_a == h_b, (
            "no_ref.cpp does not reference APP_NAME, so changing the "
            "cmdline -DAPP_NAME=... value must NOT change the PCH cache key"
        )

    def test_pch_cache_key_changes_when_referenced_cmdline_macro_changes(self, temp_config):
        """Counter-test: ``with_ref.cpp`` references ``APP_NAME``
        directly. The PCH cache key must change when the macro value
        changes -- otherwise the filter is over-aggressive and we'd
        silently reuse stale PCH bytes."""
        h_a = _hash_pch_with_app_name("A", "with_ref.cpp", temp_config)
        h_b = _hash_pch_with_app_name("B", "with_ref.cpp", temp_config)
        assert h_a != h_b, (
            "with_ref.cpp uses APP_NAME, so distinct -DAPP_NAME=... values must produce distinct PCH cache keys"
        )

    def test_pch_cache_key_via_transitive_header(self, temp_config):
        """``tu_via_header.cpp`` does not mention ``APP_NAME`` in its own
        bytes -- the reference is in ``header_ref.hpp``. The transitive
        walk must surface the macro so distinct ``-DAPP_NAME=`` values
        still produce distinct PCH cache keys."""
        h_a = _hash_pch_with_app_name("A", "tu_via_header.cpp", temp_config)
        h_b = _hash_pch_with_app_name("B", "tu_via_header.cpp", temp_config)
        assert h_a != h_b, (
            "tu_via_header.cpp pulls APP_NAME in via header_ref.hpp, so "
            "the transitive scan must keep APP_NAME in the PCH cache key"
        )


class TestPchCacheKeyNonDFlagsStillMatter:
    """Make sure the new scope-filtered hashing doesn't drop NON-D flag
    sensitivity. Without this guard the fix could over-filter."""

    def test_pch_cache_key_changes_with_meaningful_flag_changes(self):
        """``-O2`` vs ``-O3`` must still produce distinct PCH cache keys
        even after the -D scope filter is applied."""
        args_o2 = SimpleNamespace(CXX="g++", CXXFLAGS="-O2")
        args_o3 = SimpleNamespace(CXX="g++", CXXFLAGS="-O3")

        tokens_o2 = compiletools.apptools.tokenize_compile_flags("", "", args_o2.CXXFLAGS)[2]
        tokens_o3 = compiletools.apptools.tokenize_compile_flags("", "", args_o3.CXXFLAGS)[2]
        # Same scope-macro hash -- only the structured tokens differ.
        scope_zero = "0" * 16

        h_o2 = _pch_command_hash(
            args_o2,
            "/src/stdafx.h",
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            cxxflags_tokens=tokens_o2,
            scope_macro_hash=scope_zero,
            anchor_root="",
        )
        h_o3 = _pch_command_hash(
            args_o3,
            "/src/stdafx.h",
            magic_cpp_flags=[],
            magic_cxx_flags=[],
            cxxflags_tokens=tokens_o3,
            scope_macro_hash=scope_zero,
            anchor_root="",
        )
        assert h_o2 != h_o3, (
            "Non-D flag changes (-O2 vs -O3) must still produce distinct "
            "PCH cache keys -- the scope filter must not over-strip"
        )


class TestPchScopeMacroHashEdgeCases:
    """Unit tests for ``_pch_scope_macro_hash`` directly."""

    def test_pch_scope_macro_hash_empty_origin_returns_zeros(self, temp_config):
        """When ``cmdline_origin`` is empty (no ``--append-*FLAGS=-D...``),
        ``_pch_scope_macro_hash`` returns 16 zero hex chars. The full
        ``_pch_command_hash`` should still produce a stable hash."""
        hntr = _make_hunter([], temp_config)
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        # Confirm precondition.
        assert hntr.magicparser._initial_macro_state.cmdline_origin == frozenset()

        scope_hash = _pch_scope_macro_hash(hntr, sample)
        assert scope_hash == "0" * 16

        # Full pch hash is stable across calls with same inputs.
        tokens = compiletools.apptools.tokenize_compile_flags("", "", hntr.args.CXXFLAGS)[2]
        h1 = _pch_command_hash(
            hntr.args, sample, [], [], cxxflags_tokens=tokens, scope_macro_hash=scope_hash, anchor_root=""
        )
        h2 = _pch_command_hash(
            hntr.args, sample, [], [], cxxflags_tokens=tokens, scope_macro_hash=scope_hash, anchor_root=""
        )
        assert h1 == h2

    def test_pch_scope_macro_hash_no_referenced_macros_returns_zeros(self, temp_config):
        """``cmdline_origin`` non-empty but the PCH header references
        none of the cmdline-D macros: ``_pch_scope_macro_hash`` returns
        the all-zeros sentinel (no scoping applied)."""
        hntr = _make_hunter(
            ["--append-CPPFLAGS=-DAPP_NAME=A"],
            temp_config,
        )
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        assert sz.Str("APP_NAME") in hntr.magicparser._initial_macro_state.cmdline_origin

        scope_hash = _pch_scope_macro_hash(hntr, sample)
        assert scope_hash == "0" * 16, (
            "no_ref.cpp does not reference APP_NAME, so the scope "
            "filter should be empty and yield the all-zeros sentinel"
        )


# --- TOKEN-3: diagnostic-only flag tokens are excluded from PCH cache key ---


class _StubArgs:
    """Stand-in for ``args`` that ``_pch_command_hash`` only reads
    ``CXX`` from. Avoids spinning up a Hunter for these focused unit
    tests."""

    def __init__(self, cxx="g++"):
        self.CXX = cxx


def _pch_hash(args, *, cxx_tokens=("-O2",), magic_cxx=()):
    """Call `_pch_command_hash` with dummy fixed args (pch_header,
    magic_cpp_flags=[], scope_macro_hash, anchor_root) and only the
    varying `cxxflags_tokens` / `magic_cxx_flags` exposed. Shared by
    the 2 PCH warning-filter tests below."""
    return _pch_command_hash(
        args,
        pch_header="/tmp/header.hpp",
        magic_cpp_flags=[],
        magic_cxx_flags=list(magic_cxx),
        cxxflags_tokens=list(cxx_tokens),
        scope_macro_hash="0" * 16,
        anchor_root="",
    )


def test_pch_cache_key_unchanged_with_w_warning_change():
    """Two ``_pch_command_hash`` invocations differing only in
    ``-Wall`` vs ``-Wextra`` inside ``cxxflags_tokens`` must produce
    the SAME PCH cache key.

    TOKEN-5: ``_pch_command_hash`` no longer self-filters its
    ``cxxflags_tokens`` parameter -- the caller is responsible (and
    in production calls ``args.flags.hash_relevant("cxx")``). Mirror
    that contract here by pre-filtering before each invocation.
    """

    def _hr(tokens):
        return compiletools.apptools.filter_hash_irrelevant_tokens(compiletools.apptools.strip_d_u_tokens(tokens))

    args = _StubArgs()
    h_wall = _pch_hash(args, cxx_tokens=_hr(["-O2", "-Wall"]))
    h_wextra = _pch_hash(args, cxx_tokens=_hr(["-O2", "-Wextra"]))
    assert h_wall == h_wextra


def test_pch_cache_key_unchanged_with_w_warning_in_magic_cxx_flags():
    """Magic-flag warnings (``//#CXXFLAGS=-Wall``) must also be
    filtered from the PCH cache key."""
    args = _StubArgs()
    h_wall = _pch_hash(args, magic_cxx=["-Wall"])
    h_wextra = _pch_hash(args, magic_cxx=["-Wextra"])
    assert h_wall == h_wextra
