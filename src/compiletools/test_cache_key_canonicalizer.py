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
    canonicalize_path_for_cache_key,
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


def test_path_under_anchor_rewritten():
    assert canonicalize_path_for_cache_key(f"{ANCHOR}/lib/util/pch.h", ANCHOR) == "<GITROOT>/lib/util/pch.h"


def test_path_outside_anchor_unchanged():
    assert canonicalize_path_for_cache_key("/usr/include/stdio.h", ANCHOR) == "/usr/include/stdio.h"


def test_path_empty_anchor_is_identity():
    assert canonicalize_path_for_cache_key("/run-1/workspace/foo.h", "") == "/run-1/workspace/foo.h"


def test_path_anchor_exactly_equals():
    assert canonicalize_path_for_cache_key(ANCHOR, ANCHOR) == "<GITROOT>"


def test_path_anchor_with_trailing_slash():
    assert canonicalize_path_for_cache_key(f"{ANCHOR}/foo.h", ANCHOR + "/") == "<GITROOT>/foo.h"


def test_path_idempotent():
    once = canonicalize_path_for_cache_key(f"{ANCHOR}/foo.h", ANCHOR)
    twice = canonicalize_path_for_cache_key(once, ANCHOR)
    assert once == twice


# ---------------------------------------------------------------------------
# I3: -Wl,opt,/abs/path and -Xlinker /abs/path canonicalization
# ---------------------------------------------------------------------------


def test_Wl_comma_path_canonicalized():
    """``-Wl,-rpath,/abs/path/lib`` — split on comma, canonicalise each
    path-shaped segment. Without this, trace_backend's command_hash
    differs between workspaces under different gitroots even when the
    rpath is logically the same.
    """
    assert canonicalize_for_cache_key([f"-Wl,-rpath,{ANCHOR}/lib"], ANCHOR) == ["-Wl,-rpath,<GITROOT>/lib"]


def test_Wl_equals_path_canonicalized():
    """``-Wl,--version-script=/abs/path/script.ld`` — split on comma,
    then on ``=`` for value-bearing options.
    """
    assert canonicalize_for_cache_key([f"-Wl,--version-script={ANCHOR}/script.ld"], ANCHOR) == [
        "-Wl,--version-script=<GITROOT>/script.ld"
    ]


def test_Wl_multiple_paths_in_one_token_all_canonicalized():
    """``-Wl,-rpath,/abs/a,-rpath,/abs/b`` — both paths get canonicalised."""
    assert canonicalize_for_cache_key([f"-Wl,-rpath,{ANCHOR}/a,-rpath,{ANCHOR}/b"], ANCHOR) == [
        "-Wl,-rpath,<GITROOT>/a,-rpath,<GITROOT>/b"
    ]


def test_Wl_outside_anchor_unchanged():
    assert canonicalize_for_cache_key(["-Wl,-rpath,/usr/lib"], ANCHOR) == ["-Wl,-rpath,/usr/lib"]


def test_Wl_no_path_segment_unchanged():
    """``-Wl,--as-needed`` (no path) passes through unchanged."""
    assert canonicalize_for_cache_key(["-Wl,--as-needed"], ANCHOR) == ["-Wl,--as-needed"]


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
