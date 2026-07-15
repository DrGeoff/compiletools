import logging
import os
import re
import sys
import types
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

logger = logging.getLogger(__name__)

# Variant tokens may be separated by '.', ',', or whitespace anywhere they appear:
# in --variant on the CLI, in the `variant = ...` line of ct.conf, and in the
# `extends = ...` directive inside an axis conf file. All three forms are
# interchangeable so users can pick whichever reads best.
_VARIANT_SEP_RE = re.compile(r"[\s,.]+")


# Built-in canonical ordering. A project may override the whole list via
# `variant-canonical-order = ...` in its ct.conf. Tokens NOT in this list are
# appended to the end of a resolution in user-typed order, so a project axis
# (e.g. `myproj`) can be tacked on without re-declaring the whole order.
_DEFAULT_VARIANT_CANONICAL_ORDER = (
    "blank",
    # toolchain
    "gcc",
    "ccache-gcc",
    "clang",
    "ccache-clang",
    "icc",
    "msvc",
    # language standard (C)
    "c99",
    "c11",
    "c17",
    "c23",
    # language standard (C++)
    "cxx11",
    "cxx14",
    "cxx17",
    "cxx20",
    "cxx23",
    "cxx26",
    # linker (mutually exclusive — choose one; -fuse-ld=<name> on LDFLAGS)
    "ld",
    "gold",
    "gold-nommap",
    "mold",
    "wild",
    "wild-B",
    # ABI / architecture
    "m32",
    "m64",
    "native",
    # optimization
    "debug",
    "release",
    "releasewithdebinfo",
    # sanitizers (mutually exclusive in practice — pick one)
    "asan",
    "ubsan",
    "tsan",
    "msan",
    # profiling / codegen
    "coverage",
    "lto",
    "pgo-gen",
    "pgo-use",
    # hardening / codegen flags
    "hardened",
    "pie",
    "static",
    "splitdebug",
    "strip",
    # codegen knobs
    "noexceptions",
    "nortti",
    "fastmath",
    "werror",
    "libcxx",
    # advanced / specialized
    "cfi",
    "shadow-call-stack",
    "time-trace",
    # opinionated bundles (composites of the above)
    "dev",
    "ci",
    "production",
    "safety",
    "perf",
    "secure",
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


@cache
def _parse_conf_file_cached(path):
    """Parse a conf file once per process and reuse the result.

    The full parseargs flow walks the conf hierarchy several times per
    invocation (extract_variant, resolve_variant, canonicalize_variant_input,
    a second resolve_variant from _commonsubstitutions). Each pass would
    otherwise re-open and re-parse the same files. Caching the parsed dict
    keyed on absolute path collapses that to one open per file per process.

    Returned as a ``types.MappingProxyType`` to enforce read-only access
    at runtime — the configargparse parser returns a mutable dict, and
    if any consumer were to mutate it the next caller would silently see
    the changes. The proxy turns that footgun into an ``AttributeError``
    on assignment. ``clear_cache()`` flushes the cache between tests.

    Raises OSError on a read failure. Failures are NOT cached (functools.cache
    only stores successful returns), so transient errors don't get pinned.
    """
    fileparser = CfgFileParser()
    with open(path, encoding="utf-8", errors="replace") as cfg:
        return types.MappingProxyType(fileparser.parse(cfg))


def extract_item_from_ct_conf_with_source(
    key,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    verbose=0,
    gitroot=None,
):
    """Extract ``key`` from the ct.conf hierarchy. Return ``(value, path)``.

    Walks from highest to lowest priority (project ct.conf overrides the
    bundled one) and returns the first match plus the path that defined
    it. Returns ``(None, None)`` if no ct.conf defines the key.

    Used by the provenance renderer to attribute config decisions back
    to their source file; ``extract_item_from_ct_conf`` is a value-only
    wrapper over this function.
    """
    for cfgpath in reversed(
        get_existing_config_files(
            filename="ct.conf",
            user_config_dir=user_config_dir,
            system_config_dir=system_config_dir,
            exedir=exedir,
            gitroot=gitroot,
        )
    ):
        items = _parse_conf_file_cached(cfgpath)
        if key in items:
            if verbose and verbose >= 2:
                print(f"{cfgpath} contains {key} = {items[key]}")
            return items[key], cfgpath
    return None, None


def extract_item_from_ct_conf(
    key,
    user_config_dir=None,
    system_config_dir=None,
    exedir=None,
    default=None,
    verbose=0,
    gitroot=None,
):
    """Value-only convenience over ``extract_item_from_ct_conf_with_source``.

    Returns the value from the highest-priority ct.conf that defines
    ``key``, or ``default`` if no ct.conf defines it. The provenance path
    is discarded.
    """
    value, _ = extract_item_from_ct_conf_with_source(
        key,
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose,
        gitroot=gitroot,
    )
    if value is None:
        return default
    return value


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
    argv=None,
):
    """Return (order_tuple, source_string).

    Priority (highest to lowest):
      1. ``--variant-canonical-order=<tokens>`` on the CLI (when argv supplied)
      2. ``CT_VARIANT_CANONICAL_ORDER`` env var
      3. ``variant-canonical-order = ...`` in any ct.conf in the hierarchy
      4. Builtin ``_DEFAULT_VARIANT_CANONICAL_ORDER``

    `source` describes which path won: ``"argv"``, ``"env:CT_VARIANT_CANONICAL_ORDER"``,
    the absolute path of the ct.conf that defined it, or ``"builtin"``. Used
    by ``format_variant_resolution`` to surface provenance in -vv traces.
    """
    # Mirror extract_variant: when no argv is supplied, fall back to
    # sys.argv so the CLI flag is still honored when ct-* entry points
    # call create_parser(argv=None) and let configargparse resolve
    # sys.argv internally.
    scan_argv = argv if argv is not None else sys.argv
    cli_value = extract_value_from_argv("variant-canonical-order", argv=scan_argv)
    if cli_value:
        return split_variant(str(cli_value)), "argv"

    env_value = os.environ.get("CT_VARIANT_CANONICAL_ORDER")
    if env_value:
        return split_variant(env_value), "env:CT_VARIANT_CANONICAL_ORDER"

    raw, source = extract_item_from_ct_conf_with_source(
        key="variant-canonical-order",
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose or 0,
        gitroot=gitroot,
    )
    if raw is None or source is None:
        return _DEFAULT_VARIANT_CANONICAL_ORDER, "builtin"
    return split_variant(str(raw)), source


def canonicalize_variant_tokens(tokens, canonical_order):
    """Reorder *tokens* by their position in *canonical_order*, deduplicating.

    The first occurrence of each token wins; later duplicates are dropped.
    Tokens not in the order list go to the end, preserving the user-typed
    order of their first appearance (so a project can add a new axis without
    re-declaring the whole order). Deduplication makes this a true canonical
    form — ``canon(canon(x)) == canon(x)`` — which the cell fixed-point check
    in trim_cache.enumerate_cells relies on.

    A well-formed composite variant draws each axis from a disjoint value set,
    so it contains no repeated token: dedup is a no-op on every legitimate
    input and only changes the (previously broken) duplicate case, e.g. a
    doubled ``--variant`` or a malformed ``extends``.
    """
    order_pos = {name: i for i, name in enumerate(canonical_order)}
    seen = set()
    known = []
    unknown = []
    for tok in tokens:
        if tok in seen:
            continue  # first occurrence wins; drop later duplicates
        seen.add(tok)
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
    argv=None,
):
    """Convert a raw --variant string into its canonical dotted form.

    `gcc,debug,asan`, `gcc debug asan`, `debug.gcc.asan` all collapse to
    `gcc.debug.asan` (assuming the default canonical order). A single
    token round-trips unchanged.

    Called from extract_variant() and from apptools._commonsubstitutions to
    canonicalize argparse-stored --variant values. When ``argv`` is supplied
    it is consulted for ``--variant-canonical-order=...`` (highest priority).
    """
    tokens = split_variant(variant_str)
    if not tokens:
        return variant_str
    order, _ = get_canonical_order(
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose,
        gitroot=gitroot,
        argv=argv,
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

    Returns paths in low-to-high priority order (bundled first, cwd last) —
    the order configargparse expects in ``default_config_files``, where
    later entries override earlier ones.
    """
    if "current_dir" not in kwargs or kwargs["current_dir"] is None:
        kwargs["current_dir"] = os.getcwd()
    directories = default_config_directories(**kwargs)

    configs = [os.path.join(directory, filename) for directory in reversed(directories)]

    existing_configs = [cfg for cfg in configs if compiletools.wrappedos.isfile(cfg)]

    if kwargs.get("verbose", 0) >= 8:
        print(" ".join(["Existing config files:"] + existing_configs))

    return existing_configs


@dataclass(frozen=True)
class TargetConfLayer:
    """One subproject's config layer, discovered by walking up from a target.

    conf_paths are in low-to-high priority order, ready to append to
    configargparse default_config_files.
    """

    subproject_dir: str
    conf_paths: tuple[str, ...]


def _conf_paths_in_dir(directory, conf_filenames):
    """Conf files for *directory* treated as a subproject layer.

    Mirrors the cwd layer's file selection: for each conf filename, the
    ``ct.conf.d/`` entry is lower priority than the bare-directory entry
    (same relative order ``get_existing_config_files`` produces for cwd).
    """
    found = []
    conf_d = os.path.join(directory, "ct.conf.d")
    for fname in conf_filenames:
        for sub in (conf_d, directory):
            candidate = os.path.join(sub, fname)
            if compiletools.wrappedos.isfile(candidate):
                found.append(candidate)
    return found


def walk_target_conf_layers(targets, conf_filenames=("ct.conf",), verbose=0):
    """Find each target's nearest-ancestor subproject config layer.

    Walks from ``dirname(realpath(target))`` up to (exclusive) the target's
    git root. The first level carrying any of *conf_filenames* — as a bare
    file or inside ``ct.conf.d/`` — becomes that target's layer. The gitroot
    itself is the project layer and is never yielded. For a target outside
    any git repository the walk starts at the target's own directory and is
    bounded only by the filesystem root; the nearest layer wins.

    Returns a tuple of TargetConfLayer sorted by subproject_dir for
    deterministic downstream ordering. Nonexistent targets are skipped;
    downstream code raises its own error for them.
    """
    layers = {}
    for target in targets:
        if not target:
            continue
        target_dir = compiletools.wrappedos.dirname(compiletools.wrappedos.realpath(target))
        if not compiletools.wrappedos.isdir(target_dir):
            continue
        gitroot = compiletools.wrappedos.realpath(compiletools.git_utils.find_git_root(target))
        # find_git_root falls back to the file's own directory when the target
        # is not under git; a real toplevel carries a .git dir (or gitlink
        # file for linked worktrees).
        in_git_repo = os.path.exists(os.path.join(gitroot, ".git"))
        current = target_dir
        while True:
            if in_git_repo and current == gitroot:
                break
            paths = _conf_paths_in_dir(current, conf_filenames)
            if paths:
                layers.setdefault(current, tuple(paths))
                break
            parent = compiletools.wrappedos.dirname(current)
            if parent == current:
                break
            current = parent
    if verbose >= 6 and layers:
        for directory, paths in sorted(layers.items()):
            print(f"Target-anchored config layer {directory}: {' '.join(paths)}")
    return tuple(
        TargetConfLayer(subproject_dir=directory, conf_paths=paths) for directory, paths in sorted(layers.items())
    )


class ConfContradictionError(RuntimeError):
    """Two same-tier config layers set the same key to different values."""


def _effective_layer_values(conf_paths):
    """Merge one layer's conf files (low-to-high) into key -> (raw_key, value, path).

    Keys are normalized (dashes to underscores) for comparison; the raw key
    is kept for display. Later files override earlier ones — intra-layer
    override is normal layering, never a contradiction.
    """
    values = {}
    for path in conf_paths:
        parsed = _parse_conf_file_cached(compiletools.wrappedos.realpath(path))
        for raw_key, value in parsed.items():
            normalized = raw_key.replace("-", "_")
            values[normalized] = (raw_key, str(value).strip().strip("\"'"), path)
    return values


def validate_no_conf_contradictions(layers, cwd_layer_paths, invocation_variant, remedy_commands):
    """Raise ConfContradictionError when same-tier layers disagree on a key.

    Tier peers: the cwd layer (when distinct from the gitroot/project layer)
    and every target-anchored subproject layer. append-*/prepend-* keys with
    differing values count as contradictions: applying both would leak each
    subproject's flags onto the other's translation units. A layer's
    ``variant`` is compared (canonicalized) against *invocation_variant*.
    """
    tier = []
    if cwd_layer_paths:
        tier.append(("current working directory", _effective_layer_values(cwd_layer_paths)))
    for layer in layers:
        tier.append((layer.subproject_dir, _effective_layer_values(layer.conf_paths)))

    canonical_invocation_variant = canonicalize_variant_input(invocation_variant)
    merged = {}
    conflicts = []
    for _, values in tier:
        for normalized, (raw_key, value, path) in values.items():
            compare_value = value
            if normalized == "variant":
                compare_value = canonicalize_variant_input(value)
                if compare_value != canonical_invocation_variant:
                    conflicts.append((raw_key, canonical_invocation_variant, "<invocation>", compare_value, path))
                    continue
            if normalized in merged:
                prev_value, prev_path = merged[normalized]
                if prev_value != compare_value:
                    conflicts.append((raw_key, prev_value, prev_path, compare_value, path))
            else:
                merged[normalized] = (compare_value, path)

    if not conflicts:
        return

    lines = ["ERROR: conflicting subproject configs in one invocation"]
    for raw_key, value_a, path_a, value_b, path_b in conflicts:
        lines.append(f"  {raw_key} = {value_a}   (from {path_a})")
        lines.append(f"  {raw_key} = {value_b}   (from {path_b})")
    lines.append("Choose one:")
    lines.append("  1) Build separately:")
    for command in remedy_commands:
        lines.append(f"       {command}")
    lines.append("  2) Make the conflicting values identical in the files above, then re-run.")
    raise ConfContradictionError("\n".join(lines))


def build_separate_build_commands(prog, argv, layers, targets):
    """Best-effort per-subproject command reconstruction for the error remedy.

    For each layer, reproduce the original argv minus target tokens that
    belong to a DIFFERENT layer. Handles bare positional targets,
    ``--flag value`` (the value token is dropped; a flag token left with no
    following value is dropped too), and ``--flag=value`` forms. Targets
    under no conflicting layer (shared sources) stay in every command.
    """
    target_realpaths = {}
    for target in targets:
        target_realpaths[compiletools.wrappedos.realpath(target)] = target

    def owning_layer(token):
        real = compiletools.wrappedos.realpath(token)
        if real not in target_realpaths:
            return None
        for layer in layers:
            prefix = compiletools.wrappedos.realpath(layer.subproject_dir) + os.sep
            if real.startswith(prefix):
                return layer.subproject_dir
        return None

    commands = []
    for layer in layers:
        keep_dir = layer.subproject_dir
        kept = []
        for i, token in enumerate(argv):
            candidate = token.split("=", 1)[1] if token.startswith("--") and "=" in token else token
            owner = owning_layer(candidate)
            if owner is not None and owner != keep_dir:
                if (
                    candidate is token
                    and kept
                    and kept[-1] == argv[i - 1]
                    and kept[-1] in ("--tests", "--static", "--dynamic")
                ):
                    kept.pop()
                continue
            kept.append(token)
        commands.append(" ".join([prog] + kept))
    return commands


def clear_cache():
    """Clear LRU caches for testing"""
    default_config_directories.cache_clear()
    _parse_conf_file_cached.cache_clear()
    _extends_order_warnings_emitted.clear()


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
            so later files override scalar keys; append-/prepend- keys
            accumulate across the entire conf hierarchy via
            ``apptools._ComposingArgumentParser`` (stock configargparse only
            keeps the highest-priority conf's value for action='append').
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
    composite_paths: tuple[str, ...] = ()
    base_ct_conf_files: tuple[str, ...] = ()
    canonical_order: tuple[str, ...] = field(default_factory=tuple)
    canonical_order_source: str = "builtin"
    explicit_config: str | None = None

    @property
    def flat_paths(self):
        """All conf files in low-to-high priority order, for configargparse.

        All composite overrides (bundled <name>.conf, project <name>.conf,
        cwd <name>.conf, ...) are included so multi-level composites compose
        the same way as multi-level axis confs. ``composite_override`` is
        kept as a convenience alias for the highest-priority composite path,
        the one whose ``extends = ...`` directive (if any) steers chain seed
        selection.
        """
        result = list(self.base_ct_conf_files)
        for axis in self.axes:
            result.extend(axis.conf_paths)
        result.extend(self.composite_paths)
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
    try:
        items = _parse_conf_file_cached(conf_path)
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


# Tracks conf paths we've already warned about so repeated resolve_variant
# calls within the same process don't emit the same warning multiple times.
#
# TEST AUTHORS: this is process-global mutable state. The first occurrence of
# a given conf_path within a process emits the warning; subsequent occurrences
# are skipped. Tests that exercise the warning path (caplog assertions on the
# "not in canonical order" message) MUST call ``clear_cache()`` before the
# resolve_variant call, or a prior test in the same process can swallow the
# warning. The existing test (test_user_conf_with_out_of_order_extends_emits_warning)
# does this; new tests must follow suit.
_extends_order_warnings_emitted: set = set()


def _check_extends_canonical_order(conf_path, extends, canonical_order):
    """Emit a logger warning if ``extends`` parents are out of canonical order.

    The resolver walks ``extends`` in declared order; configargparse layers
    scalar keys last-writer-wins and accumulates ``append-*`` form in load
    order. So ``extends = werror, gcc`` produces different flag layering
    than the equivalent ``--variant=gcc,werror`` CLI form. This warning
    surfaces the inconsistency at parseargs time, naming the file and
    showing the fix.

    Tokens not in canonical_order are skipped (the resolver itself surfaces
    truly-unknown axes via "missing axis" errors elsewhere). Emitted via
    ``logging.getLogger(__name__).warning(...)`` so users can suppress
    per-module via ``logging.getLogger('compiletools.configutils').setLevel(logging.ERROR)``.
    One warning per process per offending path — repeated resolve_variant
    calls within one process don't repeat the message.
    """
    if not canonical_order or conf_path in _extends_order_warnings_emitted:
        return
    position = {tok: i for i, tok in enumerate(canonical_order)}
    positions: list[int] = []
    for tok in extends:
        pos = position.get(tok)
        if pos is None:
            return  # unknown axis: skip the order check entirely
        positions.append(pos)
    if positions != sorted(positions):
        expected = sorted(extends, key=lambda t: position[t])
        _extends_order_warnings_emitted.add(conf_path)
        logger.warning(
            "%s: `extends = ...` is not in canonical order.\n"
            "  got:      extends = %s\n"
            "  expected: extends = %s\n"
            "  Out-of-order extends produces different flag layering than\n"
            "  the equivalent --variant=%s CLI form\n"
            "  (the resolver walks extends in declared order, configargparse\n"
            "  layers in load order). Reorder for parity.",
            conf_path,
            ", ".join(extends),
            ", ".join(expected),
            ",".join(expected),
        )


def _find_axis_confs(name, **kwargs):
    """Return conf files for an axis in ascending priority order (low → high).

    `get_existing_config_files` returns lowest-priority first (bundled
    before cwd), which is already the order configargparse wants in
    ``default_config_files`` (later overrides earlier).
    """
    return list(get_existing_config_files(filename=f"{name}.conf", **kwargs))


def _resolve_axis(name, search_kwargs, visited, on_path, _axis_cache, canonical_order=()):
    """DFS resolve one axis. Returns ordered list of AxisResolution.

    visited: set of axis names already emitted (for diamond dedup)
    on_path: list of axis names currently in the recursion stack (preserves
        traversal order so cycle diagnostics show the actual path through
        the graph). Membership check is O(len(on_path)) but the stack is
        typically tiny (depth ~3).
    canonical_order: when non-empty, validates ``extends = ...`` parents
        against the canonical order via _check_extends_canonical_order
        (warning only, never a hard fail).

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
    extends_source = None
    for path in reversed(paths):
        e = _parse_extends_directive(path, verbose=search_kwargs.get("verbose", 0))
        if e:
            extends = e
            extends_source = path
            break

    if extends_source is not None and canonical_order:
        _check_extends_canonical_order(extends_source, extends, canonical_order)

    on_path.append(name)
    out = []
    for parent in extends:
        out.extend(_resolve_axis(parent, search_kwargs, visited, on_path, _axis_cache, canonical_order=canonical_order))
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
         anywhere in the hierarchy, use it as a composite override:
           - By default the composite layers on top of the canonical-token
             atoms (semantically equivalent to a conf with
             `extends = <each canonical token>`), so a tuned composite
             "tunes on top of" synthesized atoms.
           - If the composite specifies `extends = ...` explicitly, that
             list of parents replaces the implicit canonical-token
             derivation (use `extends = blank` to opt out of composition).
           - When several composite files exist in the hierarchy (bundled
             + project), the highest-priority file's `extends` value wins
             — same rule as every other scalar key under configargparse.
      4. Recursively resolve each chain-seed token as an axis. An axis
         with `extends = ...` pulls in its parents (DFS, first-visit
         dedup, cycle detection).
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

    canonical_order, canonical_order_source = get_canonical_order(argv=argv, **search_kwargs)
    tokens = split_variant(variant_str)
    canonical_tokens = canonicalize_variant_tokens(tokens, canonical_order)
    canonical_name = ".".join(canonical_tokens) if canonical_tokens else variant_str

    # Base ct.conf files in lowest-to-highest priority order as
    # configargparse expects (later entries in default_config_files
    # override earlier ones). get_existing_config_files already returns
    # the right order (cwd ct.conf LAST -> overrides bundled).
    base_ct_conf_files = tuple(get_existing_config_files(filename="ct.conf", **search_kwargs))

    # Composite override file: a literal `<canonical_name>.conf` that the
    # user (or a project) wrote to *tune* the composition. Layers on top of
    # the synthesized atoms — semantically equivalent to a conf whose
    # `extends = <each canonical token>`. Authors who want different
    # inheritance write `extends = ...` explicitly in the composite, in
    # which case the explicit declaration wins.
    composite_override = None
    composite_extends = ()
    composite_paths_tuple = ()
    if len(canonical_tokens) > 1:
        composite_paths = _find_axis_confs(canonical_name, **search_kwargs)
        if composite_paths:
            composite_paths_tuple = tuple(composite_paths)
            composite_override = composite_paths[-1]
            composite_extends = _parse_extends_directive(composite_override)
            if composite_extends:
                _check_extends_canonical_order(composite_override, composite_extends, canonical_order)

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
            canonical_order_source=canonical_order_source,
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
        chain = _resolve_axis(tok, search_kwargs, visited, on_path, axis_cache, canonical_order=canonical_order)
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
        composite_paths=composite_paths_tuple,
        base_ct_conf_files=base_ct_conf_files,
        canonical_order=tuple(canonical_order),
        canonical_order_source=canonical_order_source,
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
