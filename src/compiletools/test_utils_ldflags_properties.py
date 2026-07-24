"""Property-based tests for utils.merge_ldflags_with_topo_sort.

Black-box Hypothesis tests encoding the invariants stated in the
docstring of ``merge_ldflags_with_topo_sort`` (utils.py). No source
module is modified: these exercise the public function only.

Invariants under test:
  * every input ``-lX`` appears exactly once in the output (deduped, none lost)
  * the set of output libs is invariant under permutation of the input files
  * surviving hard edges form a valid topological order in the output
  * non-``-l`` flags are preserved first-seen and placed before every ``-l``
  * a purely-hard 2-cycle raises the cycle error; the same all-soft cycle never does
  * determinism: same input -> same output across repeated calls
"""

from hypothesis import given, settings
from hypothesis import strategies as st

import compiletools.utils as utils

# Small bounded alphabet keeps the search space tiny and the tests fast
# under ``pytest -n auto``.
LIB_NAMES = ["a", "b", "c", "d", "e"]
NON_L_FLAGS = ["-L/usr/lib", "-pthread", "-L/opt/lib", "-Wl,-z,now", "-static"]

lib_name = st.sampled_from(LIB_NAMES)
l_token = lib_name.map(lambda n: "-l" + n)
non_l_token = st.sampled_from(NON_L_FLAGS)
any_token = st.one_of(l_token, non_l_token)

# A "file" is a short list of tokens; the whole input is a short list of files.
file_tokens = st.lists(any_token, max_size=6)
per_file = st.lists(file_tokens, max_size=5)


def _output_lib_names(result):
    """Extract lib names from a merged-LDFLAGS result (all combined form)."""
    return [tok[2:] for tok in result if tok.startswith("-l") and len(tok) > 2]


def _input_lib_names(per_file_ldflags):
    """Extract the set of lib names present across all input files.

    Mirrors the combined/separate parsing in utils._ldflags_partition.
    """
    names = set()
    for file_flags in per_file_ldflags:
        toks = [str(f) for f in file_flags]
        i = 0
        while i < len(toks):
            flag = toks[i]
            if flag == "-l" and i + 1 < len(toks):
                names.add(toks[i + 1])
                i += 2
            elif flag.startswith("-l") and len(flag) > 2:
                names.add(flag[2:])
                i += 1
            else:
                i += 1
    return names


def _input_non_l_first_seen(per_file_ldflags):
    """Ordered-unique of non-``-l`` flags in input file/token order."""
    seen = []
    seen_set = set()
    for file_flags in per_file_ldflags:
        toks = [str(f) for f in file_flags]
        i = 0
        while i < len(toks):
            flag = toks[i]
            if flag == "-l" and i + 1 < len(toks):
                i += 2
            elif flag.startswith("-l") and len(flag) > 2:
                i += 1
            else:
                if flag not in seen_set:
                    seen_set.add(flag)
                    seen.append(flag)
                i += 1
    return seen


@settings(deadline=None)
@given(per_file=per_file)
def test_every_input_lib_appears_exactly_once(per_file):
    result = utils.merge_ldflags_with_topo_sort(per_file)
    out_names = _output_lib_names(result)
    # No lib lost, none duplicated.
    assert set(out_names) == _input_lib_names(per_file)
    assert len(out_names) == len(set(out_names)), f"duplicate -l in output: {result}"


@settings(deadline=None)
@given(per_file=per_file, perm_seed=st.randoms(use_true_random=False))
def test_output_lib_set_invariant_under_file_permutation(per_file, perm_seed):
    shuffled = list(per_file)
    perm_seed.shuffle(shuffled)
    r1 = utils.merge_ldflags_with_topo_sort(per_file)
    r2 = utils.merge_ldflags_with_topo_sort(shuffled)
    assert set(_output_lib_names(r1)) == set(_output_lib_names(r2))


@settings(deadline=None)
@given(per_file=per_file)
def test_non_l_flags_preserved_first_seen_and_before_libs(per_file):
    result = utils.merge_ldflags_with_topo_sort(per_file)
    out_non_l = [tok for tok in result if not (tok.startswith("-l") and len(tok) > 2)]
    # First-seen order preserved and deduplicated.
    assert out_non_l == _input_non_l_first_seen(per_file)
    # Every non-l flag precedes every -l flag.
    first_lib_idx = next(
        (i for i, tok in enumerate(result) if tok.startswith("-l") and len(tok) > 2),
        len(result),
    )
    for i, tok in enumerate(result):
        if not (tok.startswith("-l") and len(tok) > 2):
            assert i < first_lib_idx, f"non-l flag after a -l flag: {result}"


@settings(deadline=None)
@given(per_file=per_file)
def test_determinism(per_file):
    r1 = utils.merge_ldflags_with_topo_sort(per_file)
    r2 = utils.merge_ldflags_with_topo_sort(per_file)
    assert r1 == r2


# --- Hard-edge topological ordering -----------------------------------------

# Canonical lib ordering; forward-only hard edges guarantee a DAG (no
# purely-hard cycle can exist), so no LDFLAGSCycleError is expected.
_ORDERED = ["a", "b", "c", "d", "e"]
_FORWARD_PAIRS = [(_ORDERED[i], _ORDERED[j]) for i in range(len(_ORDERED)) for j in range(i + 1, len(_ORDERED))]

forward_hard_orderings = st.lists(st.sampled_from(_FORWARD_PAIRS), max_size=6, unique=True)

# hard_orderings without any per_file_ldflags is a documented hard error
# (the two are produced together by magicflags._handle_pkg_config), so the
# outer list must be non-empty when hard edges are supplied.
per_file_nonempty = st.lists(file_tokens, min_size=1, max_size=5)


@settings(deadline=None)
@given(per_file=per_file_nonempty, hard=forward_hard_orderings)
def test_surviving_hard_edges_are_topologically_ordered(per_file, hard):
    result = utils.merge_ldflags_with_topo_sort(per_file, hard_orderings=hard)
    out_names = _output_lib_names(result)
    pos = {name: i for i, name in enumerate(out_names)}
    for pred, succ in hard:
        # Both endpoints always survive (nothing is dropped), and a DAG of
        # hard edges is never cancelled or cycle-broken, so ordering holds.
        assert pos[pred] < pos[succ], f"hard edge {pred}->{succ} violated in {result}"


@settings(deadline=None)
@given(x=lib_name, y=lib_name)
def test_pure_hard_two_cycle_raises_but_all_soft_does_not(x, y):
    if x == y:
        return  # need two distinct libs to form a 2-cycle
    per_file = [["-l" + x, "-l" + y], ["-l" + y, "-l" + x]]

    # All-soft: the same opposed ordering is just an ambiguous hint -> no raise.
    soft_result = utils.merge_ldflags_with_topo_sort(per_file)
    assert set(_output_lib_names(soft_result)) == {x, y}

    # Pure-hard 2-cycle -> genuine conflict -> cycle error (a ValueError).
    hard = [(x, y), (y, x)]
    raised = False
    try:
        utils.merge_ldflags_with_topo_sort(per_file, hard_orderings=hard)
    except ValueError:
        raised = True
    assert raised, "pure-hard 2-cycle must raise LDFLAGSCycleError"


@settings(deadline=None)
@given(
    names=st.lists(lib_name, min_size=1, max_size=5, unique=True),
)
def test_separate_l_form_equivalent_to_combined(names):
    """``['-l', 'name']`` and ``['-lname']`` must produce identical output."""
    separate = [tok for n in names for tok in ("-l", n)]
    combined = ["-l" + n for n in names]
    r_sep = utils.merge_ldflags_with_topo_sort([separate])
    r_comb = utils.merge_ldflags_with_topo_sort([combined])
    assert r_sep == r_comb
