"""Hypothesis property tests: the algebraic laws of the pure core."""

import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import compute_build_state, stage_resolve_names
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
        cppflags=state1.flags.cpp,
        cflags=state1.flags.c,
        cxxflags=state1.flags.cxx,
        ldflags=state1.flags.ld,
    )
    state2 = compute_build_state(inputs2)
    assert state2.flags == state1.flags


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
    assert state.flags.cpp == state.flags.cxx


@settings(max_examples=100)
@given(st.booleans().flatmap(_inputs_strategy))
def test_equal_inputs_from_distinct_objects_are_equal(inputs):
    """Two structurally-equal BuildInputs built from independent containers
    (fresh tuples/frozensets, no shared identity with the originals) compute
    equal states -- pins "no identity-dependent behavior" in compute.
    pkg_config_results ORDER is deliberately preserved: declaration order is
    contractual (Task 6), so no permutation-insensitivity is claimed here."""
    rebuilt = BuildInputs(
        registered_slots=frozenset(list(inputs.registered_slots)),
        cppflags=None if inputs.cppflags is None else tuple(list(inputs.cppflags)),
        cflags=tuple(list(inputs.cflags)),
        cxxflags=tuple(list(inputs.cxxflags)),
        ldflags=None if inputs.ldflags is None else tuple(list(inputs.ldflags)),
        include_paths=tuple(list(inputs.include_paths)),
        pkg_config_results=tuple(
            (str(pkg), PkgConfigResult(cflags=tuple(list(r.cflags)), libs=tuple(list(r.libs))))
            for pkg, r in inputs.pkg_config_results
        ),
        separate_flags=inputs.separate_flags,
        gitroot=str(inputs.gitroot),
        variant_raw=str(inputs.variant_raw),
        canonical_order=tuple(list(inputs.canonical_order)),
    )
    assert rebuilt is not inputs
    assert rebuilt == inputs
    assert compute_build_state(rebuilt) == compute_build_state(inputs)


_variant_alphabet = ["gcc", "clang", "debug", "release", "asan", "extras"]


@settings(max_examples=200)
@given(
    tokens=st.lists(st.sampled_from(_variant_alphabet), min_size=1, max_size=6),
    canonical_order=st.permutations(_variant_alphabet).map(lambda p: tuple(p[:4])),
)
def test_stage_resolve_names_is_a_fixed_point(tokens, canonical_order):
    """Canonicalization is a fixed point: feeding stage_resolve_names its
    own resolved names back as raw inputs reproduces them exactly (the
    NON_CANONICAL trim-cache class cannot be minted here)."""
    first = stage_resolve_names(
        BuildInputs(
            registered_slots=ALL_SLOTS,
            variant_raw=".".join(tokens),
            canonical_order=canonical_order,
            gitroot="/repo",
        )
    )
    second = stage_resolve_names(
        BuildInputs(
            registered_slots=ALL_SLOTS,
            variant_raw=first.variant,
            canonical_order=canonical_order,
            gitroot="/repo",
            bindir_raw=first.bindir,
            cas_objdir_raw=first.cas_objdir,
            cas_pchdir_raw=first.cas_pchdir,
            cas_pcmdir_raw=first.cas_pcmdir,
            cas_exedir_raw=first.cas_exedir,
        )
    )
    assert second == first
