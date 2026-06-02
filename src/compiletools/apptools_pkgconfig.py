"""pkg-config helpers (leaf module).

Extracted from :mod:`compiletools.apptools` as a behavior-preserving facade
split. This module is an import-time leaf: at module scope it imports only stdlib
plus :mod:`compiletools.wrappedos` and :mod:`compiletools.utils` (themselves
leaves). It MUST NOT import ``compiletools.apptools`` -- doing so would
reintroduce the very cycle this split removes.

:mod:`compiletools.git_utils` is imported *inside*
:func:`_setup_pkg_config_overrides_locked` (deferred), not at module scope,
because ``git_utils`` itself does a top-level ``import compiletools.apptools``
(used only lazily). A module-scope import here would form the cycle
apptools -> apptools_pkgconfig -> git_utils -> apptools and crash at
``apptools`` initialisation. The deferred import runs only after every module
is fully initialised.

It groups the functions that invoke ``pkg-config`` and that manage the
process-wide ``PKG_CONFIG_PATH`` override state:

* :func:`cached_pkg_config` -- ``@functools.cache``-memoised single-package
  ``pkg-config --cflags`` / ``--libs`` probe.
* :func:`filter_pkg_config_cflags` -- rewrite ``-I`` to ``-isystem`` and drop
  default system include paths.
* :func:`_batch_pkg_config` -- batched multi-package query with per-package
  fallback through :func:`cached_pkg_config`.
* :func:`_add_flags_from_pkg_config` -- fold pkg-config cflags/libs into
  ``args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}``.
* :func:`_setup_pkg_config_overrides` /
  :func:`_setup_pkg_config_overrides_locked` -- apply project + CLI
  ``PKG_CONFIG_PATH`` overrides under ``_PKG_CONFIG_OVERRIDE_LOCK``.
* :func:`_pkg_config_provenance_label` -- best-effort origin attribution for
  emitted ``Prepended/Appended pkg-config path: ...`` diagnostic lines.

The ``args_parser`` provenance side-channel
(``_ComposingArgumentParser.get_conf_file_provenance()``) is reached purely
through the *parameter* passed in by the caller, never imported -- so this
module stays decoupled from the parser machinery still living in
``apptools.py``.

``apptools.py`` re-exports every name here by binding so its existing
``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
importers, and test/patch targets keep working with identical object
identity. ``apptools.clear_cache`` fans out to
:func:`clear_cache` here to clear the moved ``cached_pkg_config`` memo so the
net set of cleared caches is identical to the pre-split implementation.
"""

import functools
import os
import shlex
import subprocess
import sys
import threading
import warnings
from typing import Literal

import compiletools.wrappedos
from compiletools.utils import split_command_cached


def clear_cache():
    """Clear the pkg-config cache moved out of :mod:`compiletools.apptools`.

    ``apptools.clear_cache`` fans out here so the exact same memo
    (``cached_pkg_config``) is cleared as before the facade split. Net effect
    is identical to the previous monolithic ``apptools.clear_cache``.
    """
    cached_pkg_config.cache_clear()


@functools.cache
def cached_pkg_config(package, option):
    """Cache pkg-config results for package and option (--cflags or --libs)"""
    # First check if the package exists
    exists_result = subprocess.run(["pkg-config", "--exists", package], capture_output=True, check=False)
    if exists_result.returncode != 0:
        warnings.warn(f"pkg-config package '{package}' not found", UserWarning, stacklevel=2)
        return ""

    result = subprocess.run(
        ["pkg-config", option, package],
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.rstrip()


def filter_pkg_config_cflags(cflags_str, verbose=0):
    """
    Process pkg-config cflags output.
    Converts -I to -isystem, except for default system include paths
    which are dropped to prevent include order issues (e.g. with libc++).
    Uses shlex for robust shell tokenization and quoting.
    """
    if not cflags_str:
        return ""

    # Standard system include paths
    system_include_paths = set(["/usr/include"])
    prefix = os.environ.get("PREFIX")
    if prefix:
        system_include_paths.add(compiletools.wrappedos.normpath(os.path.join(prefix, "include")))

    # Use shlex to correctly handle quoted paths in flags
    try:
        flags = split_command_cached(cflags_str)
    except ValueError:
        # Fallback for malformed strings
        flags = cflags_str.split()

    flag_iter = iter(flags)
    processed_flags = []

    for flag in flag_iter:
        if flag.startswith("-I"):
            path = None
            if flag == "-I":
                # Detached -I
                try:
                    path = next(flag_iter)
                except StopIteration:
                    # Trailing -I at end of string, preserve as-is
                    processed_flags.append(shlex.quote(flag))
                    break
            else:
                # Attached -Ipath
                path = flag[2:]

            # Normalize and check
            normalized_path = compiletools.wrappedos.normpath(path)
            is_system = normalized_path in system_include_paths

            if is_system:
                if verbose >= 6:
                    print(f"Dropping default system include path from pkg-config: {path}")
                continue

            # Reconstruct as -isystem, quoting path for shell safety
            processed_flags.append(f"-isystem {shlex.quote(path)}")
        else:
            # Re-quote other flags to preserve them correctly in the output string
            processed_flags.append(shlex.quote(flag))

    return " ".join(processed_flags)


_PkgConfigOrigin = Literal["prepend", "append", "candidate-cwd", "candidate-gitroot"]


def _pkg_config_provenance_label(
    path,
    origin: _PkgConfigOrigin,
    provenance,
):
    """Return a parenthetical origin label for a PKG_CONFIG_PATH entry, or
    empty string if no useful attribution is available.

    ``origin`` is one of ``'prepend'``, ``'append'``, ``'candidate-cwd'``,
    or ``'candidate-gitroot'``. The candidate-* origins go straight to the
    auto-discovered label without consulting ``provenance``. For
    prepend/append, the matching ``prepend-PKG-CONFIG-PATH`` /
    ``append-PKG-CONFIG-PATH`` provenance entries are searched for a
    realpath-equal value; first match wins. Falls back to ``(from CLI)``
    when no provenance entry matches.
    """
    if origin == "candidate-cwd":
        return "(auto-discovered: cwd)"
    if origin == "candidate-gitroot":
        return "(auto-discovered: gitroot)"
    key = "prepend-PKG-CONFIG-PATH" if origin == "prepend" else "append-PKG-CONFIG-PATH"
    try:
        target_real = compiletools.wrappedos.realpath(path)
    except (OSError, ValueError):
        target_real = path
    for entry in provenance.get(key, []):
        value, source_file, lineno = entry[0], entry[1], entry[2]
        literal = entry[3] if len(entry) >= 4 else value
        try:
            value_real = compiletools.wrappedos.realpath(value)
        except (OSError, ValueError):
            value_real = value
        if value_real == target_real:
            if literal != value:
                return f"(from {source_file}:{lineno}, literal: {literal})"
            return f"(from {source_file}:{lineno})"
    return "(from CLI)"


def _setup_pkg_config_overrides(context, verbose=0, prepend_paths=None, append_paths=None, args_parser=None):
    """Apply project-level and CLI-specified pkg-config path overrides to PKG_CONFIG_PATH.

    Priority order (highest first):

    1. ``prepend-PKG-CONFIG-PATH`` entries, with CLI winning over conf-file
       entries and — within the accumulated conf-file entries — the
       higher-priority axis conf (composed later in the variant) winning
       over the lower-priority one (e.g. project ``ct.conf``).
    2. ``<cwd>/ct.conf.d/pkgconfig/`` (project-local, auto-discovered)
    3. ``<gitroot>/ct.conf.d/pkgconfig/`` (repo-level, auto-discovered)
    4. Existing ``PKG_CONFIG_PATH`` entries
    5. ``append-PKG-CONFIG-PATH`` entries, symmetric to (1): CLI wins over
       conf-file entries, higher-priority axis wins within the conf-file
       group.

    Args:
        context: BuildContext instance tracking per-build state.
        verbose: verbosity level for diagnostic output.
        prepend_paths: directories to prepend (from ``--prepend-PKG-CONFIG-PATH``).
        append_paths: directories to append (from ``--append-PKG-CONFIG-PATH``).
        args_parser: optional ``_ComposingArgumentParser`` whose
            ``get_conf_file_provenance()`` is consulted at ``verbose >= 4``
            to attribute each emitted ``Prepended/Appended pkg-config
            path: ...`` line back to its origin (conf-file:line, CLI, or
            auto-discovered). Best-effort: if absent or empty the
            output degrades to bare paths (today's format).

    Must be called before any pkg-config subprocess invocation
    (i.e., before _add_flags_from_pkg_config and before magicflags
    processing).

    Concurrency contract
    --------------------
    This function mutates the **process-wide** ``os.environ['PKG_CONFIG_PATH']``,
    which is global state. Callers MUST observe the following:

    * Per-process serialization is enforced via a module-level
      ``threading.Lock`` (``_PKG_CONFIG_OVERRIDE_LOCK``). Two threads
      racing into this function will not interleave their reads/writes
      of ``PKG_CONFIG_PATH``.
    * The lock does NOT protect against other code paths in the process
      mutating ``os.environ['PKG_CONFIG_PATH']`` independently.
    * The lock does NOT serialize across processes. Multiple processes
      sharing a single ``BuildContext`` is unsupported.
    * The ``context.pkg_config_overrides_applied`` flag is checked and
      set within the lock to make the apply-once invariant safe under
      concurrent calls on the same context.
    * After mutation, ``context._original_pkg_config_path`` records the
      prior value so ``BuildContext.restore_pkg_config_path()`` can
      undo the mutation. Restore is also single-process / serial.
    """
    with _PKG_CONFIG_OVERRIDE_LOCK:
        _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths, args_parser)


# Process-local serialization for the env-mutation in _setup_pkg_config_overrides.
# See the docstring of that function for the full contract.
_PKG_CONFIG_OVERRIDE_LOCK = threading.Lock()


def _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths, args_parser=None):
    """Body of _setup_pkg_config_overrides; assumes the module lock is held."""
    if context.pkg_config_overrides_applied:
        return

    # Deferred import: ``compiletools.git_utils`` is NOT a leaf -- it does a
    # top-level ``import compiletools.apptools`` (used only lazily inside its
    # own functions). Importing it at module scope here would create the cycle
    # apptools -> apptools_pkgconfig -> git_utils -> apptools and fail at
    # ``apptools`` init time (partially-initialised module). Importing inside
    # the function keeps apptools_pkgconfig a true import-time leaf; by the
    # time this runs every module is fully initialised. Importing the symbol
    # (``from ... import find_git_root``) rather than the submodule avoids
    # rebinding the local ``compiletools`` name, keeping the
    # ``compiletools.wrappedos.*`` references below resolvable by the type
    # checker.
    from compiletools.git_utils import find_git_root

    gitroot = find_git_root()

    cwd_candidates = []
    cwd_pkgconfig = os.path.join(os.getcwd(), "ct.conf.d", "pkgconfig")
    if compiletools.wrappedos.isdir(cwd_pkgconfig):
        cwd_candidates.append(compiletools.wrappedos.normpath(cwd_pkgconfig))

    gitroot_candidates = []
    if gitroot:
        repo_pkgconfig = os.path.join(gitroot, "ct.conf.d", "pkgconfig")
        if compiletools.wrappedos.isdir(repo_pkgconfig):
            repo_pkgconfig = compiletools.wrappedos.normpath(repo_pkgconfig)
            if repo_pkgconfig not in cwd_candidates:
                gitroot_candidates.append(repo_pkgconfig)

    existing = os.environ.get("PKG_CONFIG_PATH", "")
    existing_dirs = [compiletools.wrappedos.normpath(d) for d in existing.split(os.pathsep)] if existing else []

    # Build the final path with explicit precedence:
    #   prepend_paths (highest) > candidates > middle (existing) > append_paths
    # Each entry appears at most once. An entry that is already in
    # PKG_CONFIG_PATH gets *moved* to the requested position rather than
    # being silently dropped — so --prepend-PKG-CONFIG-PATH=/X actually
    # promotes /X to the front when /X was already present.
    #
    # ``prepend_paths`` / ``append_paths`` arrive ordered
    # ``[low-priority conf, ..., high-priority conf, CLI in parse order]``
    # — the order ``_AccumulatingConfigFileParser`` and the
    # ``_ComposingArgumentParser`` CLI re-append produce for every
    # ``prepend-*`` / ``append-*`` key. Compiler-flag slots emit that
    # list left-to-right and rely on the compiler's "last token wins"
    # rule to honor CLI > high-conf > low-conf. ``PKG_CONFIG_PATH``
    # resolves leftmost-first, so we *reverse* both lists here so the
    # same priority ordering survives the inversion of the wins rule.
    # Symmetric for prepend and append: within each group, the highest-
    # priority source ends up leftmost in PATH (winning), the
    # lowest-priority source ends up rightmost in its group (only used
    # as a fallback for packages no higher source defines).
    prepend_normd = [compiletools.wrappedos.normpath(d) for d in reversed(prepend_paths or [])]
    append_normd = [compiletools.wrappedos.normpath(d) for d in reversed(append_paths or [])]
    forced_at_end = set(append_normd)

    middle = [d for d in existing_dirs if d not in forced_at_end]

    provenance = {}
    if args_parser is not None:
        try:
            provenance = args_parser.get_conf_file_provenance()
        except Exception as exc:
            provenance = {}
            if verbose >= 4:
                print(
                    f"warning: pkg-config provenance lookup failed ({type(exc).__name__}: {exc}); "
                    f"falling back to bare-path output",
                    file=sys.stderr,
                )

    seen: set[str] = set()
    final: list[str] = []
    emission_passes: list[tuple[list[str], str | None, _PkgConfigOrigin | None]] = [
        (prepend_normd, "Prepended", "prepend"),
        (cwd_candidates, "Prepended", "candidate-cwd"),
        (gitroot_candidates, "Prepended", "candidate-gitroot"),
        (middle, None, None),
        (append_normd, "Appended", "append"),
    ]
    for source, label, origin in emission_passes:
        for d in source:
            if not d or d in seen:
                continue
            seen.add(d)
            final.append(d)
            if label is not None and origin is not None and verbose >= 4:
                attribution = _pkg_config_provenance_label(d, origin, provenance)
                if attribution:
                    print(f"{label} pkg-config path: {d} {attribution}")
                else:
                    print(f"{label} pkg-config path: {d}")

    new_value = os.pathsep.join(final) if final else None

    # Save original ONLY if we are about to mutate, so restore_pkg_config_path
    # can faithfully undo. Set the flag AFTER the mutation succeeds so a
    # caller hitting an exception above can retry.
    if new_value is not None and new_value != existing:
        context._original_pkg_config_path = existing if "PKG_CONFIG_PATH" in os.environ else True
        os.environ["PKG_CONFIG_PATH"] = new_value

    context.pkg_config_overrides_applied = True


def _add_flags_from_pkg_config(args):
    packages = list(args.pkg_config)
    if not packages:
        return

    # Batch pkg-config calls: query all packages at once instead of one subprocess
    # per package.  Falls back to per-package calls if the batch fails (e.g. a
    # package is missing and we need to identify which one).
    want_libs = hasattr(args, "LDFLAGS")

    batch_cflags = _batch_pkg_config(packages, "--cflags")
    batch_libs = _batch_pkg_config(packages, "--libs") if want_libs else {}

    for pkg in packages:
        raw_cflags = batch_cflags.get(pkg, "")
        cflags = filter_pkg_config_cflags(raw_cflags, args.verbose)

        if cflags:
            args.CPPFLAGS += f" {cflags}"
            args.CFLAGS += f" {cflags}"
            args.CXXFLAGS += f" {cflags}"
            if args.verbose >= 6:
                print(f"pkg-config --cflags {pkg} added FLAGS={cflags}")

        if want_libs:
            libs = batch_libs.get(pkg, "")
            if libs:
                args.LDFLAGS += f" {libs}"
                if args.verbose >= 6:
                    print(f"pkg-config --libs {pkg} added LDFLAGS={libs}")


def _batch_pkg_config(packages: list[str], option: str) -> dict[str, str]:
    """Query pkg-config for all *packages* at once, returning {pkg: output}.

    Fast path: validate all packages with a single ``--exists`` call, then
    query each with *option* (skipping the per-package ``--exists``).
    If the batch ``--exists`` fails, fall back to per-package cached calls
    which handle missing packages individually.
    """
    # Single --exists check for all packages at once
    exists = subprocess.run(
        ["pkg-config", "--exists"] + packages,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        # At least one package is missing — fall back to per-package
        return {pkg: cached_pkg_config(pkg, option) for pkg in packages}

    # All packages exist — query each without the redundant --exists check.
    out: dict[str, str] = {}
    for pkg in packages:
        r = subprocess.run(
            ["pkg-config", option, pkg],
            stdout=subprocess.PIPE,
            text=True,
        )
        out[pkg] = r.stdout.rstrip()
    return out
