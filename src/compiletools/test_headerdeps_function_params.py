"""Function-like macro arity survives the DirectHeaderDeps include cache.

``_create_include_list`` keeps its own two-tier cache of
``(include_list, FileEffects)`` per file. Before the effects carried
``file_function_params`` the replay restored a function-like macro's *body*
without its *parameter list*, so a warm traversal saw ``F`` as object-like
and every ``#if F(2, 0)`` downstream of it took the other branch.

The damage is not confined to branch selection.  ``magicflags`` builds
``all_source_files = [filename] + headers`` straight off
``headerdeps.process``, so a stranded replay also changes which files the
magic-flag scan ever visits — a test asserting only the include list would pass
a fix that left the scan set stale.  Every case here asserts both halves: the
header the graph selected AND the flags harvested from it.
"""

import os

import configargparse
import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


def _direct_headerdeps(context, tempdir):
    """A DirectHeaderDeps bound to ``context``, so its caches are shared."""
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.headerdeps.add_arguments(cap)
    argv = ["--headerdeps=direct", "-c", uth.create_temp_config(tempdir)]
    args = compiletools.apptools.parseargs(cap, argv, context=context)
    return compiletools.headerdeps.create(args, context=context)


# EXTLIB is 2.5, so EXTLIB_AT_LEAST(2, 0) is true and the new_api arm wins.
_FUNCTION_LIKE_MACROS = (
    "#pragma once\n"
    "#define EXTLIB_MAJOR 2\n"
    "#define EXTLIB_MINOR 5\n"
    "#define EXTLIB_AT_LEAST(major, minor) \\\n"
    "    (EXTLIB_MAJOR > (major) || (EXTLIB_MAJOR == (major) && EXTLIB_MINOR >= (minor)))\n"
)

_OBJECT_LIKE_MACROS = "#pragma once\n#define USE_NEW 1\n"

_NEW_API = "#pragma once\n//#CXXFLAGS=-DUSING_NEW_API\n"
_OLD_API = "#pragma once\n//#CXXFLAGS=-DUSING_OLD_API\n"


def _gate(condition: str) -> str:
    return f'#pragma once\n#if {condition}\n#include "new_api.h"\n#else\n#include "old_api.h"\n#endif\n'


def _self_including_gate(condition: str) -> str:
    """A gate that pulls in its own defining header before testing it."""
    return (
        "#pragma once\n"
        '#include "macros.h"\n'
        f"#if {condition}\n"
        '#include "new_api.h"\n'
        "#else\n"
        '#include "old_api.h"\n'
        "#endif\n"
    )


_REPLAY_SOURCES = {
    "macros.h": _FUNCTION_LIKE_MACROS,
    "gate.h": _gate("EXTLIB_AT_LEAST(2, 0)"),
    "new_api.h": _NEW_API,
    "old_api.h": _OLD_API,
    "main.cpp": '#include "macros.h"\n#include "gate.h"\nint main() { return 0; }\n',
}


class TestFunctionParamsSurviveTheIncludeCache(tb.BaseCompileToolsTestCase):
    """A warm traversal must select the same headers as the cold one."""

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _traverse_twice(self, sources: dict) -> tuple[list[str], list[str]]:
        """Two DirectHeaderDeps traversals sharing one BuildContext.

        The second one replays every entry the first one stored, which is the
        only way to exercise the cached branch from the outside.
        """
        files = uth.write_sources(sources, target_dir=self._tmpdir)
        context = BuildContext()
        main = str(files["main.cpp"])

        first = _direct_headerdeps(context, self._tmpdir).process(main, frozenset())
        second = _direct_headerdeps(context, self._tmpdir).process(main, frozenset())
        return (
            [os.path.basename(path) for path in first],
            [os.path.basename(path) for path in second],
        )

    def test_a_replayed_defining_header_keeps_the_gate_on_the_new_api_arm(self):
        first, second = self._traverse_twice(_REPLAY_SOURCES)
        assert "new_api.h" in first, f"cold traversal already wrong: {first}"
        assert "new_api.h" in second, f"warm traversal took the other branch: {second}"
        assert "old_api.h" not in second, second

    def test_the_two_traversals_agree(self):
        """Whatever the answer is, it must not depend on cache warmth."""
        first, second = self._traverse_twice(_REPLAY_SOURCES)
        assert sorted(first) == sorted(second)

    def test_an_object_like_gate_was_never_affected(self):
        """The no-regression half: object-like carriage already survived replay."""
        first, second = self._traverse_twice(
            {
                "macros.h": _OBJECT_LIKE_MACROS,
                "gate.h": _gate("USE_NEW"),
                "new_api.h": _NEW_API,
                "old_api.h": _OLD_API,
                "main.cpp": '#include "macros.h"\n#include "gate.h"\nint main() { return 0; }\n',
            }
        )
        assert "new_api.h" in first and "new_api.h" in second, (first, second)


class TestMagicFlagsHarvestsFromTheSelectedHeader(tb.BaseCompileToolsTestCase):
    """The scan-set half: the flags must come off the header the graph picked."""

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _cxxflags(self, sources: dict) -> str:
        files = uth.write_sources(sources, target_dir=self._tmpdir)
        parser = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(str(files["main.cpp"]))
        return " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))

    def test_a_function_like_gate_harvests_the_new_api_flag(self):
        cxxflags = self._cxxflags(
            {
                "macros.h": _FUNCTION_LIKE_MACROS,
                "gate.h": _gate("EXTLIB_AT_LEAST(2, 0)"),
                "new_api.h": _NEW_API,
                "old_api.h": _OLD_API,
                "main.cpp": '#include "macros.h"\n#include "gate.h"\nint main() { return 0; }\n',
            }
        )
        assert "-DUSING_NEW_API" in cxxflags, cxxflags
        assert "-DUSING_OLD_API" not in cxxflags, cxxflags

    def test_a_false_function_like_gate_harvests_the_old_api_flag(self):
        """Carrying the arity must yield the real value, not just the true arm."""
        cxxflags = self._cxxflags(
            {
                "macros.h": _FUNCTION_LIKE_MACROS.replace("#define EXTLIB_MAJOR 2", "#define EXTLIB_MAJOR 1"),
                "gate.h": _gate("EXTLIB_AT_LEAST(2, 0)"),
                "new_api.h": _NEW_API,
                "old_api.h": _OLD_API,
                "main.cpp": '#include "macros.h"\n#include "gate.h"\nint main() { return 0; }\n',
            }
        )
        assert "-DUSING_OLD_API" in cxxflags, cxxflags
        assert "-DUSING_NEW_API" not in cxxflags, cxxflags

    def test_an_object_like_gate_harvests_the_new_api_flag(self):
        cxxflags = self._cxxflags(
            {
                "macros.h": _OBJECT_LIKE_MACROS,
                "gate.h": _gate("USE_NEW"),
                "new_api.h": _NEW_API,
                "old_api.h": _OLD_API,
                "main.cpp": '#include "macros.h"\n#include "gate.h"\nint main() { return 0; }\n',
            }
        )
        assert "-DUSING_NEW_API" in cxxflags, cxxflags


class TestGateHeaderIncludingItsOwnDefiningHeader(tb.BaseCompileToolsTestCase):
    """R7: a gate whose defining header is included by the gate itself.

    ``_create_include_list`` evaluates a file's conditionals in one shot before
    walking any of its includes, so at the moment ``gate.h`` tests its ``#if``
    the ``macros.h`` it included on the line above has not contributed anything.
    A single ``headerdeps.process`` therefore reads the gate as false.

    ``magicflags`` recovers because it re-parses until the macro state settles:
    the second pass seeds the state with ``macros.h``'s defines, and the gate
    evaluates for real.  These tests pin both halves of that contract — the
    end-to-end answer is correct, and the reason it is correct is convergence.
    """

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _sources(self, macros: str, condition: str) -> dict:
        return {
            "macros.h": macros,
            "gate.h": _self_including_gate(condition),
            "new_api.h": _NEW_API,
            "old_api.h": _OLD_API,
            "main.cpp": '#include "gate.h"\nint main() { return 0; }\n',
        }

    def _cxxflags(self, sources: dict) -> str:
        files = uth.write_sources(sources, target_dir=self._tmpdir)
        parser = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(str(files["main.cpp"]))
        return " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))

    @pytest.mark.parametrize(
        "macros,condition",
        [
            (_FUNCTION_LIKE_MACROS, "EXTLIB_AT_LEAST(2, 0)"),
            (_OBJECT_LIKE_MACROS, "USE_NEW"),
        ],
        ids=["function-like", "object-like"],
    )
    def test_magicflags_converges_onto_the_right_arm(self, macros, condition):
        cxxflags = self._cxxflags(self._sources(macros, condition))
        assert "-DUSING_NEW_API" in cxxflags, cxxflags
        assert "-DUSING_OLD_API" not in cxxflags, cxxflags

    def test_a_single_headerdeps_pass_is_the_documented_limitation(self):
        """One ``process`` call has no second pass, so it reads the gate false.

        This is the bounded limitation R7 names, pinned so a future interleaving
        fix trips this test rather than passing unnoticed.  ``magicflags`` never
        depends on it — see the converging tests above.
        """
        files = uth.write_sources(
            self._sources(_FUNCTION_LIKE_MACROS, "EXTLIB_AT_LEAST(2, 0)"), target_dir=self._tmpdir
        )
        context = BuildContext()
        headers = [
            os.path.basename(path)
            for path in _direct_headerdeps(context, self._tmpdir).process(str(files["main.cpp"]), frozenset())
        ]
        assert "old_api.h" in headers, headers
        assert "new_api.h" not in headers, headers


# target.h both #undef's and redefines X. The FIRST traversal to reach it
# (via first.cpp) has X absent beforehand, so its cached file_defines/
# file_undefs are recorded correctly. The SECOND traversal (via second.cpp)
# reaches target.h with X already defined to a stale value, warm-hitting
# the invariant include cache built by the first. Whatever the prior value
# of X was, target.h's own #undef/#define pair must leave X == 2 -- the
# gate below must always select new_api.h.
_UNDEF_REDEFINE_SOURCES = {
    "definer.h": "#pragma once\n#define X 1\n",
    "target.h": "#pragma once\n#undef X\n#define X 2\n",
    "gate.h": ('#pragma once\n#if X == 2\n#include "new_api.h"\n#else\n#include "old_api.h"\n#endif\n'),
    "new_api.h": "#pragma once\n//#CXXFLAGS=-DUSING_NEW_API\n",
    "old_api.h": "#pragma once\n//#CXXFLAGS=-DUSING_OLD_API\n",
    "first.cpp": '#include "target.h"\n#include "gate.h"\nint main() { return 0; }\n',
    "second.cpp": '#include "definer.h"\n#include "target.h"\n#include "gate.h"\nint main() { return 0; }\n',
}


class TestUndefRedefineSurvivesTheIncludeCache(tb.BaseCompileToolsTestCase):
    """A cached '#undef X' + '#define X 2' header must reconstruct X=2 on a
    warm hit even when the warm caller's macro state already has X defined
    to something else -- see preprocessing_cache.py's reconstruction order
    fix. DirectHeaderDeps' own include-list cache (``_create_include_list``)
    replays the identical FileEffects and would be susceptible to the same
    ordering bug independently of the preprocessing cache it wraps if it
    bypassed ``FileEffects.apply``.
    """

    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def test_second_traversal_still_lands_on_new_api(self):
        files = uth.write_sources(_UNDEF_REDEFINE_SOURCES, target_dir=self._tmpdir)
        context = BuildContext()

        first = [
            os.path.basename(path)
            for path in _direct_headerdeps(context, self._tmpdir).process(str(files["first.cpp"]), frozenset())
        ]
        assert "new_api.h" in first, f"cold traversal already wrong: {first}"

        second = [
            os.path.basename(path)
            for path in _direct_headerdeps(context, self._tmpdir).process(str(files["second.cpp"]), frozenset())
        ]
        assert "new_api.h" in second, f"warm traversal took the other branch: {second}"
        assert "old_api.h" not in second, second
