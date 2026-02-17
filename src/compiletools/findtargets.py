import argparse
import os
import sys

import compiletools.apptools
import compiletools.configutils
import compiletools.file_analyzer
import compiletools.namer
import compiletools.utils
from compiletools.file_analyzer import MarkerType


def add_arguments(cap):
    """Add the command line arguments that the HeaderDeps classes require"""
    compiletools.namer.Namer.add_arguments(cap)

    # Add FileAnalyzer arguments if not already added
    # (may already be added via headerdeps when called from cake)
    try:
        compiletools.file_analyzer.FileAnalyzer.add_arguments(cap)
    except argparse.ArgumentError:
        # Arguments already added (likely via headerdeps in a parent tool)
        pass
    cap.add(
        "--exemarkers",
        action="append",
        help='String that identifies a file as being an executable source.  e.g., "main ("',
    )
    cap.add(
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

    # Figure out what style classes are available and add them to the command
    # line options
    styles = [st[:-5].lower() for st in dict(globals()) if st.endswith("Style")]
    cap.add("--style", choices=styles, default="indent", help="Output formatting style")

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


class FindTargets:
    """Search the filesystem from the current working directory to find
    all the C/C++ files with main functions and unit tests.
    """

    def __init__(self, args, argv=None, variant=None, exedir=None):
        self._args = args
        # Set global analyzer args for FileAnalyzer caching
        from compiletools.file_analyzer import set_analyzer_args

        set_analyzer_args(args)
        self.namer = compiletools.namer.Namer(self._args, argv=argv, variant=variant, exedir=exedir)

    def process(self, args, path=None):
        """Put the output of __call__ into the args"""
        executabletargets, testtargets = self(path)
        args.filename += executabletargets
        if testtargets:
            if not args.tests:
                args.tests = []
            args.tests += testtargets

        if args.verbose >= 2:
            styleobj = compiletools.findtargets.IndentStyle()
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

        tracked = get_tracked_files()

        prefix = os.path.realpath(path)
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
                    if bindir in root or self._args.objdir in root:
                        continue
                    for fname in files:
                        pathname = os.path.realpath(os.path.join(root, fname))
                        if compiletools.utils.is_source(pathname):
                            try:
                                yield pathname, get_file_hash(pathname)
                            except FileNotFoundError:
                                continue

            source_files = _walk_source_files()

        for filepath, content_hash in source_files:
            try:
                result = compiletools.file_analyzer.analyze_file(content_hash)

                filename = os.path.basename(filepath)

                # Apply filename-based test detection first
                # A file starting with "test" is a test even if it has exemarkers
                if filename.startswith("test") and self._args.filenametestmatch:
                    if result.marker_type in (MarkerType.EXE, MarkerType.TEST):
                        testtargets.append(filepath)
                        if self._args.verbose >= 3:
                            print("Found a test: " + filepath)
                        continue

                # Check marker type from FileAnalyzer
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


def main(argv=None):
    cap = compiletools.apptools.create_parser("Find C/C++ files with main functions and unit tests", argv=argv)
    compiletools.findtargets.add_arguments(cap)

    args = compiletools.apptools.parseargs(cap, argv)
    findtargets = FindTargets(args)

    styleclass = globals()[args.style.title() + "Style"]
    styleobj = styleclass()
    executabletargets, testtargets = findtargets()
    styleobj(executabletargets, testtargets)

    return 0
