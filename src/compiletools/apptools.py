import argparse
import contextlib
import functools
import importlib.util
import io
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import warnings
from collections import OrderedDict
from collections.abc import Generator, Sequence
from typing import Literal

# Only used for the verbose print.
import configargparse
import stringzilla as sz

import compiletools.configutils
import compiletools.git_utils
import compiletools.utils
import compiletools.wrappedos
from compiletools.flags import Flags
from compiletools.utils import split_command_cached
from compiletools.version import __version__

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


# Sentinel default values used by --CPP, --LD, --CPPFLAGS, --LDFLAGS to mean
# "if the user didn't supply this, fall back to CXX / CXXFLAGS". Kept as
# constants so a rename can't silently break _check_resolved_compiler_available
# (which compares against these strings to skip the existence check on slots
# that haven't been substituted yet).
_UNSUPPLIED_USE_CXX = "unsupplied_implies_use_CXX"
_UNSUPPLIED_USE_CXXFLAGS = "unsupplied_implies_use_CXXFLAGS"

# The closed set of "not supplied by the user" sentinels recognised by
# ``unsupplied_replacement``. The cas-*dir flags register the bare
# ``"unsupplied"`` sentinel; CPP/CPPFLAGS/LD/LDFLAGS register the two
# ``unsupplied_implies_*`` forms above. Membership is checked by *exact*
# equality (not substring) so a user-supplied path that merely contains the
# text ``unsupplied`` (e.g. ``--cas-objdir=/data/unsupplied/obj``) is not
# silently discarded and replaced with the computed default.
_UNSUPPLIED_SENTINELS = frozenset({"unsupplied", _UNSUPPLIED_USE_CXX, _UNSUPPLIED_USE_CXXFLAGS})


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
    cap.add_argument("--version", action="version", version=__version__)
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
    cap.add_argument("--CPP", help="C preprocessor (override)", default=_UNSUPPLIED_USE_CXX)
    cap.add_argument("--CC", help="C compiler (override)", default="gcc")
    # Default will be set later using functional compiler detection
    cap.add_argument("--CXX", help="C++ compiler (override)", default=None)
    cap.add_argument(
        "--CPPFLAGS",
        nargs="+",
        help="C preprocessor flags (override)",
        default=_UNSUPPLIED_USE_CXXFLAGS,
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
    cap.add_argument("--LD", help="Linker (override)", default=_UNSUPPLIED_USE_CXX)
    cap.add_argument(
        "--LDFLAGS",
        "--LINKFLAGS",
        help="Linker flags (override)",
        default=_UNSUPPLIED_USE_CXXFLAGS,
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
    # is a one-off direct read. Both inputs are absolute strings, so neither is
    # subject to the chdir footgun (CLAUDE.md "wrappedos" Caveat #3).
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
        new = unsupplied_replacement(current, default_value, args.verbose, f"cas-{kind}dir")
        new = _ensure_variant_suffix(new, variant)
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
        args.cas_exedir = unsupplied_replacement(args.cas_exedir, default_cas_exedir, args.verbose, "cas-exedir")
        args.cas_exedir = _ensure_variant_suffix(args.cas_exedir, variant)
        if compiletools.wrappedos.realpath(git_root_exe) != cwd_real:
            args.cas_exedir = compiletools.wrappedos.normpath(os.path.join(git_root_exe, args.cas_exedir))


def add_output_directory_arguments(cap, variant):
    if _parser_has_option(cap, "--bindir"):
        return
    # When the caller hasn't resolved the variant yet (Namer.add_arguments
    # in cake.py / findtargets.py / makefile_backend.py passes the bare
    # ``"unsupplied"`` sentinel), the bindir default must register as the
    # bare sentinel too -- NOT ``"bin/unsupplied"`` -- so the post-parse
    # ``unsupplied_replacement(args.bindir, "bin/<variant>", ...)`` in
    # ``_commonsubstitutions`` (which membership-tests against
    # ``_UNSUPPLIED_SENTINELS``) actually swaps the default for the
    # resolved-variant path. Otherwise every build lands in
    # ``bin/unsupplied/``.
    bindir_default = "unsupplied" if variant in _UNSUPPLIED_SENTINELS else "".join(["bin/", variant])
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


def unsupplied_replacement(variable, default_variable, verbose, variable_str):
    """If a given variable is one of the recognised "unsupplied" sentinels
    then return the given default variable.

    The check is exact membership in ``_UNSUPPLIED_SENTINELS`` rather than a
    substring test, so a real user-supplied value that merely contains the
    text ``unsupplied`` is preserved instead of being clobbered.
    """
    replacement = variable
    if variable in _UNSUPPLIED_SENTINELS:
        replacement = default_variable
        if verbose >= 6:
            print(" ".join([variable_str, "was unsupplied. Changed to use ", default_variable]))
    return replacement


def _ensure_variant_suffix(path, variant):
    """Return ``path`` with ``/<variant>`` appended as a final segment
    unless it is already there. Idempotent.

    Used to keep the four ``cas-*dir`` layers separated per variant
    even when the user points them at a bare shared-pool path (e.g.
    ``cas-objdir = /mnt/team-cache``). The check is segment-aware:
    ``/pool/release_old`` does NOT count as ending in ``release``.
    Trailing ``/`` is normalised away so the result never contains
    ``//``."""
    if not path or not variant:
        return path
    normalised = path.rstrip(os.sep) or path
    if os.path.basename(normalised) == variant:
        return normalised
    return os.path.join(normalised, variant)


def _substitute_CXX_for_missing(args):
    """If C PreProcessor variables (and the same for the LD*) are not set
    but CXX ones are set then just use the CXX equivalents.

    LD/LDFLAGS are only registered by ``add_link_arguments`` (used by ct-cake
    and the makefile backend); tools that build only the CPP/CXX side of the
    parser (ct-cleanup-locks, ct-trim-cache, ct-list-variants, ...) won't
    have those attrs, so we skip that substitution rather than raise.
    """
    if args.verbose > 8:
        print("Using CXX variables as defaults for missing C, CPP, LD variables")
    args.CPP = unsupplied_replacement(args.CPP, args.CXX, args.verbose, "CPP")
    args.CPPFLAGS = unsupplied_replacement(args.CPPFLAGS, args.CXXFLAGS, args.verbose, "CPPFLAGS")
    if hasattr(args, "LD"):
        args.LD = unsupplied_replacement(args.LD, args.CXX, args.verbose, "LD")
    if hasattr(args, "LDFLAGS"):
        args.LDFLAGS = unsupplied_replacement(args.LDFLAGS, args.CXXFLAGS, args.verbose, "LDFLAGS")


def _extend_includes_using_git_root(args):
    """Unless turned off, the git root will be added
    to the list of include paths
    """
    if args.git_root and (
        hasattr(args, "filename") or hasattr(args, "static") or hasattr(args, "dynamic") or hasattr(args, "tests")
    ):
        if args.verbose > 8:
            print("Extending the include paths to have the git root")

        git_roots = set()
        git_roots.add(compiletools.git_utils.find_git_root())

        # No matter whether args.filename is a single value or a list,
        # filenames will be a list
        filenames = []

        if hasattr(args, "filename") and args.filename:
            filenames.extend(args.filename)

        if hasattr(args, "static") and args.static:
            filenames.extend(args.static)

        if hasattr(args, "dynamic") and args.dynamic:
            filenames.extend(args.dynamic)

        if hasattr(args, "tests") and args.tests:
            filenames.extend(args.tests)

        for filename in filenames:
            git_roots.add(compiletools.git_utils.find_git_root(filename))

        if git_roots:
            # sorted(), not list(): set iteration order depends on
            # PYTHONHASHSEED, which would shift the -I order between processes
            # and invalidate the cas-objdir cxxflags_tokens hash component on
            # no-op rebuilds. See TestExtendIncludesUsingGitRootDeterministic.
            args.INCLUDE = " ".join(args.INCLUDE.split() + sorted(git_roots))
            if args.verbose > 6:
                print(f"Extended includes to have the gitroots {sorted(git_roots)}")
        else:
            raise ValueError(
                "args.git_root is True but no git roots found. :( .  If this is expected then specify --no-git-root."
            )


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


def _add_include_paths_to_flags(args):
    """Add all the include paths to all three compile flags.

    Token-walk dedup: a path already present as ``-Ip`` or ``-I p`` is
    skipped, but presence as ``-isystem /p``, ``-L /p``, or ``-DFOO=/p``
    is treated as absent (those are different flag families). The raw
    flag string is only *appended* to (never tokenized-and-rejoined),
    so existing shell-quoted content is preserved verbatim.
    """
    new_paths = args.INCLUDE.split()
    if not new_paths:
        return

    for raw_attr in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
        existing = split_command_cached(getattr(args, raw_attr))
        added = dedup_include_paths_to_append(existing, new_paths)
        if added:
            setattr(args, raw_attr, getattr(args, raw_attr) + " " + " ".join(added))

    if args.verbose >= 6 and len(args.INCLUDE) > 0:
        print("Extra include paths have been appended to the *FLAG variables:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def extract_system_include_paths(args, flag_sources=None, verbose=0):
    """Extract -I and -isystem include paths from command-line flags.

    Args:
        args: Parsed arguments object with flag attributes (CPPFLAGS, CFLAGS, CXXFLAGS)
        flag_sources: List of flag names to extract from (default: ['CPPFLAGS', 'CXXFLAGS'])
        verbose: Verbosity level for debugging

    Returns:
        List of unique include paths in order
    """
    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CXXFLAGS"]

    include_paths = []

    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, "")
        if not flag_value:
            continue

        # Use existing shlex functionality from split_command_cached
        try:
            tokens = split_command_cached(flag_value)
        except ValueError:
            # Fall back to simple split if shlex fails
            tokens = flag_value.split()

        # Process tokens to find -I and -isystem flags
        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token == "-I" or token == "-isystem":
                # Next token should be the path
                if i + 1 < len(tokens):
                    include_paths.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
            elif token.startswith("-I"):
                # -Ipath format
                path = token[2:]
                if path:  # Make sure it's not just "-I"
                    include_paths.append(path)
                i += 1
            elif token.startswith("-isystem"):
                # -isystempath format (though this is unusual)
                path = token[8:]
                if path:  # Make sure it's not just "-isystem"
                    include_paths.append(path)
                i += 1
            else:
                i += 1

    # Remove duplicates while preserving order using existing ordered_unique
    include_paths = compiletools.utils.ordered_unique(include_paths)

    if verbose >= 9 and include_paths:
        print(f"Extracted system include paths: {include_paths}")

    return include_paths


def find_system_header(header_name, args, verbose=0):
    """Find a system header in the -I/-isystem include paths.

    Args:
        header_name: Name of header to find (e.g., "stdio.h", "mylib/header.h")
        args: Parsed arguments object with flag attributes
        verbose: Verbosity level for debugging

    Returns:
        Absolute path to header if found, None otherwise
    """
    include_paths = extract_system_include_paths(args, verbose=verbose)

    for include_path in include_paths:
        candidate = os.path.join(include_path, header_name)
        if compiletools.wrappedos.isfile(candidate):
            return compiletools.wrappedos.realpath(candidate)

    if verbose >= 9:
        print(f"System header '{header_name}' not found in include paths: {include_paths}")

    return None


def extract_command_line_macros(args, flag_sources=None, include_compiler_macros=True, verbose=0):
    """Extract -D macro definitions from command line flags.

    Args:
        args: Parsed arguments object with flag attributes (CPPFLAGS, CFLAGS, CXXFLAGS)
        flag_sources: List of flag names to extract from (default: ['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'])
        include_compiler_macros: Whether to include compiler/platform macros
        verbose: Verbosity level (uses args.verbose if 0)

    Returns:
        Dict[str, str]: macro_name -> macro_value mapping
    """
    if verbose == 0 and hasattr(args, "verbose"):
        verbose = args.verbose

    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CFLAGS", "CXXFLAGS"]

    macros = {}

    # Extract -D macros from specified flag sources
    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, None)
        if not flag_value:
            continue

        # Handle both string and list types for flag_value
        if isinstance(flag_value, list):
            flag_string = " ".join(flag_value)
        else:
            flag_string = flag_value

        # Use shlex.split for robust parsing
        try:
            flags = split_command_cached(flag_string)
        except ValueError:
            # Fallback to simple split if shlex fails on malformed input
            flags = flag_string.split()

        # Walk tokens recognizing both attached (-DFOO, -DFOO=val) and
        # detached (-D FOO, -D FOO=val) forms. The detached form was
        # previously silently dropped and that disagreed with the macro
        # universe computed by cmdline_d_macro_names, defeating the
        # cache-key scoping.
        i = 0
        n = len(flags)
        while i < n:
            flag = flags[i]
            macro_def = None
            if flag == "-D":
                # Detached: name (and optional =value) is the next token.
                if i + 1 < n:
                    macro_def = flags[i + 1]
                i += 2
            elif flag.startswith("-D"):
                macro_def = flag[2:]
                i += 1
            else:
                i += 1
                continue

            if not macro_def:
                continue

            if "=" in macro_def:
                macro_name, macro_value = macro_def.split("=", 1)
            else:
                macro_name = macro_def
                macro_value = "1"  # Default value for macros without explicit values

            if macro_name:
                macros[macro_name] = macro_value
                if verbose >= 9:
                    print(f"extract_command_line_macros: added macro {macro_name} = {macro_value} from {flag_name}")

    # Add compiler, platform, and architecture macros if requested
    if include_compiler_macros:
        import compiletools.compiler_macros

        # Use same pattern as parseargs() - check args.CXX first to avoid redundant detection
        compiler = getattr(args, "CXX", None)
        if compiler is None:
            functional_compiler = get_functional_cxx_compiler()
            if functional_compiler:
                compiler = functional_compiler
            else:
                if verbose >= 1:
                    print("Warning: No functional C++ compiler detected. Skipping compiler macros.")

        if compiler is not None:
            compiler_macros = compiletools.compiler_macros.get_compiler_macros(compiler, verbose)
            macros.update(compiler_macros)

    return macros


def extract_command_line_macros_sz(args, flag_sources_sz, verbose=0):
    """Extract -D macro definitions from sz.Str command line flags.

    Args:
        args: Object with sz.Str list attributes
        flag_sources_sz: List of sz.Str flag names
        verbose: Verbosity level

    Returns:
        Dict[sz.Str, sz.Str]: macro_name -> macro_value mapping
    """
    import stringzilla as sz

    macros = {}

    for flag_name_sz in flag_sources_sz:
        flag_list = getattr(args, str(flag_name_sz), None)
        if not flag_list:
            continue

        for flag_sz in flag_list:
            if not flag_sz.startswith("-D"):
                continue

            macro_def = flag_sz[2:]
            eq_pos = macro_def.find("=")
            if eq_pos >= 0:
                macro_name = macro_def[:eq_pos]
                macro_value = macro_def[eq_pos + 1 :]
            else:
                macro_name = macro_def
                macro_value = sz.Str("1")

            if macro_name:
                macros[macro_name] = macro_value
                if verbose >= 9:
                    print(
                        f"extract_command_line_macros_sz: added macro {macro_name} = {macro_value} from {flag_name_sz}"
                    )

    return macros


def cmdline_d_macro_names(args, flag_sources=None, verbose=0) -> frozenset[sz.Str]:
    """Set of macro names defined via cmdline -D flags (CPPFLAGS/CFLAGS/CXXFLAGS).

    Excludes compiler builtins. The returned set is the universe of macros
    that the per-TU cache-key scoping will consider for filtering.

    Recognizes both attached form (-DFOO, -DFOO=bar) and detached form
    (-D FOO, -D FOO=bar) of the -D flag. The macro VALUE is irrelevant
    here -- only the name matters for the scope-filter universe.

    Args:
        args: Parsed arguments object (must have CPPFLAGS/CFLAGS/CXXFLAGS attrs)
        flag_sources: List of flag names to extract from
            (default: ['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'])
        verbose: Verbosity level (uses args.verbose if 0)

    Returns:
        frozenset[sz.Str]: Macro names from cmdline -D flags.
    """
    if verbose == 0 and hasattr(args, "verbose"):
        verbose = args.verbose

    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CFLAGS", "CXXFLAGS"]

    names = set()
    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, None)
        if not flag_value:
            continue

        if isinstance(flag_value, list):
            flag_string = " ".join(flag_value)
        else:
            flag_string = flag_value

        try:
            tokens = split_command_cached(flag_string)
        except ValueError:
            tokens = flag_string.split()

        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            macro_def = None
            if tok == "-D":
                # Detached form: name is the next token.
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
            eq_pos = macro_def.find("=")
            macro_name = macro_def[:eq_pos] if eq_pos >= 0 else macro_def
            if macro_name:
                names.add(macro_name)
                if verbose >= 9:
                    print(f"cmdline_d_macro_names: added {macro_name} from {flag_name}")

    return frozenset(sz.Str(name) for name in names)


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
# - -W*: warnings (pure diagnostic; -Werror is the one exception, see below)
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

# Exception: -Werror promotes warnings to errors, which CAN affect the
# build outcome (compile fails vs succeeds). Treat -Werror and
# -Werror=<warning> as hash-relevant.
_HASH_RELEVANT_W_FLAGS: tuple[str, ...] = (
    "-Werror",
    "-Werror=",
)


def filter_hash_irrelevant_tokens(tokens: Sequence[str]) -> list[str]:
    """Remove tokens that don't affect compiled output from a flag sequence.

    Used by cache-key hashing to elide diagnostic-only flag changes.
    Accepts either a list or tuple. ``-W*`` warnings are dropped
    EXCEPT ``-Werror`` and ``-Werror=...`` (which can change compile
    outcome). Returns a NEW list; input is not mutated.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # -Werror exception: hash-relevant. ``-Werror`` itself, and the
        # ``-Werror=<warning>`` parametrized form, both promote warnings
        # to errors and thus can change build outcome.
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


def _has_prefix_map_flag(raw_flags: str) -> bool:
    """Return True if *raw_flags* contains any top-level
    ``-f{file,debug,macro,canon}-prefix-map=`` flag.

    Tokenizes via ``shlex.split`` and checks ``tok.startswith(prefix)``
    so that a prefix-map substring nested inside another flag's value
    (e.g. ``-DREASON='-ffile-prefix-map=oops='``) does NOT false-
    positive — naive substring search would, and the symptom (silently
    per-user-divergent ``.o`` bytes for a project that thought it had
    cross-user CAS sharing) would be hard to diagnose.

    Empty / None returns False. Unparseable flag strings (e.g.
    unbalanced quotes from a user's CXXFLAGS) return True — the
    conservative call: an opaque string is unsafe to interpret either
    way, so decline auto-injection rather than risk appending a flag
    the user might already have inside their unparseable text.
    """
    if not raw_flags:
        return False
    try:
        tokens = shlex.split(raw_flags)
    except ValueError:
        return True
    return any(tok.startswith(prefix) for tok in tokens for prefix in _PREFIX_MAP_FLAG_PREFIXES)


def _inject_ffile_prefix_map(args) -> None:
    """Append ``-ffile-prefix-map=<gitroot>=<target>`` to args.CXXFLAGS
    and args.CFLAGS where the user has not already specified any
    prefix-map flag.

    The flag rewrites paths the compiler EMITS (DWARF debug info,
    ``__FILE__`` / ``__builtin_FILE``, ``.d`` output) so two users
    compiling the same source at different workspace paths produce
    byte-identical .o bytes -- the user-stated goal of Round 3 for
    the cas-objdir layer.

    Known scope limitation: PCH (.gch) and C++20 BMI (.pcm / .gcm)
    files embed the absolute source path through gcc's internal
    path-table, which is NOT subject to -ffile-prefix-map.
    -fdebug-compilation-dir= would address this for clang but is not
    a recognised gcc flag (rejected by gcc as of 16.1.0). Closing
    the gcc PCH / BMI gap requires either (a) workspace-relative
    source paths in the precompile rule emitter plus per-backend
    CWD discipline, or (b) a PWD=/proc/self/cwd subprocess-env
    trick. Both are deferred follow-ups; cas-pchdir and cas-pcmdir
    cross-user sharing remains per-user until then. See
    docs/superpowers/specs/2026-05-12-round3-workspace-relative-compile-paths-design.md
    "Open Questions" for the design escalation paths.

    Skipped when ``compiletools.git_utils.find_git_root()`` returns an
    empty / falsy value (no anchor to canonicalize against). Per-slot
    independently -- user can override C++ but accept the C default.

    Mutates ``args.CXXFLAGS`` and ``args.CFLAGS`` in place. The caller
    (``_commonsubstitutions``) triggers ``_finalize_flag_state``
    afterward so the ``*_tokens`` lists and ``args.flags`` reflect the
    new strings.

    Idempotent: a second call detects the previously-injected flag via
    :func:`_has_prefix_map_flag` and skips. The CLI flag
    ``--ffile-prefix-map-target`` (default ``.``) controls the RHS.
    """
    git_root = compiletools.git_utils.find_git_root()
    if not git_root:
        return
    target = getattr(args, "ffile_prefix_map_target", ".")
    flag = f"-ffile-prefix-map={git_root}={target}"
    for attr in ("CXXFLAGS", "CFLAGS"):
        existing = getattr(args, attr, "") or ""
        if _has_prefix_map_flag(existing):
            continue
        setattr(args, attr, f"{existing} {flag}".strip() if existing else flag)


def _effective_link_driver(args) -> str | None:
    """Return the compiler driver that actually performs the link.

    The link runs through ``args.LD`` (gcc.conf/clang.conf set ``LD`` to
    g++/clang++), falling back to ``args.CXX`` when LD is unset or still
    the "use CXX" sentinel. The wild linker selector (``-fuse-ld=wild`` /
    ``--ld-path=wild``) is consumed by THIS driver, so it is what
    ``compiler_kind`` must classify.
    """
    ld = getattr(args, "LD", None)
    if ld and ld not in (_UNSUPPLIED_USE_CXX, _UNSUPPLIED_USE_CXXFLAGS):
        return ld
    return getattr(args, "CXX", None)


def _variant_has_axis(args, axis_name: str) -> bool:
    """True if *axis_name* is one of the variant's selected axis tokens.

    Splits ``args.variant`` on the variant separator (``[\\s,.]+``) so it
    works whether the variant string is in user order or canonicalised.
    Hyphenated tokens (``wild-B``, ``gold-nommap``) survive the split
    because ``-`` is not a separator.
    """
    variant = getattr(args, "variant", "") or ""
    return axis_name in compiletools.configutils.split_variant(variant)


def _materialize_wild_b_searchdir() -> str | None:
    """Create (idempotently) a directory holding a symlink ``ld -> wild`` and
    return its path, for the ``wild-B`` linker axis.

    gcc/clang's ``-B<dir>`` adds <dir> to the executable search path; gcc's
    collect2 then invokes ``<dir>/ld``, which we point at the wild binary.
    This is the universal wild invocation that works on ANY gcc version
    (unlike ``-fuse-ld=wild``, which needs gcc >= 16.1).

    Location: ``<gitroot>/.ct-wild-ld/`` when a gitroot exists, so the
    injected ``-B<gitroot>/.ct-wild-ld`` is rewritten by
    ``canonicalize_for_cache_key`` (``<gitroot>/...`` -> ``<GITROOT>``
    sentinel) in the link key and stays workspace-portable across users
    sharing a CAS. Falls back to a shared system temp dir
    (``<tmpdir>/ct-wild-ld``) when no gitroot exists
    (``_inject_ffile_prefix_map`` is a no-op in that case; here
    we still produce a usable -B dir).

    Returns None if the wild binary can't be found (the startup check
    ``_check_wild_linker_usable`` raises a clear error in that case).
    """
    wild_path = shutil.which("wild")
    if not wild_path:
        return None
    git_root = compiletools.git_utils.find_git_root()
    if git_root:
        search_dir = os.path.join(git_root, ".ct-wild-ld")
    else:
        search_dir = os.path.join(tempfile.gettempdir(), "ct-wild-ld")
    os.makedirs(search_dir, exist_ok=True)
    ld_link = os.path.join(search_dir, "ld")
    # Idempotent: (re)create only when missing or pointing elsewhere.
    try:
        current = os.readlink(ld_link)
    except OSError:
        current = None
    if current != wild_path:
        # Atomic replace via a PID-scoped temp name so concurrent ct-cake
        # processes sharing this gitroot don't race on a shared temp path
        # (one's os.symlink would hit EEXIST, or one's pre-unlink would yank
        # another's live temp out from under its os.replace). os.replace is
        # atomic and every racer writes the same target, so last-writer-wins
        # is safe. The pre-unlink only clears a leftover temp from a prior
        # crash of THIS pid.
        tmp_link = f"{ld_link}.{os.getpid()}.tmp"
        try:
            os.unlink(tmp_link)
        except OSError:
            pass
        os.symlink(wild_path, tmp_link)
        os.replace(tmp_link, ld_link)
    return search_dir


def _normalize_wild_linker(args) -> None:
    """Rewrite the wild linker selection to the form the link driver accepts,
    and wire up the ``wild-B`` ``-B``/symlink fallback.

    ``wild.conf`` emits the canonical, user-facing token ``-fuse-ld=wild`` —
    exactly what gcc >= 16.1 wants, but clang rejects it ("invalid linker
    name in argument '-fuse-ld=wild'") unless an ``ld.wild`` symlink is on
    PATH, which wild's installer does NOT create. clang's portable
    invocation is ``--ld-path=wild``, so for a clang link driver we rewrite
    the token (bare name, PATH-resolved, to keep per-user absolute paths out
    of the link key).

    The ``wild-B`` axis (a comment-only conf) selects the universal
    ``-B<dir>`` + ``ld -> wild`` symlink trick that works on ANY gcc; the
    symlink dir is materialised by :func:`_materialize_wild_b_searchdir`
    and stashed on ``args._wild_b_search_dir``. The link-rule builders in
    ``build_backend`` append ``-B<absolute_dir>`` directly to the emitted
    link argv, bypassing LDFLAGS (and therefore ``canonicalize_for_command``
    rewriting) — see comment in the wild-B branch below for the silent
    fall-through hazard this avoids.

    For the clang rewrite, mutates ``args.LDFLAGS`` in place. Called from
    ``_commonsubstitutions`` before ``_finalize_flag_state`` (via
    ``substitutions``), so ``args.LDFLAGS_tokens`` / ``args.flags`` are
    rebuilt from the mutated string and ``check_flag_string_drift`` stays
    satisfied. No-op when wild is not selected. Idempotent: after the clang
    rewrite the ``-fuse-ld=wild`` token is gone, so a re-run (e.g. via
    cake's two-stage parse) makes no further change.
    """
    ldflags = getattr(args, "LDFLAGS", "") or ""

    # `wild` axis: clang needs --ld-path=wild in place of -fuse-ld=wild.
    if "-fuse-ld=wild" in split_command_cached(ldflags):
        if compiler_kind(_effective_link_driver(args)) == "clang":
            tokens = split_command_cached(ldflags)
            rewritten = ["--ld-path=wild" if t == "-fuse-ld=wild" else t for t in tokens]
            args.LDFLAGS = shlex.join(rewritten)

    # `wild-B` axis: materialise the -B search dir. The -B<dir> flag itself
    # is injected per-rule by the link-rule builders in build_backend.py —
    # routing it through LDFLAGS would let canonicalize_for_command rewrite
    # the absolute path to a target-relative form (e.g. "-B./.ct-wild-ld")
    # that only resolves when the build runs from the gitroot, silently
    # falling through to the default linker under subdir invocation while
    # the CAS link key still claims the wild-B variant.
    if _variant_has_axis(args, "wild-B"):
        args._wild_b_search_dir = _materialize_wild_b_searchdir()


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


def tokenize_compile_flags(
    cppflags,
    cflags,
    cxxflags,
    strip_unhashed: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Tokenize compile-flag strings into structured lists with -D/-U removed.

    Used by MacroState's structured build-context hash. -D and -U entries
    are stripped because cmdline -D macros are hashed separately via the
    per-TU scoping mechanism. Other flags (-I, -O, -std, -W, -f...) pass
    through unchanged.

    Each input may be a string (will be shlex-split, with simple-split
    fallback on ValueError, matching extract_command_line_macros) or a
    pre-tokenized list of strings.

    Both attached form (-DFOO, -DFOO=bar, -UFOO) and detached form
    (-D FOO, -D FOO=bar, -U FOO) of -D/-U are stripped. Detached form
    drops both the flag token and the following value token. All other
    flags (-I, -O, -std, -W, -f...) pass through unchanged.

    When ``strip_unhashed=True``, also remove hash-irrelevant diagnostic
    tokens (warnings, message formatting, ``-pipe``, ``-v``) from each
    list via :func:`filter_hash_irrelevant_tokens`. Default ``False``
    preserves the previous behavior (only -D/-U stripped).

    Returns:
        (cppflags_tokens, cflags_tokens, cxxflags_tokens) -- three lists
        of remaining tokens, in original order.
    """

    def _to_tokens(value):
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if not value:
            return []
        try:
            return split_command_cached(value)
        except ValueError:
            return value.split()

    cpp = strip_d_u_tokens(_to_tokens(cppflags))
    c = strip_d_u_tokens(_to_tokens(cflags))
    cxx = strip_d_u_tokens(_to_tokens(cxxflags))
    if strip_unhashed:
        cpp = filter_hash_irrelevant_tokens(cpp)
        c = filter_hash_irrelevant_tokens(c)
        cxx = filter_hash_irrelevant_tokens(cxx)
    return (cpp, c, cxx)


@functools.lru_cache(maxsize=64)
def compiler_identity(cxx: str, *, anchor_root: str = "") -> str:
    """Return a stable identity string for a compiler binary.

    Used as part of cache keys (PCH cache key in ``build_backend`` and the
    per-TU object cache key via ``MacroState.compiler_identity``). Two users
    on the same shared filesystem with different ``$PATH``s could otherwise
    collide on the same key while resolving ``args.CXX`` (e.g. bare ``g++``)
    to different binaries (different versions, different stdlibs). GCC's PCH
    stamp catches this at *consume* time -- but the slow fallback compile
    defeats the cache. By including binary realpath + (st_size, st_mtime),
    we make distinct compilers produce distinct cache entries.

    When the resolved binary lives under ``anchor_root``, the realpath
    *segment* of the returned ``<realpath>|<size>|<mtime_ns>`` triple is
    rewritten to ``<GITROOT>/<relpath>`` via
    :func:`canonicalize_path_for_cache_key` so two CI checkouts at
    different absolute prefixes share the same cache key. The
    ``|<size>|<mtime_ns>`` tail is unchanged. The default
    ``anchor_root=""`` is a graceful no-op (identity) for backward
    compatibility and ad-hoc test fixtures — **production call sites
    must always pass an anchor**, otherwise the workspace prefix leaks
    into every downstream cache key (PCH / PCM / per-TU object / link / ar).

    Falls back to the original string when the binary cannot be stat'd
    (e.g. user passed a non-path command like ``ccache g++``). The fallback
    string is also canonicalised against ``anchor_root`` when it parses
    as an absolute path under the anchor — otherwise the leak would
    survive the fallback path. Returns ``""`` when ``cxx`` is None /
    empty so unconfigured ``args.CXX`` (some unit-test fixtures) doesn't
    crash the helper.

    Side effect: any tool that bumps the compiler binary's mtime (e.g.
    a no-op ``touch /usr/bin/g++``) will invalidate the cache. This is
    acceptable because the false positive forces a rebuild -- a slow
    correct outcome -- whereas a false negative (which this helper is
    designed to prevent) would silently produce a stale ``.o``.
    """
    if not cxx:
        return ""
    resolved = shutil.which(cxx) or cxx
    try:
        st = os.stat(resolved)
        # Use nanosecond mtime so a sub-second compiler swap (e.g.
        # ``cp new-g++ /usr/local/bin/g++`` followed immediately by a
        # build) does not collide on the cache key.
        real = compiletools.wrappedos.realpath(resolved)
        canonical = canonicalize_path_for_cache_key(real, anchor_root)
        return f"{canonical}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return canonicalize_path_for_cache_key(resolved, anchor_root)


@functools.lru_cache(maxsize=64)
def find_system_std_module_source(cxx: str | None, kind: str) -> str | None:
    """Locate the compiler-provided source for the standard library module.

    Returns an absolute filesystem path to the file the build system can
    feed back into the compiler to materialize the ``std`` module's
    ``.gcm`` (gcc) or ``.pcm`` (clang). Returns ``None`` when the
    requested toolchain doesn't ship one (or we can't find it).

    Search strategy:

    - **gcc**: parse ``g++ -print-search-dirs`` for the compiler install
      root, then look for ``<root>/include/c++/<version>/bits/std.cc``.
      This is what the GNU toolchain ships starting with gcc 15+ as the
      canonical std-module source.
    - **clang**: walk up from the binary path two levels (``bin/`` ->
      install root) and look for ``share/libc++/v1/std.cppm``. This is
      what clang ships when built against libc++.

    Both probes are pure filesystem operations -- no compiler invocation
    -- so they are cheap and safe to call from cache-key paths.
    """
    if not cxx or kind not in ("gcc", "clang"):
        return None
    # Handle compiler-wrapper strings like ``ccache g++`` / ``distcc clang++``:
    # ``shutil.which("ccache g++")`` returns None (no binary literally named
    # ``"ccache g++"``), so falling back to the original string would feed
    # subprocess.run an unfindable argv0 (gcc branch) or os.path.realpath an
    # unresolvable path (clang branch) and silently return None. Mirror
    # ``compiler_kind``'s raw-string fallback: if the bare string isn't on
    # PATH, retry with the last whitespace-separated token (the real driver
    # after the wrapper).
    resolved = shutil.which(cxx)
    if resolved is None and " " in cxx:
        last = cxx.rsplit(None, 1)[-1]
        resolved = shutil.which(last) or last
    elif resolved is None:
        resolved = cxx
    if kind == "gcc":
        # `g++ -print-search-dirs` reports `install: <path-to-bin>/../lib/gcc/<triple>/<ver>/`
        try:
            r = subprocess.run(
                [resolved, "-print-search-dirs"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        install_dir = None
        for line in r.stdout.splitlines():
            if line.startswith("install:"):
                install_dir = line.split(":", 1)[1].strip()
                break
        if not install_dir:
            return None
        # install_dir = .../bin/../lib/gcc/<triple>/<version>/
        # Walk up to <install root> = .../bin/.., then look for include/c++/<ver>/bits/std.cc
        # The version is the same as the install_dir's last directory.
        version = os.path.basename(install_dir.rstrip(os.sep))
        # Normalize the install root (drop the .../lib/gcc/<triple>/<ver>/
        # tail) by going up four levels and resolving symlinks/.. in
        # one shot. The four-level count assumes the canonical
        # ``<root>/lib/gcc/<triple>/<version>/`` layout reported by
        # ``-print-search-dirs``; if a distro symlinks ``bin/g++``
        # somewhere unconventional and ``-print-search-dirs`` returns
        # a non-canonical install path, the candidate file won't exist
        # and we return None (graceful: caller falls back to no-cache
        # behaviour).
        gcc_root = os.path.realpath(os.path.join(install_dir, "..", "..", "..", ".."))
        candidate = os.path.join(gcc_root, "include", "c++", version, "bits", "std.cc")
        return candidate if os.path.isfile(candidate) else None
    # clang
    bin_dir = os.path.dirname(os.path.realpath(resolved))
    install_root = os.path.dirname(bin_dir)  # bin/.. -> install root
    candidate = os.path.join(install_root, "share", "libc++", "v1", "std.cppm")
    return candidate if os.path.isfile(candidate) else None


@functools.lru_cache(maxsize=64)
def compiler_kind(cxx: str | None) -> str:
    """Classify a C++ compiler binary as ``"gcc"`` / ``"clang"`` / ``"unknown"``.

    Used to pick compiler-specific code paths (e.g., the C++20 modules
    flag set: gcc needs ``-fmodules-ts`` while clang doesn't, and clang
    uses ``--precompile`` / ``-fprebuilt-module-path=`` for the BMI flow).

    Detection resolves the binary via ``shutil.which`` and inspects the
    basename. A gcc-ish basename (``g++``/``gcc``) is then verified
    against the binary's ``--version`` banner -- on Termux ``g++`` is a
    symlink to ``clang-21``, and dispatching gcc-only flags like
    ``-fmodules-ts`` at it fails the compile. Symmetric reverse case
    (``clang`` symlinked to gcc) is exceedingly rare and not probed; the
    basename wins for the clang side. The probe happens at most once per
    unique input string because the function is ``lru_cache``-d.

    Falls back to scanning the original string for ``clang`` / ``gcc`` /
    ``g++`` substrings when the binary can't be located -- callers that
    hand us a compound string like ``ccache clang++`` should still get
    the right answer.

    Returns ``"unknown"`` for ``None`` / empty input or when the basename
    matches neither toolchain. Callers must handle the unknown case
    rather than guessing.
    """
    if not cxx:
        return "unknown"
    resolved = shutil.which(cxx) or cxx
    base = os.path.basename(resolved).lower()
    # Strip versions/wrappers like ``g++-15`` or ``clang++-22.1.3``.
    if "clang" in base:
        return "clang"
    if "g++" in base or "gcc" in base:
        # Verify against --version: a gcc-ish basename on a binary that
        # actually reports clang (Termux ships ``g++`` -> ``clang-21``)
        # must be classified as clang, otherwise we dispatch gcc-only
        # flags at it and the compile fails. Use the resolved path so
        # ``--version`` is the binary on disk; fall through to "gcc" if
        # the probe can't parse a recognised banner.
        probe = _compiler_major_version(resolved)
        if probe is not None and probe[0] == "clang":
            return "clang"
        return "gcc"
    # Fall back to scanning the raw string -- handles ``ccache g++`` and
    # similar wrappers that point at a shim with no toolchain hint in
    # its name but mention the real compiler in the original argv0.
    raw = cxx.lower()
    if "clang" in raw:
        return "clang"
    if "g++" in raw or "gcc" in raw:
        return "gcc"
    return "unknown"


@functools.lru_cache(maxsize=64)
def compiler_default_cxx_std(cxx: str | None) -> str | None:
    """Return the ``-std=`` flag matching the compiler's natural default
    C++ dialect, e.g. ``-std=gnu++20`` for gcc-16, ``-std=gnu++17`` for
    clang-21. Returns ``None`` if the default cannot be determined.

    Used to align PCH/BMI prebuilt artefacts with downstream consumer
    compiles when the user hasn't explicitly set ``-std=`` in CXXFLAGS.
    Different compilers (and different versions of the same compiler)
    pick different defaults — gcc-16 ships ``gnu++20``, clang-21 ships
    ``gnu++17`` — and a hardcoded fallback would silently desync one
    of them. Bazel's ``rules_cc`` autoconfig appends its own
    ``-std=c++17`` to every C++ action; without aligning to the
    compiler's actual default, the prebuilt artefact and the bazel-
    spawned consumer end up at different dialects and gcc rejects the
    PCH (``__cpp_impl_three_way_comparison not defined``) or the BMI
    (``language dialect differs 'C++20', expected 'C++17'``).

    Always returns the ``gnu++`` mode (preserving non-ISO built-ins
    like ``unix``, ``linux``, ``__unix__``) rather than strict
    ``c++`` mode — gcc/clang both default to gnu mode, and switching
    to strict mode would itself invalidate PCH (``unix not defined``).

    Implementation: invokes ``<cxx> -dM -E -x c++ /dev/null`` and
    parses the ``__cplusplus`` macro value. Cached by ``cxx`` string.
    """
    if not cxx or not isinstance(cxx, str):
        return None
    cmd = shlex.split(cxx) + ["-dM", "-E", "-x", "c++", os.devnull]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # Parse `#define __cplusplus 202002L` etc.
    cplusplus_value: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("#define __cplusplus "):
            tok = line.split()[-1].rstrip("Ll")
            try:
                cplusplus_value = int(tok)
            except ValueError:
                return None
            break
    if cplusplus_value is None:
        return None
    # Map __cplusplus value → gnu++NN dialect string. Values are the
    # ISO C++ feature-test macro: 199711 (C++98), 201103 (C++11),
    # 201402 (C++14), 201703 (C++17), 202002 (C++20), 202302 (C++23),
    # and forward-compat for unreleased standards.
    _STD_MAP = {
        199711: "gnu++98",
        201103: "gnu++11",
        201402: "gnu++14",
        201703: "gnu++17",
        202002: "gnu++20",
        202302: "gnu++23",
        202602: "gnu++26",
    }
    dialect = _STD_MAP.get(cplusplus_value)
    if dialect is None:
        # Unknown future value — pick the closest known dialect ≤ value.
        # gnu++NN is forward-compatible (a c++23 compiler accepts
        # `-std=gnu++23` even if it predates the c++23 spec).
        known = sorted(k for k in _STD_MAP if k <= cplusplus_value)
        if not known:
            return None
        dialect = _STD_MAP[known[-1]]
    return f"-std={dialect}"


def clear_cache():
    """Clear any caches for macro extraction and pkg-config."""
    cached_pkg_config.cache_clear()
    _get_functional_cxx_compiler_cached.cache_clear()
    compiler_identity.cache_clear()
    compiler_kind.cache_clear()
    compiler_default_cxx_std.cache_clear()
    find_system_std_module_source.cache_clear()


@functools.lru_cache(maxsize=8)
def _get_functional_cxx_compiler_cached(env_cxx=None, env_cc=None, env_path=None):
    """Internal cached implementation of functional C++ compiler detection.

    This function tests compiler candidates to ensure they can:
    - Execute basic version checks
    - Compile C++20 code with -std=c++20

    Args:
        These are only used for test cases.  For normal use, call get_functional_cxx_compiler()
        env_cxx: Value of CXX environment variable (or None)
        env_cc: Value of CC environment variable (or None)
        env_path: Value of PATH environment variable (for cache invalidation)

    Returns:
        str: Path to working C++ compiler executable, or None if none found
    """
    # Compiler candidates to test, in priority order
    candidates = []

    # Check environment variables first (user preference)
    if env_cxx and env_cxx.strip():
        candidates.append(env_cxx.strip())
    if env_cc and env_cc.strip():
        # Try adding ++ suffix for C compilers that might have C++ versions
        cc = env_cc.strip()
        candidates.append(cc)
        if cc.endswith("gcc"):
            candidates.append(cc.replace("gcc", "g++"))
        elif cc.endswith("clang"):
            candidates.append(cc.replace("clang", "clang++"))

    # Common system compiler names
    common_compilers = ["g++", "clang++", "gcc", "clang"]
    for compiler in common_compilers:
        if compiler not in candidates:
            candidates.append(compiler)

    # Test each candidate
    for compiler_name in candidates:
        if _test_compiler_functionality(compiler_name):
            return compiler_name

    return None


def derive_c_compiler_from_cxx(cxx_compiler):
    """Derive a C compiler from a C++ compiler name.

    Args:
        cxx_compiler (str): C++ compiler name (e.g., 'g++', 'clang++')

    Returns:
        str: Corresponding C compiler name (e.g., 'gcc', 'clang')
    """
    cxx_to_c_map = {
        "g++": "gcc",
        "clang++": "clang",
    }

    return cxx_to_c_map.get(cxx_compiler, cxx_compiler)


def get_functional_cxx_compiler():
    """Detect and return a fully functional C++ compiler that supports C++20.

    IMPORTANT: This is a FALLBACK mechanism for when args.CXX is not set.
    Production code should rely on args.CXX being properly configured by
    parseargs() rather than calling this function directly.

    This function tests compiler candidates to ensure they can:
    - Execute basic version checks
    - Compile C++20 code with -std=c++20

    The result is cached for performance since compiler detection is expensive.
    The cache key includes environment variables so changes are detected.

    Returns:
        str: Path to working C++ compiler executable, or None if none found

    Usage:
        # PREFERRED - rely on parseargs() setting args.CXX:
        args = parseargs(cap, argv)
        compiler = args.CXX  # Already validated and set

        # FALLBACK - only when args.CXX is not available:
        if not hasattr(args, 'CXX') or args.CXX is None:
            compiler = get_functional_cxx_compiler()
    """
    return _get_functional_cxx_compiler_cached()


# Expose the cache_clear method for tests
get_functional_cxx_compiler.cache_clear = _get_functional_cxx_compiler_cached.cache_clear  # type: ignore[attr-defined]


def _test_compiler_functionality(compiler_name):
    """Test if a compiler supports the functionality needed by the test suite.

    Args:
        compiler_name: Name or path of compiler to test

    Returns:
        bool: True if compiler is fully functional, False otherwise
    """
    try:
        # Test 1: Basic version check
        # Split compiler_name to handle multi-word commands like "ccache g++"
        result = subprocess.run(
            split_command_cached(compiler_name) + ["--version"], capture_output=True, timeout=5, text=True
        )
        if result.returncode != 0:
            return False

        # Test 2: C++20 compilation test
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cpp", delete=False) as f:
            # Write a simple C++20 test program
            f.write(
                textwrap.dedent("""
                #include <iostream>
                #include <string_view>
                #include <optional>
                #include <concepts>
                template<typename T>
                concept Integral = std::integral<T>;
                int main() {
                    std::string_view sv = "C++20 test";
                    std::optional<int> opt = 42;
                    return 0;
                }
            """).strip()
            )
            test_cpp = f.name

        obj_path = None
        try:
            # Try to compile with C++20
            with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as obj_file:
                obj_path = obj_file.name

            result = subprocess.run(
                split_command_cached(compiler_name) + ["-std=c++20", "-c", test_cpp, "-o", obj_path],
                capture_output=True,
                timeout=10,
                text=True,
            )

            success = result.returncode == 0

        finally:
            # Cleanup test files
            try:
                os.unlink(test_cpp)
            except OSError:
                pass
            if obj_path is not None:
                try:
                    os.unlink(obj_path)
                except OSError:
                    pass

        return success

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


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

    gitroot = compiletools.git_utils.find_git_root()

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


_PROJECT_MACRO_DEPRECATION_MESSAGE = (
    "ct-cake: --project-version / --project-name (and their *-cmd variants) "
    "are DEPRECATED. They inject -D macros that defeat object-cache reuse "
    "for any TU whose transitive headers textually mention the macro name. "
    "Use --prebuild-script with a generated implementation file instead — "
    "see examples-end-to-end/appinfo/ and README.ct-cake.rst.\n"
)


def _warn_project_macros_deprecated(args):
    """Emit the deprecation warning once per process for the project-macro flags."""
    if getattr(args, "_project_macro_deprecation_warned", False):
        return
    sys.stderr.write(_PROJECT_MACRO_DEPRECATION_MESSAGE)
    args._project_macro_deprecation_warned = True


def _set_project_version(args):
    """Inject ``-DCT_PROJECT_VERSION="<value>"`` into CPPFLAGS/CFLAGS/CXXFLAGS,
    but only if the user opted in.

    Opt-in is any of:
      * ``--project-version VALUE`` on CLI / ct.conf / env
      * ``--project-version-cmd CMD`` on CLI / ct.conf / env

    If neither is set, do nothing — no macro is injected. This keeps
    cmdline ``-D`` cache-key noise off TUs that don't ask for it (see the
    "Macro Scope Filter" section of README.ct-cake.rst for why a
    cmdline ``-D`` macro is sticky once introduced).

    DEPRECATED — see ``_warn_project_macros_deprecated``. The
    generated-implementation-file pattern (``examples-end-to-end/appinfo``)
    is the supported replacement.
    """
    projectversion = getattr(args, "projectversion", None)
    projectversioncmd = getattr(args, "projectversioncmd", None)

    if not projectversion and projectversioncmd:
        try:
            projectversion = (
                subprocess.check_output(projectversioncmd.split(), universal_newlines=True).strip("\n").split()[0]
            )
            args.projectversion = projectversion
            if args.verbose >= 6:
                print("Used projectversioncmd to set projectversion")
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(
                " ".join(
                    [
                        "Could not use projectversioncmd =",
                        projectversioncmd,
                        "to set projectversion.\n",
                    ]
                )
            )
            if args.verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise

    if not projectversion:
        return

    _warn_project_macros_deprecated(args)

    version_escaped = projectversion.replace("\\", "\\\\").replace('"', '\\"')

    if "-DCT_PROJECT_VERSION" not in args.CPPFLAGS:
        args.CPPFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
    if "-DCT_PROJECT_VERSION" not in args.CFLAGS:
        args.CFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
    if "-DCT_PROJECT_VERSION" not in args.CXXFLAGS:
        args.CXXFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')

    if args.verbose >= 6:
        print("*FLAG variables have been modified with the project version:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def _set_project_name(args):
    """Inject ``-DCT_PROJECT_NAME="<value>"`` into CPPFLAGS/CFLAGS/CXXFLAGS,
    but only if the user opted in.

    Opt-in is any of:
      * ``--project-name VALUE`` on CLI / ct.conf / env
      * ``--project-name-cmd CMD`` on CLI / ct.conf / env

    If neither is set, do nothing — no macro is injected. Mirrors
    _set_project_version. See the "Macro Scope Filter" section of
    README.ct-cake.rst for why CT_PROJECT_NAME (like any cmdline ``-D``
    macro) should only be turned on when actually needed: comments in
    transitive headers that mention the macro by name will pull it
    into every includer's per-TU cache key.
    """
    projectname = getattr(args, "projectname", None)
    projectnamecmd = getattr(args, "projectnamecmd", None)

    if not projectname and projectnamecmd:
        try:
            projectname = (
                subprocess.check_output(projectnamecmd.split(), universal_newlines=True).strip("\n").split()[0]
            )
            args.projectname = projectname
            if args.verbose >= 6:
                print("Used projectnamecmd to set projectname")
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(
                " ".join(
                    [
                        "Could not use projectnamecmd =",
                        projectnamecmd,
                        "to set projectname.\n",
                    ]
                )
            )
            if args.verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise

    if not projectname:
        return

    _warn_project_macros_deprecated(args)

    name_escaped = projectname.replace("\\", "\\\\").replace('"', '\\"')

    if "-DCT_PROJECT_NAME" not in args.CPPFLAGS:
        args.CPPFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')
    if "-DCT_PROJECT_NAME" not in args.CFLAGS:
        args.CFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')
    if "-DCT_PROJECT_NAME" not in args.CXXFLAGS:
        args.CXXFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')

    if args.verbose >= 6:
        print("*FLAG variables have been modified with the project name:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def _do_xxpend(args, name):
    """For example, if name is CPPFLAGS, take the
    args.prependcppflags and prepend them to args.CPPFLAGS.
    Similarly for append.
    """
    xxlist = ("prepend", "append")
    for xx in xxlist:
        xxpendname = "_".join([xx, name.lower()])
        if hasattr(args, xxpendname):
            xxpendattr = getattr(args, xxpendname)
            attr = getattr(args, name)

            if xxpendattr:
                extra = []
                for flag in xxpendattr:
                    if flag not in attr:
                        extra.append(flag)
                        if args.verbose > 8:
                            print(f"{xx} {extra} to {name}")
                if xx == "prepend":
                    attr = " ".join(extra + [attr])
                else:
                    attr = " ".join([attr] + extra)
            setattr(args, name, attr)


def _do_xxpend_list(args, name, dest_name=None):
    """List-typed sibling of ``_do_xxpend`` for attrs whose canonical form
    is a Python list (e.g. ``args.pkg_config``), not a flag string. The
    base attr is read from ``args.<dest_name or name.replace('-','_')>``,
    and the prepend/append sources from
    ``args.{prepend,append}_<dest_name or name.replace('-','_')>``.

    Mirrors ``_do_xxpend``'s dedup-and-place rule (prepend leftmost,
    append rightmost, skip duplicates already present in the base) so
    consumers of ``--prepend-PKG-CONFIG`` / ``--append-PKG-CONFIG`` get
    the same composition semantics that compiler-flag slots have.
    """
    dest = (dest_name or name).lower().replace("-", "_")
    base = list(getattr(args, dest, []) or [])
    for xx in ("prepend", "append"):
        xxpendname = f"{xx}_{dest}"
        xxpendattr = getattr(args, xxpendname, None) or []
        extras = [v for v in xxpendattr if v not in base]
        if not extras:
            continue
        if xx == "prepend":
            base = extras + base
        else:
            base = base + extras
    setattr(args, dest, base)


def _unify_cpp_cxx_flags(args):
    """Combine CPPFLAGS and CXXFLAGS into a single deduplicated value.

    Skipped when --separate-flags-CPP-CXX is set.

    Uses ``shlex.join`` (not ``' '.join``) to reconstruct the raw flag
    string from the deduplicated token list.  ``combine_and_deduplicate``
    calls ``shlex.split`` to tokenise, so the token list may contain
    entries with shell-special characters (e.g. ``-DCT_PROJECT_VERSION="1.2.3"``
    with literal double-quote chars produced by ``_set_project_version``).
    A plain ``' '.join`` writes those tokens back to the raw string without
    any shell quoting, causing a subsequent ``shlex.split`` (in
    ``_finalize_flag_state``) to strip the double-quote characters — so the
    C-string-literal delimiters are lost before the token ever reaches the
    compiler.  ``shlex.join`` re-adds single-quote wrapping for tokens that
    contain shell-active characters, preserving the round-trip invariant.
    """
    if getattr(args, "separate_flags_CPP_CXX", False):
        return
    unified = shlex.join(compiletools.utils.combine_and_deduplicate_compiler_flags(args.CPPFLAGS, args.CXXFLAGS))
    args.CPPFLAGS = unified
    args.CXXFLAGS = unified


def _deduplicate_all_flags(args):
    """Deduplicate all compiler and linker flags after all processing is complete.

    Uses ``shlex.join`` to reconstruct each raw flag string from the
    deduplicated token list, preserving shell-special characters in tokens
    (e.g. double-quote chars in ``-DCT_PROJECT_VERSION="1.2.3"``).  See
    ``_unify_cpp_cxx_flags`` for the full rationale.
    """
    flaglist = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
    for flag_name in flaglist:
        if hasattr(args, flag_name):
            flag_value = getattr(args, flag_name)
            if flag_value:
                # Split the flag string into individual flags and deduplicate
                deduplicated_flags = compiletools.utils.combine_and_deduplicate_compiler_flags(flag_value)
                # Use shlex.join (not ' '.join) so tokens with shell-active
                # characters survive the round-trip through shlex.split.
                setattr(args, flag_name, shlex.join(deduplicated_flags))


def _tier_one_modifications(args):
    """Do some early modifications that can potentially cause
    downstream modifications.
    """
    if args.verbose > 8:
        print("Tier one modification")
        print(f"{args=}")
    _substitute_CXX_for_missing(args)
    flaglist = ("INCLUDE", "CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
    for flag in flaglist:
        _do_xxpend(args, flag)

    # args.pkg_config is a list of package names (not a flag string), so
    # it needs the list-typed merge — _do_xxpend would " ".join() it into
    # a single space-separated string and break downstream consumers.
    _do_xxpend_list(args, "pkg-config")

    # Deduplicate all compiler/linker flags after all processing is complete
    _deduplicate_all_flags(args)

    # Cake used preprocess to mean both magic flag preprocess and headerdeps preprocess
    if hasattr(args, "preprocess") and args.preprocess:
        args.magic = "cpp"
        args.headerdeps = "cpp"


def _strip_quotes(args):
    """Remove shell quotes from arguments while preserving content quotes.

    Uses proper shell parsing to understand when quotes are shell quoting
    vs. part of the actual content. Also strips extraneous whitespace.
    """
    for name in vars(args):
        value = getattr(args, name)
        if value is not None:
            # Can't just use the for loop directly because that would
            # try and process every character in a string
            if compiletools.utils.is_non_string_iterable(value):
                for index, element in enumerate(value):
                    value[index] = _safely_unquote_string(element)
            else:
                try:
                    # Otherwise assume its a string
                    setattr(args, name, _safely_unquote_string(value))
                except (AttributeError, ValueError, TypeError):
                    logging.debug("Could not unquote arg %s (type %s)", name, type(value).__name__)


def _safely_unquote_string(value):
    """Safely remove shell quotes from a string using proper parsing.

    Only removes quotes that are actual shell quotes, not content quotes.
    Falls back to compatibility behavior for edge cases.
    """
    if not isinstance(value, str):
        return value

    # Strip whitespace first
    value = value.strip()

    # If the string doesn't look like it has shell quotes, don't process it
    if not value.startswith(('"', "'")):
        return value

    try:
        # Use shlex to parse the string as shell would
        # If it parses to exactly one token, it was properly quoted
        tokens = split_command_cached(value)
        if len(tokens) == 1:
            # Single token means the quotes were shell quotes
            unquoted = tokens[0]

            # For backwards compatibility, if the result still has quotes at both ends,
            # recursively strip them (mimics old behavior for nested quotes)
            if (unquoted.startswith('"') and unquoted.endswith('"')) or (
                unquoted.startswith("'") and unquoted.endswith("'")
            ):
                return _safely_unquote_string(unquoted)
            return unquoted
        else:
            # Multiple tokens or parsing issues - return original
            return value
    except ValueError:
        # Malformed quoting - fall back to original naive approach for compatibility
        # but only strip matching quote pairs
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1].strip()
        return value.strip("\"'").strip()


def _flatten_variables(args):
    """Most of the code base was written to expect CXXFLAGS are a single string with space separation.
    However, around 20240920 we allowed some variables to be lists of those strings.  To allow this
    change to slip in with minimal code changes, we flatten out the list into a single string.

    Uses ``shlex.join`` (not ``' '.join``) so that list elements containing
    shell-special characters (embedded spaces, double-quotes, etc.) survive the
    subsequent ``shlex.split`` call in ``_finalize_flag_state``.  When the user
    passes ``--CPPFLAGS '-DFOO=bar baz'`` on the CLI, the shell consumes the
    outer quotes and argparse stores ``'-DFOO=bar baz'`` as a single list element;
    ``' '.join`` would produce ``'-DFOO=bar baz -Wall'`` (unsplit on the space),
    and ``shlex.split`` would then misparse it as three tokens.  Cousin fix to
    commit 5cd77781 which patched the same pattern in ``_unify_cpp_cxx_flags``
    and ``_deduplicate_all_flags``.
    """
    for varname in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "INCLUDE"):
        if isinstance(getattr(args, varname, None), list):
            setattr(args, varname, shlex.join(getattr(args, varname)))


def _commonsubstitutions(args):
    """If certain arguments have not been specified but others have
    then there are some obvious substitutions to make
    """
    args.verbose -= args.quiet

    if args.verbose > 8:
        print("Performing common substitutions")

    # Canonicalize the parsed args.variant. The default value for --variant
    # came from extract_variant(argv) at parser construction with composite
    # input already canonicalized, but an explicit --variant=gcc,debug,asan
    # in argv bypasses that — argparse stores the raw comma-separated string.
    # Resolve here so downstream consumers (cas-objdir/<variant>/, bindir,
    # compile_commands.<variant>.json) always see the dotted canonical form.
    args.variant = compiletools.configutils.canonicalize_variant_input(args.variant, argv=getattr(args, "_argv", None))
    # Re-resolve fresh from the post-argparse variant value rather than
    # caching the create_parser-time resolution. Cheap (just file stats),
    # avoids module-level mutable state, and uses the correct variant when
    # --variant was explicitly set on the CLI. Pass argv through so the
    # explicit_config branch fires when --config=path was supplied
    # (args.variant becomes the implied basename, which isn't a real
    # axis — the resolver needs argv to know that the basename is from
    # --config and not a request to look up a missing axis).
    args._variant_resolution = compiletools.configutils.resolve_variant(
        variant=args.variant, argv=getattr(args, "_argv", None)
    )
    if args.verbose > 6:
        print(f"Determined variant to be {args.variant}")
    # Per-axis provenance is short (10-15 lines) and answers "why did I get
    # these flags?" without rebuilding. Emit it at -vv and above; quiet by
    # default so build-system wrappers around ct-cake aren't surprised by
    # extra stdout. ct-config auto-bumps verbosity so it always shows.
    if args.verbose >= 2 and args._variant_resolution is not None:
        print(compiletools.configutils.format_variant_resolution(args._variant_resolution))

    _tier_one_modifications(args)
    _extend_includes_using_git_root(args)
    _add_include_paths_to_flags(args)
    _setup_pkg_config_overrides(
        args._context,
        args.verbose,
        prepend_paths=getattr(args, "prepend_pkg_config_path", None),
        append_paths=getattr(args, "append_pkg_config_path", None),
        args_parser=getattr(args, "_parser", None),
    )
    _add_flags_from_pkg_config(args)
    _set_project_version(args)
    _set_project_name(args)
    _unify_cpp_cxx_flags(args)

    try:
        # If the user didn't explicitly supply a bindir then modify the bindir to use the variant name
        args.bindir = unsupplied_replacement(args.bindir, os.path.join("bin", args.variant), args.verbose, "bindir")
    except AttributeError:
        pass

    resolve_cas_directory_arguments(args)

    # Anchor --test-xml-dir to gitroot so the value survives a `cd` into
    # a subdirectory between parseargs and the build, matching how
    # cas-objdir / cas-pchdir are anchored. This is a path resolution
    # only; the directory is created by the build graph's mkdir rule.
    test_xml_dir = getattr(args, "test_xml_dir", None)
    if test_xml_dir and not os.path.isabs(test_xml_dir):
        git_root = compiletools.git_utils.find_git_root()
        if git_root:
            args.test_xml_dir = os.path.join(git_root, test_xml_dir)
        else:
            args.test_xml_dir = os.path.abspath(test_xml_dir)

    # Round 3: inject -ffile-prefix-map=<gitroot>=<target> into CXXFLAGS /
    # CFLAGS so cas-objdir / cas-pchdir / cas-pcmdir contents are byte-
    # identical across users with different checkout paths. Must run AFTER
    # all earlier flag mutations (project version / name macros, CPP/CXX
    # unification, pkg-config flag merging) so the detection scan sees the
    # final user-state of the slot before deciding whether to inject. The
    # subsequent _finalize_flag_state call (via substitutions(args) in the
    # caller) rebuilds args.flags from the now-injected raw strings.
    _inject_ffile_prefix_map(args)

    # Rewrite the wild linker selection to the form the resolved link driver
    # accepts (clang: -fuse-ld=wild -> --ld-path=wild) and wire the wild-B
    # -B/symlink fallback. Runs here so the subsequent _finalize_flag_state
    # (via substitutions) rebuilds args.LDFLAGS_tokens / args.flags from the
    # mutated string. See _normalize_wild_linker.
    _normalize_wild_linker(args)


# List to store the callback functions for parse args
_substitutioncallbacks = [_commonsubstitutions]


def resetcallbacks():
    """Useful in tests to clear out the substitution callbacks"""
    global _substitutioncallbacks
    _substitutioncallbacks = [_commonsubstitutions]


def registercallback(callback):
    """Use this to register a function to be called back during the
    substitutions call (usually during parseargs).
    The callback function will later be given "args" as its argument.
    """
    _substitutioncallbacks.append(callback)


def substitutions(args, verbose=None):
    if verbose is None:
        verbose = args.verbose

    for func in _substitutioncallbacks:
        if verbose > 8:
            print(f"Performing substitution: {func.__qualname__}")
        func(args)

    if verbose >= 8:
        print("Args after substitutions")
        verboseprintconfig(args)

    # substitutions is the canonical mutation site for raw flag strings.
    # Refresh args.{*}_tokens, args.flags, and the drift snapshot so any
    # caller that re-runs substitutions (e.g. cake's two-stage parse for
    # findtargets) sees a coherent post-mutation state. Idempotent when
    # nothing changed.
    if hasattr(args, "CPPFLAGS") or hasattr(args, "CFLAGS") or hasattr(args, "CXXFLAGS") or hasattr(args, "LDFLAGS"):
        _finalize_flag_state(args)


def _fix_variable_handling_method(cap, argv, verbose):
    # TODO: FIXME: Correct fix is to have a PR into configargparse
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


_LEGACY_CAS_KEY_RE = re.compile(r"^\s*(objdir|pchdir)\s*=", re.MULTILINE)
_LEGACY_VARIANT_KEY_RE = re.compile(r"^\s*variantaliases\s*=", re.MULTILINE)


def _check_legacy_variant_config_keys(config_files) -> None:
    """Fail loud on the obsolete ``variantaliases =`` key.

    The alias mechanism was replaced by config-file inheritance + axis
    composition. configargparse silently ignores unknown keys, so an old
    ``ct.conf`` with ``variantaliases = {'debug':'gcc.debug'}`` would
    quietly stop working and the user would build the wrong variant. Raise
    a pointer at the upgrade guide instead.
    """
    offenders = []
    for path in config_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for match in _LEGACY_VARIANT_KEY_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append((path, line_no))
    if offenders:
        details = "\n".join(f"  {p}:{n}: variantaliases" for p, n in offenders)
        raise RuntimeError(
            "Legacy 'variantaliases =' config key detected. The variant alias "
            "mechanism has been replaced by config inheritance + axis composition:\n"
            f"{details}\n"
            "Replace the alias dict with either (a) an `extends = ...` directive in "
            "the named conf file, or (b) the default variant set to the composed name "
            "(e.g. `variant = gcc.debug` instead of "
            "`variantaliases = {'debug':'gcc.debug'}`). See "
            "README.ct-config.rst section 'Upgrading from variantaliases' for the "
            "migration recipe."
        )


# Static (compiler, min-version) table for language-standard support.
# Source: https://gcc.gnu.org/projects/cxx-status.html
#         https://clang.llvm.org/cxx_status.html
# Values are the major version of the compiler that first implemented
# (substantially complete) support for each standard.  When the user
# requests `-std=c++NN` via an axis or CLI flag we compare the detected
# compiler version against this table; an undershoot is a hard error
# (otherwise the compile fails later with an opaque "unrecognized command
# line option" diagnostic and no pointer at the variant chain).
_STD_MIN_COMPILER_VERSION = {
    # C++ standards
    "c++11": {"gcc": 4, "clang": 3},
    "c++14": {"gcc": 6, "clang": 3},
    "c++17": {"gcc": 7, "clang": 5},
    "c++20": {"gcc": 10, "clang": 10},
    "c++23": {"gcc": 13, "clang": 17},
    "c++26": {"gcc": 14, "clang": 18},
    # C standards (informational)
    "c99": {"gcc": 4, "clang": 3},
    "c11": {"gcc": 4, "clang": 3},
    "c17": {"gcc": 7, "clang": 7},
    "c23": {"gcc": 14, "clang": 18},
}


@functools.cache
def tool_version(tool: str, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Probe ``<tool> --version`` for ``(major, minor)``.

    Returns ``default`` if the tool is missing, exits non-zero, or the
    first output line does not contain a ``\\d+\\.\\d+`` token. Cached so
    repeated probes during one process are free.
    """
    try:
        line = subprocess.check_output([tool, "--version"], text=True).splitlines()[0]
    except (subprocess.CalledProcessError, OSError, IndexError):
        return default
    m = re.search(r"(\d+)\.(\d+)", line)
    if not m:
        return default
    return (int(m.group(1)), int(m.group(2)))


def _compiler_major_version(compiler_path: str) -> tuple[str, int] | None:
    """Probe a compiler binary for its (family, major version).

    Runs ``<compiler> --version`` (one shot, ~10 ms) and parses gcc/clang
    output formats. Returns ``("gcc", N)`` / ``("clang", N)`` or ``None``
    if the binary isn't a recognised compiler driver. Wrapper scripts
    (coverage/sccache shims) that forward to a real gcc/clang typically
    pass-through ``--version`` and parse just like the real binary, so
    this is intentionally permissive.
    """
    import re as _re
    import subprocess

    # Tokenize so wrapper invocations like "ccache g++" forward --version
    # to the real compiler. Feeding the compound string as argv0 raises
    # OSError and silently degrades the check to "unknown driver, skip".
    argv = split_command_cached(compiler_path) if " " in compiler_path else [compiler_path]
    try:
        proc = subprocess.run(
            argv + ["--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    # gcc:   "g++ (GCC) 16.1.1 20260501 ..."  or "gcc-13 (Debian 13.2.0-...) 13.2.0"
    # clang: "clang version 22.1.4 (Fedora 22.1.4-1.fc44)"
    m = _re.search(r"clang version (\d+)", out)
    if m:
        return ("clang", int(m.group(1)))
    m = (
        _re.search(r"\(GCC\)\s+(\d+)", out)
        or _re.search(r"\bg\+\+ \(.*?\) (\d+)\.", out)
        or _re.search(r"\bgcc\b.*?(\d+)\.\d+", out)
    )
    if m:
        return ("gcc", int(m.group(1)))
    return None


def _check_resolved_compiler_available(args) -> None:
    """Fail loud when the resolved compiler binary isn't on PATH.

    The functional-compiler auto-detect (parseargs:~2454) only fires when
    ``args.CXX`` is None. A toolchain axis like ``gcc.conf`` sets
    ``CXX=g++`` explicitly, so an explicit ``--variant=gcc.*`` request on
    a system without gcc bypasses the auto-detect AND fails at the first
    compile invocation with a generic "g++: command not found" — no
    pointer at *which* variant requested g++.

    This check runs after substitutions and emits a clear error naming
    both the missing binary and the resolved variant so the user knows
    whether to switch variants or install the toolchain.
    """
    import shutil

    variant = getattr(args, "variant", "<unknown>")
    for slot in ("CC", "CXX", "LD"):
        value = getattr(args, slot, None)
        if not value or value in (_UNSUPPLIED_USE_CXX, _UNSUPPLIED_USE_CXXFLAGS):
            # The "unsupplied" sentinel means a later step substitutes a
            # real value (typically CXX itself); skip these.
            continue
        # Tokenize so wrapper invocations like "ccache g++" resolve their
        # first token (the actual executable to invoke). Feeding the full
        # compound string to shutil.which would always return None.
        tokens = split_command_cached(value) if " " in value else (value,)
        exe = tokens[0] if tokens else value
        # shutil.which handles both bare names (PATH lookup) and absolute
        # / workspace-relative paths (existence + executability check).
        if shutil.which(exe) is None:
            raise RuntimeError(
                f"Resolved {slot}={value!r} is not on PATH and is not an executable file.\n"
                f"  variant: {variant}\n"
                f"  This usually means the toolchain axis pinned by your --variant "
                f"isn't installed. Install it, or switch to a different toolchain "
                f"axis (e.g. --variant=clang,...) that resolves to a binary you have.\n"
                f"  Run `ct-config --variant={variant} -vv` to see which conf file "
                f"set {slot}."
            )


def _check_wild_linker_usable(args) -> None:
    """Fail loud when the wild linker is selected but unusable.

    Fires only when wild is the selected linker — either the LD tokens carry
    ``-fuse-ld=wild`` / ``--ld-path=wild`` (the ``wild`` axis; the clang
    rewrite in ``_normalize_wild_linker`` runs before this), or the
    ``wild-B`` axis is selected.

    Two failure modes:
      1. ``wild`` not on PATH -> raise with the install instruction.
      2. ``wild`` axis on gcc < 16 -> raise (gcc that old can't drive
         ``-fuse-ld=wild``; use clang, upgrade gcc, or the ``wild-B`` axis).
         ``wild-B`` has no version gate (working on old gcc is its purpose).
    """
    import shutil

    ldflags = getattr(args, "LDFLAGS", "") or ""
    ld_tokens = split_command_cached(ldflags)
    wild_axis = "-fuse-ld=wild" in ld_tokens or "--ld-path=wild" in ld_tokens
    wild_b_axis = _variant_has_axis(args, "wild-B")
    if not (wild_axis or wild_b_axis):
        return

    variant = getattr(args, "variant", "<unknown>")
    if shutil.which("wild") is None:
        raise RuntimeError(
            "Wild linker selected but the 'wild' binary is not on PATH.\n"
            f"  variant: {variant}\n"
            "  Install it with: cargo install --locked wild-linker\n"
            "  (the binary lands in ~/.cargo/bin; ensure that is on PATH),\n"
            "  or switch to a different linker axis (e.g. --variant=...,mold)."
        )

    # bazel's link rule recognises -fuse-ld= / --ld-path= but NOT -B as a
    # linker selector (_token_picks_linker in bazel_backend.py). With
    # wild-B and no recognised selector, bazel adds its default
    # --linkopt=-fuse-ld=gold and silently links with gold while the
    # variant claims wild-B. Fail loud instead.
    if wild_b_axis and getattr(args, "backend", None) == "bazel":
        raise RuntimeError(
            "The wild-B axis is unsupported with --backend=bazel.\n"
            f"  variant: {variant}\n"
            "  bazel's link rule does not treat -B<dir> as a linker selector,\n"
            "  so it would silently fall through to its default linker.\n"
            "  Use --variant=...,wild instead (requires clang or gcc >= 16.1),\n"
            "  or pick a different backend."
        )

    if wild_axis and not wild_b_axis:
        cxx = _effective_link_driver(args)
        # None when no link driver resolved (no LD and no CXX) or the driver
        # isn't a recognised gcc/clang — the version gate is skipped in that case.
        identity = _compiler_major_version(cxx) if cxx else None
        if identity is not None:
            family, major = identity
            if family == "gcc" and major < 16:
                raise RuntimeError(
                    f"Wild linker (-fuse-ld=wild) requires gcc >= 16.1, but "
                    f"resolved link driver {cxx!r} is gcc {major}.\n"
                    f"  variant: {variant}\n"
                    "  Use clang (--variant=clang,wild), upgrade gcc to >= 16.1, "
                    "or use the -B fallback axis (--variant=...,wild-B), which "
                    "works on any gcc."
                )


def _check_compiler_supports_requested_standard(args) -> None:
    """Fail loud when the resolved compiler's major version is too old for
    the language standard requested by the variant.

    Probes ``<args.CXX> --version`` (and ``args.CC --version`` for C
    code) and compares against _STD_MIN_COMPILER_VERSION. Skips silently
    when the compiler driver isn't a recognised gcc/clang (so msvc /
    cross-toolchains / unrecognised wrappers don't trigger spurious
    failures).
    """
    flags_to_check: list[tuple[str, str]] = []  # [(flag-slot-name, attr)]
    if getattr(args, "CXX", None):
        flags_to_check.append(("CXX", "CXXFLAGS"))
    if getattr(args, "CC", None):
        flags_to_check.append(("CC", "CFLAGS"))

    variant = getattr(args, "variant", "<unknown>")
    import re as _re

    for compiler_slot, flags_slot in flags_to_check:
        compiler = getattr(args, compiler_slot, None)
        flags_str = getattr(args, flags_slot, "")
        if not compiler or not flags_str:
            continue
        m = _re.search(r"-std=(c(?:\+\+)?\w+)", flags_str)
        if not m:
            continue
        std = m.group(1)
        # Normalise rare alt-spellings to the table keys.
        std_norm = {"c++2c": "c++26", "c++2b": "c++23", "c++2a": "c++20", "c++1z": "c++17"}.get(std, std)
        if std_norm not in _STD_MIN_COMPILER_VERSION:
            continue
        identity = _compiler_major_version(compiler)
        if identity is None:
            continue  # unknown driver; skip silently
        family, major = identity
        min_required = _STD_MIN_COMPILER_VERSION[std_norm].get(family)
        if min_required is None or major >= min_required:
            continue
        raise RuntimeError(
            f"Resolved {compiler_slot}={compiler!r} is {family} {major}, "
            f"which does not support -std={std} (requires {family} >= {min_required}).\n"
            f"  variant: {variant}\n"
            f"  Either upgrade your {family} toolchain, or compose a lower "
            f"standard axis (e.g. --variant=..,cxx20 in place of ..,{std_norm.replace('c++', 'cxx')}).\n"
            f"  Run `ct-config --variant={variant} -vv` to see which conf file "
            f"requested -std={std}."
        )


def _check_legacy_cas_config_keys(config_files) -> None:
    """Fail loud on legacy ``objdir``/``pchdir`` keys in resolved config files.

    The CAS rename (shared-objdir → cas-objdir, shared-pchdir → cas-pchdir)
    has no backward-compat alias. configargparse silently ignores unknown
    keys, so an upgrader's existing ``ct.conf`` with ``objdir = /shared/path``
    would otherwise fall back to the per-build default and quietly defeat
    shared-cache deployments. Detect and raise instead.
    """
    offenders = []
    for path in config_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for match in _LEGACY_CAS_KEY_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append((path, line_no, match.group(1)))
    if offenders:
        details = "\n".join(f"  {p}:{n}: {k}" for p, n, k in offenders)
        raise RuntimeError(
            "Legacy CAS config keys detected (renamed to cas-objdir / cas-pchdir):\n"
            f"{details}\n"
            "Edit the offending config file(s) to use 'cas-objdir' and 'cas-pchdir'. "
            "There is no backward-compat alias; leaving the old keys in place would "
            "silently fall back to the per-build default and defeat shared-cache deployments."
        )


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
        _check_legacy_cas_config_keys(config_files)
        _check_legacy_variant_config_keys(config_files)
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


@contextlib.contextmanager
def graceful_shutdown(handler, *signums) -> Generator[None, None, None]:
    """Install *handler* for *signums*, restoring the previous handlers on exit.

    The canonical place for any ct-* tool (or library helper) to wire up
    interrupt handling. Use it like::

        with apptools.graceful_shutdown(my_handler):
            do_work()

    Why a context manager rather than bare ``signal.signal()`` calls:

    * **Restoration is automatic.** Forgetting the ``signal.signal(sig,
      prev_handler)`` line leaks the entry point's handler into the
      caller for the rest of the process. The lint test in
      ``test_entry_point_surface`` enforces this for ``--help``, but the
      context manager makes the bug structurally impossible.
    * **--help / --version safety.** ``argparse``'s ``--help`` action
      raises ``SystemExit`` *before* anything inside the ``with`` block
      runs, so a user typing ``ct-X --help`` never installs the handler.
      A bare ``signal.signal()`` line above ``parse_args`` would
      contaminate the caller (caught ``ct_lock_helper`` doing exactly
      this).
    * **Thread-aware.** ``signal.signal()`` raises ``ValueError`` off
      the main thread; this helper silently no-ops there, matching the
      pattern in ``locking.atomic_compile``.
    * **Robust to weird signums.** Platform-conditional signals
      (``SIGPIPE`` on Windows, ``SIGCHLD`` reservations under uvloop)
      that fail at install time are silently skipped rather than
      crashing the caller.

    Args:
        handler: A callable matching the ``signal.signal`` contract
            (``handler(signum, frame)``). Use the sentinels
            ``signal.SIG_DFL`` / ``signal.SIG_IGN`` if you want to
            *suppress* a signal during the block rather than handle it.
        *signums: Which signals to take over. Defaults to
            ``(SIGINT, SIGTERM)`` -- the standard "user pressed Ctrl-C
            or the process manager is asking us to stop" pair.

    Yields:
        ``None``. The body of the ``with`` block runs with the new
        handlers active.

    Restored handlers come back even if the body raises. Errors during
    restoration (mismatched handler shapes, signal already gone) are
    suppressed -- the caller's original handler may already be invalid
    if the process is in shutdown, and propagating would mask the body's
    real exception.
    """
    if not signums:
        signums = (signal.SIGINT, signal.SIGTERM)

    # Dedupe while preserving order. Without this, a contrived but legal
    # ``graceful_shutdown(h, SIGINT, SIGINT)`` would record
    # ``saved=[(SIGINT, original), (SIGINT, h)]`` and the restore loop
    # would re-install ``h`` last — leaking the body's handler past the
    # with-block exit. ``dict.fromkeys`` is the standard order-preserving
    # dedupe in 3.7+.
    signums = tuple(dict.fromkeys(signums))

    saved = []  # list of (signum, previous_handler); previous_handler matches signal.Handlers

    # ``signal.signal`` raises ``ValueError`` outside the main thread.
    # Skip the install entirely there -- mirrors ``locking.atomic_compile``
    # and ``trace_backend``'s behaviour.
    if threading.current_thread() is threading.main_thread():
        for sig in signums:
            try:
                saved.append((sig, signal.signal(sig, handler)))
            except (ValueError, OSError):
                # ValueError: signum not in the platform's valid set.
                # OSError: kernel-level rejection (rare, but seen with
                # SIGCHLD under some sandbox runners).
                pass

    try:
        yield
    finally:
        for sig, prev in saved:
            # Restoration is best-effort. ``TypeError`` covers prev being
            # a non-callable sentinel that signal.signal rejects on the
            # restore call (rare, but possible on platforms that gave us
            # back an int constant on the install side); raising here
            # would mask any genuine exception bubbling out of the body.
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, prev)


def parseargs(cap, argv, verbose=None, *, context):
    """argv must be the logical equivalent of sys.argv[1:]

    Args:
        context: BuildContext for per-build state. Stored as args._context
            and used by substitution callbacks (e.g. to set up project-level
            pkg-config overrides).
    """
    # command-line values override environment variables which override config file values which override defaults.
    args = cap.parse_args(args=argv)
    args._parser = cap
    args._context = context
    # Stash the original argv so post-parse code paths (notably the second
    # resolve_variant call in _commonsubstitutions) can route through the
    # explicit_config branch when --config=path was supplied. Without it,
    # the re-resolve would try to look up the implied basename as an axis
    # and raise VariantResolutionError.
    args._argv = argv

    if "verbose" not in vars(args):
        raise ValueError(
            "verbose was not found in args. Fix is to call apptools.add_common_arguments "
            "or apptools.add_base_arguments before calling parseargs"
        )

    # Propagate --allow-fake-git into the git_utils module-level setting
    # BEFORE any downstream find_git_root() call inside substitutions /
    # anchor_root computation. The resolver
    # ``resolve_cas_directory_arguments`` ALSO calls this (so that
    # diagnostic-only tools bypassing parseargs still get the flag
    # honoured), but we propagate it here as well so any other
    # find_git_root() callsite reached before the resolver runs (inside
    # _commonsubstitutions) sees the post-parse value. set_allow_fake_git
    # clears the @functools.cache when the value actually changes, so
    # earlier strict-mode lookups don't poison subsequent permissive ones.
    compiletools.git_utils.set_allow_fake_git(getattr(args, "allow_fake_git", False))

    if verbose is None:
        verbose = args.verbose

    # TODO: if arg.variable_handling_method == "append" then fix up the environment
    # Note that configargparse uses the "override" method, so we need to partially undo that.
    # TODO: Write up a PR for configargparse to do override
    if args.variable_handling_method == "append":
        args = _fix_variable_handling_method(cap, argv, verbose)
    _flatten_variables(args)
    _strip_quotes(args)

    if verbose > 8:
        print(f"Parsing commandline arguments has occured. Before substitutions args={args}")

    # Set CXX default if not specified and a functional compiler is available
    if hasattr(args, "CXX") and args.CXX is None:
        functional_compiler = get_functional_cxx_compiler()
        if functional_compiler:
            args.CXX = functional_compiler
            if verbose >= 6:
                print(f"Set CXX to detected functional compiler: {functional_compiler}")
        else:
            raise RuntimeError("No functional C++ compiler detected. Please set CXX explicitly.")

    if verbose > 8:
        print(f"Parsing functioanl compiler has been set. Before substitutions args={args}")

    substitutions(args, verbose)

    # After substitutions canonicalise args.variant and finalise the raw
    # compile flags, validate that the resolved compiler is actually
    # usable for what the variant requested. Three checks:
    #   1. Binary on PATH? — catches "user picked --variant=gcc.* but
    #      gcc isn't installed" (would otherwise be a generic compile
    #      failure with no pointer at the variant chain).
    #   2. Wild linker usable? — catches a missing `wild` binary or the
    #      `wild` axis paired with gcc < 16 (which can't drive
    #      -fuse-ld=wild), before the link fails opaquely.
    #   3. Compiler version supports the requested -std=c++NN? — catches
    #      "user picked cxx26 on a system with gcc 11" (would otherwise
    #      be an opaque "unrecognized command line option '-std=c++26'"
    #      from the compiler).
    # All three checks emit a clear diagnostic naming the variant and
    # suggesting either a different variant or a toolchain upgrade.
    _check_resolved_compiler_available(args)
    _check_wild_linker_usable(args)
    _check_compiler_supports_requested_standard(args)

    # Populate tokenized flag lists alongside the raw strings. Consumers
    # that only need tokens (e.g. build_backend compile commands,
    # magicflags._parse) can use these directly without re-tokenizing on
    # every call. Tokens are populated AFTER all parseargs mutations
    # (env vars, INCLUDE injection, project version macros, pkg-config,
    # CPP/CXX unification) so they reflect the final raw-string state.
    #
    # WARNING: do not mutate args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}
    # after parseargs returns. args.{*}_tokens and args.flags are
    # populated once here and will silently drift from the raw strings
    # if those strings are modified later. All known mutation sites are
    # in this function (substitutions, _add_include_paths_to_flags,
    # project version macros, pkg-config, CPP/CXX unification) and run
    # BEFORE this point.
    _finalize_flag_state(args)

    if verbose > 8:
        print("parseargs has completed.  Returning args")
    return args


def _finalize_flag_state(args) -> None:
    """Populate args.{*}_tokens, args.flags, and the drift snapshot.

    Internal post-parseargs plumbing: called once at the end of
    parseargs. Tests that bypass parseargs (constructing args via
    cap.parse_args / SimpleNamespace) should go through
    ``testhelper.finalize_flag_state`` rather than touching this
    directly. After this returns, args.{*}_tokens lists and args.flags
    (a Flags dataclass) reflect the slots registered by the caller's
    CAP. Slots not registered (e.g. compilation_database omits LDFLAGS)
    are left absent from the drift snapshot so check_flag_string_drift
    can distinguish "not applicable here" from "mutated after parseargs".
    Any subsequent in-place mutation of a registered raw string will be
    flagged by check_flag_string_drift.
    """
    # Compute the CAP-registered slot set ONCE and make it sticky.
    # The materialise step below synthesizes missing slot attributes for
    # downstream consumers; a second call (e.g. via substitutions() re-run
    # from cake's two-stage parse) would then see those synthesized attrs
    # via hasattr and silently expand the registered set, defeating the
    # "not applicable here" signal. Reading from args preserves the
    # original CAP-registration view across re-runs.
    registered = getattr(args, "_registered_flag_slots", None)
    if registered is None:
        registered = tuple(slot for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS") if hasattr(args, slot))
        args._registered_flag_slots = registered
    # Materialise raw strings and *_tokens for ALL four slots so existing
    # consumers (build_backend, magicflags, ...) don't need to handle the
    # absent-slot case. Only registered slots are snapshotted for drift.
    for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
        raw = getattr(args, slot, "") or ""
        if not hasattr(args, slot):
            setattr(args, slot, raw)
        setattr(args, f"{slot}_tokens", compiletools.utils.split_command_cached(raw))
    args.flags = Flags.from_args(args)
    args._flag_string_snapshot = tuple((slot, getattr(args, slot)) for slot in registered)


def check_flag_string_drift(args) -> None:
    """Verify args.{CPPFLAGS,...} have not been mutated since parseargs end.

    parseargs records a snapshot of the raw flag strings as
    ``args._flag_string_snapshot`` immediately after populating
    ``args.{*}_tokens`` and ``args.flags``. If anything later assigns to
    ``args.CPPFLAGS`` (etc.) the tokens and flags will silently drift
    out of sync with the raw string. This function compares the current
    raw strings to the snapshot and raises ``RuntimeError`` if they
    differ, naming the offending slot.

    Call this from any consumer that wants to assert the invariant
    holds. Tests in particular should call it before reading
    ``args.flags`` if they construct ``args`` via a path that may
    mutate flag strings.
    """
    snapshot = getattr(args, "_flag_string_snapshot", None)
    if snapshot is None:
        return
    for slot, expected in snapshot:
        actual = getattr(args, slot, None)
        if actual != expected:
            raise RuntimeError(
                f"args.{slot} mutated after parseargs end: "
                f"args.flags and args.{slot}_tokens are now stale. "
                f"All mutations to compile-flag raw strings must occur "
                f"BEFORE parseargs returns."
            )


def terminalcolumns():
    """How many columns in the text terminal"""
    try:
        result = subprocess.run(["stty", "size"], capture_output=True, text=True, check=True)
        columns = int(result.stdout.split()[1])
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, IndexError, ValueError):
        columns = 80
    return columns


def verboseprintconfig(args):
    if args.verbose >= 3:
        print(" ".join(["Using variant =", args.variant]))
        args._parser.print_values()

    if args.verbose >= 2:
        verbose_print_args(args)


# Secret-carrying arg attrs: their values are replaced with a placeholder in
# `-vv` output so credentials (auth headers, etc.) don't land in CI logs.
_REDACTED_ARG_ATTRS = frozenset({"otel_headers"})
_REDACTED_PLACEHOLDER = "***REDACTED***"


def verbose_print_args(args):
    # Print the args in two columns Attr: Value
    print("\n\nFinal aggregated variables for build:")
    maxattrlen = 0
    for attr in args.__dict__:
        if len(attr) > maxattrlen:
            maxattrlen = len(attr)
    fmt = "".join(["{0:", str(maxattrlen + 1), "}: {1}"])
    rightcolbegin = maxattrlen + 3
    maxcols = terminalcolumns()
    rightcolsize = maxcols - rightcolbegin
    if maxcols <= rightcolbegin:
        print("Verbose print of args aborted due to small terminal size!")
        return

    for attr, value in sorted(args.__dict__.items()):
        if value is None:
            print(fmt.format(attr, ""))
            continue
        if attr in _REDACTED_ARG_ATTRS and value:
            print(fmt.format(attr, _REDACTED_PLACEHOLDER))
            continue
        strvalue = str(value)
        valuelen = len(strvalue)
        if rightcolbegin + valuelen < maxcols:
            print(fmt.format(attr, strvalue))
        else:
            # values are too long to fit.  Split them on spaces
            valuesplit = strvalue.split(" ", valuelen % rightcolsize)
            print(fmt.format(attr, valuesplit[0]))
            for kk in range(1, len(valuesplit)):
                print(fmt.format("", valuesplit[kk]))
