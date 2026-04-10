import os
import shutil
import signal
import sys

import compiletools.apptools
import compiletools.compilation_database
import compiletools.configutils
import compiletools.filelist
import compiletools.findtargets
import compiletools.headerdeps
import compiletools.hunter
import compiletools.jobs
import compiletools.magicflags
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_backend import available_backends, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.version import __version__, get_package_git_sha


def _ensure_backends_registered():
    """Import all backend modules to trigger @register_backend decoration.

    Called lazily on first use (argument parsing or backend dispatch) rather
    than at module import time, to reduce startup cost for non-build paths.
    """
    import compiletools.bazel_backend
    import compiletools.cmake_backend
    import compiletools.makefile_backend
    import compiletools.ninja_backend
    import compiletools.trace_backend
    import compiletools.tup_backend  # noqa: F401


class Cake:
    def __init__(self, args, context=None):
        self.args = args
        self.context = context if context is not None else BuildContext()

        from compiletools.build_timer import BuildTimer

        timing_enabled = getattr(args, "timing", False)
        self.context.timer = BuildTimer(
            enabled=timing_enabled,
            variant=getattr(args, "variant", ""),
            backend=getattr(args, "backend", "make"),
        )

        self.namer = None
        self.headerdeps = None
        self.magicparser = None
        self.hunter = None

    @staticmethod
    def _hide_makefilename(args):
        """Change the args.makefilename to hide the Makefile in the executable_dir()
        This is a callback function for the compiletools.apptools.substitutions.
        Only applies when using the make backend.
        """
        if getattr(args, "backend", "make") != "make":
            return
        # Namer.executable_dir() just returns args.bindir, so read it directly
        # to avoid creating a throwaway Namer and BuildContext.
        bindir = args.bindir
        if bindir not in args.makefilename:
            movedmakefile = os.path.join(bindir, args.makefilename)
            if args.verbose > 4:
                print(f"Makefile location is being altered.  New location is {movedmakefile}")
            args.makefilename = movedmakefile

    @staticmethod
    def registercallback():
        """Must be called before object creation so that the args parse
        correctly
        """
        compiletools.apptools.registercallback(Cake._hide_makefilename)

    def _createctobjs(self):
        """Has to be separate because --auto fiddles with the args"""
        self.namer = compiletools.namer.Namer(self.args, context=self.context)
        self.headerdeps = compiletools.headerdeps.create(self.args, context=self.context)
        self.magicparser = compiletools.magicflags.create(self.args, self.headerdeps, context=self.context)
        self.hunter = compiletools.hunter.Hunter(self.args, self.headerdeps, self.magicparser, context=self.context)

    @staticmethod
    def add_arguments(cap):
        _ensure_backends_registered()

        # General arguments needed by all backends
        compiletools.apptools.add_target_arguments_ex(cap)
        compiletools.apptools.add_link_arguments(cap)
        compiletools.namer.Namer.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)

        # Make backend-specific arguments
        from compiletools.makefile_backend import MakefileBackend
        from compiletools.trace_backend import SlurmBackend

        MakefileBackend.add_arguments(cap)
        SlurmBackend.add_arguments(cap)

        compiletools.jobs.add_arguments(cap)

        cap.add(
            "--file-list",
            "--filelist",
            dest="filelist",
            action="store_true",
            help="Print list of referenced files.",
        )
        compiletools.filelist.Filelist.add_arguments(cap)  # To get the style arguments

        cap.add(
            "--begintests",
            dest="tests",
            nargs="*",
            help="Starts a test block. The cpp files following this declaration will generate "
            "executables which are then run. Synonym for --tests",
        )
        cap.add(
            "--endtests",
            action="store_true",
            help="Ignored. For backwards compatibility only.",
        )

        compiletools.findtargets.add_arguments(cap)

        compiletools.utils.add_flag_argument(
            parser=cap,
            name="compilation-database",
            dest="compilation_database",
            default=True,
            help="Generate compile_commands.json for clang tooling.",
        )

        cap.add(
            "--compilation-database-output",
            dest="compilation_database_output",
            default=None,
            help="Output filename for compilation database (default: <gitroot>/compile_commands.json)",
        )

        cap.add(
            "--compilation-database-relative-paths",
            dest="compilation_database_relative",
            action="store_true",
            help="Use relative paths instead of absolute paths in compilation database",
        )

        compiletools.utils.add_boolean_argument(
            parser=cap,
            name="preprocess",
            default=False,
            help="Set both --magic=cpp and --headerdeps=cpp. Defaults to false because it is slower.",
        )

        cap.add(
            "--CAKE_PREPROCESS",
            dest="preprocess",
            default=False,
            help="Deprecated. Synonym for preprocess",
        )

        cap.add("--clean", action="store_true", help="Aggressively cleanup.")
        cap.add(
            "--realclean",
            "--real-clean",
            action="store_true",
            default=False,
            help="Remove bin/ and selectively clean this build's objects from the shared objdir.",
        )

        cap.add(
            "--backend",
            default="make",
            choices=available_backends(),
            help="Build system backend to use (default: make).",
        )

        cap.add(
            "-o",
            "--output",
            help="When there is only a single build product, rename it to this name.",
        )

        compiletools.utils.add_flag_argument(
            parser=cap,
            name="timing",
            dest="timing",
            default=False,
            help="Collect and report build timing information. Writes .ct-timing.json "
            "and prints a summary table after the build.",
        )

    def _callfilelist(self):
        filelist = compiletools.filelist.Filelist(self.args, self.hunter, style="flat")
        filelist.process()

    def _call_compilation_database(self):
        """Generate compilation database if requested"""
        if not getattr(self.args, "compilation_database", True):
            return
        if self.args.clean:
            return  # Don't generate compilation database during clean

        # Reuse existing objects to avoid duplicating work
        creator = compiletools.compilation_database.CompilationDatabaseCreator(
            self.args,
            namer=self.namer,
            headerdeps=self.headerdeps,
            magicparser=self.magicparser,
            hunter=self.hunter,
            context=self.context,
        )
        creator.write_compilation_database()

    def _copyexes(self):
        # Copy the executables into the "bin" dir (as per cake)
        # Unless the user has changed the bindir (or set --output)
        # in which case assume that they know what they are doing
        if self.args.output:
            if self.args.verbose > 0:
                print(self.args.output)
            if self.args.filename:
                shutil.copy2(
                    self.namer.executable_pathname(self.args.filename[0]),
                    self.args.output,
                )
            if self.args.static:
                shutil.copy2(self.namer.staticlibrary_pathname(), self.args.output)
            if self.args.dynamic:
                shutil.copy2(self.namer.dynamiclibrary_pathname(), self.args.output)
        else:
            outputdir = self.namer.topbindir()
            filelist = self.namer.all_executable_pathnames()
            for srcexe in filelist:
                base = os.path.basename(srcexe)
                destexe = compiletools.wrappedos.realpath(os.path.join(outputdir, base))
                if compiletools.utils.is_executable(srcexe) and srcexe != destexe:
                    if self.args.verbose > 0:
                        print("".join([outputdir, base]))
                    shutil.copy2(srcexe, outputdir)

            if self.args.static:
                src = self.namer.staticlibrary_pathname()
                filename = self.namer.staticlibrary_name()
                dest = compiletools.wrappedos.realpath(os.path.join(outputdir, filename))
                if src != dest:
                    if self.args.verbose > 0:
                        print(os.path.join(outputdir, filename))
                    shutil.copy2(src, outputdir)

            if self.args.dynamic:
                src = self.namer.dynamiclibrary_pathname()
                filename = self.namer.dynamiclibrary_name()
                dest = compiletools.wrappedos.realpath(os.path.join(outputdir, filename))
                if src != dest:
                    if self.args.verbose > 0:
                        print(os.path.join(outputdir, filename))
                    shutil.copy2(src, outputdir)

    def _clean_topbindir(self):
        """Remove copied executables from the top-level bin directory."""
        if self.args.output:
            try:
                os.remove(self.args.output)
            except OSError:
                pass
        else:
            outputdir = self.namer.topbindir()
            filelist = os.listdir(outputdir)
            for ff in filelist:
                filename = os.path.join(outputdir, ff)
                try:
                    os.remove(filename)
                except OSError:
                    pass

    def _call_backend(self):
        """Dispatch to the selected build backend."""
        timer = self.context.timer
        backend_name = getattr(self.args, "backend", "make")
        BackendClass = get_backend_class(backend_name)
        backend = BackendClass(args=self.args, hunter=self.hunter, context=self.context)

        with timer.phase("build_graph"):
            graph = backend.build_graph()
        with timer.phase("generate"):
            backend.generate(graph)

        self._call_compilation_database()

        os.makedirs(self.namer.executable_dir(), exist_ok=True)

        if getattr(self.args, "realclean", False):
            backend.realclean(graph)
            self._clean_topbindir()
        elif self.args.clean:
            backend.clean()
            self._clean_topbindir()
        else:
            with timer.phase("build_execution"):
                backend.execute("build")

            if self.args.tests and "runtests" in graph.outputs:
                with timer.phase("test_execution"):
                    backend.execute("runtests")

            self._copyexes()

    def process(self):
        """Transform the arguments into suitable versions for ct-* tools
        and call the appropriate tool.
        """
        timer = self.context.timer
        try:
            # If the user specified only a single file to be turned into a library, guess that
            # they mean for ct-cake to chase down all the implied files.
            if self.args.verbose > 4:
                print("Early scanning. Cake determining targets and implied files")

            with timer.phase("target_discovery"):
                self._createctobjs()
                recreateobjs = False
                if self.args.static and len(self.args.static) == 1:
                    self.args.static.extend(self.hunter.required_source_files(self.args.static[0]))
                    recreateobjs = True

                if self.args.dynamic and len(self.args.dynamic) == 1:
                    self.args.dynamic.extend(self.hunter.required_source_files(self.args.dynamic[0]))
                    recreateobjs = True

                if self.args.auto and not any(
                    [self.args.filename, self.args.static, self.args.dynamic, self.args.tests]
                ):
                    findtargets = compiletools.findtargets.FindTargets(self.args, context=self.context)
                    findtargets.process(self.args)
                    recreateobjs = True

                if recreateobjs:
                    # Since we've fiddled with the args,
                    # run the substitutions again
                    # Primarily, this fixes the --includes for the git root of the
                    # targets. And recreate the ct objects
                    if self.args.verbose > 4:
                        print("Cake recreating objects and reparsing for second stage processing")
                    compiletools.apptools.substitutions(self.args, verbose=0)
                    self._createctobjs()

            compiletools.apptools.verboseprintconfig(self.args)

            if self.args.filelist:
                self._callfilelist()
            else:
                self._call_backend()
        finally:
            if timer.enabled:
                objdir = getattr(self.args, "objdir", ".")
                timer.to_json(os.path.join(objdir, ".ct-timing.json"))
                timer.print_summary()

    def clear_cache(self):
        """Only useful in test scenarios where you need to reset to a pristine state"""
        compiletools.wrappedos.clear_cache()
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        compiletools.configutils.clear_cache()
        self.namer.clear_cache()
        self.hunter.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()


def signal_handler(signal, frame):
    sys.exit(0)


def _print_rich_error(err: ValueError) -> None:
    """Format a ValueError with Rich styling, falling back to plain print."""
    import re

    msg = str(err)
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:
        print(msg, file=sys.stderr)
        return

    text = Text()
    for line in msg.split("\n"):
        if line.startswith("Cyclic library dependency"):
            text.append(line, style="bold red")
        elif line.strip().startswith("Cycle:"):
            label, _, path = line.partition("Cycle:")
            text.append(label + "Cycle:", style="bold")
            # Style each element: library names in cyan, arrows dim
            for part in re.split(r"( -> )", path):
                if part == " -> ":
                    text.append(part, style="dim")
                else:
                    text.append(part, style="cyan")
        elif line.strip().startswith("Root:"):
            label, _, path = line.partition("Root:")
            text.append(label + "Root:", style="bold")
            text.append(path, style="green")
        elif line.strip().startswith("Constraints"):
            text.append(line, style="bold")
        elif "must precede" in line:
            # "    X must precede Y  (from src/foo.cpp)"
            m = re.match(
                r"(\s+)(\S+)( must precede )(\S+)(.*)", line
            )
            if m:
                indent, lib_a, mid, lib_b, rest = m.groups()
                text.append(indent)
                text.append(lib_a, style="cyan")
                text.append(mid)
                text.append(lib_b, style="cyan")
                # Highlight file paths in the "(from ...)" part
                fm = re.match(r"(\s+\(from )(.*?)(\))", rest)
                if fm:
                    text.append(fm.group(1))
                    text.append(fm.group(2), style="green")
                    text.append(fm.group(3))
                else:
                    text.append(rest)
            else:
                text.append(line)
        elif line.startswith("Fix the LDFLAGS"):
            text.append(line, style="dim italic")
        else:
            text.append(line)
        text.append("\n")

    console = Console(stderr=True)
    console.print(Panel(text, border_style="red", title="Error", title_align="left"))


def main(argv=None):
    sha = get_package_git_sha()
    version_str = f"🍰 ct-cake {__version__}"
    if sha:
        version_str += f" (git {sha})"
    version_str += " 🍰"
    print(version_str)

    cap = compiletools.apptools.create_parser(
        "A convenience tool to aid migration from cake to the ct-* tools", argv=argv
    )
    Cake.add_arguments(cap)
    Cake.registercallback()

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)

    if not any([args.filename, args.static, args.dynamic, args.tests, args.auto]):
        print("Nothing for cake to do.  Did you mean cake --auto? Use cake --help for help.")
        return 0

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGPIPE, signal_handler)

    try:
        cake = Cake(args, context=context)
        cake.process()
        # For testing purposes, clear out the memcaches for the times when main is called more than once.
        cake.clear_cache()
    except OSError as ioe:
        if args.verbose < 2:
            print(f"Error processing {ioe.filename}: {ioe.strerror}")
            return 1
        else:
            raise
    except ValueError as ve:
        if args.verbose < 2:
            _print_rich_error(ve)
            return 1
        else:
            raise
    except Exception as err:
        if args.verbose < 2:
            print(err)
            return 1
        else:
            raise
    return 0
