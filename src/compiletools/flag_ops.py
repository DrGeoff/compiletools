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


def extract_d_macros(tokens: Sequence[str]) -> dict[str, str]:
    """Collect ``-D`` macro definitions from a pre-tokenized flag sequence.

    The collect half of :func:`strip_d_u_tokens`' ``-D`` handling: both
    walks must recognize the same forms or a macro would be stripped
    from the hashed tokens without landing in the macro universe.
    Recognizes attached (``-DFOO``, ``-DFOO=val``) and detached
    (``-D FOO``, ``-D FOO=val``) forms. A macro without an explicit
    value maps to ``"1"`` (the compiler default). Later occurrences
    overwrite earlier ones, matching the compiler's last-wins rule.
    ``-U`` is ignored (as are all non ``-D`` tokens).
    """
    macros: dict[str, str] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        macro_def = None
        if tok == "-D":
            # Detached form: name (and optional =value) is the next token.
            if i + 1 < n:
                macro_def = tokens[i + 1]
            i += 2
        elif tok.startswith("-D"):
            macro_def = tok[2:]
            i += 1
        else:
            i += 1
            continue
        if not macro_def:
            continue
        name, sep, value = macro_def.partition("=")
        if name:
            macros[name] = value if sep else "1"
    return macros


def system_include_paths_from_tokens(tokens: Sequence[str]) -> list[str]:
    """Ordered unique ``-I`` / ``-isystem`` paths in a token sequence.

    Recognizes attached (``-I/p``, ``-isystem/p``) and detached
    (``-I /p``, ``-isystem /p``) forms. First occurrence wins the
    ordering; duplicates are dropped. Unlike
    :func:`extract_include_paths_from_tokens` (a set, ``-I`` only) this
    preserves search order and includes ``-isystem``, because callers
    walk the result looking for the first directory containing a header.
    """
    paths: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok in ("-I", "-isystem"):
            if i + 1 < n:
                paths.append(tokens[i + 1])
                i += 2
            else:
                i += 1
        elif tok.startswith("-isystem") and len(tok) > 8:
            paths.append(tok[8:])
            i += 1
        elif tok.startswith("-I") and len(tok) > 2:
            paths.append(tok[2:])
            i += 1
        else:
            i += 1
    return list(dict.fromkeys(paths))


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
# - -W*: warnings (pure diagnostic; -Werror/-Wa,/-Wp, are the exceptions, see below)
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

# Exceptions to the -W* drop rule:
# - -Werror / -Werror=<warning> promote warnings to errors, which CAN
#   affect the build outcome (compile fails vs succeeds).
# - -Wno-error / -Wno-error=<warning> undo that promotion, so they
#   change build outcome the same way; dropping them aliases
#   "-Werror -Wno-error=X" onto bare "-Werror" and a shared cas-objdir
#   hit skips a compile that should have failed.
# - -Wa,<flags> reaches the assembler and -Wp,<flags> reaches the
#   preprocessor (-Wp,-DFOO genuinely defines FOO); both change object
#   bytes, so dropping them would alias distinct compiles onto one CAS
#   key — a silent miscompile, since cas-objdir has no link-time
#   verification. -Wl,<flags> stays dropped: it is inert under -c (the
#   link key hashes LDFLAGS separately).
_HASH_RELEVANT_W_FLAGS: tuple[str, ...] = (
    "-Werror",
    "-Werror=",
    "-Wno-error",
    "-Wa,",
    "-Wp,",
)


def filter_hash_irrelevant_tokens(tokens: Sequence[str]) -> list[str]:
    """Remove tokens that don't affect compiled output from a flag sequence.

    Used by cache-key hashing to elide diagnostic-only flag changes.
    Accepts either a list or tuple. ``-W*`` warnings are dropped
    EXCEPT ``-Werror`` / ``-Werror=...`` / ``-Wno-error`` /
    ``-Wno-error=...`` (can change compile outcome) and ``-Wa,...`` /
    ``-Wp,...`` (assembler/preprocessor pass-throughs that change
    object bytes). Returns a NEW list; input is not mutated.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # Hash-relevant -W exceptions: -Werror(=...) changes compile
        # outcome; -Wa,/-Wp, forward flags to the assembler/preprocessor
        # and change object bytes.
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


def dedup_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """Pair-aware order-preserving dedup over a token sequence.

    Tuple-native form of ``utils.deduplicate_compiler_flags`` for the
    pure build-state stages: same flag-family equality (-I/-isystem/-L/-D,
    attached or detached), first occurrence wins.
    """
    import compiletools.utils

    return tuple(compiletools.utils.deduplicate_compiler_flags(list(tokens)))


# Must stay identical to apptools_canonicalize._PREFIX_MAP_FLAG_PREFIXES
# (drift guard: test_flag_ops.test_prefix_map_stems_match_apptools_canonicalize_prefixes).
_PREFIX_MAP_STEMS = ("-ffile-prefix-map=", "-fdebug-prefix-map=", "-fmacro-prefix-map=", "-fcanon-prefix-map=")


def has_prefix_map_token(tokens) -> bool:
    """True when any token is a *-prefix-map= flag (any of the four
    gcc/clang families)."""
    return any(t.startswith(_PREFIX_MAP_STEMS) for t in tokens)
