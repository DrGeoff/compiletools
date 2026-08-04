"""Table-driven unit tests for the pure build-state stages.

Every test constructs BuildInputs literals -- no tempdirs, parsers,
conf files, or compilers.
"""

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import (
    TokenState,
    stage_defaults,
    stage_include_paths,
    stage_pkg_config_flags,
    stage_project_macros,
    stage_xxpend,
)

ALL_SLOTS = frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"})


def _inputs(**kw):
    kw.setdefault("registered_slots", ALL_SLOTS)
    return BuildInputs(**kw)


class TestStageDefaults:
    def test_slots_copied_from_inputs(self):
        ts = stage_defaults(_inputs(cppflags=("-DX",), cxxflags=("-O2",)))
        assert ts.cpp == ("-DX",)
        assert ts.cxx == ("-O2",)

    def test_empty_cppflags_falls_back_to_cxxflags(self):
        ts = stage_defaults(_inputs(cxxflags=("-O2", "-Wall")))
        assert ts.cpp == ("-O2", "-Wall")

    def test_empty_ldflags_falls_back_to_cxxflags_when_registered(self):
        ts = stage_defaults(_inputs(cxxflags=("-O2",)))
        assert ts.ld == ("-O2",)

    def test_unregistered_ldflags_stays_empty(self):
        ts = stage_defaults(_inputs(registered_slots=frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"}), cxxflags=("-O2",)))
        assert ts.ld == ()


class TestStageXxpend:
    def test_prepend_goes_leftmost_append_rightmost(self):
        inputs = _inputs(prepend_cxxflags=("-P",), append_cxxflags=("-A",))
        ts = stage_xxpend(inputs, TokenState(cxx=("-O2",)))
        assert ts.cxx == ("-P", "-O2", "-A")

    def test_already_present_token_not_duplicated(self):
        inputs = _inputs(prepend_cxxflags=("-O2",))
        ts = stage_xxpend(inputs, TokenState(cxx=("-O2",)))
        assert ts.cxx == ("-O2",)

    def test_other_slots_untouched(self):
        inputs = _inputs(prepend_cxxflags=("-P",))
        ts = stage_xxpend(inputs, TokenState(cpp=("-DX",), cxx=()))
        assert ts.cpp == ("-DX",)


class TestStageIncludePaths:
    def test_appends_detached_pairs_to_compile_slots_only(self):
        inputs = _inputs(include_paths=("/inc",))
        ts = stage_include_paths(inputs, TokenState(cpp=("-DX",), ld=("-lm",)))
        assert ts.cpp == ("-DX", "-I", "/inc")
        assert ts.c == ("-I", "/inc")
        assert ts.cxx == ("-I", "/inc")
        assert ts.ld == ("-lm",)

    def test_already_present_path_skipped_per_slot(self):
        inputs = _inputs(include_paths=("/inc",))
        ts = stage_include_paths(inputs, TokenState(cpp=("-I/inc",)))
        assert ts.cpp == ("-I/inc",)
        assert ts.c == ("-I", "/inc")

    def test_no_include_paths_is_identity(self):
        ts = TokenState(cpp=("-DX",))
        assert stage_include_paths(_inputs(), ts) is ts


class TestStageProjectMacros:
    def test_version_and_name_appended_as_single_tokens(self):
        inputs = _inputs(project_version="1.2.3", project_name="myproj")
        ts = stage_project_macros(inputs, TokenState())
        assert '-DCT_PROJECT_VERSION="1.2.3"' in ts.cpp
        assert '-DCT_PROJECT_NAME="myproj"' in ts.cxx
        assert ts.ld == ()

    def test_none_means_no_macro(self):
        ts = stage_project_macros(_inputs(), TokenState(cpp=("-DX",)))
        assert ts.cpp == ("-DX",)

    def test_existing_macro_not_doubled(self):
        tok = '-DCT_PROJECT_VERSION="1.2.3"'
        inputs = _inputs(project_version="1.2.3")
        ts = stage_project_macros(inputs, TokenState(cpp=(tok,), c=(tok,), cxx=(tok,)))
        assert ts.cpp.count(tok) == 1


def _zlib_inputs(**kw):
    return _inputs(
        pkg_config_results=(("zlib", PkgConfigResult(cflags=("-I/z",), libs=("-lz",))),),
        **kw,
    )


class TestStagePkgConfigFlags:
    def test_cflags_to_compile_slots_libs_to_ld(self):
        ts = stage_pkg_config_flags(_zlib_inputs(), TokenState())
        assert ts.cpp == ("-I/z",) and ts.c == ("-I/z",) and ts.cxx == ("-I/z",)
        assert ts.ld == ("-lz",)

    def test_unregistered_ldflags_never_receives_libs(self):
        inputs = _inputs(
            registered_slots=frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"}),
            pkg_config_results=(("zlib", PkgConfigResult(cflags=("-I/z",), libs=("-lz",))),),
        )
        ts = stage_pkg_config_flags(inputs, TokenState())
        assert ts.ld == ()

    def test_package_order_preserved(self):
        inputs = _inputs(
            pkg_config_results=(
                ("a", PkgConfigResult(cflags=(), libs=("-la",))),
                ("b", PkgConfigResult(cflags=(), libs=("-lb",))),
            )
        )
        ts = stage_pkg_config_flags(inputs, TokenState())
        assert ts.ld == ("-la", "-lb")
