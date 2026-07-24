"""Property-based tests for configutils.canonicalize_variant_tokens.

Black-box Hypothesis tests for the variant-token canonicaliser. No source
is modified.

Invariants under test (from the function docstring):
  * idempotence -- the fixed point trim_cache.enumerate_cells relies on
  * dedup, first-occurrence-wins
  * any permutation of known tokens yields the same output
  * known tokens precede unknown tokens
  * unknown tokens preserve their first-appearance order
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from compiletools.configutils import canonicalize_variant_tokens

CANONICAL_ORDER = ("gcc", "clang", "debug", "release", "asan")
KNOWN = list(CANONICAL_ORDER)
UNKNOWN = ["foo", "bar", "baz", "qux"]
_ORDER_POS = {name: i for i, name in enumerate(CANONICAL_ORDER)}

any_token = st.sampled_from(KNOWN + UNKNOWN)
token_list = st.lists(any_token, max_size=10)


def _canon(toks):
    return canonicalize_variant_tokens(toks, CANONICAL_ORDER)


@settings(deadline=None)
@given(toks=token_list)
def test_idempotent(toks):
    once = _canon(toks)
    twice = _canon(once)
    assert twice == once


@settings(deadline=None)
@given(toks=token_list)
def test_dedup_preserves_value_set(toks):
    out = _canon(toks)
    assert set(out) == set(toks)
    assert len(out) == len(set(out)), f"duplicate token in output: {out}"


@settings(deadline=None)
@given(toks=token_list)
def test_known_precede_unknown(toks):
    out = _canon(toks)
    last_known = max((i for i, t in enumerate(out) if t in _ORDER_POS), default=-1)
    first_unknown = min((i for i, t in enumerate(out) if t not in _ORDER_POS), default=len(out))
    assert last_known < first_unknown, f"known token after unknown: {out}"


@settings(deadline=None)
@given(toks=token_list)
def test_known_sorted_by_canonical_order(toks):
    out = _canon(toks)
    known_out = [t for t in out if t in _ORDER_POS]
    assert known_out == sorted(known_out, key=lambda t: _ORDER_POS[t])


@settings(deadline=None)
@given(toks=token_list)
def test_unknown_preserve_first_appearance_order(toks):
    out = _canon(toks)
    unknown_out = [t for t in out if t not in _ORDER_POS]
    expected = list(dict.fromkeys(t for t in toks if t not in _ORDER_POS))
    assert unknown_out == expected


@settings(deadline=None)
@given(
    toks=st.lists(st.sampled_from(KNOWN), min_size=1, max_size=5, unique=True),
    perm_seed=st.randoms(use_true_random=False),
)
def test_permutation_of_known_tokens_invariant(toks, perm_seed):
    shuffled = list(toks)
    perm_seed.shuffle(shuffled)
    assert _canon(toks) == _canon(shuffled)
