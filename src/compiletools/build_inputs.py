"""Frozen inputs to the pure build-state computation.

gather_inputs is the ONLY producer in production; tests construct
BuildInputs literals directly. gather_inputs owns every effectful read
(env vars, filesystem, pkg-config subprocesses, git); build_state
consumes the result as pure data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PkgConfigResult:
    cflags: tuple[str, ...]
    libs: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class BuildInputs:
    registered_slots: frozenset[str]
    cppflags: tuple[str, ...] | None = None
    cflags: tuple[str, ...] = ()
    cxxflags: tuple[str, ...] = ()
    ldflags: tuple[str, ...] | None = None
    prepend_cppflags: tuple[str, ...] = ()
    append_cppflags: tuple[str, ...] = ()
    prepend_cflags: tuple[str, ...] = ()
    append_cflags: tuple[str, ...] = ()
    prepend_cxxflags: tuple[str, ...] = ()
    append_cxxflags: tuple[str, ...] = ()
    prepend_ldflags: tuple[str, ...] = ()
    append_ldflags: tuple[str, ...] = ()
    include_paths: tuple[str, ...] = ()
    pkg_config_results: tuple[tuple[str, PkgConfigResult], ...] = ()
    separate_flags: bool = False
    gitroot: str = ""
    prefix_map_target: str = "."
    project_version: str | None = None
    project_name: str | None = None
    link_driver_is_clang: bool = False
    wild_b_selected: bool = False
    variant_raw: str = ""
    canonical_order: tuple[str, ...] = ()
    bindir_raw: str | None = None
    cas_objdir_raw: str | None = None
    cas_pchdir_raw: str | None = None
    cas_pcmdir_raw: str | None = None
    cas_exedir_raw: str | None = None
    pkg_config_path: str | None = None
    compiler_identity: str = ""
    verbose: int = 0


_SLOT_NAMES = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
# Slots whose "unsupplied" sentinel defers to the pure stage's CXXFLAGS
# fallback (stage_defaults); CFLAGS/CXXFLAGS take no fallback.
_FALLBACK_SLOTS = frozenset({"CPPFLAGS", "LDFLAGS"})


def _slot_tokens(args, name):
    """Map one raw slot string to the BuildInputs representation.

    Absent attr and the unsupplied sentinels mean "not supplied": None for
    CPPFLAGS/LDFLAGS (stage_defaults applies the CXXFLAGS fallback), () for
    CFLAGS/CXXFLAGS. An explicit empty string is (), never None -- the
    sentinel semantics of unsupplied_replacement, not emptiness.
    """
    import compiletools.apptools as apptools
    from compiletools.utils import split_command_cached

    unsupplied = None if name in _FALLBACK_SLOTS else ()
    raw = getattr(args, name, None)
    if raw is None or raw in apptools._UNSUPPLIED_SENTINELS:
        return unsupplied
    return tuple(split_command_cached(raw))


def _xxpend_tokens(args, attr):
    """Flatten one --prepend-*/--append-* list into flag tokens.

    Each list element may carry several flags (one conf value arrives as
    one element); the old pipeline space-joined elements into the raw
    string and shlex-split later, so tokenizing per element is the
    faithful port.
    """
    from compiletools.utils import split_command_cached

    tokens = []
    for element in getattr(args, attr, None) or ():
        tokens.extend(split_command_cached(element))
    return tuple(tokens)


def _merged_pkg_config_specs(args):
    """Port of _tier_one_modifications' pkg-config normalization: tokenize
    the three spec lists, then merge with _do_xxpend_list's dedup-and-place
    rule (prepend leftmost, append rightmost, skip already-present)."""
    import compiletools.apptools_pkgconfig as pkgconf

    base = pkgconf.tokenize_pkg_config_specs(list(getattr(args, "pkg_config", None) or []))
    for xx in ("prepend", "append"):
        specs = pkgconf.tokenize_pkg_config_specs(list(getattr(args, f"{xx}_pkg_config", None) or []))
        extras = [s for s in specs if s not in base]
        if not extras:
            continue
        base = extras + base if xx == "prepend" else base + extras
    return base


def _compute_pkg_config_path(args):
    """The value _setup_pkg_config_overrides_locked would write: existing
    env merged with the conf/CLI prepend/append lists and the
    auto-discovered cwd/gitroot ct.conf.d/pkgconfig candidates."""
    import os

    import compiletools.apptools_pkgconfig as pkgconf
    import compiletools.wrappedos
    from compiletools.git_utils import find_git_root

    cwd_candidates = []
    cwd_pkgconfig = os.path.join(os.getcwd(), "ct.conf.d", "pkgconfig")
    if compiletools.wrappedos.isdir(cwd_pkgconfig):
        cwd_candidates.append(compiletools.wrappedos.normpath(cwd_pkgconfig))

    gitroot_candidates = []
    gitroot = find_git_root()
    if gitroot:
        repo_pkgconfig = os.path.join(gitroot, "ct.conf.d", "pkgconfig")
        if compiletools.wrappedos.isdir(repo_pkgconfig):
            repo_pkgconfig = compiletools.wrappedos.normpath(repo_pkgconfig)
            if repo_pkgconfig not in cwd_candidates:
                gitroot_candidates.append(repo_pkgconfig)

    return pkgconf.compute_pkg_config_path(
        os.environ.get("PKG_CONFIG_PATH", ""),
        getattr(args, "prepend_pkg_config_path", None),
        getattr(args, "append_pkg_config_path", None),
        cwd_candidates,
        gitroot_candidates,
    )


def _query_pkg_config(packages, pkg_config_path, want_libs, verbose, context):
    """Query pkg-config per package, memoized on *context* keyed
    ``(pkg, pkg_config_path)``. Returns results in declaration order.

    ``_batch_pkg_config`` reads the global environment, so the computed
    PKG_CONFIG_PATH is set/restored around the query (temporary; the
    apply layer owns the durable SetEnv, and the locked writer keeps its
    own env mutation for the legacy pipeline).

    Note: ``_batch_pkg_config`` itself memoizes with ``functools.cache``
    keyed per ``(package, option)`` only -- no PKG_CONFIG_PATH in that
    key. A missing-package verdict is therefore path-insensitive
    process-wide: once a package is looked up and found missing under one
    PKG_CONFIG_PATH, that same verdict is served for every later query
    under a different path within the same process. The ``(pkg,
    pkg_config_path)`` key on *this* cache is forward-looking -- it is not
    currently deliverable for the missing-package fallback case, since the
    underlying ``_batch_pkg_config`` cache does not vary on path either.
    """
    import os

    import compiletools.apptools_pkgconfig as pkgconf
    from compiletools.utils import split_command_cached

    cache = getattr(context, "pkg_config_query_cache", None)
    if cache is None:
        cache = {}
        context.pkg_config_query_cache = cache

    uncached = [pkg for pkg in packages if (pkg, pkg_config_path) not in cache]
    if uncached:
        original = os.environ.get("PKG_CONFIG_PATH")
        if pkg_config_path is not None:
            os.environ["PKG_CONFIG_PATH"] = pkg_config_path
        try:
            batch_cflags = pkgconf._batch_pkg_config(uncached, "--cflags")
            batch_libs = pkgconf._batch_pkg_config(uncached, "--libs") if want_libs else {}
        finally:
            if pkg_config_path is not None:
                if original is None:
                    os.environ.pop("PKG_CONFIG_PATH", None)
                else:
                    os.environ["PKG_CONFIG_PATH"] = original
        for pkg in uncached:
            filtered = pkgconf.filter_pkg_config_cflags(batch_cflags.get(pkg, ""), verbose)
            cflags = tuple(split_command_cached(filtered)) if filtered else ()
            libs_str = batch_libs.get(pkg, "")
            libs = tuple(split_command_cached(libs_str)) if libs_str else ()
            cache[(pkg, pkg_config_path)] = PkgConfigResult(cflags=cflags, libs=libs)

    return tuple((pkg, cache[(pkg, pkg_config_path)]) for pkg in packages)


def _project_macro_value(args, value_attr, cmd_attr, verbose):
    """Port of _set_project_version/_set_project_name value acquisition:
    the explicit value wins; otherwise the *cmd output's first word.
    Returns the escaped literal or None when the user did not opt in."""
    import subprocess
    import sys

    value = getattr(args, value_attr, None)
    cmd = getattr(args, cmd_attr, None)
    if not value and cmd:
        try:
            value = subprocess.check_output(cmd.split(), universal_newlines=True).strip("\n").split()[0]
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(f"Could not use {cmd_attr} = {cmd} to set {value_attr}.\n")
            if verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise
    if not value:
        return None
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _flag_name_already_present(args, flag_name):
    """The original _set_project_* suppression: a substring match of the
    flag NAME in any raw compile slot suppresses injection (the pure
    stage dedups exact tokens only, so gather replicates the substring
    rule by nulling the field).

    Note: this any()-across-slots scope is BROADER than the originals'
    per-slot check -- a macro present in only one of CPPFLAGS/CFLAGS/
    CXXFLAGS suppresses injection into all three, not just the slot it
    was found in. Flagged for the Task 14 differential harness to confirm
    whether any real conf/CLI combination makes this observable.
    """
    return any(flag_name in (getattr(args, slot, None) or "") for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"))


def _raw_dir_value(args, attr):
    """Bindir/cas-dir raw value: absent attr or the unsupplied sentinels
    map to None (stage_resolve_names derives the default)."""
    import compiletools.apptools as apptools

    raw = getattr(args, attr, None)
    if raw is None or raw in apptools._UNSUPPLIED_SENTINELS:
        return None
    return raw


def _anchored_cas_dir(args, attr, gitroot, cwd_real):
    """Raw cas-dir value with the resolve_cas_directory_arguments
    gitroot-anchoring gate ported: a value supplied while the invocation
    cwd differs from the gitroot is anchored to the gitroot
    (os.path.join passes absolute values through unchanged)."""
    import os

    import compiletools.wrappedos

    raw = _raw_dir_value(args, attr)
    if raw is None:
        return None
    if compiletools.wrappedos.realpath(gitroot) != cwd_real:
        return compiletools.wrappedos.normpath(os.path.join(gitroot, raw))
    return raw


def gather_inputs(args, context) -> BuildInputs:
    """Build a BuildInputs from a parsed args namespace (post
    ``cap.parse_args`` + ``_flatten_variables`` + ``_strip_quotes``).

    The impure boundary of the functional build-state pipeline: every
    ambient read (env, filesystem, git, pkg-config subprocesses) happens
    here; ``compute_build_state`` is a pure function of the result.

    CPP/LD executable-name substitution stays in the old pipeline;
    gather deliberately does not model it.
    """
    import os

    import compiletools.apptools as apptools
    import compiletools.configutils
    from compiletools.apptools_compiler import compiler_identity, compiler_kind
    from compiletools.git_utils import find_git_root

    # Mirror _add_flags_from_pkg_config's posture (the 4d4cfd6d bug class):
    # _finalize_flag_state materializes args.LDFLAGS = "" for downstream
    # consumers even when the CAP never registered LDFLAGS, so bare hasattr
    # disagrees with the real registration on a post-finalize namespace.
    # _registered_flag_slots is the sticky, authoritative record when
    # present; hasattr is only correct pre-finalize (first pass, no
    # _finalize_flag_state run yet), so gather is safe on both namespace
    # shapes.
    slot_registration = getattr(args, "_registered_flag_slots", None)
    if slot_registration is not None:
        registered = frozenset(slot_registration)
    else:
        registered = frozenset(s for s in _SLOT_NAMES if hasattr(args, s))

    gitroot = find_git_root() or ""
    # One-off direct read of the live cwd, NOT cached -- mirrors
    # resolve_cas_directory_arguments (wrappedos caches on the input string,
    # which is fine for the repeated gitroot but wrong for a value read once).
    cwd_real = os.path.realpath(os.getcwd())
    # The --quiet latch, applied once by construction: gather never mutates
    # args, and honours _commonsubstitutions' _quiet_applied marker in case
    # the namespace already went through the old pipeline. No clamping --
    # the original subtracts unguarded, so negative verbose is legal.
    if getattr(args, "_quiet_applied", False):
        verbose = getattr(args, "verbose", 0)
    else:
        verbose = getattr(args, "verbose", 0) - getattr(args, "quiet", 0)

    pkg_config_path = _compute_pkg_config_path(args)
    packages = _merged_pkg_config_specs(args)
    want_libs = "LDFLAGS" in registered
    pkg_config_results = _query_pkg_config(packages, pkg_config_path, want_libs, verbose, context)

    project_version = _project_macro_value(args, "projectversion", "projectversioncmd", verbose)
    if project_version is not None and _flag_name_already_present(args, "-DCT_PROJECT_VERSION"):
        project_version = None
    project_name = _project_macro_value(args, "projectname", "projectnamecmd", verbose)
    if project_name is not None and _flag_name_already_present(args, "-DCT_PROJECT_NAME"):
        project_name = None

    canonical_order, _source = compiletools.configutils.get_canonical_order(argv=getattr(args, "_argv", None))

    return BuildInputs(
        registered_slots=registered,
        cppflags=_slot_tokens(args, "CPPFLAGS"),
        cflags=_slot_tokens(args, "CFLAGS") or (),
        cxxflags=_slot_tokens(args, "CXXFLAGS") or (),
        ldflags=_slot_tokens(args, "LDFLAGS"),
        prepend_cppflags=_xxpend_tokens(args, "prepend_cppflags"),
        append_cppflags=_xxpend_tokens(args, "append_cppflags"),
        prepend_cflags=_xxpend_tokens(args, "prepend_cflags"),
        append_cflags=_xxpend_tokens(args, "append_cflags"),
        prepend_cxxflags=_xxpend_tokens(args, "prepend_cxxflags"),
        append_cxxflags=_xxpend_tokens(args, "append_cxxflags"),
        prepend_ldflags=_xxpend_tokens(args, "prepend_ldflags"),
        append_ldflags=_xxpend_tokens(args, "append_ldflags"),
        include_paths=tuple((getattr(args, "INCLUDE", "") or "").split()),
        pkg_config_results=pkg_config_results,
        separate_flags=getattr(args, "separate_flags_CPP_CXX", False),
        gitroot=gitroot,
        prefix_map_target=getattr(args, "ffile_prefix_map_target", "."),
        project_version=project_version,
        project_name=project_name,
        link_driver_is_clang=compiler_kind(apptools._effective_link_driver(args)) == "clang",
        wild_b_selected=apptools._variant_has_axis(args, "wild-B"),
        variant_raw=getattr(args, "variant", "") or "",
        canonical_order=tuple(canonical_order),
        bindir_raw=_raw_dir_value(args, "bindir"),
        cas_objdir_raw=_anchored_cas_dir(args, "cas_objdir", gitroot, cwd_real),
        cas_pchdir_raw=_anchored_cas_dir(args, "cas_pchdir", gitroot, cwd_real),
        cas_pcmdir_raw=_anchored_cas_dir(args, "cas_pcmdir", gitroot, cwd_real),
        cas_exedir_raw=_anchored_cas_dir(args, "cas_exedir", gitroot, cwd_real),
        pkg_config_path=pkg_config_path,
        compiler_identity=compiler_identity(getattr(args, "CXX", "") or "", anchor_root=gitroot),
        verbose=verbose,
    )
