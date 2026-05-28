import os
import shlex
import signal
import subprocess
import sys
from typing import Optional

import compiletools.apptools
import compiletools.compilation_database
import compiletools.configutils
import compiletools.diagnostics
import compiletools.filelist
import compiletools.filesystem_utils
import compiletools.findtargets
import compiletools.git_utils
import compiletools.headerdeps
import compiletools.hunter
import compiletools.jobs
import compiletools.magicflags
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_backend import get_backend_class, known_backend_names
from compiletools.build_context import BuildContext
from compiletools.version import __version__, get_package_git_sha


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

        self.namer: Optional[compiletools.namer.Namer] = None
        self.headerdeps: Optional[compiletools.headerdeps.HeaderDepsBase] = None
        self.magicparser: Optional[compiletools.magicflags.MagicFlagsBase] = None
        self.hunter: Optional[compiletools.hunter.Hunter] = None

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
        # General arguments needed by all backends
        compiletools.apptools.add_target_arguments_ex(cap)
        compiletools.apptools.add_link_arguments(cap)
        compiletools.namer.Namer.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)

        # Backend-specific arguments are registered from lightweight metadata
        # so parser construction does not import every backend module.
        from compiletools.build_backend import register_backend_cli_arguments

        register_backend_cli_arguments(cap)

        compiletools.jobs.add_arguments(cap)

        cap.add_argument(
            "--file-list",
            "--filelist",
            dest="filelist",
            action="store_true",
            help="Print list of referenced files.",
        )
        compiletools.filelist.Filelist.add_arguments(cap)  # To get the style arguments

        cap.add_argument(
            "--begintests",
            dest="tests",
            nargs="*",
            help="Starts a test block. The cpp files following this declaration will generate "
            "executables which are then run. Synonym for --tests",
        )
        cap.add_argument(
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

        cap.add_argument(
            "--compilation-database-output",
            dest="compilation_database_output",
            default=None,
            help="Output filename for compilation database (default: <gitroot>/compile_commands.json)",
        )

        cap.add_argument(
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

        cap.add_argument(
            "--CT_PREPROCESS",
            dest="preprocess",
            default=False,
            help="Deprecated. Synonym for preprocess",
        )

        cap.add_argument("--clean", action="store_true", help="Aggressively cleanup.")
        cap.add_argument(
            "--realclean",
            "--real-clean",
            action="store_true",
            default=False,
            help="Remove bin/ and selectively clean this build's objects from the object CAS.",
        )

        cap.add_argument(
            "--backend",
            default="make",
            choices=known_backend_names(),
            help="Build system backend to use (default: make).",
        )

        cap.add_argument(
            "-o",
            "--output",
            help="When there is only a single build product, rename it to this name.",
        )

        compiletools.utils.add_flag_argument(
            parser=cap,
            name="timing",
            dest="timing",
            default=False,
            help="Collect and report build timing information. Writes timing.json "
            "into the per-invocation diagnostics directory (see --diagnostics-dir) "
            "and prints a summary table after the build.",
        )

        compiletools.apptools.add_otel_export_arguments(cap)

        cap.add_argument(
            "--diagnostics-dir",
            default=None,
            help=(
                "Parent directory for per-invocation diagnostic artifacts "
                "(build timing JSON, slurm job logs). Each ct-cake "
                "invocation gets its own <invocation-id> subdirectory under "
                "this path so concurrent peers sharing a bindir or objdir "
                "never collide. Defaults to <bindir>/diagnostics/. "
                "Also settable via the DIAGNOSTICS_DIR environment variable "
                "or 'diagnostics-dir = <path>' in any ct.conf file. "
                "Must NOT be set to --cas-objdir, which is a content-addressable "
                "cache: diagnostic files have no eviction path there and "
                "races with peer ct-cake invocations clobber the data."
            ),
        )

        cap.add_argument(
            "--prebuild-script",
            dest="prebuild_scripts",
            action="append",
            default=[],
            help=(
                "Shell command string to run before the build graph is "
                "constructed, so generated headers and other build inputs "
                "produced by the script are visible to headerdeps. May be "
                "given multiple times on the CLI, and accumulates across "
                "ct.conf layers (bundled < system < user < project < variant < "
                "env < CLI). Each entry is executed via /bin/sh in the ct-cake "
                "invocation cwd; non-zero exit aborts the build. Note: runs "
                "AFTER --auto target discovery, so generated source files "
                "(.cpp/.c) are NOT picked up by --auto — list those targets "
                "explicitly if you need them. Skipped on --clean / --realclean."
            ),
        )
        cap.add_argument(
            "--postbuild-script",
            dest="postbuild_scripts",
            action="append",
            default=[],
            help=(
                "Shell command string to run after a successful build but "
                "before executables are copied to the top-level bindir. May "
                "be given multiple times on the CLI, and accumulates across "
                "ct.conf layers. Each entry is executed via /bin/sh in the "
                "ct-cake invocation cwd; non-zero exit aborts ct-cake with "
                "a non-zero return code. Use for emitting launcher scripts "
                "that invoke the built binary in a known environment, "
                "packaging, checksum manifests, etc. Skipped on --clean / "
                "--realclean."
            ),
        )

    def _callfilelist(self):
        assert self.hunter is not None
        filelist = compiletools.filelist.Filelist(self.args, self.hunter, style="flat")
        filelist.process()

    def _call_compilation_database(self):
        """Generate compilation database if requested"""
        if not getattr(self.args, "compilation_database", True):
            return
        if self.args.clean or getattr(self.args, "realclean", False):
            # Both clean and realclean tear down build artifacts —
            # generating compile_commands.json immediately before doing
            # so is wasted work and confuses tooling that picks it up.
            return

        assert self.namer is not None
        assert self.headerdeps is not None
        assert self.magicparser is not None
        assert self.hunter is not None
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
        assert self.namer is not None
        # If the user set --output or a custom bindir, trust their layout.
        atomic_copy = compiletools.filesystem_utils.atomic_copy
        if self.args.output:
            if self.args.verbose > 0:
                print(self.args.output)
            if self.args.filename:
                atomic_copy(
                    self.namer.executable_pathname(self.args.filename[0]),
                    self.args.output,
                )
            if self.args.static:
                atomic_copy(self.namer.staticlibrary_pathname(), self.args.output)
            if self.args.dynamic:
                atomic_copy(self.namer.dynamiclibrary_pathname(), self.args.output)
            return

        outputdir = self.namer.topbindir()

        def _publish(src: str, dest: str) -> None:
            # samefile catches the hardlink-fast-path of atomic_copy on
            # rerun (the prior publish made src and dest share an inode),
            # so no-op reruns degenerate to a stat.
            if os.path.exists(dest) and os.path.samefile(src, dest):
                return
            if self.args.verbose > 0:
                print(dest)
            atomic_copy(src, dest)

        for srcexe in self.namer.all_executable_pathnames():
            if not compiletools.utils.is_executable(srcexe):
                continue
            _publish(srcexe, os.path.join(outputdir, os.path.basename(srcexe)))

        if self.args.static:
            _publish(
                self.namer.staticlibrary_pathname(),
                os.path.join(outputdir, self.namer.staticlibrary_name()),
            )

        if self.args.dynamic:
            _publish(
                self.namer.dynamiclibrary_pathname(),
                os.path.join(outputdir, self.namer.dynamiclibrary_name()),
            )

    def _clean_topbindir(self):
        """Remove copied executables from the top-level bin directory."""
        assert self.namer is not None
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

    def _run_hook_scripts(self, scripts, phase):
        """Execute a list of shell-command-string hooks; abort on non-zero exit.

        Each script is run via /bin/sh in the ct-cake invocation cwd (captured
        once so a misbehaving earlier script that chdirs cannot relocate
        subsequent ones). stdout/stderr inherit the parent's fds so the user
        sees output live.
        """
        if not scripts:
            return
        invocation_cwd = os.getcwd()
        for script in scripts:
            if self.args.verbose >= 1:
                print(f"[{phase}] {script}")
            result = subprocess.run(script, shell=True, cwd=invocation_cwd)
            if result.returncode != 0:
                raise SystemExit(f"ct-cake {phase} script failed (exit {result.returncode}): {script}")

    def _call_backend(self):
        """Dispatch to the selected build backend."""
        assert self.namer is not None
        assert self.hunter is not None
        assert self.context.timer is not None
        timer = self.context.timer
        backend_name = getattr(self.args, "backend", "make")
        BackendClass = get_backend_class(backend_name)
        backend = BackendClass(args=self.args, hunter=self.hunter, context=self.context)

        # Pre-build scripts run before build_graph() — generated headers
        # and other build inputs must exist before headerdeps walks the
        # include graph, otherwise they are invisible to the build. Skip
        # entirely on clean/realclean (no fresh build is being produced).
        is_cleaning = self.args.realclean or self.args.clean
        if not is_cleaning:
            with timer.phase("prebuild_scripts"):
                self._run_hook_scripts(self.args.prebuild_scripts, "prebuild")

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
            # Every backend runs its test rules during execute("build") —
            # test events nest inside build_execution with category="test".
            # There is no separate test_execution phase.
            with timer.phase("build_execution"):
                backend.execute("build")

            with timer.phase("postbuild_scripts"):
                self._run_hook_scripts(self.args.postbuild_scripts, "postbuild")

            self._copyexes()

    def process(self):
        """Transform the arguments into suitable versions for ct-* tools
        and call the appropriate tool.
        """
        assert self.context.timer is not None
        timer = self.context.timer
        try:
            # If the user specified only a single file to be turned into a library, guess that
            # they mean for ct-cake to chase down all the implied files.
            if self.args.verbose > 4:
                print("Early scanning. Cake determining targets and implied files")

            with timer.phase("target_discovery"):
                created_ctobjs = False
                recreateobjs = False
                if self.args.static and len(self.args.static) == 1:
                    if not created_ctobjs:
                        self._createctobjs()
                        created_ctobjs = True
                    assert self.hunter is not None
                    self.args.static.extend(self.hunter.required_source_files(self.args.static[0]))
                    recreateobjs = True

                if self.args.dynamic and len(self.args.dynamic) == 1:
                    if not created_ctobjs:
                        self._createctobjs()
                        created_ctobjs = True
                    assert self.hunter is not None
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
                    created_ctobjs = True
                elif not created_ctobjs:
                    self._createctobjs()

            compiletools.apptools.verboseprintconfig(self.args)

            if self.args.filelist:
                self._callfilelist()
            else:
                self._call_backend()
        finally:
            # P1: apptools.validate_otel_timing_pair (called from main()) flips
            # args.timing = True when --otel-export is set without --timing,
            # and hard-exits on the explicit --otel-export --no-timing combo.
            # By the time we get here, "otel_export set and timing not set"
            # is unreachable via the front door, so no warning is needed.
            if timer.enabled:
                diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(self.args)
                timer.to_json(os.path.join(diag_dir, "timing.json"))
                timer.print_summary()
                if getattr(self.args, "otel_export", False):
                    from compiletools.otel import export_buildtimer

                    # README.ct-otel.rst: "a failed export does not fail the build".
                    try:
                        export_buildtimer(timer, self.args)
                    except Exception as exc:
                        print(f"Warning: OTLP export failed: {exc}", file=sys.stderr)

    def clear_cache(self):
        """Only useful in test scenarios where you need to reset to a pristine state"""
        assert self.namer is not None
        assert self.hunter is not None
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
            m = re.match(r"(\s+)(\S+)( must precede )(\S+)(.*)", line)
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
    compiletools.apptools.validate_otel_timing_pair(args)

    if not any([args.filename, args.static, args.dynamic, args.tests, args.auto]):
        print("Nothing for cake to do.  Did you mean cake --auto? Use cake --help for help.")
        return 0

    with compiletools.apptools.graceful_shutdown(signal_handler, signal.SIGINT, signal.SIGPIPE):
        try:
            cake = Cake(args, context=context)
            cake.process()
            # For testing purposes, clear out the memcaches for the times when main is called more than once.
            cake.clear_cache()
        except subprocess.CalledProcessError as cpe:
            if args.verbose < 2:
                cmd = cpe.cmd
                if isinstance(cmd, (list, tuple)):
                    cmd_str = shlex.join(cmd)
                else:
                    cmd_str = str(cmd)
                print(f"Command failed (exit {cpe.returncode}): {cmd_str}", file=sys.stderr)
                if cpe.stderr:
                    stderr = cpe.stderr.decode() if isinstance(cpe.stderr, bytes) else cpe.stderr
                    print(stderr, file=sys.stderr)
                elif cpe.output:
                    output = cpe.output.decode() if isinstance(cpe.output, bytes) else cpe.output
                    print(output, file=sys.stderr)
                return 1
            else:
                raise
        except OSError as ioe:
            if args.verbose < 2:
                if ioe.filename:
                    print(f"Error processing {ioe.filename}: {ioe.strerror}", file=sys.stderr)
                else:
                    print(f"Error: {ioe.strerror or ioe}", file=sys.stderr)
                return 1
            else:
                raise
        except compiletools.utils.LDFLAGSCycleError as ve:
            # Catch ONLY the cycle error so unrelated ValueErrors
            # don't get rendered through the Rich cycle-error formatter
            # (which would confuse the user with a panel that doesn't apply).
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
