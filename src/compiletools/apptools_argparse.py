"""CLI argument registration and the configargparse subclasses for compiletools.

Extracted from :mod:`compiletools.apptools`. This module owns:

* Every ``add_*`` argument registrar (``add_base_arguments``,
  ``add_common_arguments``, ``add_locking_arguments``, ``add_cas_arguments``,
  ``add_link_arguments``, ``add_cas_directory_arguments``,
  ``add_output_directory_arguments``, ``add_otel_export_arguments``,
  ``add_target_arguments``, ``add_target_arguments_ex``) plus
  ``parser_has_option`` / ``_add_xxpend_argument`` / ``_add_xxpend_arguments``.
* ``create_parser`` and the two configargparse subclasses
  ``_AccumulatingConfigFileParser`` / ``_ComposingArgumentParser`` (the
  append/prepend accumulation + ``${CONF_DIR}`` provenance machinery).
* The conf-file value helpers (``_open_conf_file_utf8``, ``_expand_conf_dir``,
  ``_expand_env_and_user``) and their sentinel constants.
* ``resolve_cas_directory_arguments``, ``validate_otel_timing_pair``,
  ``_user_passed_no_timing``, ``_fix_variable_handling_method``.

Every public name is re-exported from :mod:`compiletools.apptools` by binding,
so ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
importers, and ``unittest.mock.patch`` targets keep working with identical
object identity.

Cycle management:

* Leaf modules (``configutils``, ``git_utils``, ``utils``, ``wrappedos``,
  ``apptools_validate``, ``version``) are imported directly at module level.
* The handful of apptools-core symbols this module needs — the
  ``_UNSUPPLIED_*`` sentinels and ``unsupplied_replacement`` /
  ``_ensure_variant_suffix`` (whose canonical home is the substitution core)
  — are reached through a deferred ``import compiletools.apptools`` inside the
  four functions that use them. apptools imports THIS module at its top for
  re-export, so a top-level back-import would form a hard cycle; the deferred
  import is safe because apptools is fully initialised by the time any
  registrar / resolver runs. This mirrors the documented escape hatch used by
  ``apptools_validate``.
* ``git_utils`` keeps importing the ``compiletools.apptools`` facade (NOT this
  module) for ``parser_has_option`` / ``create_parser`` — the pre-existing
  cycle, left untouched; those resolve via re-export.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from collections import OrderedDict

import configargparse

import compiletools.apptools_validate
import compiletools.configutils
import compiletools.git_utils
import compiletools.utils
import compiletools.version
import compiletools.wrappedos

_rich_rst_available = importlib.util.find_spec("rich_rst") is not None

if _rich_rst_available and sys.version_info >= (3, 9):

    class DocumentationAction(argparse.BooleanOptionalAction):
        def __init__(self, option_strings, dest):
            super().__init__(
                option_strings=option_strings,
                dest=dest,
                default=None,
                required=False,
                help="Show the documentation/manual page",
            )

        def __call__(self, parser, namespace, values, option_string=None):
            if (
                option_string is not None
                and option_string in self.option_strings
                and not option_string.startswith("--no-")
            ):
                import inspect

                import rich
                from rich_rst import RestructuredText

                this_dir = os.path.dirname(compiletools.wrappedos.realpath(inspect.getsourcefile(lambda: 0)))
                doc_filename = os.path.join(this_dir, f"README.{parser.prog}.rst")
                try:
                    with open(doc_filename) as docfile:
                        text = docfile.read()
                        rich.print(RestructuredText(text))
                except FileNotFoundError:
                    rich.print("No man/doc available")

                sys.exit(0)


def parser_has_option(cap, option_string):
    """Check whether *cap* already has an action for *option_string*.

    Used by every backend's ``add_arguments(cap)`` to remain idempotent.
    """
    return any(option_string in a.option_strings for a in cap._actions)


# Backwards-compat alias for callers that imported the underscored form.
_parser_has_option = parser_has_option


def add_base_arguments(cap, argv=None, variant=None):
    """All compiletools applications MUST call this function.

    Note that it is usually called indirectly from add_common_arguments.

    Safe to call more than once on the same parser — duplicate calls are
    silently ignored.
    """
    if _parser_has_option(cap, "--variant"):
        return

    # Even though the variant is actually sucked out of the command line by
    # parsing the sys.argv directly, we put it into the configargparse to get
    # the help.
    if variant is None:
        variant = compiletools.configutils.extract_variant(argv=argv)

    cap.add_argument(
        "--variant",
        help="Specifies which variant of the config should be used. Use the config name without the .conf. "
        "Composite variants compose multiple axis confs: --variant=gcc,debug,asan (or gcc.debug.asan, or "
        "gcc debug asan — all equivalent).",
        default=variant,
    )
    # The CLI flag and env var registered here are honored two ways:
    # (a) configutils.get_canonical_order scans argv / reads os.environ
    #     DIRECTLY at create_parser time (before configargparse parses),
    #     because canonical_order is needed to canonicalize the variant
    #     before conf files are loaded. That direct read is what makes
    #     --variant-canonical-order actually steer resolution.
    # (b) This registration also lets configargparse store the value in
    #     args.variant_canonical_order post-parse and surfaces the flag
    #     in --help / man pages. Both reads honor the same env var, so
    #     they agree on the value.
    cap.add_argument(
        "--variant-canonical-order",
        env_var="CT_VARIANT_CANONICAL_ORDER",
        help="Override the canonical token ordering used to sort composite "
        "--variant tokens. Comma/space/dot separated. Precedence: CLI > env "
        "CT_VARIANT_CANONICAL_ORDER > ct.conf `variant-canonical-order = ...` "
        "> builtin _DEFAULT_VARIANT_CANONICAL_ORDER. Rarely needed; the "
        "builtin covers all bundled axes and composite bundles. NOTE: "
        "overriding the order makes the bundled composite confs (dev / ci / "
        "production / safety / perf / secure) emit the out-of-canonical-order "
        "warning, because their `extends = ...` was authored against the "
        "builtin order. That's intentional — if you change the order, you've "
        "taken responsibility for the flag-layering consequences.",
        default=None,
    )
    cap.add_argument(
        "-v",
        "--verbose",
        help="Output verbosity. Add more v's to make it more verbose",
        action="count",
        default=0,
    )
    cap.add_argument(
        "-q",
        "--quiet",
        help="Decrement verbosity. Useful in apps where the default verbosity > 0.",
        action="count",
        default=0,
    )
    cap.add_argument("--version", action="version", version=compiletools.version.__version__)
    cap.add_argument("-?", action="help", help="Help")

    # Opt-in to legacy permissive gitroot-marker walker. By default,
    # ``compiletools.git_utils._find_git_root`` requires a real ``.git``
    # (regular-file gitlink or directory with HEAD) so a stray empty
    # ``/tmp/.git`` left by another user can't silently become the gitroot
    # for every build running under ``/tmp/...``. Users who deliberately
    # drop bare ``.git`` placeholders as project-root markers can flip this
    # on to restore the prior behaviour.
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="allow-fake-git",
        dest="allow_fake_git",
        default=False,
        help=(
            "Permit the gitroot fallback walker to accept a bare '.git' "
            "file/dir without verifying it's a real git repository "
            "(legacy/dummy marker support). Default False: only real "
            "markers are accepted — a regular-file '.git' (worktree "
            "gitlink) or a directory containing HEAD."
        ),
    )

    if _rich_rst_available and sys.version_info >= (3, 9):
        cap.add_argument("--man", "--doc", action=DocumentationAction)


def _add_xxpend_argument(cap, name, destname=None, extrahelp=None):
    """Add a prepend flags argument and an append flags argument to the config arg parser"""
    if destname is None:
        destname = name

    if extrahelp is None:
        extrahelp = ""

    xxlist = ("prepend", "append")
    for xx in xxlist:
        cap.add_argument(
            "".join(["--", xx, "-", name.upper()]),
            dest="_".join([xx, destname.lower().replace("-", "_")]),
            action="append",
            default=[],
            help=" ".join(
                [
                    xx.title(),
                    "the given text to the",
                    name.upper(),
                    "already set. Useful for adding search paths etc.",
                    extrahelp,
                ]
            ),
        )


def _add_xxpend_arguments(cap, xxpendableargs):
    """Add prepend-BLAH and append-BLAH for the common flags"""
    for arg in xxpendableargs:
        _add_xxpend_argument(cap, arg)


def add_common_arguments(cap, argv=None, variant=None):
    """Insert common arguments into the configargparse object.

    Safe to call more than once on the same parser — duplicate calls are
    silently ignored.
    """
    if _parser_has_option(cap, "--variable-handling-method"):
        return
    # Deferred import: the ``_UNSUPPLIED_*`` sentinels stay in the
    # apptools core (their canonical consumer is
    # ``apptools.unsupplied_replacement``); apptools imports this
    # module for re-export, so reaching back through the facade at
    # call time is the accepted cycle-break (same pattern as
    # apptools_validate). apptools is fully initialised by then.
    import compiletools.apptools as _apptools

    add_base_arguments(cap, argv=argv, variant=variant)
    cap.add_argument(
        "--variable-handling-method",
        dest="variable_handling_method",
        help="Does specifying --<someflag> (say CXXFLAGS) mean override existing flags "
        "or append to the existing? Choices are override or append.",
        default="override",
    )
    cap.add_argument(
        "--ID",
        help="Compiler identification string.  The same string as CMake uses.",
        default=None,
    )
    cap.add_argument("--CPP", help="C preprocessor (override)", default=_apptools._UNSUPPLIED_USE_CXX)
    cap.add_argument("--CC", help="C compiler (override)", default="gcc")
    # Default will be set later using functional compiler detection
    cap.add_argument("--CXX", help="C++ compiler (override)", default=None)
    cap.add_argument(
        "--CPPFLAGS",
        nargs="+",
        help="C preprocessor flags (override)",
        default=_apptools._UNSUPPLIED_USE_CXXFLAGS,
    )
    cap.add_argument("--CXXFLAGS", nargs="+", help="C++ compiler flags (override)", default="-fPIC -g -Wall")
    cap.add_argument("--CFLAGS", nargs="+", help="C compiler flags (override)", default="-fPIC -g -Wall")
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="git-root",
        dest="git_root",
        default=True,
        help="Determine the git root then add it to the include paths.",
    )
    cap.add_argument(
        "--ffile-prefix-map-target",
        dest="ffile_prefix_map_target",
        default=".",
        help=(
            "RHS of the auto-injected -ffile-prefix-map=<gitroot>=<target> "
            "flag added to CXXFLAGS / CFLAGS for cross-user CAS sharing "
            "(Round 3). Default '.' matches the Debian fixfilepath "
            "convention; gdb resolves automatically via $cwd when run "
            "from the workspace. VSCode-heavy teams may prefer a sentinel "
            "like '/__ct__/' paired with a sourceFileMap entry. If the "
            "user manually sets any -f{file,debug,macro,canon}-prefix-map= "
            "in their flags, the auto-injection is skipped (user choice "
            "wins, per slot independently)."
        ),
    )
    cap.add_argument(
        "--INCLUDE",
        "--include",
        dest="INCLUDE",
        nargs="+",
        default="",
        help="Extra path(s) to add to the list of include paths (override)",
    )
    cap.add_argument(
        "--pkg-config",
        dest="pkg_config",
        help="Query pkg-config to obtain libs and flags for these packages.",
        action="append",
        default=[],
    )
    _add_xxpend_argument(
        cap,
        "pkg-config",
        extrahelp=(
            "Merged into the --pkg-config package list; prepend lands "
            "leftmost, append lands rightmost. Use the append-/prepend- "
            "form in conf files so values accumulate across the variant "
            "hierarchy instead of last-writer-wins clobbering the bare "
            "pkg-config = ... key."
        ),
    )
    _add_xxpend_argument(
        cap,
        "pkg-config-path",
        extrahelp="Directories are applied to the PKG_CONFIG_PATH environment variable.",
    )
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="separate-flags-CPP-CXX",
        dest="separate_flags_CPP_CXX",
        default=False,
        help="Keep CPPFLAGS and CXXFLAGS separate instead of unified.",
    )
    compiletools.git_utils.NameAdjuster.add_arguments(cap)
    _add_xxpend_arguments(cap, xxpendableargs=("include", "cppflags", "cflags", "cxxflags"))
    add_locking_arguments(cap)


def add_locking_arguments(cap):
    """Add file locking configuration arguments.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--file-locking"):
        return
    compiletools.utils.add_boolean_argument(
        parser=cap,
        name="file-locking",
        dest="file_locking",
        default=True,
        help="Enable file locking for concurrent multi-user/multi-host builds",
    )
    cap.add_argument(
        "--lock-cross-host-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for cross-host locks before escalating warnings (default: 600 = 10 min)",
    )
    cap.add_argument(
        "--lock-warn-interval",
        type=int,
        default=60,
        help="Interval in seconds between lock wait warnings (default: 60)",
    )
    cap.add_argument(
        "--sleep-interval-lockdir",
        type=float,
        default=None,
        help="Sleep interval for lockdir polling (NFS/Lustre) (default: auto-detect based on filesystem)",
    )
    cap.add_argument(
        "--sleep-interval-cifs",
        type=float,
        default=0.2,
        help="Sleep interval for CIFS lock polling (default: 0.2)",
    )
    cap.add_argument(
        "--sleep-interval-flock-fallback",
        type=float,
        default=0.1,
        help="Sleep interval for flock fallback polling (default: 0.1)",
    )


def add_cas_arguments(cap):
    """Add CAS rebuild-policy arguments.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--use-mtime"):
        return
    compiletools.utils.add_boolean_argument(
        parser=cap,
        name="use-mtime",
        dest="use_mtime",
        default=False,
        help=(
            "Use file mtime as a rebuild signal in compile rules. Default "
            "(False): rely solely on the content-addressable object name "
            "(cas-objdir/<basename>_<file_h>_<dep_h>_<macro_h>.o) and skip "
            "compilation when the cached object exists. Set True to restore "
            "classical Make/Ninja prerequisite-mtime semantics, which forces "
            "recompilation when sources are newer than the cached object — "
            "defeating CAS reuse on fresh-checkout CI where every source has "
            "mtime=now. Only honored by --backend=make and --backend=ninja: "
            "the cmake/bazel/shake/slurm backends use their own "
            "(content-hash or self-managed) change detection and cannot "
            "deliver mtime semantics, so setting this flag True on one of "
            "them is a hard error rather than a silent no-op."
        ),
    )


def add_link_arguments(cap):
    """Insert the link arguments into the parser.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--LD"):
        return
    # Deferred import: the ``_UNSUPPLIED_*`` sentinels stay in the
    # apptools core (their canonical consumer is
    # ``apptools.unsupplied_replacement``); apptools imports this
    # module for re-export, so reaching back through the facade at
    # call time is the accepted cycle-break (same pattern as
    # apptools_validate). apptools is fully initialised by then.
    import compiletools.apptools as _apptools

    cap.add_argument("--LD", help="Linker (override)", default=_apptools._UNSUPPLIED_USE_CXX)
    cap.add_argument(
        "--LDFLAGS",
        "--LINKFLAGS",
        help="Linker flags (override)",
        default=_apptools._UNSUPPLIED_USE_CXXFLAGS,
    )
    _add_xxpend_argument(cap, "ldflags")
    _add_xxpend_argument(
        cap,
        "linkflags",
        destname="ldflags",
        extrahelp="Synonym for setting LDFLAGS.",
    )


def add_cas_directory_arguments(cap, variant):
    """Register the four ``--cas-{obj,pch,pcm,exe}dir`` flags on *cap*.

    Variant-aware defaults: each CAS lives at
    ``<git_root>/cas-<kind>dir/<variant>``, where ``<git_root>`` is the
    value ``find_git_root()`` resolves (the real git toplevel, an
    ``--allow-fake-git``-accepted marker, or the current directory as a
    fallback when no marker is found).

    The registrar deliberately stores the literal sentinel
    ``"unsupplied"`` as the argparse default; the real path is computed
    by ``resolve_cas_directory_arguments`` AFTER the post-parse
    ``set_allow_fake_git`` propagation. This lets ``--allow-fake-git``
    actually steer the gitroot lookup (issue: registrar-time
    ``find_git_root()`` ran with strict mode and baked the wrong
    answer into argparse defaults; ``unsupplied_replacement`` only
    swaps on the literal ``"unsupplied"`` string).

    Sliced out of ``add_output_directory_arguments`` so read-only tools
    (e.g. ``ct-cache-report``) can register exactly the four CAS flags
    without inheriting unrelated build-only knobs (``--bindir``,
    ``--use-mtime``).

    Callers that parse via ``cap.parse_args(argv)`` directly (instead
    of routing through ``apptools.parseargs(...)``) MUST follow up with
    ``resolve_cas_directory_arguments(args)`` to apply the
    ``unsupplied``-sentinel fallback and the variant-suffix auto-append
    — otherwise ``args.cas_*dir`` will hold the bare conf value, not
    the variant-suffixed path ct-cake writes to. The contract is
    grep-enforced by ``test_cas_dir_resolver_contract.py``.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--cas-objdir"):
        return
    # Defaults are the literal sentinel "unsupplied"; the real path is
    # computed inside ``resolve_cas_directory_arguments`` after
    # ``set_allow_fake_git`` has propagated. See docstring.
    cap.add_argument(
        "--cas-objdir",
        help=(
            "Output directory for object files (content-addressable store). "
            "Defaults to <git_root>/cas-objdir/<variant> (git_root falls back "
            "to the current directory when no repo marker is found). If the "
            "supplied path does not already end in /<variant>, the active "
            "variant is appended automatically so the layer stays separated "
            "per variant."
        ),
        default="unsupplied",
    )
    cap.add_argument(
        "--cas-pchdir",
        help=(
            "Output directory for precompiled header cache (content-addressable store). "
            "Defaults to <git_root>/cas-pchdir/<variant> (git_root falls back "
            "to the current directory when no repo marker is found). If the "
            "supplied path does not already end in /<variant>, the active "
            "variant is appended automatically so the layer stays separated "
            "per variant."
        ),
        default="unsupplied",
    )
    cap.add_argument(
        "--cas-pcmdir",
        help=(
            "Output directory for precompiled C++20 module cache (content-addressable store). "
            "Defaults to <git_root>/cas-pcmdir/<variant> (git_root falls back "
            "to the current directory when no repo marker is found). If the "
            "supplied path does not already end in /<variant>, the active "
            "variant is appended automatically so the layer stays separated "
            "per variant."
        ),
        default="unsupplied",
    )
    cap.add_argument(
        "--cas-exedir",
        help=(
            "Output directory for the content-addressable executable cache. "
            "The link rule writes to <cas-exedir>/<shard>/<name>_<linkkey>.exe; "
            "the user-facing bin/<variant>/<name> is a hard link (with symlink "
            "fallback for cross-filesystem cases) to that file. Sharing this "
            "directory across CI runners makes link rules reusable across "
            "fresh checkouts. Defaults to <git_root>/cas-exedir/<variant> "
            "(git_root falls back to the current directory when no repo marker "
            "is found). If the supplied path does not already end in /<variant>, "
            "the active variant is appended automatically so the layer stays "
            "separated per variant."
        ),
        default="unsupplied",
    )


def resolve_cas_directory_arguments(args):
    """Apply the unsupplied-sentinel defaults and variant-suffix
    auto-append to ``args.cas_objdir`` / ``cas_pchdir`` / ``cas_pcmdir``
    / ``cas_exedir`` using ``args.variant`` as the suffix, then anchor
    any *relative* cas dir to the gitroot. Idempotent.

    Gitroot-anchoring (``os.path.join(git_root, value)``, a no-op for
    already-absolute values) makes a relative ``--cas-*dir`` mean
    "relative to the gitroot" — matching the gitroot-anchored default.
    It is applied only when the gitroot differs from the invocation cwd
    (i.e. ct-cake was invoked from a subdir of the gitroot — the only
    case that trips the bug below). From the gitroot itself, or outside
    any repo (where ``find_git_root()`` falls back to the cwd), a
    relative value is left as-is, preserving the documented
    bare-relative-stays-literal contract (``test_conf_env_expansion``).
    This is load-bearing for two reasons:

    * The PCH/PCM precompile rules run under ``cwd=anchor_root``
      (``cd <gitroot> && g++ ... -o <cas-path>``) for cross-user
      byte-identity; a relative ``-o`` would resolve against the gitroot
      after the ``cd`` rather than the invocation cwd, so building from a
      subdir of the gitroot with a relative cas dir failed with "cannot
      create precompiled header ...: No such file or directory". See
      ``test_relative_cas_dir_bug.py``.
    * ``canonicalize_path_for_cache_key`` is a textual string-prefix op,
      so cross-user cache-key stability needs the cas-dir string to share
      the exact ``anchor_root`` prefix. Anchoring with the same
      ``find_git_root()`` value the build's ``anchor_root`` uses
      guarantees that; cwd-based ``abspath`` would not under
      symlinked / NFS-automounted checkouts.

    REQUIRED follow-up to ``add_cas_directory_arguments`` when the
    caller parses with ``cap.parse_args(argv)`` directly instead of
    routing through ``apptools.parseargs(...)``. Diagnostic-only tools
    that bypass ``parseargs`` (ct-cache-report, ct-trim-cache,
    ct-cleanup-locks) call this so they see the same resolved paths
    ct-cake writes to. The contract is enforced by
    ``test_cas_dir_resolver_contract.py``.

    Uses ``args.variant`` (the post-parse value) rather than an
    early-extracted variant: ``configutils.extract_variant(argv)``
    can return a stale value when a ``--config`` file's basename is
    interpreted as an axis (the working precedent inside
    ``_commonsubstitutions`` always used ``args.variant``).

    Missing attrs on ``args`` are tolerated — the resolver only
    touches attributes that were registered by
    ``add_cas_directory_arguments`` (or its caller
    ``add_output_directory_arguments``).

    Diagnostic-only tools (ct-cleanup-locks, ct-trim-cache,
    ct-cache-report, ct-timing-report) that register
    ``--allow-fake-git`` via ``add_base_arguments`` but never go
    through ``apptools.parseargs`` rely on the
    ``set_allow_fake_git`` propagation at the top of this function:
    without it, the flag would be a silent no-op for them.
    """
    # Deferred import: ``unsupplied_replacement`` / ``_ensure_variant_suffix``
    # and the ``_UNSUPPLIED_*`` sentinels stay in the apptools core.
    # apptools imports this module for re-export, so reaching back
    # through the facade at call time is the accepted cycle-break
    # (same pattern as apptools_validate). apptools is fully
    # initialised by call time.
    import compiletools.apptools as _apptools

    # Propagate --allow-fake-git into the git_utils module-level setting
    # BEFORE any find_git_root() call below resolves a default. This is the
    # single canonical propagation point: parseargs ALSO calls
    # set_allow_fake_git (redundantly, kept for explicitness), but the
    # diagnostic-only tools that bypass parseargs rely on it firing here.
    compiletools.git_utils.set_allow_fake_git(getattr(args, "allow_fake_git", False))

    variant = args.variant
    # Only gitroot-anchor a relative cas dir when the gitroot actually differs
    # from the invocation cwd -- i.e. ct-cake was invoked from a subdir of the
    # gitroot, which is the only case that trips the precompile-rule bug
    # (``cd <gitroot> && -o <relpath>``). When there is no real repo,
    # ``find_git_root()`` falls back to returning the cwd, so this guard also
    # leaves bare-relative cas dirs untouched outside a repo (the documented
    # no-auto-anchor contract; see test_conf_env_expansion).
    #
    # The per-block ``git_root`` is realpath'd via the cached wrappedos wrapper
    # (the same value across all four blocks -> three cache hits); ``cwd_real``
    # is a one-off direct read, NOT cached. Both inputs are absolute strings, so
    # neither is subject to the chdir footgun (CLAUDE.md "wrappedos" Caveat #3).
    cwd_real = os.path.realpath(os.getcwd())

    # ``find_git_root()`` always returns a usable absolute root: the real git
    # toplevel, an ``--allow-fake-git``-accepted marker, or (when nothing is
    # found) the queried directory / cwd as a fallback. It never returns a
    # falsy value, so cas dirs are always anchored at that root -- there is no
    # "no gitroot" bindir-relative fallback branch.
    def _resolve(attr, kind, registered):
        if not registered:
            return None
        git_root = compiletools.git_utils.find_git_root()
        default_value = os.path.join(git_root, f"cas-{kind}dir", variant)
        current = getattr(args, attr)
        new = _apptools.unsupplied_replacement(current, default_value, args.verbose, f"cas-{kind}dir")
        new = _apptools._ensure_variant_suffix(new, variant)
        if compiletools.wrappedos.realpath(git_root) != cwd_real:
            new = compiletools.wrappedos.normpath(os.path.join(git_root, new))
        setattr(args, attr, new)
        return git_root

    _resolve("cas_objdir", "obj", hasattr(args, "cas_objdir"))
    _resolve("cas_pchdir", "pch", hasattr(args, "cas_pchdir"))
    _resolve("cas_pcmdir", "pcm", hasattr(args, "cas_pcmdir"))

    if hasattr(args, "cas_exedir"):
        git_root_exe = compiletools.git_utils.find_git_root()
        default_cas_exedir = os.path.join(git_root_exe, "cas-exedir", variant)
        args.cas_exedir = _apptools.unsupplied_replacement(
            args.cas_exedir, default_cas_exedir, args.verbose, "cas-exedir"
        )
        args.cas_exedir = _apptools._ensure_variant_suffix(args.cas_exedir, variant)
        if compiletools.wrappedos.realpath(git_root_exe) != cwd_real:
            args.cas_exedir = compiletools.wrappedos.normpath(os.path.join(git_root_exe, args.cas_exedir))


def add_output_directory_arguments(cap, variant):
    if _parser_has_option(cap, "--bindir"):
        return
    # Deferred import: the ``_UNSUPPLIED_*`` sentinels stay in the
    # apptools core (their canonical consumer is
    # ``apptools.unsupplied_replacement``); apptools imports this
    # module for re-export, so reaching back through the facade at
    # call time is the accepted cycle-break (same pattern as
    # apptools_validate). apptools is fully initialised by then.
    import compiletools.apptools as _apptools

    # When the caller hasn't resolved the variant yet (Namer.add_arguments
    # in cake.py / findtargets.py / makefile_backend.py passes the bare
    # ``"unsupplied"`` sentinel), the bindir default must register as the
    # bare sentinel too -- NOT ``"bin/unsupplied"`` -- so the post-parse
    # ``unsupplied_replacement(args.bindir, "bin/<variant>", ...)`` in
    # ``_commonsubstitutions`` (which membership-tests against
    # ``_UNSUPPLIED_SENTINELS``) actually swaps the default for the
    # resolved-variant path. Otherwise every build lands in
    # ``bin/unsupplied/``.
    bindir_default = "unsupplied" if variant in _apptools._UNSUPPLIED_SENTINELS else "".join(["bin/", variant])
    cap.add_argument(
        "--bindir",
        help="Output directory for executables",
        default=bindir_default,
    )
    add_cas_directory_arguments(cap, variant)
    # Register --use-mtime here so every backend that calls
    # add_output_directory_arguments picks it up. Previously only
    # makefile_backend registered it, so ``ct-cake --backend=ninja
    # --use-mtime`` was rejected by argparse even though
    # ninja_backend reads ``args.use_mtime``.
    add_cas_arguments(cap)


def add_otel_export_arguments(cap):
    """Register all ``--otel-*`` flags on *cap*.

    This is the single canonical declaration point for every OpenTelemetry
    export flag.  ``cake.py`` delegates to this helper so that the full
    ``--otel-*`` surface is defined in exactly one place.  The lint in
    ``test_otel_arg_group_contract.py`` (landing in Task 4) enforces that no
    other caller re-declares these flags inline.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--otel-export"):
        return
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="otel-export",
        dest="otel_export",
        default=False,
        help=(
            "Ship the recorded BuildTimer span tree to an OTLP collector "
            "at the end of the build. Requires --timing. Install the "
            "optional 'otel' extra (pip install 'compiletools[otel]') "
            "for the OpenTelemetry SDK."
        ),
    )
    cap.add_argument(
        "--otel-endpoint",
        default=None,
        # CLI/config-only on the configargparse side: the SDK has its own
        # env-var precedence (incl. the trace-specific OTEL_EXPORTER_OTLP_TRACES_ENDPOINT)
        # and must remain the env-var authority; promoting any generic env
        # var into args.otel_endpoint would shadow that resolution.
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help=(
            "OTLP collector endpoint URL. Defaults to "
            "$OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, then "
            "$OTEL_EXPORTER_OTLP_ENDPOINT, as picked up by the SDK."
        ),
    )
    cap.add_argument(
        "--otel-service-name",
        default=None,
        # CLI/config-only: SDK owns OTEL_SERVICE_NAME (see --otel-endpoint).
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help="OTel service.name resource attribute (default: 'compiletools').",
    )
    cap.add_argument(
        "--otel-resource-attr",
        action="append",
        default=[],
        # CLI/config-only: SDK owns OTEL_RESOURCE_ATTRIBUTES (see --otel-endpoint).
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help=(
            "Extra OTel resource attribute as K=V (repeatable, or "
            "comma-separated). Merged on top of OTEL_RESOURCE_ATTRIBUTES."
        ),
    )
    cap.add_argument(
        "--otel-protocol",
        default="grpc",
        choices=["grpc", "http"],
        help="OTLP transport (default: grpc).",
    )
    cap.add_argument(
        "--otel-headers",
        default=None,
        # CLI/config-only: SDK is the env-var authority (see --otel-endpoint).
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help="OTLP exporter headers as K=V,K=V (e.g. for auth proxies).",
    )
    # Tri-state: None = unset (let SDK infer insecure from endpoint URL scheme).
    otel_insecure_group = cap.add_mutually_exclusive_group()
    otel_insecure_group.add_argument(
        "--otel-insecure",
        dest="otel_insecure",
        action="store_const",
        const=True,
        default=None,
        # CLI/config-only: SDK is the env-var authority (see --otel-endpoint).
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help=(
            "Disable TLS on the OTLP gRPC connection. If neither "
            "--otel-insecure nor --no-otel-insecure is passed, the SDK "
            "infers from the endpoint URL scheme (http:// -> insecure, "
            "https:// -> secure)."
        ),
    )
    otel_insecure_group.add_argument(
        "--no-otel-insecure",
        dest="otel_insecure",
        action="store_const",
        const=False,
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help="Force TLS on the OTLP gRPC connection.",
    )
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="otel-metrics-as-spans",
        dest="otel_metrics_as_spans",
        default=False,
        help=(
            "When a collector accepts only traces (no metrics endpoint), "
            "flatten emitted gauge/counter values into short-lived spans "
            "instead of OTLP metrics. Applies to ct-cache-report's "
            "ct.cas.* gauges and ct-cake's ct.ccache.* counters/gauges."
        ),
    )


def add_fetch_arguments(cap):
    """Register the ``//#GIT=`` external-fetch flags on *cap*.

    This is the single canonical declaration point for the externals-fetch
    surface (``--no-fetch``, ``--update``, ``--externals-dir``,
    ``--git-path``).  ``ct-cake`` (via ``cake.py``) and the standalone
    ``ct-fetch`` entry point both delegate here so the flags are defined in
    exactly one place.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--no-fetch"):
        return
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="no-fetch",
        dest="no_fetch",
        default=False,
        help="Offline: error if a //#GIT external is missing; never clone/fetch.",
    )
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="update",
        dest="update",
        default=False,
        help=(
            "Pull/fast-forward branch/unpinned externals before building. "
            "Without --update a present branch external is compared against its "
            "possibly-stale remote-tracking tip (a remote force-push is not "
            "detected until the next --update)."
        ),
    )
    cap.add_argument(
        "--externals-dir",
        dest="externals_dir",
        default=None,
        env_var="CT_EXTERNALS_DIR",
        help=(
            "Directory under which //#GIT externals are cloned (default: the "
            "parent dir of the git root, i.e. siblings ../<name>)."
        ),
    )
    cap.add_argument(
        "--git-path",
        dest="git_paths",
        action="append",
        default=[],
        metavar="NAME=PATH",
        # CLI/config-only on the configargparse side: per-external overrides
        # come from the dedicated CT_GIT_PATH_<NAME> variables (resolved in
        # fetch.parse_git_path_overrides), not from a single env var bound to
        # this multi-valued append option.
        env_var=compiletools.utils.ENV_VAR_DISABLED,
        help=(
            "Override an external's location: NAME=absolute/path (repeatable; "
            "or set CT_GIT_PATH_<NAME>). CLI wins over env."
        ),
    )


def _user_passed_no_timing(argv: list[str] | None) -> bool:
    """Return True iff the user explicitly passed ``--no-timing`` on the
    command line.

    Scans ``argv`` rather than inspecting ``args.timing`` because the parsed
    value cannot distinguish "user passed ``--no-timing``" from "user passed
    nothing and the default is False". ``--timing`` is registered via
    ``add_flag_argument`` (store_true / store_false; no value form), so the
    only literal that means "turn timing off" is the bare ``--no-timing``
    token. The conf-file equivalents (``no-timing = True`` / ``timing =
    False``) are not in scope here: this validator runs only when the user
    typed ``--otel-export`` on the command line, which is itself a
    deliberate, interactive act; treating an ambient conf-file
    ``no-timing`` as a hard error would make the conf file user-hostile.
    """
    if not argv:
        return False
    return "--no-timing" in argv


def validate_otel_timing_pair(args) -> None:
    """Enforce the ``--otel-export`` / ``--timing`` pairing contract.

    Three behaviors:

    1. ``--otel-export`` and ``--no-timing`` both explicit on the command
       line: hard-error via ``SystemExit``. Asking the exporter to ship
       data while also asking the collector not to collect it is
       internally contradictory; better to fail loudly at parse time than
       silently export an empty span tree.
    2. ``--otel-export`` set without explicit ``--no-timing``: flip
       ``args.timing = True``. Exporting requires a populated span tree;
       making the implication automatic removes a footgun without
       changing the user's apparent intent.
    3. Anything else (no ``--otel-export``, or ``--no-timing`` alone):
       silent no-op.

    Called by every ``ct-*`` entry point that registers the OTel arg
    group via ``add_otel_export_arguments``, immediately after
    ``parseargs`` returns. ``ct-cache-report`` is exempted in
    ``test_otel_timing_pair_validated.py`` because it has no ``--timing``
    concept.
    """
    if not getattr(args, "otel_export", False):
        return
    argv = getattr(args, "_argv", None)
    if _user_passed_no_timing(argv):
        raise SystemExit(
            "--otel-export and --no-timing are mutually exclusive: "
            "exporting an empty span tree is unambiguously a mistake. "
            "Drop one of them."
        )
    args.timing = True


def add_target_arguments(cap):
    """Insert the arguments that control what targets get created.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--dynamic"):
        return
    cap.add_argument("filename", nargs="*", help="File(s) to compile to an executable(s)")
    cap.add_argument(
        "--dynamic",
        "--dynamic-library",
        nargs="*",
        help="File(s) to compile to a dynamic library",
    )
    cap.add_argument(
        "--static",
        "--static-library",
        nargs="*",
        help="File(s) to compile to a static library",
    )
    cap.add_argument("--tests", nargs="*", help="File(s) to compile to a test and then execute")


def add_target_arguments_ex(cap):
    """Add the target arguments and the extra arguments that augment
    the target arguments.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--TESTPREFIX"):
        return
    add_target_arguments(cap)
    cap.add_argument(
        "--TESTPREFIX",
        help='Runs tests with the given prefix, eg. "valgrind --quiet --error-exitcode=1"',
    )
    cap.add_argument(
        "--test-xml-dir",
        dest="test_xml_dir",
        default=None,
        help=(
            "Directory to write per-test JUnit XML reports. "
            "Layout: <DIR>/<variant>/<exe_basename>.xml. "
            "ct-cake auto-selects --gtest_output / --reporters=junit / "
            "--reporter junit based on each test's transitive headers "
            "(gtest, doctest, Catch2). Default: unset (no XML produced)."
        ),
    )
    cap.add_argument(
        "--project-version",
        dest="projectversion",
        help="DEPRECATED: use --prebuild-script with a generated implementation file "
        "(see examples-end-to-end/appinfo). Set the CT_PROJECT_VERSION macro to this value.",
    )
    cap.add_argument(
        "--project-version-cmd",
        dest="projectversioncmd",
        help="DEPRECATED: use --prebuild-script with a generated implementation file "
        "(see examples-end-to-end/appinfo). Execute this command to determine the CT_PROJECT_VERSION macro.",
    )
    cap.add_argument(
        "--project-name",
        dest="projectname",
        help="DEPRECATED: use --prebuild-script with a generated implementation file "
        "(see examples-end-to-end/appinfo). Set the CT_PROJECT_NAME macro to this value.",
    )
    cap.add_argument(
        "--project-name-cmd",
        dest="projectnamecmd",
        help="DEPRECATED: use --prebuild-script with a generated implementation file "
        "(see examples-end-to-end/appinfo). Execute this command to determine the CT_PROJECT_NAME macro.",
    )


def _fix_variable_handling_method(cap, argv, verbose):
    # Re-route env-sourced flag vars (CPPFLAGS -> APPEND_CPPFLAGS, etc.) so
    # they accumulate onto config/CLI values instead of overriding them, then
    # reparse. This dance would be unnecessary if configargparse offered an
    # "append" variable-handling method for environment-sourced values; it
    # only supports global "override", which we partially undo here.
    verbose_print = verbose > 8
    fix_keys = ["CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS", "INCLUDE"]
    for key in fix_keys:
        value = os.getenv(key)
        if value:
            appendkey = f"APPEND_{key}"
            if verbose_print:
                print(f"Changing {key=} into {appendkey} with {value=}")
            os.environ[appendkey] = value
            os.environ.pop(key)

    if verbose_print:
        print(f"{os.environ=}")
        print("_fix_variable_handling_method is forcing reparsing of cap.parse_args")
    return cap.parse_args(args=argv)


# Segment header emitted by ``_ComposingArgumentParser._open_config_files``
# at the start of each conf file's contents in the concatenated stream.
# ``_AccumulatingConfigFileParser.parse()`` derives the per-file
# ``${CONF_DIR}`` directly from the segment-header path.
_CONF_DIR_SEGMENT_HEADER_PREFIX = "# --- "
_CONF_DIR_SEGMENT_HEADER_SUFFIX = " ---"
_CONF_DIR_PLACEHOLDER = "${CONF_DIR}"


def _open_conf_file_utf8(path, *args, **kwargs):
    """Open a conf file as UTF-8, replacing any invalid bytes.

    Passed as ``config_file_open_func`` to ``_ComposingArgumentParser``
    so configargparse no longer falls back to ``locale.getpreferredencoding``
    when reading conf files. See ``_ComposingArgumentParser.__init__`` for
    the failure mode this prevents.
    """
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return open(path, *args, **kwargs)


def _expand_conf_dir(value, conf_dir):
    """Expand ``${CONF_DIR}`` in *value* to *conf_dir* (an absolute path).

    Applies to scalar strings and to each element of list values; leaves
    non-string types untouched. ``conf_dir`` of ``None`` is a no-op."""
    if conf_dir is None or _CONF_DIR_PLACEHOLDER not in (value if isinstance(value, str) else ""):
        if isinstance(value, list):
            return [_expand_conf_dir(elem, conf_dir) for elem in value]
        return value
    return value.replace(_CONF_DIR_PLACEHOLDER, conf_dir)


_DOLLAR_SENTINEL = "\x00"


def _expand_env_and_user(value):
    """Expand $VAR, ${VAR}, and ~ in conf-file values.

    Applies to scalar strings and to each element of list values; leaves
    non-string types untouched. Order: env vars first, then ~ expansion
    (so $HOME and ~ agree). Unknown env vars are left as the literal
    placeholder, matching os.path.expandvars semantics. The $$ escape
    yields a literal $ in the output (sentinel-swap, since
    os.path.expandvars does not honor $$ natively)."""
    if isinstance(value, list):
        return [_expand_env_and_user(elem) for elem in value]
    if not isinstance(value, str):
        return value
    if "$" not in value and "~" not in value:
        return value
    protected = value.replace("$$", _DOLLAR_SENTINEL)
    expanded = os.path.expanduser(os.path.expandvars(protected))
    return expanded.replace(_DOLLAR_SENTINEL, "$")


class _AccumulatingConfigFileParser(configargparse.DefaultConfigFileParser):
    """Variant of ``DefaultConfigFileParser`` that accumulates duplicate
    ``append-*`` / ``prepend-*`` keys into a list rather than last-writer-wins,
    and expands ``${CONF_DIR}`` placeholders in values to the absolute
    directory of the conf file being parsed.

    Used together with ``_ComposingArgumentParser``, which concatenates the
    entire conf-file hierarchy into one stream. When several conf files set
    ``append-CXXFLAGS = ...`` (e.g. ``gcc.conf`` + ``release.conf`` +
    a user-defined ``extras.conf``), the concatenated stream contains the
    same key multiple times; this parser collects those into a list so
    configargparse's ``convert_item_to_command_line_arg`` then emits one
    ``--append-X=v`` token per value, letting argparse's ``action='append'``
    accumulate them on ``args.append_*``. All other keys (scalars like
    ``CXX = g++``) remain last-writer-wins.

    ``${CONF_DIR}`` expansion: ``_open_config_files`` injects per-file
    segment headers (``# --- <path> ---``) ahead of each file's content
    in the concatenated stream; ``parse()`` derives the current conf-dir
    from the header path and substitutes it into any ``${CONF_DIR}``
    token in subsequent values. For the single-file path (no
    concatenation) the parser falls back to the dirname of
    ``stream.name``.

    Provenance side channel: every parsed entry is also recorded into
    ``self._provenance`` as ``key -> [(expanded_value, source_file_abspath,
    lineno, pre_expansion_literal), ...]`` in parse order. The
    pre-expansion literal is the value as it appeared in the conf file
    after JSON-list parsing but before ``${CONF_DIR}`` and env-var
    expansion; equals the expanded value when no expansion happened.
    Used by ``-vv`` diagnostics to attribute each emitted setting back
    to its conf-file:line origin and to show "literal: $HOME/..." when
    a value was expanded from an env var. Does not change any
    ``args.*`` shape.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._provenance: dict[str, list[tuple[str, str, int, str]]] = {}

    def parse(self, stream):
        items = OrderedDict()
        # Parser instance is reused across configargparse invocations.
        self._provenance = {}
        # Initial conf-dir from stream.name covers the single-file path
        # (no concatenation, no segment headers).
        stream_name = getattr(stream, "name", None)
        conf_dir = None
        source_file = "<unknown>"
        if stream_name and not stream_name.startswith("<"):
            try:
                source_file = compiletools.wrappedos.realpath(stream_name)
                conf_dir = compiletools.wrappedos.dirname(source_file)
            except (OSError, ValueError):
                conf_dir = None
                source_file = "<unknown>"

        # Provenance line numbers are 1-based within source_file, not the
        # concatenated stream; reset whenever a segment header is consumed.
        segment_lineno = 0
        for i, line in enumerate(stream):
            stripped = line.strip()
            if stripped.startswith(_CONF_DIR_SEGMENT_HEADER_PREFIX) and stripped.endswith(
                _CONF_DIR_SEGMENT_HEADER_SUFFIX
            ):
                header_path = stripped[
                    len(_CONF_DIR_SEGMENT_HEADER_PREFIX) : -len(_CONF_DIR_SEGMENT_HEADER_SUFFIX)
                ].strip()
                # Existence check rejects user-authored comments matching
                # the header shape but naming fictional paths.
                if header_path and compiletools.wrappedos.isfile(header_path):
                    source_file = compiletools.wrappedos.realpath(header_path)
                    conf_dir = compiletools.wrappedos.dirname(source_file)
                    segment_lineno = 0
                    continue
            segment_lineno += 1
            line = stripped
            if not line or line[0] in ["#", ";", "["] or line.startswith("---"):
                continue
            match = re.match(
                r"^(?P<key>[^:=;#\s]+)\s*"
                r'(?:(?P<equal>[:=\s])\s*([\'"]?)(?P<value>.+?)?\3)?'
                r"\s*(?:\s[;#]\s*(?P<comment>.*?)\s*)?$",
                line,
            )
            if not match:
                raise configargparse.ConfigFileParserException(
                    "Unexpected line {} in {}: {}".format(i, getattr(stream, "name", "stream"), line)
                )
            key = match.group("key")
            equal = match.group("equal")
            value = match.group("value")
            if value is None and equal is not None and equal != " ":
                value = ""
            elif value is None:
                value = "true"
            if value.startswith("[") and value.endswith("]"):
                try:
                    value = json.loads(value)
                except Exception:
                    value = [elem.strip() for elem in value[1:-1].split(",")]

            if isinstance(value, list):
                pre_expansion_literal = [str(elem) for elem in value]
            else:
                pre_expansion_literal = str(value)

            value = _expand_conf_dir(value, conf_dir)
            value = _expand_env_and_user(value)

            if key.startswith(("append-", "prepend-")) and key in items:
                existing = items[key]
                if not isinstance(existing, list):
                    existing = [existing]
                if isinstance(value, list):
                    existing.extend(value)
                else:
                    existing.append(value)
                items[key] = existing
            else:
                items[key] = value

            prov_bucket = self._provenance.setdefault(key, [])
            if isinstance(value, list):
                for i, elem in enumerate(value):
                    literal_elem = pre_expansion_literal[i] if i < len(pre_expansion_literal) else str(elem)
                    prov_bucket.append((str(elem), source_file, segment_lineno, literal_elem))
            else:
                literal_str = pre_expansion_literal if isinstance(pre_expansion_literal, str) else str(value)
                prov_bucket.append((str(value), source_file, segment_lineno, literal_str))
        return items


class _ComposingArgumentParser(configargparse.ArgumentParser):
    """``configargparse.ArgumentParser`` that lets ``append-*`` / ``prepend-*``
    keys accumulate across the entire conf-file hierarchy AND across the
    conf-vs-CLI boundary.

    Stock configargparse uses ``already_on_command_line`` to decide whether
    to inject a conf-file value, and that check fires for ``action='append'``
    arguments too — so the first appearance of ``--append-CXXFLAGS`` (CLI
    or a higher-priority conf) silently suppresses every lower-priority
    contribution. That breaks compiletools' composition model in two ways:

    1. Multi-conf: ``--variant=gcc,release,extras`` should merge the
       ``append-CXXFLAGS`` value from each axis conf, but stock behavior
       keeps only the highest-priority conf's value.
    2. CLI vs conf: ``--append-CXXFLAGS=-Wfoo`` on the CLI alongside any
       conf-file ``append-CXXFLAGS = -DBAR`` drops the conf value entirely.

    Fix is in two halves working together:

    * ``_open_config_files`` (multi-conf): concatenate the entire hierarchy
      into a single stream before configargparse's per-file processing runs.
      The companion ``_AccumulatingConfigFileParser`` collects duplicate
      ``append-*`` / ``prepend-*`` keys into lists, which configargparse
      then turns into multiple ``--key=val`` tokens via
      ``convert_item_to_command_line_arg`` so argparse's ``action='append'``
      accumulates them. Scalar keys still see last-writer-wins inside the
      concatenated stream (matching prior behavior, since
      ``DefaultConfigFileParser`` overwrites duplicates).
    * ``parse_known_args`` (CLI vs conf): strip every CLI
      ``--append-*`` / ``--prepend-*`` token before calling ``super()`` so
      ``already_on_command_line`` doesn't see them and conf-file values
      flow through. The stripped CLI values are re-appended to the parsed
      namespace afterwards, so they still land in
      ``args.append_*`` / ``args.prepend_*`` (after the conf-file values,
      preserving "CLI is highest priority" for any conflicting late-wins
      flag like ``-O3`` vs ``-O0``).
    """

    def __init__(self, *args, **kwargs):
        # configargparse defaults to plain ``open(path)``, which honors
        # ``locale.getpreferredencoding(False)`` — that resolves to ASCII
        # under ``PYTHONUTF8=0`` + a ``C``/POSIX locale, and any non-ASCII
        # byte in a conf-file comment (e.g. an em-dash, U+2014 →
        # 0xE2 0x80 0x94) then dies with ``UnicodeDecodeError`` before the
        # parser ever sees the line. Conf files are author-controlled text
        # and overwhelmingly UTF-8; pin the encoding and replace any stray
        # invalid bytes so the parser is robust regardless of process
        # locale. Matches the discipline of ``safe_read_text_file``.
        kwargs.setdefault("config_file_open_func", _open_conf_file_utf8)
        super().__init__(*args, **kwargs)

    def get_conf_file_provenance(self) -> dict[str, list[tuple[str, str, int, str]]]:
        """Return a shallow copy of the per-conf-file provenance dict from
        the most recent parse (the entry tuples themselves are immutable,
        so callers cannot mutate the parser's internal state through the
        returned structure). Keys are conf-file keys as they appeared
        (e.g. ``'prepend-PKG-CONFIG-PATH'``); values are lists of
        ``(expanded_value, source_file_abspath, lineno)`` tuples in
        parse order.

        Used by ``-vv`` diagnostics in
        ``_setup_pkg_config_overrides_locked`` and ``ct-config`` to
        attribute each emitted setting back to the conf file (and line)
        that contributed it.
        """
        prov = getattr(self._config_file_parser, "_provenance", None) or {}
        return {key: list(entries) for key, entries in prov.items()}

    def format_help(self):
        # Hide the ENV_VAR_DISABLED sentinel from --help: configargparse
        # would otherwise render '[env var: __CT_ENV_VAR_DISABLED__]'.
        # The env-pickup suppression itself lives in parse_known_args
        # (which filters the sentinel key out of env_vars).
        restore = []
        for a in self._actions:
            if getattr(a, "env_var", None) == compiletools.utils.ENV_VAR_DISABLED:
                restore.append(a)
                a.env_var = None  # type: ignore[attr-defined]
        try:
            return super().format_help()
        finally:
            for a in restore:
                a.env_var = compiletools.utils.ENV_VAR_DISABLED  # type: ignore[attr-defined]

    def _open_config_files(self, command_line_args):
        streams = super()._open_config_files(command_line_args)
        if len(streams) < 2:
            return streams

        parts = []
        for stream in streams:
            try:
                content = stream.read()
            finally:
                with contextlib.suppress(OSError):
                    stream.close()
            if content and not content.endswith("\n"):
                content += "\n"
            stream_name = getattr(stream, "name", "<?>")
            # Segment header demarcates each file's contents in the
            # concatenated stream; _AccumulatingConfigFileParser derives
            # the per-file ${CONF_DIR} from the header path.
            parts.append(f"# --- {stream_name} ---\n{content}")

        merged = io.StringIO("".join(parts))
        merged.name = f"<merged: {len(streams)} conf files>"
        return [merged]

    def _extract_cli_append_prepend(self, args):
        """Pop ``--append-*`` / ``--prepend-*`` tokens out of *args*.

        Returns ``(clean_args, captured)`` where ``captured`` maps each
        action's ``dest`` to the list of values seen on the CLI in order.
        Only the long-form ``--key=value`` and ``--key value`` syntaxes are
        recognized (which is the only form ``_add_xxpend_argument``
        registers).
        """
        # Build the set of option strings that belong to append/prepend
        # actions registered on this parser. Done lazily per call rather
        # than at __init__ because add_argument() is called after
        # construction in compiletools' two-phase parser-build flow.
        opt_to_action: dict[str, argparse.Action] = {}
        for action in self._actions:
            if not isinstance(action, argparse._AppendAction):
                continue
            for opt in action.option_strings:
                opt_to_action[opt] = action

        if not opt_to_action:
            return list(args), {}

        clean: list[str] = []
        captured: dict[str, list[str]] = {}
        i = 0
        while i < len(args):
            tok = args[i]
            name = tok.split("=", 1)[0] if "=" in tok else tok
            action = opt_to_action.get(name)
            if action is None:
                clean.append(tok)
                i += 1
                continue
            if "=" in tok:
                value = tok.split("=", 1)[1]
                i += 1
            elif i + 1 < len(args):
                value = args[i + 1]
                i += 2
            else:
                # Malformed: leave it for argparse to complain.
                clean.append(tok)
                i += 1
                continue
            captured.setdefault(action.dest, []).append(value)
        return clean, captured

    def parse_known_args(
        self,
        args=None,
        namespace=None,
        config_file_contents=None,
        env_vars=os.environ,
        ignore_help_args=False,
    ):
        if args is None:
            args = sys.argv[1:]
        elif isinstance(args, str):
            args = args.split()
        else:
            args = list(args)

        clean_args, captured_cli = self._extract_cli_append_prepend(args)

        # Make the ENV_VAR_DISABLED sentinel collision-proof: even if a
        # real process exports a variable with this exact name, drop it
        # from the view configargparse's env-pickup loop sees.
        if compiletools.utils.ENV_VAR_DISABLED in env_vars:
            env_vars = {k: v for k, v in env_vars.items() if k != compiletools.utils.ENV_VAR_DISABLED}

        namespace, unknown = super().parse_known_args(
            args=clean_args,
            namespace=namespace,
            config_file_contents=config_file_contents,
            env_vars=env_vars,
            ignore_help_args=ignore_help_args,
        )

        # Re-attach CLI values AFTER the conf-file values so the CLI tokens
        # apply last in _do_xxpend (compilers honor the last occurrence of
        # conflicting flags — CLI override semantics preserved).
        for dest, values in captured_cli.items():
            existing = getattr(namespace, dest, None) or []
            setattr(namespace, dest, list(existing) + values)

        return namespace, unknown


def create_parser(description, argv=None, include_config=True, include_write_config=False):
    """Create a standardized parser with consistent compiletools behavior.

    Parameters:
            description (str): Human-readable parser description shown in --help.
            argv (list[str] | None): The command-line argv (excluding argv[0]) to use
                    when extracting the variant/config file set. If None, the current
                    process args are used by helper utilities where applicable.
            include_config (bool):
                    - True (default): Build a full config-aware parser using
                        compiletools.configutils to:
                            * extract the active variant (respecting argv), and
                            * compute default_config_files for that variant.
                        The returned parser supports -c/--config and loads defaults from
                        those config files (env vars still apply via configargparse).
                    - False: Create a simple parser and only add the base/common
                        arguments via add_base_arguments(); no variant/config file
                        plumbing is wired up.
            include_write_config (bool): If True, expose -w/--write-out-config-file
                    on the returned parser (only meaningful when include_config=True).

    Returns:
            configargparse.ArgumentParser: A configured parser ready for use with
            parseargs().

    Notes:
            - The config-aware branch sets formatter_class to
                ArgumentDefaultsHelpFormatter, provides --config, and ignores unknown
                keys in config files to keep tools resilient across versions.
            - Call add_common_arguments()/add_link_arguments()/etc. on the parser as
                needed by each tool after creation when include_config=False.
    """
    if include_config:
        # Propagate --allow-fake-git BEFORE config discovery resolves the
        # gitroot (extract_variant / resolve_variant -> find_git_root). The
        # authoritative propagation still happens post-parse in parseargs /
        # resolve_cas_directory_arguments; this only ensures config discovery
        # sees the right mode. Only clear the configutils caches when the
        # toggle actually changes, so the steady state keeps its cache hits.
        scan_argv = argv if argv is not None else sys.argv
        fake = "--allow-fake-git" in scan_argv
        if fake != compiletools.git_utils.get_allow_fake_git():
            compiletools.git_utils.set_allow_fake_git(fake)
            compiletools.configutils.clear_cache()
        variant = compiletools.configutils.extract_variant(argv=argv)
        resolution = compiletools.configutils.resolve_variant(variant=variant, argv=argv)
        config_files = resolution.flat_paths
        compiletools.apptools_validate._check_legacy_cas_config_keys(config_files)
        compiletools.apptools_validate._check_legacy_variant_config_keys(config_files)
        kwargs = {
            "description": description,
            "formatter_class": configargparse.ArgumentDefaultsHelpFormatter,
            "auto_env_var_prefix": "",
            "default_config_files": config_files,
            "args_for_setting_config_path": ["-c", "--config"],
            "ignore_unknown_config_file_keys": True,
            "conflict_handler": "resolve",
            "config_file_parser_class": _AccumulatingConfigFileParser,
        }
        if include_write_config:
            kwargs["args_for_writing_out_config_file"] = ["-w", "--write-out-config-file"]
        return _ComposingArgumentParser(**kwargs)
    else:
        cap = configargparse.ArgumentParser(description=description, conflict_handler="resolve")
        add_base_arguments(cap, argv=argv)
        return cap
