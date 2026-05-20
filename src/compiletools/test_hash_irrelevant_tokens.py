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

import pytest

from compiletools.apptools import filter_hash_irrelevant_tokens


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(["-Wall", "-O2", "-Wextra"], ["-O2"], id="strip-warnings"),
        pytest.param(["-Wall", "-Werror", "-O2"], ["-Werror", "-O2"], id="keep-werror"),
        pytest.param(
            ["-Werror=unused-variable", "-O2"],
            ["-Werror=unused-variable", "-O2"],
            id="keep-werror-value",
        ),
        pytest.param(["-fdiagnostics-color=always", "-O2"], ["-O2"], id="strip-diagnostics-color"),
        pytest.param(["-fmessage-length=80", "-O2"], ["-O2"], id="strip-message-length"),
        pytest.param(["-pipe", "-O2"], ["-O2"], id="strip-pipe"),
        pytest.param(["-v", "-O2", "--verbose"], ["-O2"], id="strip-verbose"),
        pytest.param(
            ["-O2", "-std=c++20", "-fPIC", "-DFOO", "-Iinclude"],
            ["-O2", "-std=c++20", "-fPIC", "-DFOO", "-Iinclude"],
            id="keep-other-flags",
        ),
        pytest.param([], [], id="empty"),
    ],
)
def test_filter_hash_irrelevant_tokens(tokens, expected):
    assert filter_hash_irrelevant_tokens(tokens) == expected


def test_filter_does_not_mutate_input():
    tokens = ["-Wall", "-O2", "-Wextra"]
    snapshot = list(tokens)
    _ = filter_hash_irrelevant_tokens(tokens)
    assert tokens == snapshot
