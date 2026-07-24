"""Property-based tests for the pure flag-token helpers.

Covers:
  * utils.deduplicate_compiler_flags
  * utils.ordered_unique / ordered_union / ordered_difference
  * flag_ops.strip_d_u_tokens
  * flag_ops.filter_hash_irrelevant_tokens (the -Werror carve-out)
  * flags.Flags.hash_relevant (composition of the two above)

No source is modified: these are black-box property tests.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

import compiletools.utils as utils
from compiletools.flag_ops import filter_hash_irrelevant_tokens, strip_d_u_tokens
from compiletools.flags import Flags


def _is_subsequence(sub, full):
    """True if *sub* appears in *full* in order (not necessarily contiguous)."""
    it = iter(full)
    return all(item in it for item in sub)


# --- deduplicate_compiler_flags ---------------------------------------------

# Combined-form flags only, so semantic dedup coincides with string dedup and
# ``set(output) == set(input)`` is a clean invariant. (Mixing ``-I p`` with
# ``-Ip`` would collapse them semantically -- covered by the explicit test.)
_DEDUP_TOKENS = st.sampled_from(
    ["-Ipath", "-Iother", "-DFOO", "-DBAR", "-lm", "-O2", "-g", "-pthread", "-Wall", "-std=c++20"]
)
dedup_list = st.lists(_DEDUP_TOKENS, max_size=12)


@settings(deadline=None)
@given(flags=dedup_list)
def test_dedup_idempotent(flags):
    once = utils.deduplicate_compiler_flags(flags)
    twice = utils.deduplicate_compiler_flags(once)
    assert twice == once


@settings(deadline=None)
@given(flags=dedup_list)
def test_dedup_is_order_preserving_subsequence(flags):
    out = utils.deduplicate_compiler_flags(flags)
    assert _is_subsequence(out, flags)
    assert set(out) <= set(flags)  # no new tokens invented


@settings(deadline=None)
@given(flags=dedup_list)
def test_dedup_combined_form_preserves_set_and_removes_dups(flags):
    out = utils.deduplicate_compiler_flags(flags)
    assert set(out) == set(flags)
    assert len(out) == len(set(out)), f"duplicate survived: {out}"


def test_dedup_mixed_forms_collapse_semantically():
    """Separate and combined forms of the same -I path collapse to one."""
    out = utils.deduplicate_compiler_flags(["-I", "path", "-Ipath"])
    assert out == ["-I", "path"]


# --- ordered_unique / ordered_union / ordered_difference --------------------

_ANY = st.sampled_from(["a", "b", "c", "d", "e", "f"])
any_list = st.lists(_ANY, max_size=12)


@settings(deadline=None)
@given(items=any_list)
def test_ordered_unique_properties(items):
    out = utils.ordered_unique(items)
    assert set(out) == set(items)
    assert len(out) == len(set(out))
    assert _is_subsequence(out, items)
    assert utils.ordered_unique(out) == out  # idempotent


@settings(deadline=None)
@given(a=any_list, b=any_list)
def test_ordered_union_properties(a, b):
    out = utils.ordered_union(a, b)
    assert set(out) == set(a) | set(b)
    assert len(out) == len(set(out))
    # First-seen order across the concatenation is preserved.
    assert out == utils.ordered_unique(list(a) + list(b))


@settings(deadline=None)
@given(a=any_list, b=any_list)
def test_ordered_difference_properties(a, b):
    out = utils.ordered_difference(a, b)
    assert set(out) == set(a) - set(b)
    assert _is_subsequence(out, a)
    assert len(out) == len(set(out))


# --- strip_d_u_tokens -------------------------------------------------------

_DU_TOKENS = st.sampled_from(["-DFOO", "-DBAR=1", "-UBAZ", "-D", "-U", "FOO", "BAR", "-I/x", "-O2", "-g", "-std=c++20"])
du_list = st.lists(_DU_TOKENS, max_size=12)


@settings(deadline=None)
@given(toks=du_list)
def test_strip_d_u_idempotent(toks):
    once = strip_d_u_tokens(toks)
    twice = strip_d_u_tokens(once)
    assert twice == once


@settings(deadline=None)
@given(toks=du_list)
def test_strip_d_u_removes_all_define_undef(toks):
    out = strip_d_u_tokens(toks)
    # Attached forms and any surviving bare -D/-U are gone.
    assert not any(t.startswith("-D") or t.startswith("-U") for t in out)
    # No token is invented.
    assert _is_subsequence(out, toks)


def test_strip_d_u_all_forms_explicit():
    """Attached, detached, and dangling-trailing forms all vanish."""
    assert strip_d_u_tokens(["-DFOO", "-UBAR"]) == []
    assert strip_d_u_tokens(["-D", "FOO", "-U", "BAR"]) == []
    assert strip_d_u_tokens(["-c", "-D", "FOO", "-O2"]) == ["-c", "-O2"]
    assert strip_d_u_tokens(["-O2", "-D"]) == ["-O2"]  # dangling trailing -D
    assert strip_d_u_tokens(["-O2", "-U"]) == ["-O2"]  # dangling trailing -U


# --- filter_hash_irrelevant_tokens (the -Werror carve-out) ------------------

_WERROR = ["-Werror", "-Werror=return-type", "-Werror=all"]
_OTHER_W = ["-Wall", "-Wextra", "-Wno-unused", "-Wshadow"]
_DIAG = ["-pipe", "-v", "--verbose", "-fdiagnostics-color", "-fmessage-length=0"]
_NORMAL = ["-O2", "-std=c++20", "-I/x", "-g", "-fPIC", "-c"]

_HASH_TOKENS = st.sampled_from(_WERROR + _OTHER_W + _DIAG + _NORMAL)
hash_list = st.lists(_HASH_TOKENS, max_size=14)


@settings(deadline=None)
@given(toks=hash_list)
def test_filter_idempotent(toks):
    once = filter_hash_irrelevant_tokens(toks)
    twice = filter_hash_irrelevant_tokens(once)
    assert twice == once


@settings(deadline=None)
@given(toks=hash_list)
def test_filter_werror_retained_other_w_stripped(toks):
    out = filter_hash_irrelevant_tokens(toks)
    assert _is_subsequence(out, toks)
    # Every surviving -W token is a -Werror form.
    for t in out:
        if t.startswith("-W"):
            assert t == "-Werror" or t.startswith("-Werror="), f"non-Werror -W survived: {t}"
    # Every -Werror form in the input survives (count-preserving, no dedup).
    for we in _WERROR:
        assert toks.count(we) == out.count(we)
    # No purely-diagnostic flag survives.
    for d in _DIAG:
        assert d not in out
    # Every normal (hash-relevant) flag is preserved with its count.
    for n in _NORMAL:
        assert toks.count(n) == out.count(n)


# --- Flags.hash_relevant (strip_d_u + filter composition) -------------------


@settings(deadline=None)
@given(toks=st.lists(st.sampled_from(_WERROR + _OTHER_W + _DIAG + _NORMAL + ["-DFOO", "-UBAR"]), max_size=14))
def test_hash_relevant_strips_defines_and_non_werror_w(toks):
    flags = Flags(cxx=tuple(toks))
    out = flags.hash_relevant("cxx")
    assert not any(t.startswith("-D") or t.startswith("-U") for t in out)
    for t in out:
        if t.startswith("-W"):
            assert t == "-Werror" or t.startswith("-Werror=")
    # Idempotent-in-spirit: re-filtering the result is a no-op.
    assert filter_hash_irrelevant_tokens(strip_d_u_tokens(out)) == out
