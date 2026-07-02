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
import compiletools.fetch
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
from compiletools.build_backend import (
    get_backend_class,
    known_backend_names,
    register_backend_cli_arguments,
)
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

    def _fetch_and_register_externals(self):
        """Auto-clone //#GIT= externals and register their include dirs.

        Collects the settled target source files, drives
        ``fetch.fetch_externals`` to clone/update every reachable //#GIT=
        external to a fixpoint, then appends each resolved external's root
        (and its ``include/`` subdir, when present) plus ``externals_dir`` to
        ``args.INCLUDE``.

        Returns ``True`` if anything was appended to ``args.INCLUDE`` (so the
        caller re-runs ``substitutions()`` to redistribute INCLUDE into the
        *FLAGS and re-finalize the frozen ``args.flags``), else ``False``.

        Mutating ``args.INCLUDE`` (not a frozen flag slot) and letting the
        downstream ``substitutions()`` re-run is the sanctioned way to widen
        the include path post-parseargs without tripping
        ``check_flag_string_drift``.

        ``--filelist`` is a read-only query (list the source files that WOULD
        be built) and must not have a surprising network side effect. In that
        mode the fetch step runs offline (``no_fetch``): an already-present
        external is still folded in (so the list stays complete), but a
        not-yet-cloned external fails fast with a ``fetch.FetchError`` rather
        than triggering a live ``git clone``. Run ``ct-fetch`` (or a plain
        ``ct-cake``) first to populate externals, then re-query the filelist.

        A ``fetch.FetchError`` propagates unchanged; ``main()`` renders it as a
        clean fatal error (non-zero exit) rather than a traceback.
        """
        # Single source of truth for the "reachable targets" set, shared with
        # fetch.main() so the two definitions cannot drift.
        target_files = compiletools.fetch.collect_target_files(self.args)

        if not target_files:
            return False

        gitroot = compiletools.git_utils.find_git_root()
        externals_dir = compiletools.fetch.resolve_externals_dir(getattr(self.args, "externals_dir", None), gitroot)
        overrides = compiletools.fetch.parse_git_path_overrides(getattr(self.args, "git_paths", []) or [])
        # --filelist is a read-only source-listing query: force offline so it
        # never performs a network clone as a side effect. Present externals are
        # still used; a missing one fails fast (see method docstring).
        no_fetch = getattr(self.args, "no_fetch", False) or bool(getattr(self.args, "filelist", False))
        resolved = compiletools.fetch.fetch_externals(
            target_files,
            self.args,
            self.context,
            externals_dir=externals_dir,
            overrides=overrides,
            no_fetch=no_fetch,
            update=getattr(self.args, "update", False),
            verbose=self.args.verbose,
        )

        if not resolved:
            return False

        # Build the new include-dir list from the just-fetched externals.
        # NOTE: args.INCLUDE is a whitespace-separated string by long-standing
        # convention, so this split/join cannot represent an externals path that
        # itself contains a space. That is a pre-existing INCLUDE limitation (the
        # scan layer supports spaces via raw-string extra_include_dirs, but the
        # INCLUDE round-trip here does not). Externals-dir paths with spaces are
        # unsupported; use --externals-dir to point at a space-free location.
        existing = set(self.args.INCLUDE.split())
        new_dirs = []

        def _add(directory):
            if directory and directory not in existing:
                existing.add(directory)
                new_dirs.append(directory)

        for r in resolved:
            _add(r.path)
            include_subdir = os.path.join(r.path, "include")
            # NOT cached: fetch just created this directory, so isdir() must
            # read live state -- a cached wrappedos "missing" answer from a
            # pre-fetch probe would be stale.
            if os.path.isdir(include_subdir):
                _add(include_subdir)
        _add(externals_dir)

        if not new_dirs:
            return False

        self.args.INCLUDE = (self.args.INCLUDE + " " + " ".join(new_dirs)).strip()
        if self.args.verbose > 4:
            print("Cake registered //#GIT= external include dirs: " + " ".join(new_dirs))
        return True

    def _discover_targets(self):
        """Settle the final target set and (re)create the ct helper objects.

        Runs the single-file library implied-source expansion, ``--auto``
        target discovery, and the ``//#GIT=`` external fetch, then re-runs
        ``substitutions()`` and rebuilds the ct objects whenever any of those
        steps changed the args. Mutates ``self.args`` and populates
        ``self.namer`` / ``self.headerdeps`` / ``self.magicparser`` /
        ``self.hunter``.

        The single-lib expansion runs a first pass BEFORE the fetch step (so
        the fetched externals reachable from the seed's headers are seen by the
        fetch scan), and a second pass AFTER the fetch step widened the include
        path (so implied sources that only become reachable through a
        freshly-cloned external's headers are still folded in). The two-pass
        design is what closes the earlier ordering gap where a single-source
        library never re-scanned once an external widened INCLUDE.
        """
        created_ctobjs = False
        recreateobjs = False

        # Remember the single-file library seeds. The first-pass expansion
        # below grows the list past length 1, so the ``len() == 1`` guard can
        # never re-fire; the seeds let the post-fetch second pass re-scan.
        static_lib_seed = None
        dynamic_lib_seed = None

        if self.args.static and len(self.args.static) == 1:
            static_lib_seed = self.args.static[0]
            if not created_ctobjs:
                self._createctobjs()
                created_ctobjs = True
            assert self.hunter is not None
            self.args.static.extend(self.hunter.required_source_files(static_lib_seed))
            recreateobjs = True

        if self.args.dynamic and len(self.args.dynamic) == 1:
            dynamic_lib_seed = self.args.dynamic[0]
            if not created_ctobjs:
                self._createctobjs()
                created_ctobjs = True
            assert self.hunter is not None
            self.args.dynamic.extend(self.hunter.required_source_files(dynamic_lib_seed))
            recreateobjs = True

        if self.args.auto and not any([self.args.filename, self.args.static, self.args.dynamic, self.args.tests]):
            findtargets = compiletools.findtargets.FindTargets(self.args, context=self.context)
            findtargets.process(self.args)
            recreateobjs = True

        # Auto-clone any //#GIT= externals reachable from the now-final target
        # list and register their include dirs. Must run AFTER single-lib
        # expansion and --auto discovery (so the target set is settled) but
        # BEFORE the recreateobjs re-substitution: it mutates args.INCLUDE only,
        # and the substitutions() re-run below redistributes INCLUDE into the
        # *FLAGS and re-finalizes the frozen args.flags -- the sanctioned path
        # that keeps check_flag_string_drift happy.
        externals_changed = self._fetch_and_register_externals()
        if externals_changed:
            recreateobjs = True

        if recreateobjs:
            # Since we've fiddled with the args, run the substitutions again.
            # Primarily, this fixes the --includes for the git root of the
            # targets. And recreate the ct objects.
            if self.args.verbose > 4:
                print("Cake recreating objects and reparsing for second stage processing")
            compiletools.apptools.substitutions(self.args, verbose=0)
            self._createctobjs()
            created_ctobjs = True
        elif not created_ctobjs:
            self._createctobjs()

        # Second pass: now that externals have been fetched, the include path
        # widened, and the hunter recreated over that wider path, re-scan the
        # single-file library seed(s). Implied sources that live inside a
        # freshly-cloned external were invisible to the first-pass expansion
        # (the external's headers were unresolvable then), so pick them up now.
        if externals_changed:
            self._reexpand_single_lib_seeds(static_lib_seed, dynamic_lib_seed)

    def _reexpand_single_lib_seeds(self, static_lib_seed, dynamic_lib_seed):
        """Re-scan single-file library seeds after externals widened INCLUDE.

        Merges any newly-reachable implied sources into ``args.static`` /
        ``args.dynamic``. Idempotent: sources already discovered in the
        first pass are not duplicated.
        """
        assert self.hunter is not None
        if static_lib_seed is not None:
            self._merge_new_sources(self.args.static, self.hunter.required_source_files(static_lib_seed))
        if dynamic_lib_seed is not None:
            self._merge_new_sources(self.args.dynamic, self.hunter.required_source_files(dynamic_lib_seed))

    @staticmethod
    def _merge_new_sources(target_list, discovered):
        """Append entries of *discovered* not already present in *target_list*
        (preserving order, deduping on realpath so a differently-spelled path
        for an already-listed source is not added twice)."""
        existing = {compiletools.wrappedos.realpath(p) for p in target_list}
        for src in discovered:
            real = compiletools.wrappedos.realpath(src)
            if real not in existing:
                existing.add(real)
                target_list.append(src)

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

        # Adds built-in backend flags via module-local helpers, without
        # importing the backend implementation modules (kept cold until dispatch).
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

        # //#GIT= external-fetch flags (--no-fetch / --update /
        # --externals-dir / --git-path). Single declaration point lives in
        # apptools_argparse; ct-fetch delegates to the same helper.
        compiletools.apptools.add_fetch_arguments(cap)

        cap.add_argument(
            "--ccache-statslog",
            default=None,
            nargs="?",
            const="auto",
            metavar="PATH|auto",
            help=(
                "Capture ccache per-call events for this build. "
                "Exports CCACHE_STATSLOG=<path> into the build subprocess "
                "environment; ccache writes one event-name per line to "
                "<path> for the duration of the build. With value 'auto' "
                "(or no value), the path is allocated under the "
                "per-invocation diagnostics dir as ccache.statslog and "
                "removed after post-build ingest. With an explicit path, "
                "the file's lifecycle is the caller's responsibility. "
                "When combined with --otel-export, the parsed counts are "
                "shipped as OTLP metrics (ct.ccache.events / "
                "ct.ccache.hit_rate / ct.ccache.remote_hit_rate) on the "
                "same exporter as the build spans, and the headline "
                "numbers are lifted onto the root build span as "
                "ct.ccache.* attributes. The statslog file is useful on "
                "its own (without --otel-export), so the flag is allowed "
                "in that mode -- a warning is emitted explaining no "
                "metrics will be shipped."
            ),
        )

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

        # Set up the per-build rule-outcomes log when --otel-export is on
        # so the build backends (trace_backend in-process, ct-lock-helper
        # for ninja/make) have a path to append CAS hit/miss decisions to.
        # The exporter ingests this file post-build, joins rows to
        # TimingEvents by target, and _emit_event lifts the cas.* keys
        # onto span attributes.  Allocated under the diagnostics dir so
        # it shares the build's lifecycle.  Stashed on self for the
        # post-build ingest path; cleaned up after export.
        self._rule_outcomes_log_path: str | None = None
        if getattr(self.args, "otel_export", False) and timer.enabled:
            diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(self.args)
            os.makedirs(diag_dir, exist_ok=True)
            self._rule_outcomes_log_path = os.path.join(diag_dir, "rule_outcomes.log")
            # Best-effort: drop any stale file from a prior aborted build
            # so we don't ingest decisions made under a different graph.
            try:
                os.unlink(self._rule_outcomes_log_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            os.environ["CT_RULE_OUTCOMES_LOG"] = self._rule_outcomes_log_path

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
            if self.args.prebuild_scripts:
                # A prebuild script may have just created generated headers
                # (e.g. build/version.h). Earlier phases -- target discovery
                # and the //#GIT fetch scan -- already walked the include
                # graph while those headers did not exist, caching "missing"
                # at three layers: wrappedos' global stat cache, the
                # BuildContext include-list caches, and the hunter /
                # headerdeps instance caches. Clear them so build_graph()'s
                # dep walk re-resolves against the post-script filesystem;
                # a stale miss keeps the generated header out of
                # rule.inputs (harmless for make -- the compiler still
                # finds it -- but fatal for bazel's undeclared-inclusion
                # sandbox check). Mirrors the per-round clear in
                # fetch._fixpoint_scan.
                compiletools.wrappedos.clear_cache()
                compiletools.headerdeps.clear_caches(self.context)
                self.hunter.clear_instance_cache()
                clear_headerdeps = getattr(self.hunter.headerdeps, "clear_instance_cache", None)
                if clear_headerdeps is not None:
                    clear_headerdeps()

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

    def _resolve_ccache_statslog_path(self) -> Optional[str]:
        """Resolve --ccache-statslog into an absolute filesystem path.

        ``--ccache-statslog=auto`` (or the flag passed without a value)
        allocates ``<diagnostics-dir>/ccache.statslog`` under the per-
        invocation diagnostics directory, matching where ct-cake already
        writes ``timing.json``. An explicit path is returned verbatim
        (made absolute relative to the invocation cwd if needed) and is
        treated as caller-owned -- not auto-removed after publish.

        Returns ``None`` when the flag was not set.
        """
        value = getattr(self.args, "ccache_statslog", None)
        if not value:
            return None
        if value == "auto":
            diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(self.args)
            return os.path.join(diag_dir, "ccache.statslog")
        return os.path.abspath(os.path.expanduser(value))

    def _setup_ccache_statslog_env(self) -> Optional[str]:
        """Export CCACHE_STATSLOG into this process's environment.

        Mutates ``os.environ`` rather than building per-rule env dicts
        because the build backends (trace_backend / ninja_backend /
        makefile_backend) spawn many subprocesses through different paths
        and all of them inherit the parent env -- one mutation reaches
        every compile. Returns the resolved path (for the post-build
        ingest hook) or ``None`` when the flag was not set.
        """
        path = self._resolve_ccache_statslog_path()
        if path is None:
            return None
        # Ensure the parent dir exists -- ccache silently no-ops the
        # statslog write if the dir is missing, which would manifest as
        # an empty Counter at ingest time. ``auto`` lands under the
        # diagnostics dir (already created by resolve_diagnostics_dir)
        # but an explicit user path may be brand-new.
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                print(
                    f"Warning: --ccache-statslog parent dir {parent!r} not creatable ({exc}); "
                    "ccache stats will not be collected.",
                    file=sys.stderr,
                )
                return None
        os.environ["CCACHE_STATSLOG"] = path
        return path

    def _publish_ccache_stats(self, statslog_path: str, counts, root_trace_id: Optional[str]) -> None:
        """Ship parsed ccache counts as OTLP metrics + log a one-line summary.

        Called from the ``process()`` finally block once the build (and
        ``export_buildtimer``) have run. ``counts`` is the pre-parsed
        Counter from the same statslog file (or ``None`` if pre-parse
        failed). Best-effort: any failure here is logged to stderr and
        the build still succeeds. The statslog file is removed afterwards
        iff the user picked ``auto`` mode -- explicit paths are caller-
        owned.
        """
        try:
            if counts is None:
                # Pre-parse failed earlier; fall back to a fresh parse so
                # the summary line still has a chance to land.
                from compiletools import ccache_stats

                counts = ccache_stats.parse_statslog(statslog_path)

            # Whether or not we end up shipping to OTLP, the parsed
            # numbers belong in the build log so a grep on the build
            # output answers "did ccache help?" without a metrics
            # backend round-trip.
            cacheable = (
                counts.get("direct_cache_hit", 0)
                + counts.get("preprocessed_cache_hit", 0)
                + counts.get("cache_miss", 0)
            )
            if cacheable > 0:
                hits = counts.get("direct_cache_hit", 0) + counts.get("preprocessed_cache_hit", 0)
                rate_pct = (hits / cacheable) * 100.0
                print(
                    f"ccache: cacheable={cacheable} hits={hits} misses={counts.get('cache_miss', 0)} "
                    f"hit_rate={rate_pct:.1f}%"
                )

            if not getattr(self.args, "otel_export", False):
                if getattr(self.args, "verbose", 0) >= 1:
                    print(
                        "Note: --ccache-statslog set without --otel-export; statslog written but no metrics shipped.",
                        file=sys.stderr,
                    )
                return

            if not counts:
                return

            from compiletools.otel import export_ccache_metrics

            try:
                export_ccache_metrics(counts, self.args, invocation_id=root_trace_id)
            except Exception as exc:
                print(f"Warning: OTLP ccache-metrics export failed: {exc}", file=sys.stderr)
        except Exception as exc:
            # Defensive belt: parse_statslog / import faults must never
            # propagate. Build success comes first.
            print(f"Warning: ccache stats publish failed: {exc}", file=sys.stderr)
        finally:
            if getattr(self.args, "ccache_statslog", None) == "auto":
                try:
                    os.remove(statslog_path)
                except OSError:
                    pass

    def process(self):
        """Transform the arguments into suitable versions for ct-* tools
        and call the appropriate tool.
        """
        assert self.context.timer is not None
        timer = self.context.timer
        # Snapshot CCACHE_STATSLOG BEFORE we mutate it so we can restore
        # (or pop) it on the way out. Without this, a second ct-cake run
        # in the same Python process (in-process batch mode does this)
        # would inherit a stale CCACHE_STATSLOG -- possibly pointing at
        # an already-deleted ``auto``-mode path. The restore must run
        # unconditionally, regardless of build success/failure/exception.
        _ccache_statslog_prev = os.environ.get("CCACHE_STATSLOG")
        # CCACHE_STATSLOG must be set before any compile subprocess fires.
        # Setting it here (top of process, outside the try) means the
        # finally block has a deterministic value to switch on regardless
        # of whether the build itself raised before any compile ran.
        statslog_path = self._setup_ccache_statslog_env()
        try:
            try:
                # If the user specified only a single file to be turned into a library, guess that
                # they mean for ct-cake to chase down all the implied files.
                if self.args.verbose > 4:
                    print("Early scanning. Cake determining targets and implied files")

                with timer.phase("target_discovery"):
                    self._discover_targets()

                compiletools.apptools.verboseprintconfig(self.args)

                if self.args.filelist:
                    self._callfilelist()
                else:
                    self._call_backend()
            finally:
                # apptools.validate_otel_timing_pair (called from main()) flips
                # args.timing = True when --otel-export is set without --timing,
                # and hard-exits on the explicit --otel-export --no-timing combo.
                # By the time we get here, "otel_export set and timing not set"
                # is unreachable via the front door, so no warning is needed.
                #
                # Wrap the post-build pipeline in its own try/finally so the
                # CT_RULE_OUTCOMES_LOG pop runs even if a step in the
                # pipeline (ccache parse / outcomes merge / aggregate
                # derivation / to_json / export / metric publish) raises.
                # The env var must not leak into a subsequent invocation in
                # the same process (in-process batch mode, tests, REPL).
                try:
                    # Pre-parse the ccache statslog so the headline numbers can be
                    # lifted onto the root build span via timer._root.metadata
                    # (the root-metadata loop in otel/traces.py:export_buildtimer
                    # picks them up, alongside the per-rule metadata lift in
                    # _emit_event). Doing the parse here -- before
                    # export_buildtimer -- keeps the metric export path further
                    # down and avoids re-parsing the file twice.
                    ccache_counts = None
                    if statslog_path:
                        try:
                            from compiletools import ccache_stats

                            ccache_counts = ccache_stats.parse_statslog(statslog_path)
                            if ccache_counts and timer.enabled:
                                timer._root.metadata.update(ccache_stats.summary_attributes(ccache_counts))
                        except Exception as exc:
                            print(
                                f"Warning: ccache stats pre-parse failed: {exc}",
                                file=sys.stderr,
                            )
                    root_trace_id: Optional[str] = None
                    if timer.enabled:
                        diag_dir = compiletools.diagnostics.resolve_diagnostics_dir(self.args)
                        # Ingest the per-build rule-outcomes log (CAS hit/miss per
                        # rule, written by backends during the build) and merge
                        # the cas.* metadata into the recorded TimingEvents
                        # before serialising to JSON.  Doing the merge BEFORE
                        # to_json means the on-disk timing.json carries cas.*
                        # too, so offline tooling (timing-report, ad-hoc jq
                        # queries) sees the same metadata the OTel spans do.
                        outcomes_path = getattr(self, "_rule_outcomes_log_path", None)
                        if outcomes_path:
                            from compiletools.build_timer import read_rule_outcomes

                            outcomes = read_rule_outcomes(outcomes_path)
                            timer.merge_rule_outcomes(outcomes)
                        # Derive cross-layer cache aggregates from the now-
                        # merged per-rule CAS metadata and the pre-parsed
                        # ccache event counts.  Writing the aggregates into
                        # timer._root.metadata BEFORE to_json means timing.json
                        # carries them too -- offline tooling sees what the OTel
                        # spans see -- and the root-metadata lift in
                        # otel/traces.py:export_buildtimer turns them into root
                        # span attributes with no exporter changes.  Gracefully
                        # degrades when either signal is absent (see aggregates
                        # module docstring).
                        try:
                            from compiletools.otel.aggregates import (
                                annotate_rule_cache_layers,
                                derive_build_aggregates,
                            )

                            timer._root.metadata.update(derive_build_aggregates(timer, ccache_counts))
                            # Per-rule ccache attribution is not available for
                            # ninja/make backends (ccache statslog is build-wide),
                            # so pass None and let derive_rule_cache_layer collapse
                            # CAS-misses to "other".
                            annotate_rule_cache_layers(timer, ccache_attribution=None)
                        except Exception as exc:
                            # Aggregation is best-effort -- a bug here must not
                            # take down the JSON/export path that is the actual
                            # build product.
                            print(
                                f"Warning: cache-aggregate derivation failed: {exc}",
                                file=sys.stderr,
                            )
                        timer.to_json(os.path.join(diag_dir, "timing.json"))
                        timer.print_summary()
                        if getattr(self.args, "otel_export", False):
                            from compiletools.otel import export_buildtimer

                            # README.ct-otel.rst: "a failed export does not fail the build".
                            try:
                                root_trace_id = export_buildtimer(timer, self.args)
                            except Exception as exc:
                                print(f"Warning: OTLP export failed: {exc}", file=sys.stderr)
                        # Best-effort cleanup of the outcomes log.  Leaving it in
                        # the diagnostics dir would accumulate across builds; the
                        # diagnostics dir is meant for the latest build's
                        # artefacts only.
                        if outcomes_path:
                            try:
                                os.unlink(outcomes_path)
                            except OSError:
                                pass
                    # Now publish ccache metrics on the same exporter so they
                    # share resource attrs (ct.variant / ct.backend) and the
                    # invocation_id resource attr carries the root span's
                    # trace_id for native trace<->metric joins.
                    if statslog_path:
                        self._publish_ccache_stats(statslog_path, ccache_counts, root_trace_id)
                finally:
                    # Pop CT_RULE_OUTCOMES_LOG unconditionally (belt-and-
                    # braces). Setup is gated on ``timer.enabled`` so a
                    # safely-paired teardown would also be gated -- but a
                    # caller that flips ``timer.enabled`` mid-process (or sets
                    # the env var from outside) must not leak it into the next
                    # caller in the same interpreter (tests, REPL, library use).
                    # The pop is in its own finally so any exception raised by
                    # a step in the post-build pipeline above (merge / to_json /
                    # export / _publish_ccache_stats) still leaves the env var
                    # cleaned up.
                    os.environ.pop("CT_RULE_OUTCOMES_LOG", None)
        finally:
            # Restore CCACHE_STATSLOG to its pre-invocation state. This
            # runs unconditionally -- whether the build succeeded, failed,
            # raised, or whether _publish_ccache_stats ran. Without this,
            # a second ct-cake call in the same Python process (in-process
            # batch mode) inherits a stale env var pointing at an
            # already-deleted ``auto``-mode path.
            #
            # Note: this restore-not-pop is strictly more correct than a
            # blind pop -- if the user supplied CCACHE_STATSLOG via env
            # rather than via flag, we preserve their value.
            if _ccache_statslog_prev is None:
                os.environ.pop("CCACHE_STATSLOG", None)
            else:
                os.environ["CCACHE_STATSLOG"] = _ccache_statslog_prev

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
        except compiletools.fetch.FetchError as err:
            # A //#GIT= external failed to resolve. FetchError messages already
            # name the offending external and its URL, so print to stderr
            # (consistent with the sibling fatal handlers above) rather than
            # letting it fall through to the generic stdout catch-all.
            if args.verbose < 2:
                print(f"Error: {err}", file=sys.stderr)
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
