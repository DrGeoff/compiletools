import os
import re
import sys
from dataclasses import dataclass, field
from functools import cache

import appdirs

try:
    from configargparse import DefaultConfigFileParser as CfgFileParser
except ImportError:
    from configargparse import ConfigFileParser as CfgFileParser

import compiletools.git_utils
import compiletools.utils
import compiletools.wrappedos

# Variant tokens may be separated by '.', ',', or whitespace anywhere they appear:
# in --variant on the CLI, in the `variant = ...` line of ct.conf, and in the
# `extends = ...` directive inside an axis conf file. All three forms are
# interchangeable so users can pick whichever reads best.
_VARIANT_SEP_RE = re.compile(r"[\s,.]+")


# Built-in canonical ordering. A project may override the whole list via
# `variant-canonical-order = ...` in its ct.conf. Tokens NOT in this list are
# appended to the end of a resolution in user-typed order, so a project axis
# (e.g. `myproj`) can be tacked on without re-declaring the whole order.
_DEFAULT_CANONICAL_ORDER = (
    "blank",
    # toolchain
    "gcc",
    "clang",
    "icc",
    "msvc",
    # linker (mutually exclusive — choose one; -fuse-ld=<name> on LDFLAGS)
    "ld",
    "gold",
    "mold",
    "wild",
    # optimization
    "debug",
    "release",
    "releasewithdebinfo",
    # instrumentation
    "asan",
    "ubsan",
    "tsan",
    "msan",
    "coverage",
    "lto",
    "pgo",
)


def extract_value_from_argv(key, argv=None, default=None, verbose=0):
    """Extract the value for the given key from the argv.
    Return the given default if no key was identified
    """
    if argv is None:
        argv = sys.argv

    value = default

    hyphens = ("-", "--")
    for hh in hyphens:
        for arg in argv:
            try:
                keywithhyphens = "".join([hh, key, "="])
                if arg.startswith(keywithhyphens):
                    value = arg.split("=")[1]
                else:
                    keywithhyphens = "".join([hh, key])
                    if arg.startswith(keywithhyphens):
                        index = argv.index(keywithhyphens)
                        if index + 1 < len(argv):
                            value = argv[index + 1]
            except ValueError:
                pass

    if verbose >= 4:
        msg = "argv extraction: " + key + " "
        if value:
            msg += str(value)
        print(msg)
    return value


def extract_item_from_ct_conf(
    key,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    default=None,
    verbose=0,
    gitroot=None,
):
    """Extract the value for the given key from the ct.conf files.
    Return the given default if no key was identified.

    Walks the ct.conf hierarchy from highest to lowest priority and returns
    the first match (so a project ct.conf overrides the bundled one).
    """
    fileparser = CfgFileParser()
    for cfgpath in reversed(
        get_existing_config_files(
            filename="ct.conf",
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            exedir=exedir,
            gitroot=gitroot,
        )
    ):
        with open(cfgpath) as cfg:
            items = fileparser.parse(cfg)
            try:
                value = items[key]
                if verbose >= 2:
                    print(" ".join([cfgpath, "contains", key, "=", str(value)]))
                return value
            except KeyError:
                continue

    return default


def extract_item_from_ct_conf_with_source(
    key,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=0,
    gitroot=None,
):
    """Like extract_item_from_ct_conf but also returns the path of the
    ct.conf that defined the value (or None if no ct.conf defines it).

    Used by the provenance renderer to attribute config decisions back to
    their source file.
    """
    fileparser = CfgFileParser()
    for cfgpath in reversed(
        get_existing_config_files(
            filename="ct.conf",
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            exedir=exedir,
            gitroot=gitroot,
        )
    ):
        with open(cfgpath) as cfg:
            items = fileparser.parse(cfg)
            if key in items:
                if verbose and verbose >= 2:
                    print(f"{cfgpath} contains {key} = {items[key]}")
                return items[key], cfgpath
    return None, None


def removedotconf(config):
    if config[-5:] == ".conf":
        return config[:-5]
    else:
        return config


def extractconfig(argv):
    config = None
    config = extract_value_from_argv(key="config", argv=argv, default=None)

    if not config:
        config = extract_value_from_argv(key="c", argv=argv, default=None)
    return config


def impliedvariant(argv):
    """If the user specified a config directly then we imply the variant name"""
    config = extractconfig(argv)

    if config:
        return removedotconf(os.path.basename(config))
    else:
        return None


def split_variant(variant_str):
    """Split a variant string into atomic tokens.

    Accepts '.', ',', and whitespace (or any mix) as separators. Empty
    tokens are dropped. Used for both --variant input and `extends = ...`
    parsing inside conf files.
    """
    if not variant_str:
        return ()
    return tuple(tok for tok in _VARIANT_SEP_RE.split(variant_str.strip()) if tok)


def get_canonical_order(
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=None,
    gitroot=None,
):
    """Return (order_tuple, source_path_or_'builtin')."""
    raw, source = extract_item_from_ct_conf_with_source(
        key="variant-canonical-order",
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose or 0,
        gitroot=gitroot,
    )
    if raw is None or source is None:
        return _DEFAULT_CANONICAL_ORDER, "builtin"
    return split_variant(str(raw)), source


def canonicalize_variant_tokens(tokens, canonical_order):
    """Reorder *tokens* by their position in *canonical_order*.

    Tokens not in the order list go to the end, preserving user-typed order
    (so a project can add a new axis without re-declaring the whole order).
    Stable when two tokens have the same canonical position (shouldn't happen
    in well-formed config).
    """
    order_pos = {name: i for i, name in enumerate(canonical_order)}
    known = []
    unknown = []
    for tok in tokens:
        if tok in order_pos:
            known.append((order_pos[tok], tok))
        else:
            unknown.append(tok)
    known.sort(key=lambda t: t[0])
    return tuple(tok for _, tok in known) + tuple(unknown)


def canonicalize_variant_input(
    variant_str,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=0,
    gitroot=None,
):
    """Convert a raw --variant string into its canonical dotted form.

    `gcc,debug,asan`, `gcc debug asan`, `debug.gcc.asan` all collapse to
    `gcc.debug.asan` (assuming the default canonical order). A single
    token round-trips unchanged.

    Called from extract_variant() and from apptools._commonsubstitutions to
    canonicalize argparse-stored --variant values.
    """
    tokens = split_variant(variant_str)
    if not tokens:
        return variant_str
    order, _src = get_canonical_order(
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose,
        gitroot=gitroot,
    )
    canonical_tokens = canonicalize_variant_tokens(tokens, order)
    return ".".join(canonical_tokens)


def extract_variant(argv=None, user_config_dir=None, system_config_dir=None, exedir=None, verbose=0, gitroot=None):
    """Determine which variant to build, canonicalizing the result.

    Precedence (lowest -> highest):
      built-in default 'debug' < ct.conf `variant` < $VARIANT env < --variant argv

    The returned string is in canonical dotted form: composite inputs like
    'gcc,debug,asan' or 'debug gcc asan' are normalized to 'gcc.debug.asan'.
    If the user passed --config=foo.conf, the variant is implied from the
    basename and is NOT re-canonicalized (treated as an explicit literal).
    """
    if argv is None:
        argv = sys.argv

    implied = impliedvariant(argv)
    if implied:
        if verbose >= 1:
            print("Using implied variant from directly specified config")
        return implied

    variant = "debug"
    variant = extract_item_from_ct_conf(
        key="variant",
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        default=variant,
        verbose=verbose,
        gitroot=gitroot,
    )
    try:
        variant = os.environ["VARIANT"]
    except KeyError:
        pass
    variant = extract_value_from_argv(key="variant", argv=argv, default=variant)

    result = canonicalize_variant_input(
        str(variant) if variant is not None else "debug",
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose or 0,
        gitroot=gitroot,
    )

    if verbose and verbose >= 4:
        print("Extract variant: " + str(result))

    return result


@cache
def default_config_directories(
    user_config_dir=None, system_config_dir=None, exedir=None, repoonly=False, verbose=0, gitroot=None, current_dir=None
):
    # Use configuration in the order (lowest to highest priority)
    # If repoonly is true, start the procedure at step 4
    # 1) ct/ct.conf.d subdirectory alongside the ct-* executable
    # 2) system config (XDG compliant: /etc/xdg/ct)
    # 2b) python virtual environment configs (${python-site-packages}/ct/ct.conf.d)
    # 2c) package bundled config (<installed-package>/ct.conf.d)
    # 3) user config (XDG compliant: ~/.config/ct)
    # 4) project config (<gitroot>/ct.conf.d)
    # 5) gitroot
    # 6) current working directory
    # 7) environment variables
    # 8) command line arguments

    # These variables are settable to assist writing tests
    if user_config_dir is None:
        user_config_dir = appdirs.user_config_dir(appname="ct")

    system_dirs = []
    if system_config_dir is not None:
        system_dirs.append(system_config_dir)
    else:
        # Add package's bundled config directory (step 2b - highest priority among system configs)
        package_config_dir = os.path.join(os.path.dirname(__file__), "ct.conf.d")
        if compiletools.wrappedos.isdir(package_config_dir):
            system_dirs.append(package_config_dir)

        for python_config_dir in sys.path[::-1]:
            trialpath = os.path.join(python_config_dir, "ct", "ct.conf.d")
            if compiletools.wrappedos.isdir(trialpath) and trialpath not in system_dirs:
                system_dirs.append(trialpath)
        system_dirs.append(appdirs.site_config_dir(appname="ct"))

    if exedir is None:
        exedir = compiletools.wrappedos.dirname(compiletools.wrappedos.realpath(sys.argv[0]))

    executable_config_dir = os.path.join(exedir, "ct", "ct.conf.d")
    if current_dir is None:
        current_dir = os.getcwd()
    if gitroot is None:
        gitroot = compiletools.git_utils.find_git_root()
    results = [current_dir, gitroot]

    # Add config directories that actually exist
    project_config_dir = os.path.join(gitroot, "ct.conf.d")
    if compiletools.wrappedos.isdir(project_config_dir):
        results.append(project_config_dir)

    repo_config_dir = os.path.join(gitroot, "src", "compiletools", "ct.conf.d")
    if compiletools.wrappedos.isdir(repo_config_dir):
        results.append(repo_config_dir)

    # Priority order (lowest -> highest): bundled < system < venv < user
    # < project < cwd < env < CLI. The list returned here is in *reverse*
    # priority — later entries WIN. ``get_existing_config_files`` (line ~250)
    # consumes this list via ``reversed(...)`` so the highest-priority
    # directory is read LAST, letting its values override earlier ones.
    cwd_config_dir = os.path.join(current_dir, "ct.conf.d")
    if compiletools.wrappedos.isdir(cwd_config_dir):
        results.append(cwd_config_dir)

    if not repoonly:
        results.extend([user_config_dir] + system_dirs + [executable_config_dir])
    results = compiletools.utils.ordered_unique(results)
    if verbose >= 9:
        print(" ".join(["Default config directories"] + list(results)))

    return results


def get_existing_config_files(filename="ct.conf", **kwargs):
    """Get list of existing config files in standard directories.

    Returns paths in priority order: highest-priority first, lowest last.
    """
    if "current_dir" not in kwargs or kwargs["current_dir"] is None:
        kwargs["current_dir"] = os.getcwd()
    directories = default_config_directories(**kwargs)

    configs = [os.path.join(directory, filename) for directory in reversed(directories)]

    existing_configs = [cfg for cfg in configs if compiletools.wrappedos.isfile(cfg)]

    if kwargs.get("verbose", 0) >= 8:
        print(" ".join(["Existing config files:"] + existing_configs))

    return existing_configs


def clear_cache():
    """Clear LRU caches for testing"""
    default_config_directories.cache_clear()


# ---------------------------------------------------------------------------
# Variant resolution: inheritance + composition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AxisResolution:
    """Per-axis resolution result.

    Attributes:
        name: Canonical axis token (e.g. "gcc").
        conf_paths: All conf files matching this axis, in ascending priority
            order (bundled first, project last). configargparse layers them
            so later files override scalar keys and append- form accumulates.
        extends: Parents named by the highest-priority conf's `extends = ...`
            directive. Empty tuple if no `extends` is present.
    """

    name: str
    conf_paths: tuple[str, ...]
    extends: tuple[str, ...] = ()


@dataclass(frozen=True)
class VariantResolution:
    """Structured result of resolving a --variant string.

    `flat_paths` is the list configargparse consumes; `axes`,
    `composite_override`, and `canonical_order_source` are for the
    `format_variant_resolution` renderer used by parseargs() to print
    the unconditional per-axis provenance trace.
    """

    raw_input: str
    canonical_name: str
    axes: tuple[AxisResolution, ...]
    composite_override: str | None = None
    base_ct_conf_files: tuple[str, ...] = ()
    canonical_order: tuple[str, ...] = field(default_factory=tuple)
    canonical_order_source: str = "builtin"
    explicit_config: str | None = None

    @property
    def flat_paths(self):
        """All conf files in low-to-high priority order, for configargparse."""
        result = list(self.base_ct_conf_files)
        for axis in self.axes:
            result.extend(axis.conf_paths)
        if self.composite_override:
            result.append(self.composite_override)
        if self.explicit_config:
            result.append(self.explicit_config)
        return result


class VariantResolutionError(RuntimeError):
    """Raised when --variant cannot be resolved (missing axis, cycle, etc)."""

    pass


def _parse_extends_directive(conf_path, verbose=0):
    """Return a tuple of parent variant names from `extends = ...`, or ().

    A conf that can't be read (permission denied, transient FS error) is
    treated as having no `extends`, but the failure is announced at
    verbose>=1 so a silently-skipped permission-denied conf doesn't
    masquerade as "no inheritance configured".
    """
    fileparser = CfgFileParser()
    try:
        with open(conf_path) as cfg:
            items = fileparser.parse(cfg)
    except OSError as exc:
        if verbose >= 1:
            print(f"warning: could not read {conf_path}: {exc}; treating as no `extends`")
        return ()
    raw = items.get("extends")
    if raw is None:
        return ()
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw)
    return split_variant(str(raw))


def _find_axis_confs(name, **kwargs):
    """Return conf files for an axis in ascending priority order (low → high).

    `get_existing_config_files` returns highest-priority first, so we reverse
    to put the bundled defaults at the front.
    """
    paths = get_existing_config_files(filename=f"{name}.conf", **kwargs)
    return list(reversed(paths))


def _resolve_axis(name, search_kwargs, visited, on_path, _axis_cache):
    """DFS resolve one axis. Returns ordered list of AxisResolution.

    visited: set of axis names already emitted (for diamond dedup)
    on_path: list of axis names currently in the recursion stack (preserves
        traversal order so cycle diagnostics show the actual path through
        the graph). Membership check is O(len(on_path)) but the stack is
        typically tiny (depth ~3).

    Cache semantics: an entry in _axis_cache is only populated AFTER the
    DFS for that name has completed without raising — so the cached path
    bypasses the cycle check safely (cycles can never reach a cached node).
    """
    if name in _axis_cache:
        if name in visited:
            return []
        visited.add(name)
        return [_axis_cache[name]]

    if name in on_path:
        cycle = list(on_path) + [name]
        raise VariantResolutionError(f"extends cycle: {' -> '.join(cycle)}")

    paths = _find_axis_confs(name, **search_kwargs)
    if not paths:
        return [AxisResolution(name=name, conf_paths=(), extends=())]  # caller decides if this is an error

    # extends is read from the highest-priority conf that has it.
    extends = ()
    for path in reversed(paths):
        e = _parse_extends_directive(path, verbose=search_kwargs.get("verbose", 0))
        if e:
            extends = e
            break

    on_path.append(name)
    out = []
    for parent in extends:
        out.extend(_resolve_axis(parent, search_kwargs, visited, on_path, _axis_cache))
    on_path.pop()

    axis = AxisResolution(name=name, conf_paths=tuple(paths), extends=extends)
    _axis_cache[name] = axis
    if name not in visited:
        visited.add(name)
        out.append(axis)
    return out


def resolve_variant(
    variant=None,
    argv=None,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=0,
    gitroot=None,
):
    """Resolve a --variant string into a VariantResolution.

    The flow:
      1. Split *variant* into atomic tokens on '.', ',', whitespace.
      2. Canonicalize their order using `variant-canonical-order` (or the
         builtin default if no ct.conf defines one).
      3. If a literal conf file matching the canonical dotted name exists
         anywhere in the hierarchy, use it as a composite override (its
         flags layer on top of the composed axes; its extends, if present,
         is ignored — the canonical decomposition is authoritative).
      4. Recursively resolve each token as an axis. An axis with
         `extends = ...` pulls in its parents (DFS, first-visit dedup,
         cycle detection).
      5. Build a flat priority-ordered list of conf paths:
            ct.conf hierarchy < axis_1 (bundled→project) < axis_2 < ... < composite_override < --config

    Raises VariantResolutionError when an axis has no conf file anywhere in
    the hierarchy, or when an extends-cycle is detected.
    """
    if variant is None:
        variant = extract_variant(
            argv,
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            exedir=exedir,
            verbose=verbose,
            gitroot=gitroot,
        )

    variant_str = str(variant) if variant is not None else ""
    search_kwargs = dict(
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose or 0,
        gitroot=gitroot,
    )

    canonical_order, canonical_order_source = get_canonical_order(**search_kwargs)
    tokens = split_variant(variant_str)
    canonical_tokens = canonicalize_variant_tokens(tokens, canonical_order)
    canonical_name = ".".join(canonical_tokens) if canonical_tokens else variant_str

    # Base ct.conf files, lowest-priority first
    base_ct_conf_files = tuple(reversed(get_existing_config_files(filename="ct.conf", **search_kwargs)))

    # Composite override file: a literal `<canonical_name>.conf` that the
    # user (or a project) wrote to *tune* the composition. Layers on top of
    # the synthesized atoms — semantically equivalent to a conf whose
    # `extends = <each canonical token>`. Authors who want different
    # inheritance write `extends = ...` explicitly in the composite, in
    # which case the explicit declaration wins.
    composite_override = None
    composite_extends = ()
    if len(canonical_tokens) > 1:
        composite_paths = _find_axis_confs(canonical_name, **search_kwargs)
        if composite_paths:
            composite_override = composite_paths[-1]
            composite_extends = _parse_extends_directive(composite_override)

    # --config=<path> takes highest priority (appended last). When the user
    # supplies one, the path itself is authoritative — we don't also require
    # an axis conf for every token of the implied variant name (the implied
    # name is just `basename(path).removesuffix('.conf')`, which usually
    # isn't a real axis). Axis resolution is therefore suppressed in this
    # branch; the explicit_config provides all values.
    explicit_config = extractconfig(argv) if argv is not None else None
    if explicit_config and not composite_override:
        return VariantResolution(
            raw_input=variant_str,
            canonical_name=canonical_name,
            axes=(),
            composite_override=None,
            base_ct_conf_files=base_ct_conf_files,
            canonical_order=tuple(canonical_order),
            canonical_order_source=canonical_order_source or "builtin",
            explicit_config=explicit_config,
        )

    # Resolve each axis. Missing axes are collected so we can report all of
    # them in one error rather than failing on the first.
    visited = set()
    on_path = []
    axis_cache = {}
    axes_out = []
    missing_axes = []
    if composite_override is None:
        # Pure synthesis from canonical tokens.
        chain_seed = canonical_tokens
    elif composite_extends:
        # Composite with explicit `extends = ...` — author picked the parents.
        chain_seed = composite_extends
    else:
        # Composite with no explicit extends — implicitly extends from each
        # canonical token so the composite "tunes on top" of the synthesized
        # atoms rather than replacing them.
        chain_seed = canonical_tokens

    for tok in chain_seed:
        # When a composite override exists and its extends= references the
        # composite itself (`extends = gcc.debug.asan` inside
        # gcc.debug.asan.conf), skip — the composite is already emitted
        # via composite_override and re-resolving would recurse. For pure
        # synthesis (no override) a token equal to the canonical name is
        # legitimate (single-axis variant `myrelease` whose canonical name
        # is also `myrelease`).
        if composite_override is not None and tok == canonical_name:
            continue
        chain = _resolve_axis(tok, search_kwargs, visited, on_path, axis_cache)
        for axis in chain:
            if not axis.conf_paths:
                missing_axes.append(axis.name)
            else:
                axes_out.append(axis)

    if missing_axes:
        searched_dirs = default_config_directories(
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            exedir=exedir,
            verbose=verbose or 0,
            gitroot=gitroot,
            current_dir=os.getcwd(),
        )
        joined = ", ".join(missing_axes)
        dirs = "\n  ".join(searched_dirs)
        raise VariantResolutionError(
            f"Could not find conf for axis(es): {joined}\n"
            f"  resolving --variant={variant_str!r} (canonical: {canonical_name})\n"
            f"  searched:\n  {dirs}"
        )

    return VariantResolution(
        raw_input=variant_str,
        canonical_name=canonical_name,
        axes=tuple(axes_out),
        composite_override=composite_override,
        base_ct_conf_files=base_ct_conf_files,
        canonical_order=tuple(canonical_order),
        canonical_order_source=canonical_order_source or "builtin",
        explicit_config=explicit_config,
    )


def config_files_from_variant(
    variant=None,
    argv=None,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=0,
    gitroot=None,
):
    """Backward-compatible flat-list view of resolve_variant().

    Returns conf files in low-to-high priority order, ready to feed to
    configargparse as `default_config_files`. New callers that need
    structured access (per-axis provenance, canonical-order source) should
    call resolve_variant() directly.
    """
    resolution = resolve_variant(
        variant=variant,
        argv=argv,
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose,
        gitroot=gitroot,
    )
    paths = resolution.flat_paths
    if verbose >= 1:
        print("Using config files = ")
        print(paths)
    return paths


def format_variant_resolution(resolution):
    """Human-readable trace of how a variant was resolved.

    Rendered unconditionally by parseargs() to answer the user's question
    'why did I get these flags?' — shows the contributing conf file per axis,
    the canonical-order source, the extends graph, and any composite override.
    """
    lines = []
    if resolution.raw_input == resolution.canonical_name:
        lines.append(f"Variant: {resolution.canonical_name}")
    else:
        lines.append(f"Variant: {resolution.raw_input!r}  ->  {resolution.canonical_name}  (canonicalized)")

    lines.append(f"Canonical order: {', '.join(resolution.canonical_order)}")
    lines.append(f"  source: {resolution.canonical_order_source}")
    lines.append("")

    lines.append("Base ct.conf files (low -> high priority):")
    if not resolution.base_ct_conf_files:
        lines.append("  (none found)")
    else:
        for p in resolution.base_ct_conf_files:
            lines.append(f"  {p}")
    lines.append("")

    lines.append("Axes (each axis lists its conf files low -> high priority):")
    if not resolution.axes:
        lines.append("  (none)")
    else:
        for axis in resolution.axes:
            ext = f"  extends = {', '.join(axis.extends)}" if axis.extends else ""
            lines.append(f"  [{axis.name}]{ext}")
            for p in axis.conf_paths:
                lines.append(f"      {p}")
    lines.append("")

    if resolution.composite_override:
        lines.append(f"Composite override (highest priority): {resolution.composite_override}")
    if resolution.explicit_config:
        lines.append(f"Explicit --config (highest priority): {resolution.explicit_config}")

    return "\n".join(lines)
