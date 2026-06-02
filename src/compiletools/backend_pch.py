"""Precompiled-header (PCH) free-function helpers for the build backends.

These free functions implement the filename/flag/cache-key plumbing the
concrete BuildBackend subclasses need for GCC/clang precompiled headers
(``.gch``): the cas-pchdir command hash and its per-PCH scope-macro hash,
the sidecar manifest + scope diagnostics writers, the ``-include``-path
gch resolver, the cross-user-safety warning, and the source-header
staging hardlink (with EXDEV copy fallback) that lets gcc fall back to
the bare ``.h`` when a cached ``.gch`` is invalidated at consume time.

This module is a deliberately thin lower layer: it imports only stdlib
plus genuinely-leaf compiletools modules (``apptools``, ``wrappedos``,
``global_hash_registry``, ``diagnostics`` -- all already below
``build_backend``) so that ``build_backend`` can re-export these names
without creating an import cycle. ``build_backend`` binds them back into
its own namespace, preserving object identity for both call sites inside
``BuildBackend`` and ``unittest.mock.patch`` targets.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

import compiletools.apptools
import compiletools.diagnostics
import compiletools.global_hash_registry
import compiletools.wrappedos

# Backward-compat alias mirroring ``build_backend._compiler_identity``:
# ``compiler_identity`` was promoted to ``compiletools.apptools`` so it
# can be shared with ``preprocessing_cache`` (per-TU object cache key).
# The moved PCH helpers reference it through this module's own namespace;
# object identity is preserved at the ``apptools`` level so a patch on
# ``compiletools.apptools.compiler_identity`` is seen by both call sites.
_compiler_identity = compiletools.apptools.compiler_identity


def _stage_pch_header_alongside_gch(source_header: str, staged_path: str) -> None:
    """Place a copy of *source_header* at *staged_path* so the consumer's
    ``-include <staged_path>`` directive resolves to a real file on disk
    even when GCC has to fall back from the cached ``.gch``.

    Background. The consumer compile uses ``-include <cache>/<basename>``
    (NOT ``-I <cache>``) to force GCC to load the cached precompiled
    header — see the rationale comment in ``BuildBackend._create_compile_rule``.
    GCC's ``-include`` semantics are: try ``<path>.gch`` first; if it
    matches and validates, use it; otherwise open ``<path>`` itself as
    a regular header. The fallback path is rare (compiler upgrades
    that don't change ``compiler_identity``, or backends like Bazel
    whose ``rules_cc`` injects flags the PCH wasn't built with), but
    when it fires the bare header MUST exist at the cache path or
    GCC reports ``No such file or directory`` and the build aborts.

    Mechanism. Try ``os.link`` first — atomic, zero disk cost (one
    inode shared with the original), survives concurrent stagings.
    Fall back to ``shutil.copy2`` on ``EXDEV`` (cross-filesystem
    cache) or any other ``OSError``. Idempotent: a successful
    staging from a peer ct-cake invocation is treated as success.

    Cleanup. ``ct-trim-cache`` already evicts entries by hash-dir,
    so the staged ``.h`` is reaped together with its sibling ``.gch``
    and ``manifest.json``.
    """
    if os.path.lexists(staged_path):
        # A peer staging won the race, or this same invocation already
        # ran (this codepath fires per-PCH-per-build_graph call).
        return
    if not os.path.exists(source_header):
        # No source on disk to stage. In production this means
        # headerdeps would have already raised; here we silently
        # no-op so unit tests with mocked hunters that pass synthetic
        # paths don't crash. The downstream consumer compile will
        # report a clear error if the bare .h is ever needed.
        return
    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
    try:
        os.link(source_header, staged_path)
        return
    except FileExistsError:
        return  # Lost a race; the file is now staged.
    except (OSError, AttributeError):
        # EXDEV (cross-FS), EPERM (no link permission), or AttributeError
        # on platforms without os.link (e.g. Termux/Android). Fall through.
        pass
    # Copy fallback. Use a temp + atomic rename so a concurrent reader
    # never sees a partial file.
    tmp_path = f"{staged_path}.staging.{os.getpid()}"
    try:
        shutil.copy2(source_header, tmp_path)
        os.replace(tmp_path, staged_path)
    except FileExistsError:
        # Lost a race during rename; clean up the temp.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _is_under(path: str, anchor: str) -> bool:
    """True iff ``path`` lies inside ``anchor`` (or equals it).

    Uses ``os.path.commonpath`` rather than string-prefix comparison so
    sibling-prefix cases (e.g. ``/tmp/foo`` vs ``/tmp/foo-other``) don't
    falsely match. Empty/missing inputs return False — callers gate on
    a non-empty anchor before relativizing.
    """
    if not anchor or not path:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(anchor)]) == os.path.abspath(anchor)
    except ValueError:
        # Different drives on Windows; can't be "under".
        return False


def _gch_path(header: str, pchdir: str | None = None, command_hash: str | None = None) -> str:
    """Return the precompiled header output path for a header file.

    When *pchdir* and *command_hash* are provided the .gch is placed under
    ``<pchdir>/<command_hash>/<basename>.gch`` so that GCC can find it via
    ``-include <pchdir>/<command_hash>/<basename>``.  Otherwise falls back
    to the legacy ``header.gch`` path next to the header.
    """
    if pchdir and command_hash:
        return os.path.join(pchdir, command_hash, os.path.basename(header) + ".gch")
    return header + ".gch"


_PCHDIR_WARNED: set[str] = set()


def _warn_if_pchdir_not_cross_user_safe(pchdir: str, verbose: int) -> None:
    """Emit a one-time warning if pchdir's parent isn't group-writable + SGID.

    The PCH CAS is intended to be readable across users:
    user A creates ``<pchdir>/<cmd_hash>/stdafx.h.gch``, user B should be
    able to consume it. With a default ``umask 0077`` and no SGID on the
    parent, A's directory is mode ``0700`` and B silently re-builds the
    PCH every time. Warn early so the operator can fix the parent dir
    permissions (typically ``chmod 2775`` + ``chgrp <build-group>``).

    The warning is one-time per (pchdir) per process to avoid spam in
    multi-target builds.
    """
    if pchdir in _PCHDIR_WARNED:
        return
    _PCHDIR_WARNED.add(pchdir)

    # Skip the warning when pchdir is a per-user path (cwd-relative
    # or under the build's bin tree). The cross-user-safety guidance only
    # applies to genuinely shared cache locations.
    abs_pchdir = os.path.abspath(pchdir)
    cwd = os.path.abspath(os.getcwd())
    if abs_pchdir == cwd or abs_pchdir.startswith(cwd + os.sep):
        return

    parent = os.path.dirname(os.path.abspath(pchdir)) or "."
    target = pchdir if os.path.isdir(pchdir) else parent
    try:
        st = os.stat(target)
    except OSError:
        return  # No parent yet, mkdir will create it; nothing useful to warn about

    mode = st.st_mode
    issues = []
    # Group-write needed so user B can create new <cmd_hash>/ subdirs.
    if not (mode & 0o020):
        issues.append("not group-writable (need at least mode 2775)")
    # SGID needed so children inherit the parent's group, not the creator's
    # primary group.
    if not (mode & 0o2000):
        issues.append("missing SGID bit (chmod g+s)")

    if issues and verbose >= 1:
        joined = "; ".join(issues)
        print(
            f"WARNING: PCH CAS {target!r} is {joined}. "
            "Cross-user PCH cache hits will silently miss. Fix with: "
            f"chmod 2775 {target!r} && chgrp <build-group> {target!r}",
            file=sys.stderr,
        )


def _pch_command_hash(
    args,
    pch_header: str,
    magic_cpp_flags: list,
    magic_cxx_flags: list,
    cxxflags_tokens: list[str],
    scope_macro_hash: str,
    *,
    anchor_root: str,
) -> str:
    """Compute a content-addressable hash for a PCH compile command.

    The hash captures compiler identity (binary realpath + size + mtime,
    not just the user-supplied command name), all flags, and the realpath
    of the header so that different compilers / flags / headers produce
    distinct cache entries while identical configurations share a single
    .gch file. Uses ``json.dumps`` rather than space-join so flag values
    containing literal spaces (``-DFOO="a b"``) cannot collide with
    space-separated flag pairs.

    .. note:: PCH (and PCM, the C++20 modules cache it inspired)
       intentionally use a **single** command_hash directory plus
       sidecar manifest, not the object cache's three path-axis
       hashes. The 3-axis structure on the object cache exists
       because ``.o`` files have no in-band verification at link
       time -- a hash collision would cause a silent miscompile, so
       the path needs the entropy of multiple independent hashes to
       make collisions statistically impossible. PCH and PCM have
       BMI / PCH-stamp verification at consume time: a hypothetical
       64-bit collision degrades to a slow re-precompile, never a
       miscompile, so the lower-entropy single-hash key is safe and
       simpler. (An earlier exploration of refactoring PCM to the
       3-axis layout was reverted for this exact reason.)

    ``cxxflags_tokens`` is the hash-relevant structured form of
    ``args.CXXFLAGS`` -- the caller is responsible for pre-filtering
    via ``args.flags.hash_relevant("cxx")`` (which strips ``-D``/``-U``
    AND drops diagnostic-only flags). This function does NOT re-filter
    that parameter; only the per-file ``magic_cpp_flags`` /
    ``magic_cxx_flags`` (which arrive un-filtered from the magic-flag
    pipeline) are filtered here. The cmdline ``-D`` macros relevant to
    this PCH header are folded in via ``scope_macro_hash`` (see
    :func:`_pch_scope_macro_hash`), so two apps that differ only in an
    irrelevant ``-DAPP_NAME=...`` value share the same PCH cache key.
    """
    # 64 bits (16 hex chars) of SHA-256 — birthday-collision risk at
    # ~4 billion entries, fine in practice. PCH cache validity is also
    # guarded by GCC's PCH stamp at consume time, so a hash collision
    # would only cause a slow rebuild, not a miscompile.
    # The cache key hashes only the immediate header's realpath, but
    # transitive-header content hashes are recorded in the sidecar
    # manifest written by ``_write_pch_manifest``. ``trim_cache.trim_pchdir``
    # reads those hashes and pre-evicts entries whose transitive headers
    # have changed, so the slow ``cc1`` PCH-stamp rebuild is avoided in
    # the cross-user-mixed-content case.
    # Diagnostic-only flags (warnings, message formatting, -pipe, -v...)
    # never affect the compiled .gch bytes. Filter them out of every
    # flag-token list so flipping -Wall <-> -Wextra (or annotating a
    # header with //#CXXFLAGS=-Wall) doesn't pollute the PCH cache key.
    #
    # Path-bearing flag tokens (-I/-isystem/etc.) and the header path
    # itself are then canonicalized against the gitroot anchor so the
    # cache key is decoupled from the absolute workspace path -- two
    # CI runs landing under different attempt directories share the
    # same PCH cache entries. ``anchor_root`` is required (matching
    # ``MacroState`` / ``_write_pch_manifest`` / ``compiler_identity``);
    # an explicit empty string disables canonicalization (graceful
    # no-op for tests / out-of-tree usage).
    canonical = {
        # In-workspace wrapper scripts (coverage / sccache / distcc) leak
        # the per-checkout absolute path through both fields otherwise.
        "compiler_identity": _compiler_identity(args.CXX, anchor_root=anchor_root),
        "cxx_command": compiletools.apptools.canonicalize_path_for_cache_key(args.CXX, anchor_root),
        # Structured tokens with -D/-U stripped AND diagnostic-only flags
        # removed; pre-filtered by caller via args.flags.hash_relevant("cxx").
        # Cmdline -D macros are captured by ``scope_macro_hash`` after
        # per-PCH-header scoping.
        "CXXFLAGS_TOKENS": compiletools.apptools.canonicalize_for_cache_key(list(cxxflags_tokens), anchor_root),
        "magic_cpp_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cpp_flags]),
            anchor_root,
        ),
        "magic_cxx_flags": compiletools.apptools.canonicalize_for_cache_key(
            compiletools.apptools.filter_hash_irrelevant_tokens([str(f) for f in magic_cxx_flags]),
            anchor_root,
        ),
        "header": compiletools.apptools.canonicalize_path_for_cache_key(
            compiletools.wrappedos.realpath(pch_header), anchor_root
        ),
        "stage": "c++-header",
        "scope_macro_hash": scope_macro_hash,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


def _pch_scope_macro_hash(hunter, pch_header: str) -> str:
    """Hash the cmdline ``-D`` macros relevant to a single PCH header.

    Mirrors the per-TU scope-filter logic in
    :meth:`compiletools.hunter.Hunter.macro_state_hash`, but for PCH
    cache keys. Only cmdline-D macros that the PCH header (or any of
    its transitive headers) references as identifiers are folded in.
    Compiler builtins are not included -- they're already captured by
    ``compiler_identity`` in :func:`_pch_command_hash`.

    Returns 16 hex chars of sha256 over a sorted, deterministic
    (name, value) pair list. Returns ``"0" * 16`` when:

    * ``cmdline_origin`` is empty (no ``--append-*FLAGS=-D...`` at all), or
    * No cmdline-D macro is referenced by this PCH header.

    The all-zeros sentinel is intentional -- it makes "no scoping
    applied" visible in the canonical dict rather than masking it as a
    sha256 of an empty list.
    """
    cmdline_origin = hunter.magicparser._initial_macro_state.cmdline_origin
    if not cmdline_origin:
        return "0" * 16

    pch_content_hash = compiletools.global_hash_registry.get_file_hash(pch_header, hunter.context)
    transitive = hunter._transitive_content_hashes(pch_header)
    # Hunter has no Namer attached, so derive a stable dep_hash from
    # the sorted transitive content hashes directly. The exact value
    # doesn't matter -- it only needs to be content-addressed and
    # stable so CmdlineMacroIndex's per-TU scope cache stays coherent.
    dep_hash = hashlib.sha256("\n".join(sorted(transitive)).encode()).hexdigest()[:14]

    scope_filter = hunter._get_cmdline_macro_index().tu_referenced_macros(
        tu_filename=pch_header,
        tu_content_hash=pch_content_hash,
        dep_hash=dep_hash,
        transitive_content_hashes=transitive,
    )

    _write_pch_scope_diagnostic(hunter.args, pch_header, cmdline_origin, scope_filter)

    if not scope_filter:
        return "0" * 16

    core = hunter.magicparser._initial_macro_state.core
    pairs = sorted((str(name), str(core[name])) for name in scope_filter if name in core)
    if not pairs:
        return "0" * 16
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def _write_pch_scope_diagnostic(
    args,
    pch_header: str,
    cmdline_origin: frozenset,
    scope_filter: frozenset,
) -> None:
    """Write per-PCH scope diagnostics JSON when --scope-diagnostics is on.

    File path: ``<diagnostics_dir>/scope/pch/<basename>.json``

    Why no dep_hash in the filename: the PCH cache itself is keyed by
    cmd_hash; one PCH header in one invocation has one canonical scope
    decision. (Multiple variant builds in one invocation would share
    a process and one diagnostics dir, but get distinct cmd_hashes via
    the regular PCH cache.) If we ever observe collisions in practice
    we can extend with a discriminator.

    Mirrors :meth:`compiletools.hunter.Hunter._write_scope_diagnostic`,
    but for PCH cache keys. Silently no-ops when no diagnostics dir is
    resolvable -- callers without ``--diagnostics-dir`` or ``--bindir``
    set must not crash.
    """
    if not getattr(args, "scope_diagnostics", False):
        return

    try:
        diagnostics_dir = compiletools.diagnostics.resolve_diagnostics_dir(args)
    except RuntimeError:
        return  # No diagnostics dir resolvable -- silently skip

    scope_dir = os.path.join(diagnostics_dir, "scope", "pch")
    os.makedirs(scope_dir, exist_ok=True)

    excluded = sorted(str(n) for n in cmdline_origin if n not in scope_filter)
    included = sorted(str(n) for n in scope_filter if n in cmdline_origin)

    payload = {
        "pch_header": pch_header,
        "cmdline_d_macros_total": len(cmdline_origin),
        "cmdline_d_macros_in_hash": included,
        "cmdline_d_macros_excluded": excluded,
    }

    basename = os.path.basename(pch_header)
    out_path = os.path.join(scope_dir, f"{basename}.json")
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def _write_pch_manifest(
    pchdir: str,
    cmd_hash: str,
    pch_header: str,
    transitive_headers: list[str],
    cxx_command: str,
    context,
    *,
    anchor_root: str,
) -> None:
    """Write a sidecar manifest next to a cached .gch file.

    The manifest enables ``trim_cache.trim_pchdir`` to:

    * Bucket ``<pchdir>/<cmd_hash>/`` directories by ``header_realpath``
      so ``keep_count`` is enforced per real header rather than globally —
      cross-variant builds of the same header no longer evict each other
      at the default ``keep_count=1``.
    * Pre-evict entries whose transitive header content has changed
      since the .gch was built, avoiding the slow ``cc1`` PCH-stamp
      rejection at consume time.

    Hashes are git-blob SHA1 (the algorithm used by
    ``global_hash_registry``) so that ``trim_cache``'s standalone
    re-computation produces identical values.

    Written atomically via ``os.replace`` so a concurrent reader either
    sees the prior manifest or the new one, never a partial file.
    """
    manifest_dir = os.path.join(pchdir, cmd_hash)
    os.makedirs(manifest_dir, exist_ok=True)

    transitive_hashes: dict[str, str] = {}
    for h in transitive_headers:
        h_real = compiletools.wrappedos.realpath(h)
        try:
            transitive_hashes[h_real] = compiletools.global_hash_registry.get_file_hash(h_real, context=context)
        except (OSError, KeyError):
            pass

    manifest = {
        "header_realpath": compiletools.wrappedos.realpath(pch_header),
        "compiler": cxx_command,
        "compiler_identity": _compiler_identity(cxx_command, anchor_root=anchor_root),
        "transitive_hashes": transitive_hashes,
    }

    manifest_path = os.path.join(manifest_dir, "manifest.json")
    tmp_path = f"{manifest_path}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, sort_keys=True)
    os.replace(tmp_path, manifest_path)
