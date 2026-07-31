"""The build-context hash must track argv equality, not flag-slot layout.

The compile argv is ``CXX + flags.cxx`` for C++ TUs and ``CC + flags.c``
for C TUs; raw CPPFLAGS never appears on a compile line by itself — in
unified mode it is folded into CXXFLAGS. Hashing the CPPFLAGS slot on
its own therefore forked the object CAS key space whenever a token was
promoted between CPPFLAGS and CXXFLAGS without changing any argv (the
--auto vs --no-auto ffile-prefix-map fork).

The hash contract pinned here:
- cxx and c token lists are each hashed exactly (key equality implies
  argv equality — a collision on differing argvs would be a silent
  miscompile, since cas-objdir has no in-band verification at link).
- CPPFLAGS participates only through dedup(cpp + cxx), so a token
  present in cxx hashes the same whether or not it also sits in cpp.
"""

from compiletools.preprocessing_cache import MacroState

_BASE = ["-I/ws/lib", "-fPIC", "-g"]
_MAP = "-ffile-prefix-map=/ws=."


def _hash(cpp, c, cxx):
    state = MacroState(
        core={b"__GNUC__": b"13"},
        variable={},
        compiler_path="/usr/bin/g++",
        cppflags=" ".join(cpp),
        cflags=" ".join(c),
        cxxflags=" ".join(cxx),
        cmdline_origin=frozenset(),
        cppflags_tokens=list(cpp),
        cflags_tokens=list(c),
        cxxflags_tokens=list(cxx),
        compiler_identity="/usr/bin/g++|123456|1700000000",
        anchor_root="/ws",
    )
    return state.get_hash(include_core=True)


def test_cxx_token_promoted_into_cpp_does_not_fork_the_key():
    """The --auto vs --no-auto fork: two-pass runs carried the injected
    prefix-map token in BOTH cpp and cxx, single-pass runs in cxx only.
    Every emitted compile command line was identical, so the keys must be
    identical too."""
    two_pass = _hash(cpp=[*_BASE, _MAP], c=["-O2", _MAP], cxx=[*_BASE, _MAP])
    single_pass = _hash(cpp=_BASE, c=["-O2", _MAP], cxx=[*_BASE, _MAP])
    assert two_pass == single_pass, (
        "A token present in cxx must hash the same whether or not it is "
        "also present in cpp — the compile argv is identical in both "
        "layouts, so the object CAS key must be too."
    )


def test_cxx_only_flag_change_still_forks_the_key():
    """Adding a token to cxx changes the C++ argv, so the key must change."""
    assert _hash(cpp=_BASE, c=["-O2"], cxx=_BASE) != _hash(cpp=_BASE, c=["-O2"], cxx=[*_BASE, "-fno-exceptions"])


def test_c_vs_cxx_placement_still_forks_the_key():
    """-fno-exceptions in the C slot vs the C++ slot produces different
    compile argvs, so the keys must differ — a full three-slot union
    would collapse these and silently miscompile."""
    in_c = _hash(cpp=_BASE, c=["-O2", "-fno-exceptions"], cxx=_BASE)
    in_cxx = _hash(cpp=_BASE, c=["-O2"], cxx=[*_BASE, "-fno-exceptions"])
    assert in_c != in_cxx


def test_cpp_only_flag_change_still_forks_the_key():
    """A cpp-only token (not mirrored in cxx) changes ct's own header
    resolution (__has_include search paths), so it must stay in the key
    via the cpp-union part."""
    assert _hash(cpp=_BASE, c=["-O2"], cxx=_BASE) != _hash(cpp=[*_BASE, "-I/elsewhere"], c=["-O2"], cxx=_BASE)


def test_cxx_token_order_still_forks_the_key():
    """Token order is an argv property (-I search order, last-wins flags);
    the hash must not sort it away."""
    assert _hash(cpp=[], c=[], cxx=["-I/a", "-I/b"]) != _hash(cpp=[], c=[], cxx=["-I/b", "-I/a"])
