"""Path / flag cache-key canonicalization (leaf module).

Extracted from :mod:`compiletools.apptools` as a behavior-preserving facade
split. This module is a leaf: it imports only stdlib plus
:mod:`compiletools.wrappedos` (itself a leaf). It MUST NOT import
``compiletools.apptools`` — doing so would reintroduce the very cycle this
split removes.

The four CAS layers hash *gitroot-relative* paths so that the same source
compiled by two users in differently-named workspaces produces byte-identical
cache keys. This module provides:

* :func:`canonicalize_path_for_cache_key` / :func:`canonicalize_paths_for_cache_key`
  -- single path / path-list, rewriting ``<gitroot>/...`` to the literal
  ``<GITROOT>`` sentinel (hash-stable).
* :func:`canonicalize_for_cache_key` -- token-list variant that parses
  path-bearing flag families (``-I``, ``-isystem``, ``-Wl,...``, ``-Xlinker``,
  ``-f{file,debug,macro,canon}-prefix-map=``).
* :func:`canonicalize_path_for_command` / :func:`canonicalize_for_command`
  -- sister functions that substitute a configurable *target* (typically
  ``.``) in place of the sentinel, for the actual emitted argv.

See top-level CLAUDE.md "Path-canonical CAS keys" for the full rationale.

``apptools.py`` re-exports every public name here by binding so its existing
``apptools.<name>`` call sites and test/patch targets keep working with
identical object identity.
"""

from collections.abc import Sequence

import compiletools.wrappedos

# Path-bearing flag families recognized by the cache-key canonicalizer.
# Both attached form (``-Ipath``) and detached form (``-I path``, two
# tokens) are handled. Order is significance-aware: longer prefixes must
# come before shorter prefixes that would otherwise eat them
# (``-include-pch`` before ``-include``, ``-isystem`` before ``-I``).
_PATH_BEARING_FLAGS: tuple[str, ...] = (
    "-include-pch",
    "-isystem",
    "-idirafter",
    "-iquote",
    "-include",
    "-I",
    "-L",
    "-F",
    "-B",
)

# Sentinel that replaces the workspace root in canonicalized hash inputs.
# Idempotent by construction: once present in a token, the rewriter
# leaves it alone (anchor paths never start with ``<``).
_GITROOT_SENTINEL = "<GITROOT>"


# Prefix-map flag families. Each takes ``OLD=NEW`` syntax and rewrites
# paths the compiler emits (debug info, ``__FILE__``, ``.d`` output,
# etc.). Round 3 auto-injects ``-ffile-prefix-map`` for cross-user CAS
# sharing unless the user has already set any of these (their choice
# wins, per CXXFLAGS / CFLAGS slot independently).
#
# Trailing ``=`` is part of the prefix to keep the substring search
# tight — a bare ``-ffile-prefix-map`` (no equals, malformed) is not
# a recognized prefix-map flag.
_PREFIX_MAP_FLAG_PREFIXES: tuple[str, ...] = (
    "-ffile-prefix-map=",
    "-fdebug-prefix-map=",
    "-fmacro-prefix-map=",
    "-fcanon-prefix-map=",
)


def _canonicalize_one_path_to_target(path: str, anchor_prefix: str, target: str) -> str:
    """Replace anchor_prefix with `target` if `path` is anchor-rooted.

    `anchor_prefix` is the anchor with a trailing slash already attached.
    The exact-match case (path == anchor without slash) is handled by the
    caller. When `target == _GITROOT_SENTINEL` the rewrite is idempotent:
    paths already containing the sentinel pass through unchanged. For
    non-sentinel targets (e.g. ``.``) idempotency falls out for free
    because once rewritten the path no longer starts with anchor_prefix.

    Round 3: this is the shared core of both
    :func:`_canonicalize_one_path` (cache-key flavour, target=sentinel)
    and :func:`canonicalize_path_for_command` (emitted-command flavour,
    target configurable).

    Cache-key flavour additionally collapses ``..`` segments, redundant
    separators, and ``./`` prefixes via :func:`compiletools.wrappedos.normpath`
    so that textually distinct but semantically identical paths
    (``<GITROOT>/lib/../src/include`` vs ``<GITROOT>/src/include``)
    produce the same cache key. Emitted-command flavour skips normpath because
    lexical ``..`` collapse changes what the compiler resolves through
    symlinked intermediates (``a/../b`` ≠ ``b`` when ``a`` is a symlink),
    and emitted commands feed gcc's actual ``open()`` calls rather than a
    hash. See top-level CLAUDE.md "Path-canonical CAS keys" for the
    cache-side rationale.
    """
    if target == _GITROOT_SENTINEL and _GITROOT_SENTINEL in path:
        return compiletools.wrappedos.normpath(path)
    if path.startswith(anchor_prefix):
        rewritten = target + "/" + path[len(anchor_prefix) :]
        if target == _GITROOT_SENTINEL:
            return compiletools.wrappedos.normpath(rewritten)
        return rewritten
    return path


def _canonicalize_one_path(path: str, anchor_prefix: str) -> str:
    """Replace anchor_prefix with _GITROOT_SENTINEL if `path` is anchor-rooted.

    `anchor_prefix` is the anchor with a trailing slash already attached.
    The exact-match case (path == anchor without slash) is handled by the
    caller. Idempotent: paths already containing _GITROOT_SENTINEL pass
    through unchanged.

    Thin wrapper around :func:`_canonicalize_one_path_to_target` with
    target fixed to ``_GITROOT_SENTINEL``.
    """
    return _canonicalize_one_path_to_target(path, anchor_prefix, _GITROOT_SENTINEL)


def canonicalize_path_for_cache_key(path: str, anchor_root: str) -> str:
    """Rewrite `path` to be anchor-relative for stable cache-key hashing.

    If `path` is exactly `anchor_root` or lives under it, the
    anchor portion is replaced with the literal `<GITROOT>` sentinel.
    Anything outside the anchor (system headers, sibling repos) and
    anything already containing the sentinel passes through unchanged.

    `anchor_root="" ` (or any falsy anchor) is the identity function —
    graceful no-op when gitroot can't be resolved.

    Hash-input only: callers must NOT pass canonicalized paths to the
    actual compile command. For emitted-command rewriting, see
    :func:`canonicalize_path_for_command`.
    """
    if not anchor_root:
        return path
    anchor = anchor_root.rstrip("/")
    if path == anchor:
        return _GITROOT_SENTINEL
    return _canonicalize_one_path(path, anchor + "/")


def canonicalize_path_for_command(path: str, anchor_root: str, *, target: str) -> str:
    """Rewrite `path` to be anchor-relative, substituting *target* in place
    of the anchor.

    Sister of :func:`canonicalize_path_for_cache_key`. The cache-key
    version uses the ``<GITROOT>`` sentinel (hash-stable across users);
    the command version uses a configurable target (typically ``.``)
    so the rewritten path is what the compiler / linker actually sees.

    Use for the actual emitted argv (compile / link / ar) so absolute
    paths rooted at the workspace become target-prefixed in the bytes
    those tools write (debug info, RPATHs, version-script paths). The
    cache key continues to use :func:`canonicalize_path_for_cache_key`
    so two users get the same hash regardless of their workspace
    location.

    `anchor_root="" ` (or any falsy anchor) is the identity function —
    graceful no-op when gitroot can't be resolved.
    """
    if not anchor_root:
        return path
    anchor = anchor_root.rstrip("/")
    if path == anchor:
        return target
    return _canonicalize_one_path_to_target(path, anchor + "/", target)


def canonicalize_paths_for_cache_key(paths: Sequence[str], anchor_root: str) -> list[str]:
    """Apply :func:`canonicalize_path_for_cache_key` element-wise.

    For raw path lists (argv slots, object/library file lists) where every
    element is a path. Distinct from :func:`canonicalize_for_cache_key`,
    which parses path-bearing flags (``-I``, ``-Wl,...``, ``-Xlinker``).
    Empty anchor short-circuits to a list copy.
    """
    if not anchor_root:
        return list(paths)
    return [canonicalize_path_for_cache_key(p, anchor_root) for p in paths]


def _canonicalize_tokens_to_target(tokens: Sequence[str], anchor_root: str, target: str) -> list[str]:
    """Shared core of :func:`canonicalize_for_cache_key` and
    :func:`canonicalize_for_command`.

    Walks `tokens` recognizing path-bearing flag families (-I, -isystem,
    -idirafter, -iquote, -include, -include-pch, -F, -B, -Wl,...,
    -Xlinker, -f{file,debug,macro,canon}-prefix-map=) and substitutes
    *target* in place of the anchor in the path portion of each.

    target == ``<GITROOT>`` produces the hash-stable form (cache keys).
    target == ``.`` (or another configured string) produces the actual
    emitted form (compile / link / ar argv).

    `anchor_root="" ` is the identity. Returns a NEW list; input not
    mutated.
    """
    if not anchor_root:
        return list(tokens)
    anchor = anchor_root.rstrip("/")
    anchor_prefix = anchor + "/"

    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # Round 3: ``-f{file,debug,macro,canon}-prefix-map=OLD=NEW`` —
        # the path-shaped LHS (OLD) is canonicalised; NEW (the rewrite
        # target the compiler will use) is preserved verbatim. Without
        # this, the auto-injected ``-ffile-prefix-map=<gitroot>=.``
        # token would carry per-user absolute paths into the cache key
        # and defeat cross-user CAS sharing.
        prefix_map_handled = False
        for prefix in _PREFIX_MAP_FLAG_PREFIXES:
            if not tok.startswith(prefix):
                continue
            rest = tok[len(prefix) :]
            if "=" not in rest:
                # Malformed (no inner '='): pass through unchanged
                # rather than guess the user's intent.
                break
            old, _, new = rest.partition("=")
            if old == anchor:
                out.append(f"{prefix}{target}={new}")
            elif old.startswith(anchor_prefix):
                relative = old[len(anchor_prefix) :]
                out.append(f"{prefix}{target}/{relative}={new}")
            else:
                # OLD lives outside the anchor: pass through. The user
                # explicitly mapped a non-workspace path; we don't
                # touch it.
                out.append(tok)
            i += 1
            prefix_map_handled = True
            break
        if prefix_map_handled:
            continue
        # ``-Wl,opt[=value][,opt2,/abs/path,...]`` — passes args to the
        # linker. Split on comma, canonicalise each path-shaped segment.
        # Without this, an rpath or version-script absolute path leaks
        # the workspace prefix into the link command_hash and trace
        # verify fails across workspaces (I3).
        if tok.startswith("-Wl,") and len(tok) > 4:
            parts = tok.split(",")
            # parts[0] is "-Wl"; parts[1:] are linker options/values.
            rewritten_parts = [parts[0]]
            for p in parts[1:]:
                if "=" in p:
                    opt, _, val = p.partition("=")
                    if val == anchor:
                        rewritten_parts.append(f"{opt}={target}")
                    elif val.startswith("/"):
                        rewritten_parts.append(f"{opt}={_canonicalize_one_path_to_target(val, anchor_prefix, target)}")
                    else:
                        rewritten_parts.append(p)
                elif p == anchor:
                    rewritten_parts.append(target)
                elif p.startswith("/"):
                    rewritten_parts.append(_canonicalize_one_path_to_target(p, anchor_prefix, target))
                else:
                    rewritten_parts.append(p)
            out.append(",".join(rewritten_parts))
            i += 1
            continue
        # ``-Xlinker /abs/path`` (two-token form). Pass through ``-Xlinker``
        # and canonicalise the next token if it looks like a path. The
        # next token may be a non-path option like ``-rpath`` (which is
        # then itself followed by another ``-Xlinker /path``); pass that
        # through and let the loop catch the next ``-Xlinker /path`` pair.
        if tok == "-Xlinker" and i + 1 < n:
            out.append(tok)
            nxt = tokens[i + 1]
            if nxt == anchor:
                out.append(target)
            elif nxt.startswith("/"):
                out.append(_canonicalize_one_path_to_target(nxt, anchor_prefix, target))
            else:
                out.append(nxt)
            i += 2
            continue
        # Detached form: token is exactly a path-bearing flag, the next
        # token is the path. Consume both.
        if tok in _PATH_BEARING_FLAGS and i + 1 < n:
            out.append(tok)
            path_tok = tokens[i + 1]
            if path_tok == anchor:
                out.append(target)
            else:
                out.append(_canonicalize_one_path_to_target(path_tok, anchor_prefix, target))
            i += 2
            continue
        # Attached form: token starts with a path-bearing flag and the
        # remainder is the path. Match longest-prefix first
        # (_PATH_BEARING_FLAGS is ordered).
        rewritten = None
        for flag in _PATH_BEARING_FLAGS:
            if tok.startswith(flag) and len(tok) > len(flag):
                path_part = tok[len(flag) :]
                if path_part == anchor:
                    rewritten = flag + target
                else:
                    rewritten = flag + _canonicalize_one_path_to_target(path_part, anchor_prefix, target)
                break
        if rewritten is not None:
            out.append(rewritten)
        else:
            out.append(tok)
        i += 1
    return out


def canonicalize_for_cache_key(tokens: Sequence[str], anchor_root: str) -> list[str]:
    """Rewrite path-bearing flag tokens to be anchor-relative.

    For each token, if it parses as a path-bearing flag whose path
    argument is an absolute path under `anchor_root`, replace the path
    portion with the literal token `<GITROOT>/<relpath>`. Both attached
    form (``-I/path``) and detached form (``-I /path``, two tokens)
    are handled.

    Path-bearing flag families recognized: -I -isystem -iquote
    -idirafter -F -B -include -include-pch -Wl,... -Xlinker
    -f{file,debug,macro,canon}-prefix-map=.

    Anything else passes through unchanged: paths outside `anchor_root`,
    non-path flags (``-O2``, ``-std=c++20``, ``-DFOO``), already-relative
    paths, and tokens already containing the `<GITROOT>` sentinel
    (idempotent — applying twice is a no-op).

    `anchor_root="" ` (or any falsy anchor) is the identity function —
    graceful no-op when gitroot can't be resolved.

    Returns a NEW list; input is not mutated. Hash-input only — for
    emitted-command rewriting, see :func:`canonicalize_for_command`.
    """
    return _canonicalize_tokens_to_target(tokens, anchor_root, _GITROOT_SENTINEL)


def canonicalize_for_command(tokens: Sequence[str], anchor_root: str, *, target: str) -> list[str]:
    """Sister of :func:`canonicalize_for_cache_key`. Substitutes *target*
    in place of the ``<GITROOT>`` sentinel.

    Use for the actual emitted argv (compile / link / ar) so absolute
    paths rooted at the workspace become target-prefixed paths in the
    bytes the compiler / linker writes (debug info, RPATHs,
    version-script paths). The cache key continues to use
    :func:`canonicalize_for_cache_key` so two users get the same hash
    regardless of their workspace location.

    *target* of ``.`` matches the Debian fixfilepath convention;
    ``/__ct__`` or similar absolute sentinels work better with VSCode
    sourceFileMap. Same flag families recognized as
    :func:`canonicalize_for_cache_key`.
    """
    return _canonicalize_tokens_to_target(tokens, anchor_root, target)
