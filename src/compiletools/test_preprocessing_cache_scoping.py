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


def test_cmdline_macro_in_filter_is_hashed():
    """A cmdline-origin macro whose name IS in the scope filter is hashed."""
    s1 = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    s2 = _make_state(
        core={sz.Str("FOO"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    flt = frozenset({sz.Str("FOO")})
    assert s1.get_hash(include_core=True, scope_filter=flt) != s2.get_hash(include_core=True, scope_filter=flt)


def test_cmdline_macro_not_in_filter_is_excluded():
    """A cmdline-origin macro whose name is NOT in the filter is excluded."""
    s1 = _make_state(
        core={sz.Str("FOO"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    s2 = _make_state(
        core={sz.Str("FOO"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("FOO")}),
    )
    flt = frozenset()
    assert s1.get_hash(include_core=True, scope_filter=flt) == s2.get_hash(include_core=True, scope_filter=flt)


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


def test_structured_flag_tokens_replace_raw_string_hashing():
    """When tokens are provided, hashing uses them instead of the raw strings."""
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
    assert h_raw != h_tokens


def test_with_updates_propagates_cmdline_origin():
    """with_updates() must forward cmdline_origin to the new state."""
    state = _make_state(
        core={sz.Str("X"): sz.Str("1")},
        cmdline_origin=frozenset({sz.Str("X")}),
    )
    new_state = state.with_updates({sz.Str("Y"): sz.Str("2")})
    assert new_state.cmdline_origin == frozenset({sz.Str("X")})


def test_with_updates_propagates_tokens():
    """with_updates() must forward all *_tokens fields."""
    state = _make_state(
        cppflags_tokens=["-O2"],
        cflags_tokens=["-Wall"],
        cxxflags_tokens=["-std=c++17"],
    )
    new_state = state.with_updates({sz.Str("Y"): sz.Str("2")})
    assert new_state.cppflags_tokens == ["-O2"]
    assert new_state.cflags_tokens == ["-Wall"]
    assert new_state.cxxflags_tokens == ["-std=c++17"]


def test_without_keys_propagates_cmdline_origin():
    """without_keys() must forward cmdline_origin to the new state."""
    state = _make_state(
        core={sz.Str("X"): sz.Str("1")},
        variable={sz.Str("Y"): sz.Str("2")},
        cmdline_origin=frozenset({sz.Str("X")}),
    )
    new_state = state.without_keys([sz.Str("Y")])
    assert new_state.cmdline_origin == frozenset({sz.Str("X")})


def test_without_keys_propagates_tokens():
    """without_keys() must forward all *_tokens fields."""
    state = _make_state(
        variable={sz.Str("Y"): sz.Str("2")},
        cppflags_tokens=["-O2"],
        cflags_tokens=["-Wall"],
        cxxflags_tokens=["-std=c++17"],
    )
    new_state = state.without_keys([sz.Str("Y")])
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
