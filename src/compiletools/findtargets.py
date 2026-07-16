import os
import sys

import compiletools.apptools
import compiletools.file_analyzer
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.file_analyzer import MarkerType


def add_arguments(cap):
    """Add the command line arguments that findtargets requires.

    Safe to call more than once on the same parser.
    """
    if compiletools.apptools._parser_has_option(cap, "--exemarkers"):
        return
    compiletools.namer.Namer.add_arguments(cap)
    compiletools.file_analyzer.add_arguments(cap)
    cap.add_argument(
        "--exemarkers",
        action="append",
        help='String that identifies a file as being an executable source.  e.g., "main ("',
    )
    cap.add_argument(
        "--testmarkers",
        action="append",
        help='String that identifies a file as being an test source.  e.g., "unit_test.hpp"',
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="auto",
        default=True,
        help="Search the filesystem from the current working directory to find all the "
        "C/C++ files with main functions and unit tests",
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="disable-tests",
        default=False,
        dest="disable_tests",
        help="When --auto is specified, add --disable-tests to stop automatic building and running of tests",
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="disable-exes",
        default=False,
        dest="disable_exes",
        help="When --auto is specified, add --disable-exes to stop automatic building of exes. "
        "(Useful for automatically building tests)",
    )

    # Style choices come from the explicit registry below.
    cap.add_argument("--style", choices=list(_STYLE_REGISTRY), default="indent", help="Output formatting style")

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="filenametestmatch",
        default=True,
        help="Identify tests based on filename in addition to testmarkers",
    )


class NullStyle:
    def __call__(self, executabletargets, testtargets):
        print(executabletargets)
        print(testtargets)


class FlatStyle:
    def __call__(self, executabletargets, testtargets):
        print(" ".join(executabletargets + testtargets))


class IndentStyle:
    def __call__(self, executabletargets, testtargets):
        print("Executable Targets:")
        if executabletargets:
            for target in executabletargets:
                print(f"\t{target}")
        else:
            print("\tNone found")

        print("Test Targets:")
        if testtargets:
            for target in testtargets:
                print(f"\t{target}")
        else:
            print("\tNone found")


class ArgsStyle:
    def __call__(self, executabletargets, testtargets):
        if executabletargets:
            for target in executabletargets:
                sys.stdout.write(f" {target}")

        if testtargets:
            sys.stdout.write(" --tests")
            for target in testtargets:
                sys.stdout.write(f" {target}")


_STYLE_REGISTRY = {
    "null": NullStyle,
    "flat": FlatStyle,
    "indent": IndentStyle,
    "args": ArgsStyle,
}


class FindTargets:
    """Search the filesystem from the current working directory to find
    all the C/C++ files with main functions and unit tests.
    """

    def __init__(self, args, argv=None, variant=None, exedir=None, *, context):
        self._args = args
        self.context = context
        # Set analyzer args for file_analyzer caching
        from compiletools.file_analyzer import set_analyzer_args

        set_analyzer_args(args, context)
        self.namer = compiletools.namer.Namer(self._args, argv=argv, variant=variant, exedir=exedir, context=context)

    def process(self, args, path=None):
        """Put the output of __call__ into the args"""
        executabletargets, testtargets = self(path)
        args.filename += executabletargets
        if testtargets:
            if not args.tests:
                args.tests = []
            args.tests += testtargets

        if args.verbose >= 2:
            styleobj = IndentStyle()
            styleobj(executabletargets, testtargets)

    def __call__(self, path=None):
        """Do the file system search and
        return the tuple ([executabletargets], [testtargets])
        """
        if self._args.exemarkers is None:
            variant = getattr(self._args, "variant", "unknown")
            config_file = getattr(self._args, "config", None)

            print("Error: No exemarkers configured.", file=sys.stderr)
            print(f"  Variant: {variant}", file=sys.stderr)
            if config_file:
                print(f"  Config file: {config_file}", file=sys.stderr)
            print(f"  exemarkers value: {self._args.exemarkers}", file=sys.stderr)
            print("", file=sys.stderr)
            print("This is unexpected and hints at other issues. Potential solutions:", file=sys.stderr)
            print(f"  1. Configure exemarkers in your {variant}.conf file", file=sys.stderr)
            print("  2. Specify exemarkers on command line: --exemarkers='main('", file=sys.stderr)
            sys.exit(1)

        if path is None:
            path = "."
        executabletargets = []
        testtargets = []

        # Use the global hash registry instead of os.walk to avoid
        # traversing large non-source files (e.g. core dumps).
        # Fall back to os.walk for non-git directories.
        from compiletools.global_hash_registry import get_file_hash, get_tracked_files

        tracked = get_tracked_files(self.context)

        prefix = compiletools.wrappedos.realpath(path)
        if not prefix.endswith(os.sep):
            prefix += os.sep

        if tracked:
            source_files = (
                (fp, h) for fp, h in tracked.items() if fp.startswith(prefix) and compiletools.utils.is_source(fp)
            )
        else:
            # Non-git directory: fall back to os.walk
            bindir = self.namer.topbindir()

            def _walk_source_files():
                for root, _dirs, files in os.walk(path):
                    if bindir in root or self._args.cas_objdir in root:
                        continue
                    for fname in files:
                        pathname = compiletools.wrappedos.realpath(os.path.join(root, fname))
                        if compiletools.utils.is_source(pathname):
                            try:
                                yield pathname, get_file_hash(pathname, self.context)
                            except FileNotFoundError:
                                continue

            source_files = _walk_source_files()

        for filepath, content_hash in source_files:
            try:
                result = compiletools.file_analyzer.analyze_file(content_hash, self.context)

                filename = os.path.basename(filepath)

                # Apply filename-based test detection first
                # A file starting with "test" is a test even if it has exemarkers
                if filename.startswith("test") and self._args.filenametestmatch:
                    if result.marker_type in (MarkerType.EXE, MarkerType.TEST):
                        testtargets.append(filepath)
                        if self._args.verbose >= 3:
                            print("Found a test: " + filepath)
                        continue

                # Check marker type from file_analyzer
                if result.marker_type == MarkerType.EXE:
                    executabletargets.append(filepath)
                    if self._args.verbose >= 3:
                        print("Found an executable source: " + filepath)
                elif result.marker_type == MarkerType.TEST:
                    testtargets.append(filepath)
                    if self._args.verbose >= 3:
                        print("Found a test: " + filepath)

            except (OSError, FileNotFoundError):
                continue

        if self._args.disable_tests:
            testtargets = []
        if self._args.disable_exes:
            executabletargets = []
        return executabletargets, testtargets


# Defensive bound mirroring apptools._MAX_TARGET_CONF_ROUNDS: each re-anchor
# strictly widens the parser's config-file set, which is bounded by the conf
# files on the discovered targets' ancestor chains, so a correct run
# converges in one or two rounds.
_MAX_DISCOVERY_REANCHOR_ROUNDS = 10


def discover_targets_and_reanchor(args, context):
    """Run ``--auto`` target discovery and config re-anchoring to a fixpoint.

    Discovery classifies files with the exemarkers/testmarkers in force at
    call time, but the discovered targets may pull in subproject conf layers
    that CHANGE those markers (or set ``disable-tests``). One discovery pass
    followed by one re-anchor therefore isn't enough: the re-anchored config
    must drive a fresh discovery, repeated until the config set stops
    growing. Both ``cake.process`` and ``compilation_database.main`` use
    this driver so their ``--auto`` semantics cannot drift apart.

    Each round: discover onto *args* (``FindTargets.process`` appends via
    ``+=``; a conf-injected target that discovery also finds is deduped
    below), re-anchor. ``reanchor_config_for_discovered_targets`` returns
    ``None`` when the walk surfaced nothing new -- fixpoint, return *args*
    with the discovered targets on it. Otherwise it returns a fresh
    namespace whose target lists hold only argv/conf-level values, and the
    next round re-discovers under the new config. The analyze-file cache is
    cleared between rounds because ``marker_type`` -- computed from the OLD
    markers -- is baked into cached ``FileAnalysisResult`` objects keyed
    only by content hash; without the clear, re-discovery would replay
    round-one classifications and marker changes would be invisible.

    Terminates because each re-anchor strictly widens the parser's config
    set (bounded by the targets' ancestor conf files); exhaustion raises
    rather than returning a half-anchored namespace.
    """
    for _round in range(_MAX_DISCOVERY_REANCHOR_ROUNDS):
        FindTargets(args, context=context).process(args)
        for attr in ("filename", "tests"):
            value = getattr(args, attr, None)
            if value:
                setattr(args, attr, compiletools.utils.ordered_unique(value))
        new_args = compiletools.apptools.reanchor_config_for_discovered_targets(args)
        if new_args is None:
            return args
        context.analyze_file_cache.clear()
        args = new_args
    raise RuntimeError(
        f"--auto discovery and config re-anchoring did not converge after "
        f"{_MAX_DISCOVERY_REANCHOR_ROUNDS} rounds; every round kept widening the config file set "
        f"(discovered targets keep pulling in new subproject conf layers)"
    )


def main(argv=None):
    cap = compiletools.apptools.create_parser("Find C/C++ files with main functions and unit tests", argv=argv)
    add_arguments(cap)

    from compiletools.build_context import BuildContext

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)
    findtargets = FindTargets(args, context=context)

    styleclass = _STYLE_REGISTRY[args.style.lower()]
    styleobj = styleclass()
    executabletargets, testtargets = findtargets()
    styleobj(executabletargets, testtargets)

    return 0
