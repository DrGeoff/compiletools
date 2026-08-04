"""Table-driven unit tests for the pure build-state stages.

Every test constructs BuildInputs literals -- no tempdirs, parsers,
conf files, or compilers.
"""

from compiletools.build_inputs import BuildInputs
from compiletools.build_state import TokenState, stage_defaults, stage_include_paths, stage_xxpend

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
