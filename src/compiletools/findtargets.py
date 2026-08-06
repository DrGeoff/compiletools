import fnmatch
import os
import sys

import compiletools.apptools
import compiletools.build_apply
import compiletools.file_analyzer
import compiletools.git_utils
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.file_analyzer import MarkerType


def add_discovery_arguments(cap):
    """Add the ``--auto`` discovery arguments.

    This is the surface every ``--auto`` consumer registers (ct-cake,
    ct-compilation-database, ct-filelist). ``--style`` is deliberately NOT
    here: it formats ct-findtargets' own output and its choices conflict
    with ct-filelist's. ``add_arguments`` layers it on for the tools that
    want it.

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

    cap.add_argument(
        "--auto-exclude",
        dest="auto_exclude",
        action="append",
        help="Glob excluding files from --auto discovery. A pattern with a path separator "
        "matches the gitroot-relative path (a leading / anchors there); a directory name "
        "excludes its whole subtree. A pattern without a separator matches any single "
        "component of the gitroot-relative path, whole. Redundant path syntax is normalised "
        "away first, so vendor/ means vendor, ./vendor and src//vendor mean what their "
        "plain forms mean, and //vendor anchors like /vendor. Neither reaches above the gitroot; "
        "an absolute pattern matches the absolute path only when it reaches into the tree, so "
        "/tmp excludes the project's own tmp directory, never a checkout that sits under /tmp. "
        "Explicitly named targets are never "
        'filtered. e.g., "vendor", "test_*.cpp", "src/legacy", "/src/legacy/*"',
    )
    compiletools.apptools._add_xxpend_argument(
        cap,
        "auto-exclude",
        extrahelp=(
            "Merged into the --auto-exclude pattern list. Use the "
            "append-AUTO-EXCLUDE / prepend-AUTO-EXCLUDE form in conf files "
            "(uppercase -- the lowercase append-auto-exclude spelling is "
            "silently ignored) so a subproject ADDS exclusions instead of "
            "last-writer-wins clobbering the whole list. A command-line "
            "--auto-exclude appends to the conf values either way; there is "
            "no un-exclude. Order is irrelevant for an exclusion set, so "
            "prepend and append differ only in spelling."
        ),
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

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="filenametestmatch",
        default=True,
        help="Identify tests based on filename in addition to testmarkers",
    )


def add_arguments(cap):
    """Add the discovery arguments plus ct-findtargets' own ``--style``.

    Safe to call more than once on the same parser.
    """
    add_discovery_arguments(cap)
    if compiletools.apptools._parser_has_option(cap, "--style"):
        return
    add_style_argument(cap)


def add_style_argument(cap):
    """Register ct-findtargets' ``--style``, replacing any already present.

    ct-cake composes this surface with ct-filelist's, which registers a
    narrower ``--style`` of its own. Parsers built by
    ``apptools.create_parser`` resolve the conflict by replacement, so
    whoever registers last decides the composed choices; calling this
    explicitly is how a composing tool says which of the two it means.
    """
    # Style choices come from the explicit registry below.
    cap.add_argument("--style", choices=list(_STYLE_REGISTRY), default="indent", help="Output formatting style")


def _commits_to_a_path_inside(pattern, prefix):
    """True when *pattern*'s literal head lands inside *prefix*.

    The literal head is everything before the pattern's first ``*``, ``?``
    or ``[``: the part that names a real path rather than a shape. Gates
    the absolute candidate in ``is_auto_excluded``, because reaching INTO
    the tree is the only reason that candidate exists (the
    ``${CONF_DIR}/legacy`` expansion). A pattern whose head lands anywhere
    else can only ever match ancestors of the anchor, and since ``--auto``
    never walks outside the project that has exactly one meaning --
    exclude everything -- which is the silent-whole-tree outcome this rule
    exists to prevent, not a behaviour to preserve.
    """
    cut = min((pattern.find(metachar) for metachar in "*?[" if metachar in pattern), default=len(pattern))
    return bool(prefix) and pattern[:cut].startswith(prefix)


def is_auto_excluded(filepath, patterns, anchor_root=""):
    """True when *filepath* is excluded from ``--auto`` discovery.

    A pattern containing a path separator is matched against the
    *anchor_root*-relative path and that path rooted at ``/`` (the
    gitignore spelling, so ``/src/legacy`` is the anchored form it looks
    like). Each is tried directly and with ``/*`` appended, so a directory
    name excludes its whole subtree without one. Both halves are
    ``fnmatch``, whose ``*`` spans separators, so ``*/src/legacy``
    excludes that subtree at any depth and ``src/legacy`` never reaches
    ``src/legacyish``.

    The absolute path joins the candidates only for a pattern whose
    literal head reaches INTO the tree (the ``${CONF_DIR}/legacy``
    spelling a conf file expands to) or for a file with no relative
    spelling -- see ``_commits_to_a_path_inside``. Offering it more widely
    would let a pattern reach above the anchor: ``*/tmp/*`` would exclude
    every file in a checkout that merely sits under ``/tmp``, discovering
    nothing, over a path component the project never chose. Neither a
    leading ``/`` nor a glob-free first component is enough, since
    ``/*/tmp/*`` and ``/tmp/*`` have that same reach -- and ``/tmp`` is
    the likelier spelling, being what a gitignore-trained user with a
    project-level ``tmp`` writes.

    Such a pattern still resolves, as the gitroot-anchored form it looks
    like: at a checkout under ``/tmp``, ``/tmp/*`` excludes the project's
    own ``tmp`` directory and nothing else, which is what the user meant.

    Redundant path syntax is normalised away first, so which branch a
    pattern takes is decided by what it means rather than by how it was
    spelled: ``vendor/`` is the gitignore spelling of ``vendor`` and keeps
    the any-depth component reading, and ``./vendor`` / ``src//vendor`` /
    ``src/./vendor`` mean what their plain forms mean instead of matching
    nothing. A leading ``/`` survives normalisation, so the anchored
    spelling stays anchored, and any number of leading slashes reads as
    that one anchored spelling -- globs included, so ``//*`` excludes
    everything exactly as the ``/*`` it is a spelling of already did.

    A pattern without a separator is fnmatched against each component of
    the *anchor_root*-relative path -- so ``vendor`` excludes every file
    under any ``vendor`` directory (never ``vendorlib``: components match
    whole) and ``test_*.cpp`` excludes by basename. Deliberately only the
    relative components: scanning an absolute path would let a pattern
    match ancestor directories the project never chose (a ``tmp`` or a
    username component above the gitroot). A file whose realpath escapes
    *anchor_root* -- reachable via an in-tree symlink -- therefore offers
    only its basename to the separator-free patterns.
    """
    if not patterns:
        return False
    absolute = compiletools.wrappedos.realpath(filepath)
    relative = None
    prefix = ""
    if anchor_root:
        # An anchor of "/" already ends in the separator; concatenating one
        # would test for "//" and leave every path looking un-anchored.
        anchor = compiletools.wrappedos.realpath(anchor_root)
        prefix = anchor if anchor.endswith(os.sep) else anchor + os.sep
        if absolute.startswith(prefix):
            relative = absolute[len(prefix) :]
    components = (relative or compiletools.wrappedos.basename(absolute)).split(os.sep)
    for pattern in patterns:
        if os.sep in pattern:
            # Normalise before deciding which branch the pattern takes, so
            # redundant syntax cannot change its meaning: "vendor/" is the
            # gitignore spelling of "vendor" and must keep the any-depth
            # component reading rather than being anchored at the gitroot,
            # and "./vendor" / "src//vendor" must not land in the anchored
            # branch, whose candidates can never match them. The separator
            # test is re-run because normalising can remove the separator
            # that sent the pattern here.
            pattern = compiletools.wrappedos.normpath(pattern)
            # normpath keeps EXACTLY two leading separators (POSIX leaves
            # "//path" implementation-defined) and collapses three or more, so
            # a doubled leading separator is the one redundant spelling it
            # hands back unchanged. Left alone, "//vendor" would reach the
            # anchored branch, whose candidates start with a single separator,
            # and match nothing. Literal "//" rather than os.sep * 2, unlike
            # every other separator test here: on NT a doubled separator is a
            # UNC prefix that must survive, and normpath has already rewritten
            # separators to backslashes there, so the literal is inert off
            # POSIX where the os.sep form would corrupt \\server\share.
            if pattern.startswith("//"):
                pattern = pattern[1:]
        if os.sep in pattern:
            if relative is None:
                candidates = [absolute]
            elif _commits_to_a_path_inside(pattern, prefix):
                candidates = [absolute, relative, os.sep + relative]
            else:
                candidates = [relative, os.sep + relative]
            subtree = pattern.rstrip(os.sep)
            if any(
                fnmatch.fnmatch(candidate, pattern) or (subtree and fnmatch.fnmatch(candidate, subtree + os.sep + "*"))
                for candidate in candidates
            ):
                return True
        elif any(fnmatch.fnmatch(component, pattern) for component in components):
            return True
    return False


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
            print(file=sys.stderr)
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

        # Excluded files are dropped before analyze_file, so an excluded
        # subtree costs one fnmatch per path and nothing else. The patterns
        # are read here rather than in __init__ because the re-anchor
        # driver re-discovers under a namespace whose auto_exclude may have
        # grown a subproject conf's values since the last round.
        exclude = tuple(self._args.auto_exclude or ())
        anchor_root = compiletools.git_utils.find_git_root() if exclude else ""

        def _included(filepath):
            if not is_auto_excluded(filepath, exclude, anchor_root):
                return True
            if self._args.verbose >= 3:
                print("Excluded from discovery by --auto-exclude: " + filepath)
            return False

        if tracked:
            source_files = (
                (fp, h)
                for fp, h in tracked.items()
                if fp.startswith(prefix) and compiletools.utils.is_source(fp) and _included(fp)
            )
        else:
            # Non-git directory: fall back to os.walk
            bindir = self.namer.topbindir()
            cas_objdir = compiletools.build_apply.get_build_state(self._args).names.cas_objdir

            def _walk_source_files():
                for root, _dirs, files in os.walk(path):
                    if bindir in root or cas_objdir in root:
                        continue
                    for fname in files:
                        pathname = compiletools.wrappedos.realpath(os.path.join(root, fname))
                        if compiletools.utils.is_source(pathname) and _included(pathname):
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


# Shared runaway backstop: each re-anchor strictly widens the parser's
# config-file set, which is bounded by the conf files on the discovered
# targets' ancestor chains.
_MAX_DISCOVERY_REANCHOR_ROUNDS = compiletools.apptools._MAX_TARGET_CONF_ROUNDS


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

    ``--auto-exclude`` needs no retro-filtering of earlier rounds for the
    same reason: an exclusion that only becomes visible once a discovered
    target anchors its subproject conf arrives with a widened config set,
    so the round that reads it starts from the fresh namespace and
    re-discovers everything under the new exclusion. The residue is that
    the excluded target's conf layer stays loaded (the config set is
    monotone -- that monotonicity is what bounds this loop), so such a run
    agrees with the equivalent CLI ``--auto-exclude`` on TARGETS while
    still carrying the layer's flags.

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
    raise compiletools.apptools._fixpoint_not_converged_error(
        "--auto discovery and config re-anchoring",
        "every round kept widening the config file set (discovered targets keep pulling in new subproject conf layers)",
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
