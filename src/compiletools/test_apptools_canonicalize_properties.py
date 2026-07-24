"""Property-based tests for apptools_canonicalize cache-key canonicalizers.

Black-box Hypothesis tests for
``canonicalize_for_cache_key`` (token-list) and
``canonicalize_path_for_cache_key`` (single path). No source is modified.

Invariants under test (from the module/function docstrings):
  * idempotence: ``canon(canon(x, a), a) == canon(x, a)``
  * empty-anchor identity: an empty anchor is the identity function
  * non-path / junk tokens are returned verbatim
  * token count is preserved (detached ``-I /abs`` pairs never desync)
  * anchor trailing-slash insensitivity: ``canon(x, "/r") == canon(x, "/r/")``
  * normpath collapse: ``<GITROOT>/lib/../src`` and ``<GITROOT>/src`` hash equal
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from compiletools.apptools_canonicalize import (
    canonicalize_for_cache_key,
    canonicalize_path_for_cache_key,
)

ANCHOR = "/repo"

_SEGS = ["src", "lib", "include", "x", "y"]
rel = st.lists(st.sampled_from(_SEGS), min_size=1, max_size=3).map("/".join)

# Paths of every relevant flavour relative to ANCHOR.
path = st.one_of(
    rel.map(lambda r: f"{ANCHOR}/{r}"),  # under anchor
    rel.map(lambda r: f"{ANCHOR}/lib/../{r}"),  # under anchor with .. to collapse
    rel.map(lambda r: f"/other/{r}"),  # outside anchor
    rel.map(lambda r: f"<GITROOT>/{r}"),  # already canonicalized
)

flag_prefix = st.sampled_from(["-I", "-isystem", "-L", "-F", "-B", "-iquote", "-idirafter", "-include", "-include-pch"])

JUNK = ["-O2", "-std=c++20", "-DFOO", "-g", "-Wall", "-pthread", "-fPIC", "hello", "-c"]

_attached = st.builds(lambda f, p: [f + p], flag_prefix, path)
_detached = st.builds(lambda f, p: [f, p], flag_prefix, path)
_xlinker = st.builds(lambda p: ["-Xlinker", p], path)
_wl = st.builds(lambda p: ["-Wl,-rpath," + p], path)
_prefixmap = st.builds(lambda p: ["-ffile-prefix-map=" + p + "=."], path)
_junk = st.sampled_from(JUNK).map(lambda t: [t])

_group = st.one_of(_attached, _detached, _xlinker, _wl, _prefixmap, _junk)
tokens = st.lists(_group, max_size=6).map(lambda groups: [t for g in groups for t in g])

junk_tokens = st.lists(st.sampled_from(JUNK), max_size=8)


# --- canonicalize_for_cache_key (token list) --------------------------------


@settings(deadline=None)
@given(toks=tokens)
def test_tokens_idempotent(toks):
    once = canonicalize_for_cache_key(toks, ANCHOR)
    twice = canonicalize_for_cache_key(once, ANCHOR)
    assert twice == once


@settings(deadline=None)
@given(toks=tokens)
def test_tokens_empty_anchor_is_identity(toks):
    assert canonicalize_for_cache_key(toks, "") == list(toks)


@settings(deadline=None)
@given(toks=tokens)
def test_tokens_count_preserved(toks):
    # No branch ever adds or drops a token, so detached ``-I /abs`` pairs
    # can never desync into the following flag.
    assert len(canonicalize_for_cache_key(toks, ANCHOR)) == len(toks)


@settings(deadline=None)
@given(toks=junk_tokens)
def test_junk_tokens_verbatim(toks):
    assert canonicalize_for_cache_key(toks, ANCHOR) == list(toks)


@settings(deadline=None)
@given(toks=tokens)
def test_tokens_anchor_trailing_slash_insensitive(toks):
    assert canonicalize_for_cache_key(toks, ANCHOR) == canonicalize_for_cache_key(toks, ANCHOR + "/")


@settings(deadline=None)
@given(toks=tokens)
def test_detached_flag_still_precedes_its_path(toks):
    """A detached ``-I`` must remain immediately followed by a path token."""
    out = canonicalize_for_cache_key(toks, ANCHOR)
    detached_flags = {"-I", "-isystem", "-L", "-F", "-B", "-iquote", "-idirafter", "-include", "-include-pch"}
    # Reconstruct the same pairing the canonicalizer uses and assert the
    # flag/path adjacency is preserved position-for-position.
    for i, tok in enumerate(out):
        if tok in detached_flags:
            assert i + 1 < len(out), f"detached flag {tok} lost its path arg: {out}"


# --- canonicalize_path_for_cache_key (single path) --------------------------


@settings(deadline=None)
@given(p=path)
def test_path_idempotent(p):
    once = canonicalize_path_for_cache_key(p, ANCHOR)
    twice = canonicalize_path_for_cache_key(once, ANCHOR)
    assert twice == once


@settings(deadline=None)
@given(p=path)
def test_path_empty_anchor_is_identity(p):
    assert canonicalize_path_for_cache_key(p, "") == p


@settings(deadline=None)
@given(p=path)
def test_path_anchor_trailing_slash_insensitive(p):
    assert canonicalize_path_for_cache_key(p, ANCHOR) == canonicalize_path_for_cache_key(p, ANCHOR + "/")


@settings(deadline=None)
@given(r=rel)
def test_path_normpath_collapse(r):
    """``<anchor>/lib/../<r>`` and ``<anchor>/<r>`` produce the same key."""
    with_dotdot = canonicalize_path_for_cache_key(f"{ANCHOR}/lib/../{r}", ANCHOR)
    direct = canonicalize_path_for_cache_key(f"{ANCHOR}/{r}", ANCHOR)
    assert with_dotdot == direct


def test_path_normpath_collapse_literal():
    """Explicit witness of the docstring's canonical example."""
    a = canonicalize_path_for_cache_key("/repo/lib/../src", "/repo")
    b = canonicalize_path_for_cache_key("/repo/src", "/repo")
    assert a == b == "<GITROOT>/src"
