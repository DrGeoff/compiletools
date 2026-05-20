"""Integration tests for the Hunter -> CmdlineMacroIndex wiring (Task E).

Validates the report's reproducer: cmdline ``-D`` macros that aren't
referenced by a TU or its transitive headers must NOT contribute to that
TU's ``macro_state_hash``. Without the fix, two builds that differ only
in an irrelevant ``-DAPP_NAME=...`` value produce different object-file
paths -- the cache pollution this PR exists to fix.

Also covers:
  - Backward compatibility: ``macro_state_hash(filename)`` (no dep_hash)
    matches the pre-fix behaviour exactly.
  - No-cmdline-D early-return: ``dep_hash`` argument is a no-op when
    ``cmdline_origin`` is empty.
  - Counter-test: a TU that DOES reference ``APP_NAME`` keeps it in the
    hash, so distinct ``-DAPP_NAME=...`` values produce distinct hashes.
  - Transitive scan: a TU that references ``APP_NAME`` only via a header
    still keeps it in the hash.
  - ``_transitive_content_hashes`` excludes the TU itself.
"""

import configargparse
import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


def _make_hunter(extra_args, temp_config):
    """Build a fresh Hunter wired up with its own BuildContext.

    A fresh BuildContext per call ensures the global hash registry and
    per-build caches don't leak between scenarios -- critical for the
    hash-comparison tests below.
    """
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
    """Drive the hunter's magicflags pipeline so macro_state_hash is callable.

    Skips if no functional C++ compiler is available -- ``CppMagicFlags``
    construction can fail with ``RuntimeError("No functional C++ compiler
    detected")`` on bare CI hosts.
    """
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
    """Provide a temp config path plus a fresh isolated tmp dir (no cwd change).

    The bare ``TempDirContextNoChange()`` creates a sandbox dir so the test runs
    in a predictable filesystem state; the config file itself is created by
    ``TempConfigContext()`` in the system temp dir.
    """
    with uth.TempDirContextNoChange(), uth.TempConfigContext() as cfg:
        yield cfg


class TestMacroStateHashBackwardCompat:
    def test_macro_state_hash_no_dep_hash_unchanged(self, temp_config):
        """Without ``dep_hash``, ``Hunter.macro_state_hash`` must equal
        ``magicparser.get_final_macro_state_hash`` exactly. This is the
        pre-fix backward-compat path -- callers that haven't been
        updated to pass ``dep_hash`` see identical behaviour."""
        hntr = _make_hunter([], temp_config)
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        via_hunter = hntr.macro_state_hash(sample)
        via_parser = hntr.magicparser.get_final_macro_state_hash(sample)
        assert via_hunter == via_parser

    def test_macro_state_hash_with_dep_hash_no_cmdline_origin_unchanged(self, temp_config):
        """``dep_hash`` is a no-op when there are no cmdline ``-D`` macros
        in scope. The early-return in ``Hunter.macro_state_hash`` short-
        circuits the whole index walk in this case."""
        hntr = _make_hunter([], temp_config)
        sample = _sample("no_ref.cpp")
        _process(hntr, sample)

        assert hntr.magicparser._initial_macro_state.cmdline_origin == frozenset()
        without = hntr.macro_state_hash(sample)
        with_dep = hntr.macro_state_hash(sample, dep_hash="deadbeef" * 4)
        assert without == with_dep


class TestMacroStateHashScopeFilter:
    """The report's reproducer plus its correctness counter-test."""

    def _hash_with_app_name(self, value, sample_rel, dep_hash):
        """Build a fresh hunter with ``-DAPP_NAME=<value>`` and return
        the per-TU macro_state_hash for ``sample_rel``."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr = _make_hunter(
                [f"--append-CPPFLAGS=-DAPP_NAME={value}"],
                temp_config,
            )
            sample = _sample(sample_rel)
            _process(hntr, sample)
            # Sanity: cmdline_origin actually contains APP_NAME.
            import stringzilla as sz

            assert sz.Str("APP_NAME") in hntr.magicparser._initial_macro_state.cmdline_origin
            return hntr.macro_state_hash(sample, dep_hash=dep_hash)

    def test_macro_state_hash_with_dep_hash_excludes_unused_cmdline_macro(self):
        """The reproducer: ``no_ref.cpp`` does not reference ``APP_NAME``
        anywhere, so ``-DAPP_NAME=A`` and ``-DAPP_NAME=B`` must produce
        IDENTICAL ``macro_state_hash`` values. Pre-fix this assertion
        fails (the cmdline ``-D`` value leaks into the hash)."""
        dep_hash = "0" * 16
        h_a = self._hash_with_app_name("A", "no_ref.cpp", dep_hash)
        h_b = self._hash_with_app_name("B", "no_ref.cpp", dep_hash)
        assert h_a == h_b, (
            "no_ref.cpp does not reference APP_NAME, so changing the "
            "cmdline -DAPP_NAME=... value must NOT change the hash"
        )

    def test_macro_state_hash_with_dep_hash_includes_referenced_cmdline_macro(self):
        """The counter-test: ``with_ref.cpp`` references ``APP_NAME``
        directly. The scope filter must keep ``APP_NAME`` in the hash so
        distinct values produce distinct hashes -- otherwise the filter
        is over-aggressive and we'd silently reuse stale objects."""
        dep_hash = "0" * 16
        h_a = self._hash_with_app_name("A", "with_ref.cpp", dep_hash)
        h_b = self._hash_with_app_name("B", "with_ref.cpp", dep_hash)
        assert h_a != h_b, "with_ref.cpp uses APP_NAME, so distinct -DAPP_NAME=... values must produce distinct hashes"

    def test_macro_state_hash_via_transitive_header(self):
        """``tu_via_header.cpp`` does not mention ``APP_NAME`` in its own
        bytes -- the reference is in the ``header_ref.hpp`` header it
        includes. The transitive walk in
        ``Hunter._transitive_content_hashes`` must surface the macro so
        distinct ``-DAPP_NAME=`` values still produce distinct hashes."""
        dep_hash = "0" * 16
        h_a = self._hash_with_app_name("A", "tu_via_header.cpp", dep_hash)
        h_b = self._hash_with_app_name("B", "tu_via_header.cpp", dep_hash)
        assert h_a != h_b, (
            "tu_via_header.cpp pulls APP_NAME in via header_ref.hpp, so "
            "the transitive scan must keep APP_NAME in the hash"
        )


class TestTransitiveContentHashes:
    def test_transitive_content_hashes_excludes_tu_itself(self, temp_config):
        """``_transitive_content_hashes`` returns headers only -- the TU
        itself is scanned separately (via ``tu_content_hash`` in
        ``CmdlineMacroIndex.tu_referenced_macros``). Including it twice
        would just be wasted work, but more importantly the contract
        with the index module assumes the TU hash is supplied
        separately."""
        from compiletools.global_hash_registry import get_file_hash

        hntr = _make_hunter([], temp_config)
        sample = _sample("tu_via_header.cpp")
        _process(hntr, sample)

        transitive = hntr._transitive_content_hashes(sample)
        tu_hash = get_file_hash(sample, hntr.context)
        assert tu_hash not in transitive, "_transitive_content_hashes must NOT include the TU's own content hash"
        # Sanity: the transitive list is non-empty (header_ref.hpp).
        assert len(transitive) >= 1


class TestMacroStateHashCacheClear:
    """The lazily-built CmdlineMacroIndex must be evicted by
    ``clear_instance_cache`` so a subsequent reparse with different
    cmdline -D flags can't see the old ``cmdline_origin``."""

    def test_clear_instance_cache_drops_cmdline_macro_index(self, temp_config):
        hntr = _make_hunter(
            ["--append-CPPFLAGS=-DAPP_NAME=A"],
            temp_config,
        )
        sample = _sample("with_ref.cpp")
        _process(hntr, sample)
        # Force the index to be built.
        hntr.macro_state_hash(sample, dep_hash="0" * 16)
        assert hasattr(hntr, "_cmdline_macro_index_cached")

        hntr.clear_instance_cache()
        assert not hasattr(hntr, "_cmdline_macro_index_cached")
