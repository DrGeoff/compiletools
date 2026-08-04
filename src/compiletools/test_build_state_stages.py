"""Table-driven unit tests for the pure build-state stages.

Every test constructs BuildInputs literals -- no tempdirs, parsers,
conf files, or compilers.
"""

from compiletools.build_inputs import BuildInputs
from compiletools.build_state import TokenState, stage_defaults, stage_xxpend

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
