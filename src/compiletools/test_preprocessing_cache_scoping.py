"""Tests for MacroState scope_filter / cmdline_origin / structured flag tokens.

These tests cover the cache-key-pollution fix: MacroState.get_hash(include_core=True)
gains an optional scope_filter to restrict which cmdline-derived -D macros are
hashed, and the build-context section can hash structured (tokenized) flag lists
instead of the raw flag strings.

Backward compatibility is critical: a MacroState constructed without any of the
new fields (or with them at their defaults) must produce an identical hash to
today's code, and the variable-only _hash path / get_cache_key() path must remain
untouched.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import stringzilla as sz

from compiletools.preprocessing_cache import MacroState


def _make_state(
    core=None,
    variable=None,
    compiler_path="gcc",
    cppflags="-I/usr/include",
    cflags="-O2",
    cxxflags="-std=c++17",
    cmdline_origin=None,
    cppflags_tokens=None,
    cflags_tokens=None,
    cxxflags_tokens=None,
):
    kwargs = {
        "core": core if core is not None else {},
        "variable": variable if variable is not None else {},
        "compiler_path": compiler_path,
        "cppflags": cppflags,
        "cflags": cflags,
        "cxxflags": cxxflags,
    }
    if cmdline_origin is not None:
        kwargs["cmdline_origin"] = cmdline_origin
    if cppflags_tokens is not None:
        kwargs["cppflags_tokens"] = cppflags_tokens
    if cflags_tokens is not None:
        kwargs["cflags_tokens"] = cflags_tokens
    if cxxflags_tokens is not None:
        kwargs["cxxflags_tokens"] = cxxflags_tokens
    kwargs.setdefault("anchor_root", "")
    return MacroState(**kwargs)


def test_default_construction_unchanged_hash():
    """Two states with identical defaults (no new fields) must hash the same."""
    a = _make_state(core={sz.Str("__GNUC__"): sz.Str("13")})
    b = _make_state(core={sz.Str("__GNUC__"): sz.Str("13")})
    assert a.get_hash(include_core=True) == b.get_hash(include_core=True)


def test_get_hash_unchanged_when_scope_filter_none():
    """Passing scope_filter=None must be identical to omitting it."""
    state = _make_state(core={sz.Str("__GNUC__"): sz.Str("13")})
    h_omit = state.get_hash(include_core=True)
    # Fresh state to avoid the cache returning the same object.
    state2 = _make_state(core={sz.Str("__GNUC__"): sz.Str("13")})
    h_explicit_none = state2.get_hash(include_core=True, scope_filter=None)
    assert h_omit == h_explicit_none


def test_compiler_builtins_always_hashed():
    """Compiler builtins (in core but NOT in cmdline_origin) are always hashed."""
    s_v13 = _make_state(
        core={sz.Str("__GNUC__"): sz.Str("13")},
        cmdline_origin=frozenset(),
    )
    s_v14 = _make_state(
        core={sz.Str("__GNUC__"): sz.Str("14")},
        cmdline_origin=frozenset(),
    )
    s_empty = _make_state(core={}, cmdline_origin=frozenset())

    h_v13 = s_v13.get_hash(include_core=True, scope_filter=frozenset())
    h_v14 = s_v14.get_hash(include_core=True, scope_filter=frozenset())
    h_empty = s_empty.get_hash(include_core=True, scope_filter=frozenset())
    assert h_v13 != h_v14
    assert h_v13 != h_empty


@pytest.mark.parametrize(
    "scope_filter,hashes_match",
    [
        (frozenset({sz.Str("FOO")}), False),  # FOO in filter -> values hashed -> different hashes
        (frozenset(), True),  # FOO excluded by empty filter -> values irrelevant
    ],
    ids=["foo_in_filter", "foo_excluded"],
)
def test_cmdline_macro_scope_filter(scope_filter, hashes_match):
    """cmdline-origin macros are hashed iff their name is in scope_filter."""
    s1 = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    s2 = _make_state(
        core={sz.Str("FOO"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    h1 = s1.get_hash(include_core=True, scope_filter=scope_filter)
    h2 = s2.get_hash(include_core=True, scope_filter=scope_filter)
    assert (h1 == h2) is hashes_match


def test_filter_does_not_affect_compiler_builtins():
    """Filter only excludes cmdline-origin names, not builtins."""
    s_base = _make_state(
        core={sz.Str("__GNUC__"): sz.Str("13"), sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    s_change_builtin = _make_state(
        core={sz.Str("__GNUC__"): sz.Str("14"), sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    s_change_foo = _make_state(
        core={sz.Str("__GNUC__"): sz.Str("13"), sz.Str("FOO"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    flt = frozenset()
    h_base = s_base.get_hash(include_core=True, scope_filter=flt)
    h_change_builtin = s_change_builtin.get_hash(include_core=True, scope_filter=flt)
    h_change_foo = s_change_foo.get_hash(include_core=True, scope_filter=flt)

    assert h_base != h_change_builtin  # builtin matters
    assert h_base == h_change_foo  # FOO is filtered out


def test_filter_does_not_affect_variable_macros():
    """scope_filter never affects variable macros (always hashed)."""
    s1 = _make_state(
        variable={sz.Str("V"): sz.Str("1")},
        core={},
        cmdline_origin=frozenset(),
    )
    s2 = _make_state(
        variable={sz.Str("V"): sz.Str("2")},
        core={},
        cmdline_origin=frozenset(),
    )
    flt = frozenset()
    assert s1.get_hash(include_core=True, scope_filter=flt) != s2.get_hash(include_core=True, scope_filter=flt)


def test_raw_strings_and_pretokenized_hash_identically():
    """Raw flag strings and pre-tokenized lists must produce the same hash.

    Post-a7328d5 invariant: there is no separate raw-string fallback. When
    *_tokens is None, get_hash() lazily tokenizes the raw string through the
    same tokenize_compile_flags + filter_hash_irrelevant_tokens +
    canonicalize_for_cache_key pipeline that pre-tokenized callers go through.
    A caller that forgets to pre-tokenize must land in the same hash regime as
    one that does.
    """
    cmdline = frozenset({sz.Str("FOO")})
    flt = frozenset()

    state_raw = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cppflags="-O2 -DFOO=1 -Wall",
        cflags="-O2 -DFOO=1",
        cxxflags="-O2 -DFOO=1",
        cmdline_origin=cmdline,
    )
    state_tokens = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cppflags="-O2 -DFOO=1 -Wall",
        cflags="-O2 -DFOO=1",
        cxxflags="-O2 -DFOO=1",
        cmdline_origin=cmdline,
        cppflags_tokens=["-O2", "-Wall"],
        cflags_tokens=["-O2"],
        cxxflags_tokens=["-O2"],
    )
    h_raw = state_raw.get_hash(include_core=True, scope_filter=flt)
    h_tokens = state_tokens.get_hash(include_core=True, scope_filter=flt)
    assert h_raw == h_tokens


# Each mutator must produce an *effective* change so MacroState constructs a
# new instance via the forwarding path (with_updates short-circuits to self on
# no-op updates). with_updates adds a fresh key Z; without_keys removes Y.
_MUTATORS = [
    pytest.param(lambda s: s.with_updates({sz.Str("Z"): sz.Str("3")}), id="with_updates"),
    pytest.param(lambda s: s.without_keys([sz.Str("Y")]), id="without_keys"),
]


@pytest.mark.parametrize("mutator", _MUTATORS)
def test_state_mutator_propagates_cmdline_origin(mutator):
    """with_updates() and without_keys() both forward cmdline_origin to the new state."""
    state = _make_state(
        core={sz.Str("X"): sz.Str("1")},
        variable={sz.Str("Y"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("X")}),
    )
    new_state = mutator(state)
    assert new_state is not state, "mutator must construct a new state, not short-circuit"
    assert new_state.cmdline_origin == frozenset({sz.Str("X")})


@pytest.mark.parametrize("mutator", _MUTATORS)
def test_state_mutator_propagates_tokens(mutator):
    """with_updates() and without_keys() both forward all *_tokens fields."""
    state = _make_state(
        variable={sz.Str("Y"): sz.Str("2")},
        cppflags_tokens=["-O2"],
        cflags_tokens=["-Wall"],
        cxxflags_tokens=["-std=c++17"],
    )
    new_state = mutator(state)
    assert new_state is not state, "mutator must construct a new state, not short-circuit"
    assert new_state.cppflags_tokens == ["-O2"]
    assert new_state.cflags_tokens == ["-Wall"]
    assert new_state.cxxflags_tokens == ["-std=c++17"]


def test_filtered_hash_not_cached_via_hash_full():
    """Calling with a scope_filter must not corrupt the cached unfiltered hash."""
    state = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    h_unfiltered = state.get_hash(include_core=True)
    h_filtered = state.get_hash(include_core=True, scope_filter=frozenset())
    assert h_unfiltered != h_filtered
    # Repeat the unfiltered call: must return the originally-cached value.
    h_unfiltered_again = state.get_hash(include_core=True)
    assert h_unfiltered == h_unfiltered_again


def test_existing_get_cache_key_unaffected():
    """get_cache_key() must be unaffected by cmdline_origin / tokens."""
    s_plain = _make_state(variable={sz.Str("V"): sz.Str("1")})
    s_with_extras = _make_state(
        variable={sz.Str("V"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
        cppflags_tokens=["-O2"],
        cflags_tokens=["-Wall"],
        cxxflags_tokens=["-std=c++17"],
    )
    assert s_plain.get_cache_key() == s_with_extras.get_cache_key()


def test_get_or_compute_preprocessing_propagates_cmdline_origin_and_tokens():
    """Regression: the updated_macros returned from preprocessing must keep
    the cmdline_origin and *_tokens fields. Without this, every file walked
    in a build silently loses the per-TU scoping data that Task D wires up.

    Builds a minimal FileAnalysisResult + BuildContext stub so the cache miss
    path runs end-to-end and we can read updated_macros off the result.
    """
    from compiletools.file_analyzer import FileAnalysisResult
    from compiletools.preprocessing_cache import get_or_compute_preprocessing

    # Minimal FileAnalysisResult: no conditionals (permanently invariant), no
    # includes/defines/magic flags. The miss path still runs SimplePreprocessor
    # to construct updated_macros from the (untouched) input macros.
    file_result = FileAnalysisResult(
        line_count=0,
        line_byte_offsets=[0],
        include_positions=[],
        magic_positions=[],
        directive_positions={},
        directives=[],
        directive_by_line={},
        bytes_analyzed=0,
        was_truncated=False,
        content_hash="test_hash_propagate_origin_tokens",
        conditional_macros=frozenset(),
    )

    class _StubContext:
        def __init__(self):
            self.invariant_preprocessing_cache = {}
            self.variant_preprocessing_cache = {}
            self.preprocessing_stats = {
                "total_calls": 0,
                "hits": 0,
                "misses": 0,
                "invariant_hits": 0,
                "invariant_misses": 0,
                "variant_hits": 0,
                "variant_misses": 0,
            }
            # process_structured calls get_filepath_by_hash(content_hash, context)
            self.reverse_hashes = {"test_hash_propagate_origin_tokens": ["<stub>"]}

    ctx = _StubContext()

    cmdline = frozenset({sz.Str("FOO")})
    cpp_tokens = ["-O2", "-Wall"]
    c_tokens = ["-std=c11"]
    cxx_tokens = ["-std=c++17"]

    input_macros = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        variable={},
        cmdline_origin=cmdline,
        cppflags_tokens=cpp_tokens,
        cflags_tokens=c_tokens,
        cxxflags_tokens=cxx_tokens,
    )

    result = get_or_compute_preprocessing(file_result, input_macros, verbose=0, context=ctx)

    assert result.updated_macros.cmdline_origin == cmdline
    assert result.updated_macros.cppflags_tokens == cpp_tokens
    assert result.updated_macros.cflags_tokens == c_tokens
    assert result.updated_macros.cxxflags_tokens == cxx_tokens


def test_filter_only_includes_names_in_intersection():
    """Filter applies to (core ∩ cmdline_origin) ∩ filter; names outside core are irrelevant."""
    s_base = _make_state(
        core={sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("A"), sz.Str("B")}),
    )
    s_change_a = _make_state(
        core={sz.Str("A"): sz.Str("9"), sz.Str("B"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("A"), sz.Str("B")}),
    )
    s_change_b = _make_state(
        core={sz.Str("A"): sz.Str("1"), sz.Str("B"): sz.Str("9")},
        cmdline_origin=frozenset({sz.Str("A"), sz.Str("B")}),
    )
    flt = frozenset({sz.Str("A"), sz.Str("Z")})  # Z is irrelevant

    h_base = s_base.get_hash(include_core=True, scope_filter=flt)
    h_change_a = s_change_a.get_hash(include_core=True, scope_filter=flt)
    h_change_b = s_change_b.get_hash(include_core=True, scope_filter=flt)

    assert h_base != h_change_a  # A is in filter -> hashed
    assert h_base == h_change_b  # B is not in filter -> excluded


# --- TOKEN-3: diagnostic-only flag tokens are excluded from build-context hash ---


def test_macro_state_hash_unchanged_with_w_warning_change():
    """Flipping ``-Wall`` <-> ``-Wextra`` in cxxflags_tokens must NOT
    change the MacroState build-context hash (warnings are diagnostic
    only and don't affect the compiled object bytes)."""
    s_wall = _make_state(cxxflags_tokens=["-O2", "-Wall"])
    s_wextra = _make_state(cxxflags_tokens=["-O2", "-Wextra"])
    assert s_wall.get_hash(include_core=True) == s_wextra.get_hash(include_core=True)


def test_macro_state_hash_changes_with_werror_change():
    """``-Werror`` is the documented exception: it can change build
    outcome (warning vs error) and so it must remain hash-relevant."""
    s_with = _make_state(cxxflags_tokens=["-O2", "-Werror"])
    s_without = _make_state(cxxflags_tokens=["-O2"])
    assert s_with.get_hash(include_core=True) != s_without.get_hash(include_core=True)


def test_macro_state_hash_unchanged_with_pipe_added():
    """``-pipe`` is purely a driver-side I/O strategy and never affects
    the compiled bytes; adding it must not change the hash."""
    s_no_pipe = _make_state(cxxflags_tokens=["-O2"])
    s_pipe = _make_state(cxxflags_tokens=["-O2", "-pipe"])
    assert s_no_pipe.get_hash(include_core=True) == s_pipe.get_hash(include_core=True)
