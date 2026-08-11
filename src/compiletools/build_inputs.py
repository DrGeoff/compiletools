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
    # No pure stage reads this. It records the quiet-adjusted level gather
    # itself ran at, which is the only observable of the unclamped
    # verbose-minus-quiet arithmetic below short of capturing stderr.
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

    The raw attrs are gather's alone: populate_args never writes the
    flag slots (the derived surface lives on the stashed BuildState), so
    the live attr IS the pre-parse raw value on every pass and a
    re-gather rebuilds from the same base by construction.
    """
    import compiletools.apptools as apptools
    from compiletools.utils import tokenize_flags_or_raise

    unsupplied = None if name in _FALLBACK_SLOTS else ()
    raw = getattr(args, name, None)
    if raw is None or raw in apptools._UNSUPPLIED_SENTINELS:
        return unsupplied
    return tuple(tokenize_flags_or_raise(raw, slot=name))


def _xxpend_tokens(args, attr):
    """Flatten one --prepend-*/--append-* list into flag tokens.

    Each list element may carry several flags (one conf value arrives as
    one element), so each element is tokenized individually.
    """
    from compiletools.utils import tokenize_flags_or_raise

    xxpend, _, name = attr.partition("_")
    slot = f"{xxpend}-{name.upper()}"
    tokens = []
    for element in getattr(args, attr, None) or ():
        tokens.extend(tokenize_flags_or_raise(element, slot=slot))
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


def _compute_pkg_config_path(args, verbose=0):
    """The value the build runs under: the existing
    env merged with the conf/CLI prepend/append lists and the
    auto-discovered cwd/gitroot ct.conf.d/pkgconfig candidates. Emits the
    verbose >= 4 provenance lines (gather is the impure boundary; the
    apply layer that performs the env write has no parser access)."""
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

    existing = os.environ.get("PKG_CONFIG_PATH", "")
    prepend_paths = getattr(args, "prepend_pkg_config_path", None)
    append_paths = getattr(args, "append_pkg_config_path", None)
    pkgconf.emit_pkg_config_path_provenance(
        existing,
        prepend_paths,
        append_paths,
        cwd_candidates,
        gitroot_candidates,
        verbose,
        getattr(args, "_parser", None),
    )
    return pkgconf.compute_pkg_config_path(existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates)


def _query_pkg_config(packages, pkg_config_path, want_libs, verbose, context):
    """Query pkg-config per package, memoized on *context* keyed
    ``(pkg, pkg_config_path, errors_policy, want_libs)``. Returns results
    in declaration order.

    ``_batch_pkg_config`` reads the global environment, so the computed
    PKG_CONFIG_PATH is set/restored around the query (temporary; the
    apply layer owns the durable SetEnv, and the standalone locked
    writer keeps its own env mutation).

    Note: ``_batch_pkg_config`` itself memoizes with ``functools.cache``
    keyed per ``(package, option)`` only -- no PKG_CONFIG_PATH in that
    key. A missing-package verdict is therefore path-insensitive
    process-wide: once a package is looked up and found missing under one
    PKG_CONFIG_PATH, that same verdict is served for every later query
    under a different path within the same process. The ``(pkg,
    pkg_config_path)`` key on *this* cache is forward-looking -- it is not
    currently deliverable for the missing-package fallback case, since the
    underlying ``_batch_pkg_config`` cache does not vary on path either.

    The failure policy is part of the key because it decides what a failing
    probe *produces*: warn mode stores an empty result, error mode raises.
    ``set_pkg_config_errors`` clears the module-level memos in
    ``apptools_pkgconfig`` on a mode change but cannot reach a per-context
    dict, so without the policy in the key a warm warn-mode empty result
    would be served after strict mode is armed and enforcement would never
    fire.

    ``want_libs`` is in the key for the same reason: it decides whether the
    entry carries libs at all, so a ``False`` entry's ``libs=()`` served to
    a later ``True`` caller would silently drop the package's link flags. A
    ``True`` entry is deliberately not reused for a later ``False`` caller
    either -- one extra ``--cflags`` query is cheaper than a key whose
    entries mean different things.
    """
    import os

    import compiletools.apptools_pkgconfig as pkgconf

    cache = context.pkg_config_query_cache
    errors_policy = pkgconf.get_pkg_config_errors()

    uncached = [pkg for pkg in packages if (pkg, pkg_config_path, errors_policy, want_libs) not in cache]
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
            filtered = pkgconf.filter_pkg_config_cflags(batch_cflags.get(pkg, ""), verbose, package=pkg)
            cflags = (
                tuple(pkgconf.tokenize_pkg_config_output(filtered, package=pkg, option="--cflags", verbose=verbose))
                if filtered
                else ()
            )
            libs_str = batch_libs.get(pkg, "")
            libs = (
                tuple(pkgconf.tokenize_pkg_config_output(libs_str, package=pkg, option="--libs", verbose=verbose))
                if libs_str
                else ()
            )
            cache[(pkg, pkg_config_path, errors_policy, want_libs)] = PkgConfigResult(cflags=cflags, libs=libs)

    return tuple((pkg, cache[(pkg, pkg_config_path, errors_policy, want_libs)]) for pkg in packages)


def _project_macro_value(args, value_attr, cmd_attr, verbose, *, slot):
    """Port of _set_project_version/_set_project_name value acquisition:
    the explicit value wins; otherwise the *cmd output's first word.
    Returns the escaped literal or None when the user did not opt in.

    *cmd* is shlex-tokenized via tokenize_flags_or_raise (slot=*slot*,
    e.g. "project-version-cmd") rather than a plain whitespace .split() --
    an unbalanced quote in the conf/CLI value now raises FlagTokenizeError
    instead of silently mis-splitting into a garbage argv. The tokenize
    call sits inside the same try as the subprocess call but is unaffected
    by the except tuple below (FlagTokenizeError isn't CalledProcessError/
    OSError), so it propagates to the gather_inputs caller unchanged.
    """
    import subprocess
    import sys

    from compiletools.utils import tokenize_flags_or_raise

    value = getattr(args, value_attr, None)
    cmd = getattr(args, cmd_attr, None)
    if not value and cmd:
        try:
            argv = tokenize_flags_or_raise(cmd, slot=slot)
            value = subprocess.check_output(argv, universal_newlines=True).strip("\n").split()[0]
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

    Blessed divergence D1, recorded here because the design doc it was
    labelled in is not in this repo: the any()-across-slots scope is
    BROADER than the original's, which ran three independent
    ``if "-DCT_PROJECT_VERSION" not in args.<SLOT>`` checks and so still
    injected into the other two. A macro the user set in only one of
    CPPFLAGS/CFLAGS/CXXFLAGS now suppresses injection into all three.

    Blessed rather than fixed because per-slot suppression is
    unrepresentable downstream: ``project_version`` / ``project_name`` are
    ONE BuildInputs field each, consumed by ``stage_project_macros`` for
    all three slots, so the only suppression gather can express is nulling
    the field. Reaching the divergent case needs the user to hand-set the
    macro in one or two of the three slots while also passing
    --projectversion / --projectname -- both of which are themselves
    deprecated (_PROJECT_MACRO_DEPRECATION_MESSAGE fires on the raw opt-in
    below), so the divergence is scoped to a feature already on the way
    out. Widening, not narrowing: the new rule injects strictly less.
    """
    return any(flag_name in (getattr(args, slot, None) or "") for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"))


_TARGET_ATTRS = ("filename", "static", "dynamic", "tests")


def _include_paths_with_gitroots(args, gitroot):
    """Port of the two INCLUDE-widening steps gather must model because
    they feed stage_include_paths:

    1. _do_xxpend for INCLUDE: --prepend-INCLUDE / --append-INCLUDE
       elements merge into the raw string (substring-presence dedup,
       matching _do_xxpend's `flag not in attr` check on the string).
    2. _extend_includes_using_git_root: with --git-root on a
       target-registering CAP (any of the four target attrs present),
       the gitroots of the cwd and of every target file extend the
       include list, sorted, skipping already-present paths.

    gather computes the widened tuple instead of mutating args.INCLUDE.
    An empty gitroot set silently no-ops (unreachable in production
    since find_git_root falls back to the cwd).

    Each contributing string (the bare INCLUDE value, and each
    --prepend-INCLUDE / --append-INCLUDE element) is shlex-tokenized
    individually via tokenize_flags_or_raise rather than whitespace-split
    on the concatenated string: a quoted space-containing path now
    survives as one token instead of shredding into fragments with
    literal quote characters, and an unbalanced quote raises
    FlagTokenizeError (attributed to INCLUDE/prepend-INCLUDE/
    append-INCLUDE) instead of silently misparsing.
    """
    from compiletools.git_utils import find_git_root
    from compiletools.utils import tokenize_flags_or_raise

    include = getattr(args, "INCLUDE", "") or ""
    prepend = [e for e in (getattr(args, "prepend_include", None) or ()) if e not in include]
    merged = " ".join(prepend + [include]) if prepend else include
    append = [e for e in (getattr(args, "append_include", None) or ()) if e not in merged]

    paths: list[str] = []
    for element in prepend:
        paths.extend(tokenize_flags_or_raise(element, slot="prepend-INCLUDE"))
    if include:
        paths.extend(tokenize_flags_or_raise(include, slot="INCLUDE"))
    for element in append:
        paths.extend(tokenize_flags_or_raise(element, slot="append-INCLUDE"))

    if getattr(args, "git_root", False) and any(hasattr(args, attr) for attr in _TARGET_ATTRS):
        roots = {gitroot} if gitroot else set()
        for attr in _TARGET_ATTRS:
            for filename in getattr(args, attr, None) or []:
                roots.add(find_git_root(filename))
        existing = set(paths)
        paths.extend(root for root in sorted(roots) if root not in existing)
    return tuple(paths)


def _raw_dir_value(args, attr):
    """Bindir/cas-dir raw value: absent attr or the unsupplied sentinels
    map to None (stage_resolve_names derives the default)."""
    import compiletools.apptools as apptools

    raw = getattr(args, attr, None)
    if raw is None or raw in apptools._UNSUPPLIED_SENTINELS:
        return None
    return raw


def _anchored_cas_dir(args, attr, gitroot, cwd_real):
    """Raw cas-dir value through the shared gitroot-anchoring gate, the
    same one ``resolve_cas_directory_arguments`` applies for the diagnostic
    tools. None (unsupplied) stays None so ``cas_dir_name`` derives the
    default from the pure side."""
    from compiletools.apptools_argparse import anchor_cas_dir_to_gitroot

    raw = _raw_dir_value(args, attr)
    if raw is None:
        return None
    return anchor_cas_dir_to_gitroot(raw, gitroot, cwd_real)


def gather_inputs(args, context) -> BuildInputs:
    """Build a BuildInputs from a parsed args namespace (post
    ``cap.parse_args`` + ``_flatten_variables`` + ``_strip_quotes``).

    The impure boundary of the functional build-state pipeline: every
    ambient read (env, filesystem, git, pkg-config subprocesses) happens
    here; ``compute_build_state`` is a pure function of the result.

    CPP/LD executable-name substitution happens in ``parseargs``'s
    pre-gather namespace steps; gather deliberately does not model it.
    """
    import os

    import compiletools.apptools as apptools
    import compiletools.configutils
    from compiletools.apptools_compiler import compiler_identity, compiler_kind
    from compiletools.git_utils import find_git_root

    # hasattr IS the CAP registration (the 4d4cfd6d bug class is closed
    # structurally): populate_args never materializes slot attrs, so an
    # unregistered slot stays absent on every pass and no sticky record
    # is needed to distinguish "registered empty" from "never registered".
    registered = frozenset(s for s in _SLOT_NAMES if hasattr(args, s))

    gitroot = find_git_root() or ""
    # One-off direct read of the live cwd, NOT cached -- mirrors
    # resolve_cas_directory_arguments (wrappedos caches on the input string,
    # which is fine for the repeated gitroot but wrong for a value read once).
    cwd_real = os.path.realpath(os.getcwd())
    # The --quiet latch, applied once by construction: gather never mutates
    # args, and honours the _quiet_applied marker parseargs sets after
    # folding --quiet into args.verbose. No clamping -- the subtraction is
    # unguarded, so negative verbose is legal.
    if getattr(args, "_quiet_applied", False):
        verbose = getattr(args, "verbose", 0)
    else:
        verbose = getattr(args, "verbose", 0) - getattr(args, "quiet", 0)

    pkg_config_path = _compute_pkg_config_path(args, verbose)
    packages = _merged_pkg_config_specs(args)
    want_libs = "LDFLAGS" in registered
    pkg_config_results = _query_pkg_config(packages, pkg_config_path, want_libs, verbose, context)

    project_version = _project_macro_value(
        args, "projectversion", "projectversioncmd", verbose, slot="project-version-cmd"
    )
    project_name = _project_macro_value(args, "projectname", "projectnamecmd", verbose, slot="project-name-cmd")
    # Deprecation warning fires on the raw opt-in, before any
    # suppression logic. Context-level once-latch: gather never mutates args.
    if (project_version is not None or project_name is not None) and not getattr(
        context, "_project_macro_deprecation_warned", False
    ):
        import sys

        sys.stderr.write(apptools._PROJECT_MACRO_DEPRECATION_MESSAGE)
        context._project_macro_deprecation_warned = True
    if project_version is not None and _flag_name_already_present(args, "-DCT_PROJECT_VERSION"):
        project_version = None
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
        include_paths=_include_paths_with_gitroots(args, gitroot),
        pkg_config_results=pkg_config_results,
        separate_flags=getattr(args, "separate_flags_CPP_CXX", False),
        gitroot=gitroot,
        prefix_map_target=getattr(args, "ffile_prefix_map_target", "."),
        project_version=project_version,
        project_name=project_name,
        link_driver_is_clang=compiler_kind(
            apptools._effective_link_driver(args), slot=apptools._effective_link_driver_slot(args)
        )
        == "clang",
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
