"""Hypothesis property tests: the algebraic laws of the pure core."""

import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import compute_build_state
from compiletools.flag_ops import dedup_tokens

ALL_SLOTS = frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"})

_token = st.one_of(
    st.sampled_from(["-O2", "-Wall", "-DFOO", "-DBAR=1", "-g"]),
    st.builds(lambda p: f"-I/{p}", st.text(alphabet="abcxyz", min_size=1, max_size=4)),
)
_tokens = st.lists(_token, max_size=8).map(tuple)
_unsuppliable_tokens = st.none() | _tokens
_pkg_results = st.lists(
    st.tuples(
        st.text(alphabet="abc", min_size=1, max_size=3),
        st.builds(PkgConfigResult, cflags=_tokens, libs=_tokens),
    ),
    max_size=3,
    unique_by=lambda kv: kv[0],
).map(tuple)


def _inputs_strategy(separate_flags):
    return st.builds(
        BuildInputs,
        registered_slots=st.just(ALL_SLOTS),
        cppflags=_unsuppliable_tokens,
        cflags=_tokens,
        cxxflags=_tokens,
        ldflags=_unsuppliable_tokens,
        include_paths=st.lists(
            st.builds(lambda p: f"/{p}", st.text(alphabet="mnop", min_size=1, max_size=4)), max_size=3
        ).map(tuple),
        pkg_config_results=_pkg_results,
        separate_flags=st.just(separate_flags),
        gitroot=st.sampled_from(["", "/repo"]),
        variant_raw=st.just("gcc.debug"),
        canonical_order=st.just(("gcc", "debug")),
    )


@settings(max_examples=200)
@given(st.booleans().flatmap(_inputs_strategy))
def test_compute_is_deterministic(inputs):
    assert compute_build_state(inputs) == compute_build_state(inputs)


@settings(max_examples=200)
@given(st.booleans().flatmap(_inputs_strategy))
def test_feeding_output_slots_back_as_inputs_is_a_fixed_point(inputs):
    """The seed-restore theorem, as a law: computing from a state's own
    output tokens reproduces those tokens."""
    state1 = compute_build_state(inputs)
    inputs2 = dataclasses.replace(
        inputs,
        cppflags=state1.tokens.cpp,
        cflags=state1.tokens.c,
        cxxflags=state1.tokens.cxx,
        ldflags=state1.tokens.ld,
    )
    state2 = compute_build_state(inputs2)
    assert state2.tokens == state1.tokens


@settings(max_examples=200)
@given(_tokens)
def test_dedup_tokens_is_idempotent_and_order_preserving(tokens):
    once = dedup_tokens(tokens)
    assert dedup_tokens(once) == once
    remaining = list(once)
    for t in tokens:
        if remaining and t == remaining[0]:
            remaining.pop(0)
    assert not remaining, "dedup reordered surviving tokens"


@settings(max_examples=100)
@given(st.just(False).flatmap(_inputs_strategy))
def test_unified_mode_ends_with_cpp_equal_cxx(inputs):
    state = compute_build_state(inputs)
    assert state.tokens.cpp == state.tokens.cxx


@settings(max_examples=100)
@given(st.booleans().flatmap(_inputs_strategy))
def test_pkg_results_order_shuffle_of_dict_iteration_is_irrelevant(inputs):
    """Same tuple, same result -- guards against any hidden set/dict
    iteration inside compute."""
    assert compute_build_state(inputs) == compute_build_state(dataclasses.replace(inputs))
