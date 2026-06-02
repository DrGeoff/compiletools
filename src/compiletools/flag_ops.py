"""Pure, dependency-free token helpers for compile-flag manipulation.

This is a true *leaf* module: it imports only the standard library and
must never import ``apptools``, ``flags``, ``headerdeps``, or any other
heavier compiletools module. Its sole purpose is to hold the pure
token-list operations that both ``apptools`` and ``flags`` need, so that
``flags.py`` can import them at top level without reintroducing the
historical ``flags <-> apptools`` import cycle.

The helpers here operate purely on pre-tokenized flag sequences (lists
or tuples of ``str``). They perform no filesystem access and have no
side effects. ``apptools`` re-exports every public name below so that
existing ``apptools.<name>`` call sites and test/patch targets keep
working with identical object identity.

NOTE: ``compiler_identity`` is deliberately *not* here -- it does
filesystem stat/realpath and depends on apptools' path-canonicalization
chain, so it is not a pure token helper and stays in ``apptools``.
"""

from __future__ import annotations

from collections.abc import Sequence


def extract_include_paths_from_tokens(tokens) -> set[str]:
    """Return the set of -I paths (attached or detached form) in tokens.

    Recognises ``-I/p``, ``-I /p`` (two-token detached form), and
    ``-Idir`` only -- not ``-isystem`` or ``-L`` (those are different
    flag families). Used by include-path dedup helpers in apptools and
    flags.py.
    """
    paths: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-I" and i + 1 < n:
            paths.add(tokens[i + 1])
            i += 2
        elif tok.startswith("-I") and len(tok) > 2:
            paths.add(tok[2:])
            i += 1
        else:
            i += 1
    return paths


def dedup_include_paths_to_append(existing_tokens, new_paths) -> list[str]:
    """Return tokens to append (in detached ``-I path`` form) to add
    ``new_paths`` to ``existing_tokens`` without duplicating any path
    already present as a -I entry.
    """
    seen = extract_include_paths_from_tokens(existing_tokens)
    out: list[str] = []
    for path in new_paths:
        if path in seen:
            continue
        out.extend(("-I", path))
        seen.add(path)
    return out


def strip_d_u_tokens(tokens: Sequence[str]) -> list[str]:
    """Strip ``-D`` and ``-U`` entries (in both attached and detached
    forms) from a pre-tokenized flag sequence.

    This is the strip-only half of :func:`tokenize_compile_flags`,
    extracted so that callers that already hold a pre-tokenized list
    or tuple (e.g. ``magicflags._parse``, ``_pch_command_hash``,
    ``Flags.hash_relevant``) don't have to pay the tokenization cost
    a second time.

    Both attached form (``-DFOO``, ``-DFOO=bar``, ``-UFOO``) and
    detached form (``-D FOO``, ``-D FOO=bar``, ``-U FOO``) are
    stripped. Detached form drops both the flag token and the
    following value token. A dangling ``-D`` / ``-U`` at the end of
    the list drops just the flag token. All other flags (``-I``,
    ``-O``, ``-std``, ``-W``, ``-f``...) pass through unchanged.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-D" or tok == "-U":
            # Detached form: skip flag and the next token (value).
            # Dangling flag at end of list: skip just the flag.
            i += 2
            continue
        if tok.startswith("-D") or tok.startswith("-U"):
            # Attached form: skip this single token.
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


# Flag-prefix classification: tokens whose presence/value never affects
# the compiled object bytes. Excluded from cache-key hashing so that
# changing a warning level doesn't trigger a rebuild.
#
# These cover the GCC/Clang diagnostic and verbosity ecosystem:
# - -W*: warnings (pure diagnostic; -Werror is the one exception, see below)
# - -fdiagnostics-*, -fmessage-length=, -fno-show-column,
#   -fno-diagnostics-show-option, -fcaret-diagnostics,
#   -fno-color-diagnostics, -fcolor-diagnostics: message formatting
# - -pipe: tells compiler to use pipes for I/O between stages
# - -v / --verbose: prints the compile invocation
# - --help / -###: introspection-only
# Prefix-matched diagnostic flag families: any token starting with one
# of these strings is hash-irrelevant. -W and -fdiagnostics- are open-
# ended families (-Wall, -Wextra, -Wno-foo, -fdiagnostics-color, ...),
# so prefix matching is correct.
_HASH_IRRELEVANT_PREFIXES: tuple[str, ...] = (
    "-W",  # warnings (see _HASH_RELEVANT_W_FLAGS exception below)
    "-fdiagnostics-",
    "-fmessage-length=",
    "-fno-show-column",
    "-fno-diagnostics-show-option",
    "-fcaret-diagnostics",
    "-fno-color-diagnostics",
    "-fcolor-diagnostics",
)

# Exact-matched diagnostic flags: single-token flags that should NOT
# match prefix-style. e.g. ``-v`` must not silently swallow a hypothetical
# future ``-vN``-style flag, and ``-pipe`` must not match
# ``-pipefoo``. These are checked with ``tok ==`` rather than
# ``tok.startswith()`` so the match is precise.
_HASH_IRRELEVANT_EXACT: frozenset[str] = frozenset(
    {
        "-pipe",
        "-v",
        "--verbose",
        "--help",
        "-###",
    }
)

# Exception: -Werror promotes warnings to errors, which CAN affect the
# build outcome (compile fails vs succeeds). Treat -Werror and
# -Werror=<warning> as hash-relevant.
_HASH_RELEVANT_W_FLAGS: tuple[str, ...] = (
    "-Werror",
    "-Werror=",
)


def filter_hash_irrelevant_tokens(tokens: Sequence[str]) -> list[str]:
    """Remove tokens that don't affect compiled output from a flag sequence.

    Used by cache-key hashing to elide diagnostic-only flag changes.
    Accepts either a list or tuple. ``-W*`` warnings are dropped
    EXCEPT ``-Werror`` and ``-Werror=...`` (which can change compile
    outcome). Returns a NEW list; input is not mutated.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # -Werror exception: hash-relevant. ``-Werror`` itself, and the
        # ``-Werror=<warning>`` parametrized form, both promote warnings
        # to errors and thus can change build outcome.
        if any(tok == we or tok.startswith(we) for we in _HASH_RELEVANT_W_FLAGS):
            out.append(tok)
            i += 1
            continue
        # Exact-matched diagnostic flags: drop without prefix-eating risk.
        if tok in _HASH_IRRELEVANT_EXACT:
            i += 1
            continue
        # Prefix-matched diagnostic flag families: drop. None of these
        # take a separate value token in current GCC/Clang
        # (``-fmessage-length=`` is the attached form), so a single-
        # token skip suffices.
        if any(tok.startswith(prefix) for prefix in _HASH_IRRELEVANT_PREFIXES):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out
