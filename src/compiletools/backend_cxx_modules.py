"""C++20-module free-function helpers for the build backends.

These free functions implement the filename/flag/cache-key plumbing the
concrete BuildBackend subclasses need for C++20 named modules and header
units: make-safe ``.pcm``/``.gcm`` filenames, header-unit token parsing,
the cas-pcmdir command hash and its sidecar manifest, and the gcc
header-unit system-header resolution probe.

This module is a deliberately thin lower layer: it imports only stdlib
plus genuinely-leaf compiletools modules (``apptools``, ``wrappedos``,
``global_hash_registry`` -- all already below ``build_backend``) so that
``build_backend`` can re-export these names without creating an import
cycle. ``build_backend`` binds them back into its own namespace,
preserving object identity for both call sites inside ``BuildBackend``
and ``unittest.mock.patch`` targets.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess

import compiletools.apptools
import compiletools.global_hash_registry
import compiletools.wrappedos

# Backward-compat alias mirroring ``build_backend._compiler_identity``:
# ``compiler_identity`` lives in ``compiletools.apptools`` (shared with
# ``preprocessing_cache`` for the per-TU object cache key). The moved PCM
# helpers reference it through this module's own namespace; object identity
# is preserved at the ``apptools`` level so a patch on
# ``compiletools.apptools.compiler_identity`` is seen by both call sites.
_compiler_identity = compiletools.apptools.compiler_identity

# Sentinel string used to escape characters that are unsafe in make
# targets / filesystem paths (``:`` for module partition separator,
# ``/`` for header-name path separator). ``^^`` is chosen because:
#   - underscore (``_``) collides: identifiers and headers commonly
#     contain it, so escaping ``/`` to ``_`` could make ``<sys/socket.h>``
#     and a real ``<sys_socket.h>`` map to the same filename;
#   - hyphen (``-``) is technically allowed in some module-name proposals
#     and can also appear in real header filenames;
#   - ``^^`` does not appear in any C identifier, any module-name token
#     (which is a dotted ``[A-Za-z_][A-Za-z0-9_]*``), or any reasonable
#     header path, so collisions are vanishingly unlikely;
#   - ``^`` has no special meaning to make outside the ``$^`` automatic
#     variable (which requires the ``$``), so doubled ``^^`` is safe.
_NAME_ESCAPE = "^^"


def _module_pcm_filename(module_name: str) -> str:
    """Return a make-safe ``.pcm`` filename for a possibly-partitioned module.

    ``:`` is illegal in a Makefile target (make parses ``a:b`` as ``a``
    depends on ``b``), so we map the partition separator to ``^^`` for
    the on-disk filename. The clang ``-fmodule-file=NAME=PATH`` flag
    uses the real (colon-bearing) module name on the lookup side, so
    the filename is purely a storage detail. See ``_NAME_ESCAPE`` for
    why ``^^`` rather than ``-`` / ``_``.
    """
    return module_name.replace(":", _NAME_ESCAPE) + ".pcm"


def _header_unit_arg(token: str) -> str:
    """Strip the surrounding ``<...>`` or ``"..."`` from a header-unit token.

    The bare header name is what gcc's ``-x c++-system-header`` and
    clang's ``-xc++-system-header`` expect as the source argument.
    Anything else (callers should validate upstream that the token is a
    well-formed header reference) passes through unchanged.
    """
    if len(token) >= 2 and ((token[0], token[-1]) in (("<", ">"), ('"', '"'))):
        return token[1:-1]
    return token


def _header_unit_safe_stem(token: str) -> str:
    """Return a filesystem/make-safe stem for a header-unit token.

    Escape both ``/`` (path separator in nested system headers like
    ``<sys/socket.h>``) and ``:`` (which a make target parser would
    misread) to ``^^``. ``<vector>`` -> ``vector``;
    ``<sys/socket.h>`` -> ``sys^^socket.h``. See ``_NAME_ESCAPE`` for
    the rationale (we deliberately don't use ``_`` or ``-`` since
    those characters legitimately appear in real header filenames and
    would alias different headers to the same on-disk name).
    """
    bare = _header_unit_arg(token)
    return bare.replace("/", _NAME_ESCAPE).replace(":", _NAME_ESCAPE)


# Detached system-include flag families: each occupies two tokens
# (``-isystem`` ``path``). Attached forms (``-isystempath``) are detected
# by prefix match and travel as a single token.
#
# Deliberately excludes ``-I`` and ``-iquote`` (project search paths):
# the header-unit cache key uses a single command_hash with no content
# fold-in, so anything reachable through these flags would silently
# stale-cache on edit. The contract is "header units must be routed
# through a system-include flag" -- documented in src/compiletools/CLAUDE.md
# under "header-unit -isystem immutability contract".
#
# The list is ordered longest-prefix-first so ``-isystem`` isn't
# misclassified as a shorter prefix during attached-form matching.
# Each entry is verified non-prefix of every other.
_SYSTEM_INCLUDE_FLAG_FAMILIES: tuple[str, ...] = (
    "-iframework",
    "-idirafter",
    "--sysroot",
    "-isysroot",
    "-isystem",
)

# Reserved for future families that *only* support an attached form
# (no detached two-token spelling). Currently empty:
#
# * ``--sysroot=/path`` is handled by the attached-prefix loop over
#   :data:`_SYSTEM_INCLUDE_FLAG_FAMILIES` matching the ``--sysroot``
#   base spelling, since the prefix loop accepts any token longer than
#   the bare flag. ``--sysroot /path`` (detached) is handled by the
#   same families tuple via exact-equality match.
#
# Kept as a named tuple so future additions land in one place rather
# than threading through a new flag axis.
_SYSTEM_INCLUDE_ATTACHED_ONLY_PREFIXES: tuple[str, ...] = ()


def _extract_system_include_path_flags(tokens: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return the subset of *tokens* that points at a system-include search path.

    Keeps the families in :data:`_SYSTEM_INCLUDE_FLAG_FAMILIES`
    (``-isystem`` / ``-iframework`` / ``-idirafter`` / ``-isysroot``) in
    both detached and attached forms, plus attached-only forms in
    :data:`_SYSTEM_INCLUDE_ATTACHED_ONLY_PREFIXES` (``--sysroot=``).
    Everything else -- including project search paths ``-I`` and
    ``-iquote`` -- is dropped. Order is preserved so the compiler's
    search precedence is unchanged.

    **The -isystem immutability contract.** The header-unit cache key
    (``_pcm_command_hash``) folds in the compiler identity and the
    user's CXXFLAGS but does NOT fold in the resolved header's content
    hash. That is sound for system headers (compiler-shipped or
    user-routed through ``-isystem``) by convention: the user is
    declaring "these inputs do not mutate between builds". A header
    reached via ``-I`` would break this invariant -- editing it would
    leave the cached ``.gcm`` stale, and gcc's consume-time BMI check
    is flag-aware, not content-aware. Restricting resolution to
    system-include families makes the contract enforceable: a header
    that isn't on a system-include path simply can't become a header
    unit, and ``import <h>;`` against an ``-I``-only header fails the
    same way as a non-existent header.

    Malformed detached flags are silently dropped: a bare trailing
    ``-isystem`` (no path), or a detached flag whose ``next`` token
    starts with ``-`` (the flag stream skipped its path), are both
    treated as no-ops rather than raising. Production callers feed
    flag tuples that have already been through ``apptools.parseargs``
    so this is a defensive guard, not the common path.

    The probe in :func:`_resolve_system_header_abs_paths` runs the
    compiler in a minimal context; without these flags it cannot resolve
    headers that live behind project-supplied ``-isystem`` paths
    (e.g. ``-isystem ${CONF_DIR}/extlib/include``).
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in _SYSTEM_INCLUDE_FLAG_FAMILIES:
            # Detached form: emit flag + path. Drop the bare flag if the
            # path is missing OR if it looks like another flag (the
            # value should be a path, not ``-Wall``).
            if i + 1 < n and not tokens[i + 1].startswith("-"):
                out.append(t)
                out.append(tokens[i + 1])
                i += 2
                continue
            i += 1
            continue
        # Attached form: -Ipath / -isystempath / etc. Longest-prefix-first
        # via the ordered tuple ensures ``-isystem/p`` doesn't get sliced
        # as ``-I`` + ``system/p``.
        matched = False
        for prefix in _SYSTEM_INCLUDE_FLAG_FAMILIES:
            if t.startswith(prefix) and len(t) > len(prefix):
                out.append(t)
                matched = True
                break
        if matched:
            i += 1
            continue
        # Attached-only prefixes: ``--sysroot=/path``.
        for prefix in _SYSTEM_INCLUDE_ATTACHED_ONLY_PREFIXES:
            if t.startswith(prefix) and len(t) > len(prefix):
                out.append(t)
                break
        i += 1
    return tuple(out)


@functools.lru_cache(maxsize=512)
def _resolve_system_header_abs_paths(
    cxx: str,
    token: str,
    std_flag: str = "-std=c++20",
    include_flags: tuple[str, ...] = (),
) -> list[str]:
    """Resolve a header-unit token to every path the compiler may key it by.

    Used by the gcc cas-pcmdir mapper. gcc keys header-unit lookups by
    the *string form* of the resolved include path -- and that string
    depends on the compiler's flag context. Two cases that produce
    different strings for the same physical header:

    * Default: ``-fcanonical-system-headers`` is on, so gcc reports the
      include path with ``..`` segments collapsed and symlinks resolved.
    * ``-fno-canonical-system-headers``: gcc reports whatever raw search
      path produced the hit -- typically containing ``..`` segments
      (e.g. ``.../gcc/16/bin/../lib/gcc/.../include/vector``) and
      preserving symlinks.

    Bazel's gcc autoconfig appends ``-fno-canonical-system-headers``
    AFTER user ``copts`` / ``--cxxopt``, so the importer compile sees
    the non-canonical form even though our explicit
    ``--cxxopt=-fcanonical-system-headers`` is present. If the mapper
    only carries the canonical key, the importer's lookup misses with
    "unknown compiled module interface". The fix is to emit BOTH
    spellings as mapper keys (both pointing to the same ``.gcm``) so
    the lookup hits regardless of how the consumer's flag set ended up
    canonicalizing.

    ``include_flags`` carries the user's system-include flags --
    ``-isystem`` / ``-isysroot`` / ``-iframework`` / ``-idirafter`` /
    ``--sysroot=`` -- as distilled by
    :func:`_extract_system_include_path_flags`. ``-I`` / ``-iquote``
    are intentionally excluded; see that helper's docstring for the
    immutability contract. Without these flags, a header reachable
    only through a project-supplied ``-isystem`` path (typical for
    pkg-config'd third-party libraries) fails the probe -- the mapper
    then has no entry for the canonical resolved path, and the gcc
    precompile silently misroutes through the global mapper and
    reports the import as ``unknown compiled module interface``.

    Returns a list with the canonical path first (for stability) and
    any additional non-canonical spelling. Duplicates collapsed.
    Empty list when the compiler probe fails -- callers must handle
    this (for the mapper case, omit those entries and gcc will fall
    back to its default ``gcm.cache`` placement, still correct just
    not cached).
    """
    bare = _header_unit_arg(token)
    delim_open, delim_close = ("<", ">") if (token.startswith("<") and token.endswith(">")) else ('"', '"')
    snippet = f"#include {delim_open}{bare}{delim_close}\n"

    def _probe(extra_flags: list[str]) -> str | None:
        try:
            r = subprocess.run(
                [cxx, std_flag, *include_flags, *extra_flags, "-M", "-x", "c++", "-"],
                input=snippet,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if r.returncode != 0:
            return None
        # gcc emits a make-style dep listing: `<obj>: <dep1> <dep2> \\\n  <dep3> ...`
        # Flatten line continuations, split on whitespace, then find a dep
        # whose tail matches the bare header path.
        deps = r.stdout.replace("\\\n", " ").split()
        if deps and deps[0].endswith(":"):
            deps = deps[1:]
        for dep in deps:
            if dep == "-" or dep.endswith("/stdc-predef.h"):
                continue
            if dep.endswith("/" + bare) or os.path.basename(dep) == bare:
                return dep
        return None

    paths: list[str] = []
    seen: set[str] = set()
    # Probe canonical first -- preserves the existing cache key shape and
    # gives the bazel-free build path the same string as before.
    for extra in ([], ["-fno-canonical-system-headers"]):
        p = _probe(extra)
        if p and p not in seen:
            paths.append(p)
            seen.add(p)
    return paths


def _resolve_system_header_abs_path(
    cxx: str,
    token: str,
    std_flag: str = "-std=c++20",
    include_flags: tuple[str, ...] = (),
) -> str | None:
    """Backward-compatible single-path wrapper around
    ``_resolve_system_header_abs_paths``. Returns the canonical
    spelling when both probes succeed, the only spelling when only one
    does, ``None`` when both fail.
    """
    paths = _resolve_system_header_abs_paths(cxx, token, std_flag=std_flag, include_flags=include_flags)
    return paths[0] if paths else None


def _cas_pcm_path(filename_stem: str, pcmdir: str, command_hash: str) -> str:
    """Return the cache path for a clang ``.pcm`` or gcc ``.gcm``.

    Layout: ``<pcmdir>/<command_hash>/<filename_stem>``.

    Mirrors ``cas-pchdir``'s shape: one ``command_hash`` directory per
    unique compile configuration (compiler + flags + source content +
    transitive headers), with the artefact and a sidecar
    ``manifest.json`` inside.

    .. note:: An earlier revision of this file split the ``command_hash``
       into three independent path components (file_hash + dep_hash +
       cmd_hash), mirroring the object cache's
       ``<basename>_<file_hash_12>_<dep_hash_14>_<macro_state_hash_16>.o``
       filename. That refactor was reverted: the object cache predates
       sidecar manifests and uses path-axis separation because
       ``trim_objdir`` reads ``file_hash[:12]`` directly out of the
       filename to do its "is this source still tracked?" check.
       PCM (a) ships with a manifest from day one that already carries
       ``bucket_key`` and ``transitive_hashes``, (b) holds two orders
       of magnitude fewer entries than the object cache so sharding is
       overkill, and (c) has header-unit entries that can't fit the
       (source, deps, env) triple cleanly -- their "source" is a
       compiler-shipped header with no git identity. Single hash +
       manifest is the right shape for PCM.
    """
    return os.path.join(pcmdir, command_hash, filename_stem)


def _pcm_command_hash(
    args,
    source_path: str,
    transitive_content_hash: str,
    cxxflags_tokens: list[str],
    magic_cpp_flags: list,
    magic_cxx_flags: list,
    extra_flags: list[str],
    stage: str,
    *,
    anchor_root: str,
) -> str:
    """Single content-addressable hash for a PCM cache entry.

    Folds every input that affects the BMI bytes into one 16-hex-char
    sha256 truncation: compiler identity, hash-relevant flags, magic
    flags, source identity (path), transitive header content, and a
    stage marker. Identical inputs -> identical hash -> shared cache
    entry. Any drift -> different hash -> different cache path.

    .. note:: 16 hex chars (64 bits) is the right entropy budget for
       PCM. The object cache uses 168 bits across three path
       components because a hash collision on ``.o`` files would cause
       a **silent miscompile** -- the linker doesn't verify object
       contents against the inputs that produced them. PCM and PCH
       have **in-band BMI verification at consume time**: GCC's PCH
       stamp / clang's BMI signature record the compile environment
       and reject on mismatch. A hypothetical 64-bit collision
       therefore degrades to a slow re-precompile, never a
       miscompile. Single-hash + manifest is the right shape; an
       earlier 3-axis refactor mimicking the object cache was
       reverted because it added complexity without addressing a
       safety problem PCM doesn't have.

    ``stage`` (e.g. ``"clang_module_interface"``,
    ``"clang_header_unit"``, ``"gcc_module_interface"``,
    ``"gcc_header_unit"``) prevents a same-named module and header
    unit from colliding under the same flag set.

    ``transitive_content_hash`` is the caller's responsibility to
    compose -- typically ``f"{source_hash}:{dep_hash}"`` for named
    modules and the empty string (or a token-derived value) for header
    units whose transitive deps are implicit in ``compiler_identity``.

    ``cxxflags_tokens`` is the hash-relevant structured form of
    ``args.CXXFLAGS`` -- the caller is responsible for pre-filtering
    via ``args.flags.hash_relevant("cxx")`` (which strips ``-D``/``-U``
    AND drops diagnostic-only flags). This function does NOT re-filter
    that parameter; only the per-file ``magic_cpp_flags`` /
    ``magic_cxx_flags`` (which arrive un-filtered from the magic-flag
    pipeline) are filtered here. Symmetric with ``_pch_command_hash``.
    """
    # Canonicalize path-bearing flag tokens and the source path against
    # the gitroot anchor so the cache key is decoupled from the absolute
    # workspace path -- two CI runs landing under different attempt
    # directories share the same PCM cache entries. ``anchor_root`` is
    # required (matching ``MacroState`` / ``_write_pcm_manifest`` /
    # ``compiler_identity``); an explicit empty string disables
    # canonicalization (graceful no-op for tests / out-of-tree usage).
    canonical = {
        "stage": stage,
        "compiler_identity": _compiler_identity(args.CXX, anchor_root=anchor_root),
        "cxx_command": compiletools.apptools.canonicalize_path_for_cache_key(args.CXX, anchor_root),
        "CXXFLAGS_TOKENS": compiletools.apptools.canonicalize_for_cache_key(list(cxxflags_tokens), anchor_root),
        "magic_cpp_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cpp_flags]),
            anchor_root,
        ),
        "magic_cxx_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cxx_flags]),
            anchor_root,
        ),
        "extra_flags": compiletools.apptools.canonicalize_for_cache_key(list(extra_flags), anchor_root),
        "source": compiletools.apptools.canonicalize_path_for_cache_key(source_path, anchor_root),
        "transitive_content_hash": transitive_content_hash,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def _write_pcm_manifest(
    pcmdir: str,
    cmd_hash: str,
    bucket_key: str,
    transitive_headers: list[str],
    cxx_command: str,
    stage: str,
    context,
    *,
    anchor_root: str,
) -> None:
    """Write a sidecar manifest next to a cached ``.pcm`` / ``.gcm`` file.

    Layout matches ``_write_pch_manifest``: the manifest lands at
    ``<pcmdir>/<cmd_hash>/manifest.json``, alongside the artefact.

    Enables ``trim_cache.trim_pcmdir`` to (a) bucket cmd_hash dirs by
    ``bucket_key`` so cross-variant builds of the same source/header
    don't evict each other at ``keep_count=1`` (source realpath for
    named modules, verbatim token like ``<vector>`` for header units),
    and (b) pre-evict entries whose transitive header content has
    shifted since the artefact was built.

    ``stage`` is the same string handed to ``_pcm_command_hash`` so the
    trim CLI can reason about which compiler-stage produced the entry.

    Atomic via ``os.replace``.
    """
    manifest_dir = os.path.join(pcmdir, cmd_hash)
    os.makedirs(manifest_dir, exist_ok=True)

    transitive_hashes: dict[str, str] = {}
    for h in transitive_headers:
        h_real = compiletools.wrappedos.realpath(h)
        try:
            transitive_hashes[h_real] = compiletools.global_hash_registry.get_file_hash(h_real, context=context)
        except (OSError, KeyError):
            pass

    manifest = {
        "bucket_key": bucket_key,
        "stage": stage,
        "compiler": cxx_command,
        "compiler_identity": _compiler_identity(cxx_command, anchor_root=anchor_root),
        "transitive_hashes": transitive_hashes,
    }

    manifest_path = os.path.join(manifest_dir, "manifest.json")
    tmp_path = f"{manifest_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, sort_keys=True)
    os.replace(tmp_path, manifest_path)
