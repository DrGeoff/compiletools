"""Property-based tests for namer.Namer.compute_dep_hash.

``get_file_hash`` is mocked to a pure path->hex map so ``compute_dep_hash``
becomes a pure function of its header list. No source is modified.

Invariants under test (from the function docstring):
  * order-independence (XOR + sort)
  * duplicate-idempotence (dedup before XOR)
  * a missing / FileNotFound header contributes XOR-identity (0)
  * empty list -> sentinel ``"0" * 14``

Plus a CHARACTERIZATION test documenting that the XOR fold is NOT
injective: two distinct header sets can XOR-collide to the same dep_hash.
This is a known, deliberate algebraic limitation -- safe today only
because the full object-CAS key carries 168 bits across three independent
hashes (see top-level CLAUDE.md "CAS layers").
"""

import hashlib
import types
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import compiletools.global_hash_registry as ghr
import compiletools.namer

_MISSING_PREFIX = "::missing::"

PRESENT_PATHS = ["/a/foo.h", "/a/bar.h", "/b/baz.h", "/c/qux.h", "/d/quux.h"]
MISSING_PATHS = [_MISSING_PREFIX + "gen1.h", _MISSING_PREFIX + "gen2.h"]


def _fake_get_file_hash(path, ctx):
    """Pure path->hex. Paths flagged missing raise FileNotFoundError."""
    s = str(path)
    if s.startswith(_MISSING_PREFIX):
        raise FileNotFoundError(s)
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture(autouse=True, scope="module")
def _patch_get_file_hash():
    orig = ghr.get_file_hash
    ghr.get_file_hash = _fake_get_file_hash
    yield
    ghr.get_file_hash = orig


def _stub():
    """A minimal object exposing just what compute_dep_hash touches.

    Cast to Namer so the unbound-method call typechecks; compute_dep_hash
    only reads ``self.args.verbose`` and ``self.context``.
    """
    stub = types.SimpleNamespace(args=types.SimpleNamespace(verbose=0), context=None)
    return cast(compiletools.namer.Namer, stub)


def _dep_hash(header_list):
    return compiletools.namer.Namer.compute_dep_hash(_stub(), header_list)


present_list = st.lists(st.sampled_from(PRESENT_PATHS), max_size=6)


@settings(deadline=None)
@given(headers=present_list, perm_seed=st.randoms(use_true_random=False))
def test_order_independent(headers, perm_seed):
    shuffled = list(headers)
    perm_seed.shuffle(shuffled)
    assert _dep_hash(headers) == _dep_hash(shuffled)


@settings(deadline=None)
@given(headers=present_list)
def test_duplicate_idempotent(headers):
    assert _dep_hash(headers) == _dep_hash(headers + headers)


@settings(deadline=None)
@given(headers=present_list, missing=st.sampled_from(MISSING_PATHS))
def test_missing_header_is_xor_identity(headers, missing):
    assert _dep_hash(headers) == _dep_hash(headers + [missing])


def test_empty_is_sentinel():
    assert _dep_hash([]) == "0" * 14


def test_only_missing_headers_give_sentinel():
    assert _dep_hash(MISSING_PATHS) == "0" * 14
    assert _dep_hash(list(MISSING_PATHS)) == "0" * 14


def test_xor_is_non_injective_characterization():
    """Two distinct header sets that XOR-collide produce the same dep_hash.

    Documents the known algebraic limitation of the XOR fold. Values are
    chosen so 1 ^ 2 == 4 ^ 7 == 3; both distinct sets fold to the same
    56-bit result. Safe in production only because the full object-CAS
    key adds file-hash (12 hex) and macro-state-hash (16 hex) on top.
    """
    mapping = {
        "/set1/p1.h": format(1, "014x"),
        "/set1/p2.h": format(2, "014x"),
        "/set2/p3.h": format(4, "014x"),
        "/set2/p4.h": format(7, "014x"),
    }

    def mapped_get_file_hash(path, ctx):
        return mapping[str(path)]

    orig = ghr.get_file_hash
    ghr.get_file_hash = mapped_get_file_hash
    try:
        set_a = ["/set1/p1.h", "/set1/p2.h"]
        set_b = ["/set2/p3.h", "/set2/p4.h"]
        assert set(set_a) != set(set_b)
        hash_a = _dep_hash(set_a)
        hash_b = _dep_hash(set_b)
        assert hash_a == hash_b == format(3, "014x"), f"expected XOR collision, got {hash_a} vs {hash_b}"
    finally:
        ghr.get_file_hash = orig
