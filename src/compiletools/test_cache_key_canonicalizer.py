"""Unit tests for the CAS cache-key path canonicalizer.

These cover the building block. Per-cache-site integration tests
(MacroState / _pch_command_hash / _pcm_command_hash) live in
their own files; the acceptance suite is in
test_cas_path_binding_acceptance.py.

Reference: docs/superpowers/specs/2026-05-08-cas-path-bound-cache-design.md
"""

from __future__ import annotations

import pytest

from compiletools.apptools import (
    canonicalize_for_cache_key,
    canonicalize_for_command,
    canonicalize_path_for_cache_key,
    canonicalize_path_for_command,
)

ANCHOR = "/run-1/workspace"


# ---------------------------------------------------------------------------
# canonicalize_for_cache_key — token-list canonicalization
# ---------------------------------------------------------------------------


def test_empty_anchor_is_identity():
    """When no anchor is supplied (empty/None), every token passes through."""
    tokens = ["-I/run-1/workspace/foo", "-O2", "-DBAR=1"]
    assert canonicalize_for_cache_key(tokens, "") == tokens


def test_no_path_flags_is_identity():
    """A token list with no path-bearing flags is unchanged."""
    tokens = ["-O2", "-std=c++20", "-DFOO=bar"]
    assert canonicalize_for_cache_key(tokens, ANCHOR) == tokens


def test_dash_I_attached_under_anchor_rewritten():
    """-I/anchor/foo becomes -I<GITROOT>/foo."""
    assert canonicalize_for_cache_key([f"-I{ANCHOR}/lib/util"], ANCHOR) == ["-I<GITROOT>/lib/util"]


def test_dash_I_detached_under_anchor_rewritten():
    """Detached form: -I /anchor/foo (two tokens) becomes -I <GITROOT>/foo."""
    out = canonicalize_for_cache_key(["-I", f"{ANCHOR}/lib/util"], ANCHOR)
    assert out == ["-I", "<GITROOT>/lib/util"]


def test_dash_I_outside_anchor_passes_through():
    """System / out-of-workspace include directories must NOT be rewritten."""
    tokens = ["-I/usr/include", "-I/opt/sibling-repo/inc"]
    assert canonicalize_for_cache_key(tokens, ANCHOR) == tokens


def test_anchor_exactly_equals_path():
    """-I/anchor (no subdirectory) becomes -I<GITROOT>."""
    assert canonicalize_for_cache_key([f"-I{ANCHOR}"], ANCHOR) == ["-I<GITROOT>"]


def test_anchor_with_trailing_slash_handled():
    """A trailing slash on the anchor must not break path matching."""
    out = canonicalize_for_cache_key([f"-I{ANCHOR}/lib"], ANCHOR + "/")
    assert out == ["-I<GITROOT>/lib"]


def test_relative_path_passes_through():
    """-Ifoo (already relative) is left alone."""
    tokens = ["-Ifoo", "-Ilib/util", "-I."]
    assert canonicalize_for_cache_key(tokens, ANCHOR) == tokens


@pytest.mark.parametrize(
    "flag",
    [
        "-I",
        "-isystem",
        "-iquote",
        "-idirafter",
        "-F",
        "-B",
        "-include",
        "-include-pch",
        "-L",
    ],
)
def test_each_flag_family_recognized_attached(flag):
    """Every path-bearing flag family canonicalizes its attached form."""
    assert canonicalize_for_cache_key([f"{flag}{ANCHOR}/foo"], ANCHOR) == [f"{flag}<GITROOT>/foo"]


@pytest.mark.parametrize(
    "flag",
    [
        "-I",
        "-isystem",
        "-iquote",
        "-idirafter",
        "-F",
        "-B",
        "-include",
        "-include-pch",
        "-L",
    ],
)
def test_each_flag_family_recognized_detached(flag):
    """Every path-bearing flag family canonicalizes its detached form."""
    out = canonicalize_for_cache_key([flag, f"{ANCHOR}/foo"], ANCHOR)
    assert out == [flag, "<GITROOT>/foo"]


def test_preexisting_GITROOT_token_passes_through():
    """A token already containing the <GITROOT> sentinel is idempotent —
    re-canonicalization must produce the same output."""
    tokens = ["-I<GITROOT>/lib/util", "-isystem", "<GITROOT>/foo"]
    assert canonicalize_for_cache_key(tokens, ANCHOR) == tokens


def test_idempotent_under_double_application():
    """canonicalize(canonicalize(x)) == canonicalize(x) for any input."""
    tokens = [
        f"-I{ANCHOR}/foo",
        "-I/usr/include",
        "-isystem",
        f"{ANCHOR}/bar",
        "-O2",
    ]
    once = canonicalize_for_cache_key(tokens, ANCHOR)
    twice = canonicalize_for_cache_key(once, ANCHOR)
    assert once == twice


def test_non_path_flags_passthrough():
    """Non-path flags coexisting with path flags are not touched."""
    tokens = [
        "-O2",
        "-std=c++20",
        "-DFOO=bar",
        f"-I{ANCHOR}/foo",
        "-Wall",
        "-fno-exceptions",
    ]
    out = canonicalize_for_cache_key(tokens, ANCHOR)
    assert out == [
        "-O2",
        "-std=c++20",
        "-DFOO=bar",
        "-I<GITROOT>/foo",
        "-Wall",
        "-fno-exceptions",
    ]


def test_detached_at_end_with_no_following_token():
    """A detached path flag with no following token (malformed but
    possible) must not crash; trailing flag passes through unchanged."""
    tokens = ["-O2", "-I"]
    assert canonicalize_for_cache_key(tokens, ANCHOR) == tokens


# ---------------------------------------------------------------------------
# canonicalize_path_for_cache_key — single-path canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "anchor", "expected"),
    [
        pytest.param(f"{ANCHOR}/lib/util/pch.h", ANCHOR, "<GITROOT>/lib/util/pch.h", id="under-anchor"),
        pytest.param("/usr/include/stdio.h", ANCHOR, "/usr/include/stdio.h", id="outside-anchor"),
        pytest.param("/run-1/workspace/foo.h", "", "/run-1/workspace/foo.h", id="empty-anchor"),
        pytest.param(ANCHOR, ANCHOR, "<GITROOT>", id="anchor-exact"),
        pytest.param(f"{ANCHOR}/foo.h", ANCHOR + "/", "<GITROOT>/foo.h", id="trailing-slash"),
    ],
)
def test_path_canonicalization(path, anchor, expected):
    assert canonicalize_path_for_cache_key(path, anchor) == expected


def test_path_idempotent():
    once = canonicalize_path_for_cache_key(f"{ANCHOR}/foo.h", ANCHOR)
    twice = canonicalize_path_for_cache_key(once, ANCHOR)
    assert once == twice


# ---------------------------------------------------------------------------
# I3: -Wl,opt,/abs/path and -Xlinker /abs/path canonicalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(
            [f"-Wl,-rpath,{ANCHOR}/lib"],
            ["-Wl,-rpath,<GITROOT>/lib"],
            id="comma-path",
        ),
        pytest.param(
            [f"-Wl,--version-script={ANCHOR}/script.ld"],
            ["-Wl,--version-script=<GITROOT>/script.ld"],
            id="equals-path",
        ),
        pytest.param(
            [f"-Wl,-rpath,{ANCHOR}/a,-rpath,{ANCHOR}/b"],
            ["-Wl,-rpath,<GITROOT>/a,-rpath,<GITROOT>/b"],
            id="multiple-paths",
        ),
        pytest.param(["-Wl,-rpath,/usr/lib"], ["-Wl,-rpath,/usr/lib"], id="outside-anchor"),
        pytest.param(["-Wl,--as-needed"], ["-Wl,--as-needed"], id="no-path-segment"),
    ],
)
def test_Wl_canonicalization(tokens, expected):
    """``-Wl,`` tokens are split on ``,`` and ``=``, and each path-shaped
    segment is canonicalised. Without this, trace_backend's ``command_hash``
    differs between workspaces under different gitroots even when the
    rpath / version-script is logically the same. Cases:

    - ``comma-path``: ``-Wl,-rpath,/abs/path/lib`` — comma split.
    - ``equals-path``: ``-Wl,--version-script=/abs/path/script.ld`` — comma
      then ``=`` split for value-bearing options.
    - ``multiple-paths``: both rpaths in a single ``-Wl,`` token canonicalised.
    - ``outside-anchor``: unrelated paths pass through unchanged.
    - ``no-path-segment``: ``-Wl,--as-needed`` (no path) passes through.
    """
    assert canonicalize_for_cache_key(tokens, ANCHOR) == expected


def test_Xlinker_two_token_path_canonicalized():
    """``-Xlinker -rpath -Xlinker /abs/path`` — the SECOND ``-Xlinker``
    is followed by an rpath; canonicalize it. The first ``-Xlinker``'s
    next token is ``-rpath`` (not a path), pass through.
    """
    out = canonicalize_for_cache_key(
        ["-Xlinker", "-rpath", "-Xlinker", f"{ANCHOR}/lib"],
        ANCHOR,
    )
    assert out == ["-Xlinker", "-rpath", "-Xlinker", "<GITROOT>/lib"]


def test_Xlinker_outside_anchor_unchanged():
    out = canonicalize_for_cache_key(
        ["-Xlinker", "-rpath", "-Xlinker", "/usr/lib"],
        ANCHOR,
    )
    assert out == ["-Xlinker", "-rpath", "-Xlinker", "/usr/lib"]


def test_Wl_idempotent():
    once = canonicalize_for_cache_key([f"-Wl,-rpath,{ANCHOR}/lib"], ANCHOR)
    twice = canonicalize_for_cache_key(once, ANCHOR)
    assert once == twice


# ---------------------------------------------------------------------------
# Round 3: -f{file,debug,macro,canon}-prefix-map=OLD=NEW recognition
#
# The canonicalizer must rewrite the OLD (LHS) of these flags so two users
# at different checkout paths produce the same cache-key hash for the same
# auto-injected -ffile-prefix-map=<gitroot>=<target> token. NEW (RHS) is
# preserved verbatim — only OLD is path-shaped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        pytest.param(f"-ffile-prefix-map={ANCHOR}=.", "-ffile-prefix-map=<GITROOT>=.", id="file"),
        pytest.param(
            f"-fdebug-prefix-map={ANCHOR}/sub=/__ct__/sub",
            "-fdebug-prefix-map=<GITROOT>/sub=/__ct__/sub",
            id="debug",
        ),
        pytest.param(f"-fmacro-prefix-map={ANCHOR}=.", "-fmacro-prefix-map=<GITROOT>=.", id="macro"),
        pytest.param(f"-fcanon-prefix-map={ANCHOR}=.", "-fcanon-prefix-map=<GITROOT>=.", id="canon"),
    ],
)
def test_prefix_map_recognized(token, expected):
    out = canonicalize_for_cache_key([token], ANCHOR)
    assert out == [expected]


def test_prefix_map_outside_anchor_passes_through():
    """``/system/path`` is outside ``ANCHOR`` — the LHS is not anchor-rooted,
    so the token passes through unchanged. The user explicitly mapped a
    non-workspace path; we don't touch it."""
    out = canonicalize_for_cache_key(["-ffile-prefix-map=/system/path=foo"], ANCHOR)
    assert out == ["-ffile-prefix-map=/system/path=foo"]


def test_prefix_map_idempotent():
    once = canonicalize_for_cache_key([f"-ffile-prefix-map={ANCHOR}=."], ANCHOR)
    twice = canonicalize_for_cache_key(once, ANCHOR)
    assert once == twice


def test_prefix_map_malformed_no_equals_passes_through():
    """A bare ``-ffile-prefix-map=NOEQUALS`` (missing the inner ``=`` between
    OLD and NEW) is malformed — pass through unchanged rather than guess
    the user's intent."""
    out = canonicalize_for_cache_key([f"-ffile-prefix-map={ANCHOR}"], ANCHOR)
    assert out == [f"-ffile-prefix-map={ANCHOR}"]


def test_prefix_map_anchor_exact_match():
    """When the LHS is exactly the anchor (no trailing slash, no subpath),
    the rewrite produces ``<GITROOT>`` without any trailing slash."""
    out = canonicalize_for_cache_key([f"-ffile-prefix-map={ANCHOR}=."], ANCHOR)
    assert out == ["-ffile-prefix-map=<GITROOT>=."]


def test_prefix_map_inside_other_tokens_unaffected():
    """Mixing prefix-map tokens with normal -I and -O flags doesn't perturb
    either side."""
    out = canonicalize_for_cache_key(
        ["-O2", f"-I{ANCHOR}/include", f"-ffile-prefix-map={ANCHOR}=.", "-Wall"],
        ANCHOR,
    )
    assert out == ["-O2", "-I<GITROOT>/include", "-ffile-prefix-map=<GITROOT>=.", "-Wall"]


# ---------------------------------------------------------------------------
# Round 3: canonicalize_for_command + canonicalize_path_for_command
#
# Sister functions of canonicalize_for_cache_key / canonicalize_path_for_cache_key.
# Same parsing logic; substitute a configurable target string instead of
# the <GITROOT> sentinel. Used by link-rule constructors to rewrite the
# actual emitted argv so binary RPATHs / version-script paths / -L paths
# are workspace-location-independent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "target", "expected"),
    [
        pytest.param(
            [f"-I{ANCHOR}/include", f"-Wl,-rpath,{ANCHOR}/lib"],
            ".",
            ["-I./include", "-Wl,-rpath,./lib"],
            id="target-not-sentinel",
        ),
        pytest.param([f"-I{ANCHOR}/include"], "/__ct__", ["-I/__ct__/include"], id="custom-target"),
        pytest.param(["-I/usr/include", "-O2"], ".", ["-I/usr/include", "-O2"], id="outside-anchor"),
    ],
)
def test_command_token_canonicalization(tokens, target, expected):
    assert canonicalize_for_command(tokens, ANCHOR, target=target) == expected


def test_canonicalize_for_command_empty_anchor_is_identity():
    """Falsy anchor -> identity; same contract as canonicalize_for_cache_key."""
    tokens = [f"-I{ANCHOR}/include", "-O2"]
    assert canonicalize_for_command(tokens, "", target=".") == tokens


def test_canonicalize_for_command_handles_prefix_map_flag():
    """The prefix-map flag's LHS gets the target substitution too -- so the
    auto-injected ``-ffile-prefix-map=<gitroot>=.`` becomes
    ``-ffile-prefix-map=.=.`` in the emitted command (which is harmless
    -- gcc maps ``.`` to ``.``)."""
    out = canonicalize_for_command([f"-ffile-prefix-map={ANCHOR}=."], ANCHOR, target=".")
    assert out == ["-ffile-prefix-map=.=."]


def test_canonicalize_path_for_command_substitutes_target():
    assert canonicalize_path_for_command(f"{ANCHOR}/foo.o", ANCHOR, target=".") == "./foo.o"
    assert canonicalize_path_for_command(ANCHOR, ANCHOR, target=".") == "."
    assert canonicalize_path_for_command("/usr/lib/libc.a", ANCHOR, target=".") == "/usr/lib/libc.a"


def test_canonicalize_path_for_command_empty_anchor_is_identity():
    assert canonicalize_path_for_command(f"{ANCHOR}/foo.o", "", target=".") == f"{ANCHOR}/foo.o"


def test_cache_key_collapses_dotdot_segments():
    """Two textually distinct conf-driven include paths that resolve to the
    same directory (``${CONF_DIR}/../shared/src/include`` from a sibling
    conf vs. ``${CONF_DIR}/shared/src/include`` from the parent) must
    hash to the same cache key. Without normpath in the sentinel-rewrite
    path, each consumer forks the macro_state_hash on a token that
    compiles to the same ``-I`` semantically -- the central-include
    fragility that motivates this patch.
    """
    a = canonicalize_path_for_cache_key(f"{ANCHOR}/lib/../src/include", ANCHOR)
    b = canonicalize_path_for_cache_key(f"{ANCHOR}/src/include", ANCHOR)
    assert a == b == "<GITROOT>/src/include"


def test_cache_key_collapses_redundant_separators_and_dot():
    """Interior double-slashes and ``./`` segments collapse to a single
    separator / dropped segment. (Leading ``//`` is preserved by
    :func:`os.path.normpath` per POSIX -- not exercised here because the
    sentinel-rooted paths produced by the rewrite never start with
    ``//``.)"""
    assert canonicalize_path_for_cache_key(f"{ANCHOR}//foo/./bar", ANCHOR) == "<GITROOT>/foo/bar"


def test_cache_key_normalises_already_sentinel_path_with_dotdot():
    """The early-return arm (path already contains the sentinel) must
    normalise too -- otherwise a re-entrant caller that passes back a
    previously-sentinel-rewritten-but-not-yet-normalised string would
    silently bypass the fragmentation fix. Pins symmetry with the
    rewrite arm."""
    assert canonicalize_path_for_cache_key("<GITROOT>/lib/../src", ANCHOR) == "<GITROOT>/src"


def test_cache_key_token_normalisation_in_I_flag():
    """The same fragmentation surface, but on the token form
    (``-I<gitroot>/lib/../src/include``) that magicflags actually
    emits."""
    a = canonicalize_for_cache_key([f"-I{ANCHOR}/lib/../src/include"], ANCHOR)
    b = canonicalize_for_cache_key([f"-I{ANCHOR}/src/include"], ANCHOR)
    assert a == b == ["-I<GITROOT>/src/include"]


def test_command_path_not_normalised_through_dotdot():
    """Emitted-command paths skip normpath: lexical ``..`` collapse can
    change what gcc resolves when intermediate segments are symlinks. The
    cache-key flavour normalises; the command flavour preserves the
    original lexical form."""
    out = canonicalize_path_for_command(f"{ANCHOR}/lib/../src/include", ANCHOR, target=".")
    assert out == "./lib/../src/include"


def test_canonicalize_for_cache_key_unchanged_after_refactor():
    """Smoke check that the existing public API still produces sentinel
    output (the refactor extracted a shared core but the public function
    must keep its pre-refactor semantics)."""
    out = canonicalize_for_cache_key([f"-I{ANCHOR}/include"], ANCHOR)
    assert out == ["-I<GITROOT>/include"]
    out = canonicalize_path_for_cache_key(f"{ANCHOR}/foo.o", ANCHOR)
    assert out == "<GITROOT>/foo.o"
