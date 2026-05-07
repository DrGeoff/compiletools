"""Tests for ``apptools.filter_hash_irrelevant_tokens`` (TOKEN-3).

The filter strips diagnostic-only flag tokens (warnings, message
formatting, ``-pipe``, ``-v``) from a flag-token list so they do NOT
contribute to cache-key hashing. Toggling ``-Wall`` <-> ``-Wextra`` or
``-fdiagnostics-color=...`` must not invalidate per-TU object or PCH
cache entries.

The exception is ``-Werror`` (and ``-Werror=<warning>``): promoting a
warning to an error CAN change the build outcome, so it must remain
hash-relevant.
"""

from compiletools.apptools import filter_hash_irrelevant_tokens


def test_filter_strips_w_warnings():
    assert filter_hash_irrelevant_tokens(["-Wall", "-O2", "-Wextra"]) == ["-O2"]


def test_filter_keeps_werror():
    assert filter_hash_irrelevant_tokens(["-Wall", "-Werror", "-O2"]) == ["-Werror", "-O2"]


def test_filter_keeps_werror_with_value():
    assert filter_hash_irrelevant_tokens(["-Werror=unused-variable", "-O2"]) == [
        "-Werror=unused-variable",
        "-O2",
    ]


def test_filter_strips_fdiagnostics_color():
    assert filter_hash_irrelevant_tokens(["-fdiagnostics-color=always", "-O2"]) == ["-O2"]


def test_filter_strips_fmessage_length():
    assert filter_hash_irrelevant_tokens(["-fmessage-length=80", "-O2"]) == ["-O2"]


def test_filter_strips_pipe():
    assert filter_hash_irrelevant_tokens(["-pipe", "-O2"]) == ["-O2"]


def test_filter_strips_v_and_verbose():
    assert filter_hash_irrelevant_tokens(["-v", "-O2", "--verbose"]) == ["-O2"]


def test_filter_keeps_other_flags():
    tokens = ["-O2", "-std=c++20", "-fPIC", "-DFOO", "-Iinclude"]
    assert filter_hash_irrelevant_tokens(tokens) == tokens


def test_filter_empty_input():
    assert filter_hash_irrelevant_tokens([]) == []


def test_filter_does_not_mutate_input():
    tokens = ["-Wall", "-O2", "-Wextra"]
    snapshot = list(tokens)
    _ = filter_hash_irrelevant_tokens(tokens)
    assert tokens == snapshot
