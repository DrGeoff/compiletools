"""Abstract base class for build backends.

A BuildBackend knows how to:
1. Take a BuildGraph (backend-agnostic) and produce a native build file
   (Makefile, build.ninja, CMakeLists.txt, etc.)
2. Execute the build using the native tool (make, ninja, cmake --build, etc.)

The base class provides `build_graph()` which populates a BuildGraph from the
Hunter/Namer dependency data. This is the shared logic across all backends.

Backends may implement a static ``add_arguments(cap)`` method to register
backend-specific CLI arguments (see MakefileBackend for an example).
"""

from __future__ import annotations

import abc
import functools
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import TypeVar

import compiletools.filesystem_utils
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_graph import BuildGraph, BuildRule
from compiletools.magicflags import _HARD_ORDERINGS_KEY


def _touch(path: str) -> None:
    """Create or update the modification time of a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a"):
        os.utime(path, None)


def compute_link_signature(rule: BuildRule) -> str:
    """Hash sorted input names + command. Input names are content-addressed."""
    key = json.dumps({"inputs": sorted(rule.inputs), "command": rule.command}, sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()


def _read_link_sig(output: str) -> str | None:
    try:
        with open(output + ".ct-sig") as f:
            return f.read().strip()
    except OSError:
        return None


def _write_link_sig(output: str, sig: str) -> None:
    with open(output + ".ct-sig", "w") as f:
        f.write(sig)


def split_compound_args(args: list[str]) -> list[str]:
    """Split compound space-separated arguments (e.g. CXXFLAGS as one string).

    Uses shlex to correctly handle quoted values like -DFOO='bar baz'.
    """
    result = []
    for arg in args:
        if " " in arg:
            try:
                result.extend(shlex.split(arg))
            except ValueError:
                result.extend(arg.split())
        else:
            result.append(arg)
    return result


def extract_copts(command: list[str], *, strip_includes: bool = False) -> list[str]:
    """Extract compiler flags from a compile command.

    Strips the compiler binary, -c, source file, -o, and output file.
    When strip_includes is True, drops all -I/-isystem/-iquote flags
    (needed by Bazel which manages include paths itself).
    When False, recombines space-separated ``-I <dir>`` into ``-I<dir>``.
    """
    if not command:
        return []
    args = split_compound_args(command[1:])
    copts = []
    skip_next = False
    include_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if include_next:
            if not strip_includes:
                copts.append(f"-I{arg}")
            include_next = False
            continue
        if arg == "-c":
            continue
        if arg == "-o":
            skip_next = True
            continue
        if arg == "-I":
            include_next = True
            continue
        if strip_includes:
            if arg.startswith(("-isystem", "-iquote")):
                if arg in ("-isystem", "-iquote"):
                    skip_next = True
                continue
            if arg.startswith("-I") and len(arg) > 2:
                continue
        if not arg.startswith("-"):
            continue
        copts.append(arg)
    return copts


def extract_linkopts(command: list[str], object_files: set[str]) -> list[str]:
    """Extract linker flags from a link command.

    Strips the linker binary, -o, output executable, and object file paths.
    Object-file matching is normalised via ``os.path.normpath`` on both
    sides so that ``./obj/foo.o`` and ``obj/foo.o`` are treated as the
    same file — without this, the divergent form would leak into
    linkopts and break Bazel/CMake link rules.
    """
    if not command:
        return []
    normalised_objects = {os.path.normpath(o) for o in object_files}
    args = split_compound_args(command[1:])
    linkopts = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "-o":
            skip_next = True
            continue
        if os.path.normpath(arg) in normalised_objects:
            continue
        linkopts.append(arg)
    return linkopts


def build_obj_info(graph: BuildGraph, *, strip_includes: bool = False) -> dict[str, tuple[str, list[str], list[str]]]:
    """Build mapping from object file path to (source, headers, copts).

    Args:
        graph: The BuildGraph to extract compile rules from.
        strip_includes: When True, drop -I/-isystem/-iquote flags from copts
            (needed by Bazel which manages include paths itself).
    """
    obj_info: dict[str, tuple[str, list[str], list[str]]] = {}
    for rule in graph.rules_by_type("compile"):
        source = rule.inputs[0] if rule.inputs else ""
        # Filter out .gch files — they are build artifacts (precompiled headers),
        # not source files that backends like CMake/Bazel should list.
        headers = [h for h in rule.inputs[1:] if not h.endswith(".gch")] if len(rule.inputs) > 1 else []
        copts = extract_copts(rule.command, strip_includes=strip_includes) if rule.command else []
        obj_info[rule.output] = (source, headers, copts)
    return obj_info


def mangle_target_name(basename: str) -> str:
    """Convert a filename to a valid build-system target name."""
    return basename.replace(".", "_").replace("-", "_")


def aggregate_rule_sources(
    rule: BuildRule,
    obj_info: dict[str, tuple[str, list[str], list[str]]],
) -> tuple[list[str], list[str]]:
    """Collect source files and deduplicated copts from a rule's object inputs.

    Returns (source_and_header_files, deduplicated_copts).
    """
    srcs: list[str] = []
    all_copts: list[str] = []
    seen_copts: set[str] = set()
    for obj in rule.inputs:
        if obj in obj_info:
            source, headers, copts = obj_info[obj]
            if source:
                srcs.append(source)
            srcs.extend(headers)
            for c in copts:
                if c not in seen_copts:
                    all_copts.append(c)
                    seen_copts.add(c)
    return srcs, all_copts


class BuildBackend(abc.ABC):
    """Abstract base class for build system backends."""

    def __init__(self, args, hunter, *, context=None):
        self.args = args
        self.hunter = hunter
        if context is not None:
            self.context = context
        elif hunter is not None:
            self.context = hunter.context
        else:
            # The BuildContext-mandatory refactor (commit e352d20c) requires
            # callers to thread a BuildContext through. Silently constructing
            # a fresh one here would let the backend's caches diverge from any
            # other component's caches — the exact bug the refactor existed
            # to prevent. Force the caller to be explicit.
            raise ValueError(
                "BuildBackend requires either hunter or context. Pass context=BuildContext() if you have no hunter."
            )
        self.namer = compiletools.namer.Namer(args, context=self.context)
        self._graph: BuildGraph | None = None
        self._dynamic_sources: set[str] = set()

    @property
    def _timer(self):
        """Return the enabled BuildTimer from context, or None."""
        from compiletools.build_timer import get_timer

        return get_timer(self.context)

    @staticmethod
    @abc.abstractmethod
    def name() -> str:
        """Short identifier for this backend (e.g., 'make', 'ninja')."""

    @staticmethod
    @abc.abstractmethod
    def build_filename() -> str:
        """Default output filename (e.g., 'Makefile', 'build.ninja')."""

    @abc.abstractmethod
    def generate(self, graph: BuildGraph, output=None) -> None:
        """Write the native build file from the given BuildGraph.

        Args:
            graph: The build graph to render.
            output: A file-like object to write to. If None, writes to the
                backend's default file path.
        """

    def execute(self, target: str = "build") -> None:
        """Invoke the native build tool to execute the build.

        Handles the common template: runtests delegation, early exit when all
        outputs are current, backend-specific build, and link signature recording.
        Override this method entirely for backends with non-standard execution
        (e.g. ShakeBackend which uses its own build engine).
        """
        if target == "runtests":
            self._run_tests()
            return
        if self._graph is not None and self._all_outputs_current(self._graph):
            return
        self._execute_build(target)
        if self._graph is not None:
            self._record_link_signatures(self._graph)

    @abc.abstractmethod
    def _execute_build(self, target: str) -> None:
        """Backend-specific build invocation (subprocess call to native tool)."""

    def clean(self) -> None:
        """Remove build artifacts. Override for backend-specific cleanup."""
        exe_dir = self.namer.executable_dir()
        obj_dir = self.namer.object_dir()
        if os.path.isdir(exe_dir):
            shutil.rmtree(exe_dir)
        if obj_dir != exe_dir and os.path.isdir(obj_dir):
            shutil.rmtree(obj_dir)

    def realclean(self, graph: BuildGraph) -> None:
        """Remove bin/ entirely and selectively clean this build's objects from the shared objdir.

        Unlike clean(), which removes the entire exe_dir and obj_dir trees,
        realclean() only removes individual build products listed in the graph
        from the obj_dir.  This is important when obj_dir is a shared location
        (e.g. shared-objdir/) used by multiple sub-projects -- we must not
        destroy other sub-projects' objects.

        The exe_dir is still removed entirely since it is per-project.
        """
        exe_dir = self.namer.executable_dir()
        if os.path.isdir(exe_dir):
            shutil.rmtree(exe_dir)

        # Selectively remove only this build's products from the objdir.
        # `compile` covers both .o and PCH .gch outputs (PCH rules are emitted
        # as compile rules in build_graph()). `copy` covers backend-emitted
        # copy artifacts. .gch files in a shared pchdir cache outside obj_dir
        # are intentionally NOT cleaned: that cache is cross-variant and may
        # be in use by peer builds; use ct-trim-cache to age them out.
        # Mirrors makefile_backend._write_clean_rules realclean recipe.
        obj_dir = self.namer.object_dir()
        if obj_dir != exe_dir and os.path.isdir(obj_dir):
            for rule in graph.rules:
                if rule.rule_type in ("compile", "link", "static_library", "shared_library", "copy"):
                    target = rule.output
                    if os.path.isfile(target):
                        os.remove(target)
            # Prune empty subdirectories (bottom-up) to mirror the Makefile
            # `find -type d -empty -delete` step.
            for dirpath, dirnames, filenames in os.walk(obj_dir, topdown=False):
                if dirpath == obj_dir:
                    continue
                if not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass

    def _copy_built_executables(self, build_output_dir: str, dest_dir: str | None = None) -> None:
        """Copy built executables from a build output dir to dest_dir.

        Walks build_output_dir recursively to find executables, matching
        them by name (original or mangled) back to source files.
        Backends that produce outputs in a non-standard location (e.g.
        bazel-bin/, cmake-build/) call this after a successful build.

        If dest_dir is None, copies to namer.executable_pathname() locations.
        Otherwise, copies directly to dest_dir (e.g. topbindir).
        """
        all_sources = list(self.args.filename or []) + list(self.args.tests or [])
        source_by_basename: dict[str, str] = {}
        for source in all_sources:
            exe_basename = os.path.splitext(os.path.basename(source))[0]
            mangled = mangle_target_name(exe_basename)
            source_by_basename[exe_basename] = source
            source_by_basename[mangled] = source

        if dest_dir is not None:
            os.makedirs(dest_dir, exist_ok=True)

        for dirpath, dirs, files in os.walk(build_output_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not d.endswith(".runfiles")]
            for fname in files:
                full = os.path.join(dirpath, fname)
                if not (os.path.isfile(full) and os.access(full, os.X_OK)):
                    continue
                if fname.endswith(".cmake"):
                    continue
                if fname not in source_by_basename:
                    continue
                source = source_by_basename.pop(fname)
                exe_basename = os.path.splitext(os.path.basename(source))[0]
                mangled = mangle_target_name(exe_basename)
                source_by_basename.pop(exe_basename, None)
                source_by_basename.pop(mangled, None)
                if dest_dir is not None:
                    dest_path = os.path.join(dest_dir, exe_basename)
                else:
                    dest_path = self.namer.executable_pathname(compiletools.wrappedos.realpath(source))
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                shutil.copy2(full, dest_path)

    def build_graph(self) -> BuildGraph:
        """Populate a BuildGraph from hunter/namer data.

        This is the backend-agnostic logic shared by all backends.
        Subclasses call this, then pass the result to generate().
        """
        self.hunter.huntsource()
        graph = BuildGraph()

        all_sources = []
        if self.args.filename:
            all_sources.extend(self.args.filename)
        if self.args.tests:
            all_sources.extend(self.args.tests)

        if not all_sources and not self.args.static and not self.args.dynamic:
            return graph

        all_compile_sources = set()
        for source in all_sources:
            complete = self.hunter.required_source_files(source)
            all_compile_sources.update(complete)

        library_compile_sources = set()
        if self.args.static:
            for source in self.args.static:
                library_compile_sources.update(self.hunter.required_source_files(source))
        if self.args.dynamic:
            for source in self.args.dynamic:
                library_compile_sources.update(self.hunter.required_source_files(source))
        all_compile_sources.update(library_compile_sources)

        # Create objdir creation rule (needed by compile rules as order-only dep)
        graph.add_rule(
            BuildRule(
                output=self.args.objdir,
                inputs=[],
                command=["mkdir", "-p", self.args.objdir],
                rule_type="mkdir",
            )
        )

        # Create executable dir creation rule (needed by link rules as order-only dep)
        exe_dir = self.namer.executable_dir()
        if exe_dir != self.args.objdir:
            graph.add_rule(
                BuildRule(
                    output=exe_dir,
                    inputs=[],
                    command=["mkdir", "-p", exe_dir],
                    rule_type="mkdir",
                )
            )

        # Track which sources are used for dynamic libraries (need -fPIC)
        if self.args.dynamic:
            self._dynamic_sources = set()
            for source in self.args.dynamic:
                self._dynamic_sources.update(self.hunter.required_source_files(source))
        else:
            self._dynamic_sources = set()

        # Discover PCH headers from magic flags and create PCH compile rules.
        # When pchdir is configured, .gch files are placed in a shared
        # content-addressable cache: <pchdir>/<command_hash>/<header>.gch
        import stringzilla as sz

        pchdir = getattr(self.args, "pchdir", None)
        self._pch_gch_paths: dict[str, str] = {}  # header_abs -> gch_output
        self._pch_include_dirs: dict[str, str] = {}  # header_abs -> -I dir

        if pchdir:
            _warn_if_pchdir_not_cross_user_safe(pchdir, getattr(self.args, "verbose", 0))

        pch_headers: set[str] = set()
        for filename in all_compile_sources:
            magicflags = self.hunter.magicflags(filename)
            for pch_header in magicflags.get(sz.Str("PCH"), []):
                pch_headers.add(str(pch_header))

        pch_mkdir_dirs: set[str] = set()
        for pch_header in sorted(pch_headers):
            pch_magicflags = self.hunter.magicflags(pch_header)
            magic_cpp_flags = pch_magicflags.get(sz.Str("CPPFLAGS"), [])
            magic_cxx_flags = pch_magicflags.get(sz.Str("CXXFLAGS"), [])

            cmd_hash = _pch_command_hash(self.args, pch_header, magic_cpp_flags, magic_cxx_flags) if pchdir else None
            gch_path = _gch_path(pch_header, pchdir=pchdir, command_hash=cmd_hash)
            self._pch_gch_paths[pch_header] = gch_path
            if pchdir and cmd_hash:
                self._pch_include_dirs[pch_header] = os.path.join(pchdir, cmd_hash)
                pch_mkdir_dirs.add(os.path.join(pchdir, cmd_hash))

            pch_deps = [pch_header] + sorted(str(d) for d in self.hunter.header_dependencies(pch_header))
            pch_cmd = (
                [self.args.CXX]
                + compiletools.utils.split_command_cached(self.args.CXXFLAGS)
                + [str(f) for f in magic_cpp_flags]
                + [str(f) for f in magic_cxx_flags]
                + ["-x", "c++-header", pch_header, "-o", gch_path]
            )
            order_deps = [os.path.join(pchdir, cmd_hash)] if pchdir and cmd_hash else [self.args.objdir]
            graph.add_rule(
                BuildRule(
                    output=gch_path,
                    inputs=pch_deps,
                    command=pch_cmd,
                    rule_type="compile",
                    order_only_deps=order_deps,
                )
            )

        for pch_dir in sorted(pch_mkdir_dirs):
            graph.add_rule(
                BuildRule(
                    output=pch_dir,
                    inputs=[],
                    command=["mkdir", "-p", pch_dir],
                    rule_type="mkdir",
                )
            )

        for filename in all_compile_sources:
            rule = self._create_compile_rule(filename)
            graph.add_rule(rule)

        library_outputs = []
        if self.args.static:
            rule = self._create_static_library_rule()
            graph.add_rule(rule)
            library_outputs.append(rule.output)
        if self.args.dynamic:
            rule = self._create_shared_library_rule()
            graph.add_rule(rule)
            library_outputs.append(rule.output)

        if self.args.filename:
            for source in self.args.filename:
                rule = self._create_link_rule(source, library_outputs=library_outputs)
                graph.add_rule(rule)

        if self.args.tests:
            for source in self.args.tests:
                rule = self._create_link_rule(source, library_outputs=library_outputs)
                graph.add_rule(rule)

        build_deps = []
        if self.args.filename:
            build_deps.extend(
                self.namer.executable_pathname(compiletools.wrappedos.realpath(s)) for s in self.args.filename
            )
        test_exe_paths = []
        if self.args.tests:
            test_exe_paths = [
                self.namer.executable_pathname(compiletools.wrappedos.realpath(s)) for s in self.args.tests
            ]
            build_deps.extend(test_exe_paths)
        build_deps.extend(library_outputs)
        graph.add_rule(BuildRule(output="build", inputs=build_deps, command=None, rule_type="phony"))

        all_deps = ["build"]

        if test_exe_paths:
            # Create per-test execution rules so build files can run tests standalone
            testprefix_parts = []
            if getattr(self.args, "TESTPREFIX", ""):
                testprefix_parts = self.args.TESTPREFIX.split()

            test_result_paths = []
            for exe_path in test_exe_paths:
                result_path = exe_path + ".result"
                test_cmd = testprefix_parts + [exe_path, "&&", "touch", result_path]
                graph.add_rule(
                    BuildRule(
                        output=result_path,
                        inputs=[exe_path],
                        command=test_cmd,
                        rule_type="test",
                    )
                )
                test_result_paths.append(result_path)

            graph.add_rule(BuildRule(output="runtests", inputs=test_result_paths, command=None, rule_type="phony"))
            all_deps.append("runtests")

        graph.add_rule(BuildRule(output="all", inputs=all_deps, command=None, rule_type="phony"))

        return graph

    def _run_tests(self) -> None:
        """Run test executables built from args.tests.

        Provides a backend-agnostic way to run tests with:
        - Result-file markers: skips tests whose .result file is newer than
          the executable (incremental test execution).
        - Parallel execution: uses ThreadPoolExecutor with args.parallel workers.
        - Serialisation: when args.serialisetests is set, forces sequential execution.
        - TESTPREFIX: honours args.TESTPREFIX (e.g., valgrind) by prepending to
          the test command.
        """
        if not self.args.tests:
            return

        exe_paths = [
            self.namer.executable_pathname(compiletools.wrappedos.realpath(source)) for source in self.args.tests
        ]

        # Filter out tests whose .result marker is up-to-date
        tests_to_run = []
        for exe_path in exe_paths:
            result_file = exe_path + ".result"
            if os.path.exists(result_file) and os.path.exists(exe_path):
                if os.path.getmtime(result_file) >= os.path.getmtime(exe_path):
                    if self.args.verbose >= 2:
                        print(f"Skipping up-to-date test: {exe_path}", file=sys.stderr)
                    continue
            tests_to_run.append(exe_path)

        if not tests_to_run:
            if self.args.verbose >= 1:
                print("All tests up-to-date, nothing to run.", file=sys.stderr)
            return

        parallel = getattr(self.args, "parallel", 1)
        if getattr(self.args, "serialisetests", False):
            parallel = 1

        testprefix = getattr(self.args, "TESTPREFIX", "")

        if parallel > 1:
            self._run_tests_parallel(tests_to_run, testprefix, parallel)
        else:
            self._run_tests_sequential(tests_to_run, testprefix)

    def _run_single_test(self, exe_path: str, testprefix: str) -> tuple[str, int, str, str]:
        """Run a single test executable. Returns (exe_path, returncode, stdout, stderr)."""
        cmd = []
        if testprefix:
            cmd.extend(testprefix.split())
        cmd.append(exe_path)

        result = subprocess.run(cmd, capture_output=True, text=True)
        return exe_path, result.returncode, result.stdout, result.stderr

    def _run_tests_sequential(self, tests_to_run: list[str], testprefix: str) -> None:
        """Run tests one at a time, printing output immediately."""
        failures = []
        for exe_path in tests_to_run:
            if self.args.verbose >= 1:
                print(f"... {exe_path}")
            exe_path, rc, stdout, stderr = self._run_single_test(exe_path, testprefix)
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            if rc != 0:
                failures.append(exe_path)
            else:
                # Touch the .result file to mark success
                _touch(exe_path + ".result")

        if failures:
            raise RuntimeError(f"Test failures: {', '.join(failures)}")

    def _run_tests_parallel(self, tests_to_run: list[str], testprefix: str, parallel: int) -> None:
        """Run tests in parallel, buffering output and printing in order."""
        import concurrent.futures

        failures = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {
                executor.submit(self._run_single_test, exe_path, testprefix): exe_path for exe_path in tests_to_run
            }
            # Collect results as they complete; reorder below to match submission order
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

        # Sort by original order and print
        order = {path: i for i, path in enumerate(tests_to_run)}
        results.sort(key=lambda r: order[r[0]])
        for exe_path, rc, stdout, stderr in results:
            if self.args.verbose >= 1:
                print(f"... {exe_path}")
            if stdout:
                print(stdout, end="")
            if stderr:
                print(stderr, end="", file=sys.stderr)
            if rc != 0:
                failures.append(exe_path)
            else:
                _touch(exe_path + ".result")

        if failures:
            raise RuntimeError(f"Test failures: {', '.join(failures)}")

    def _build_file_uptodate(self, graph: BuildGraph) -> bool:
        """Check whether the generated build file is still current.

        Default implementation always returns False (always regenerate).
        Backends that write a build file can override this to skip
        unnecessary regeneration by checking args signatures and mtimes.
        """
        return False

    def _validate_umask_for_file_locking(self) -> None:
        """Log warning if umask may affect multi-user file-locking mode."""
        current_umask = os.umask(0)
        os.umask(current_umask)  # Restore immediately

        if (current_umask & 0o060) and self.args.verbose >= 1:
            print(
                f"Warning: file-locking enabled with restrictive umask {oct(current_umask)}\n"
                f"  Single-user mode: Works fine (you can always remove your own locks)\n"
                f"  Multi-user mode: Requires umask 0002 or 0007 for cross-user lock cleanup\n"
                f"  If using multi-user cache, set: umask 0002",
                file=sys.stderr,
            )

    def _setup_file_locking(self) -> None:
        """Configure file-locking infrastructure for this backend.

        Sets self._filesystem_type to the detected filesystem type when
        file_locking is enabled, or None when disabled.
        """
        if getattr(self.args, "file_locking", False):
            if not check_lock_helper_available():
                report_lock_helper_missing()
            self._filesystem_type = compiletools.filesystem_utils.get_filesystem_type(self.args.objdir)
            if self.args.verbose >= 3:
                print(f"Detected filesystem type: {self._filesystem_type}")
            self._validate_umask_for_file_locking()
        else:
            self._filesystem_type = None

    def _apply_build_only_changed(self, graph: BuildGraph) -> BuildGraph:
        """Filter graph to changed files if --build-only-changed is set.

        Always updates self._graph and returns the (possibly filtered) graph.
        """
        build_only_changed = getattr(self.args, "build_only_changed", None)
        if isinstance(build_only_changed, str):
            changed = set(build_only_changed.split())
            graph = graph.filter_to_changed(changed, verbose=self.args.verbose)
        self._graph = graph
        return graph

    def _wrap_compile_cmd(self, command: list[str]) -> str:
        """Return the command string for a compile rule, lock-wrapped if needed.

        Locates ``-o target`` in the command by index (not position) so a
        trailing token after the output path doesn't desync the wrap. When
        file_locking is enabled, the -o and target are stripped and
        ct-lock-helper wraps the remainder. Mirrors ShakeBackend's compile
        path (commit a3c67675).
        """
        try:
            o_idx = command.index("-o")
        except ValueError as e:
            raise AssertionError(f"compile rule missing -o flag: {command}") from e

        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return " ".join(command)

        compile_part = command[:o_idx] + command[o_idx + 2 :]
        target = command[o_idx + 1]

        return wrap_compile_with_lock(" ".join(compile_part), target, self.args, self._filesystem_type)

    def _wrap_link_cmd(self, command: list[str]) -> str:
        """Return the command string for a link rule, lock-wrapped if needed.

        Unlike _wrap_compile_cmd, the command is passed through as-is
        (including -o flag) since atomic_link does not manipulate output paths.
        """
        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return " ".join(command)

        # Extract target from -o flag for locking.
        # build_graph.py always emits -o in link/library commands; if absent,
        # fall back to unwrapped to avoid silently mis-targeting the lock.
        try:
            o_idx = command.index("-o")
            target = command[o_idx + 1]
        except (ValueError, IndexError):
            return " ".join(command)

        return wrap_link_with_lock(" ".join(command), target, self.args, self._filesystem_type)

    def _all_outputs_current(self, graph: BuildGraph) -> bool:
        """Pre-check: all compile outputs exist and all link sigs match?

        Returns False when the graph has no compile/link rules, since the
        graph may not capture all build steps (e.g. library builds).
        """
        has_build_rules = False
        for rule in graph.rules:
            if rule.rule_type == "compile":
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
            elif rule.rule_type in ("link", "static_library", "shared_library"):
                has_build_rules = True
                if not os.path.exists(rule.output):
                    return False
                if _read_link_sig(rule.output) != compute_link_signature(rule):
                    return False
        return has_build_rules

    def _record_link_signatures(self, graph: BuildGraph) -> None:
        for rule in graph.rules:
            if rule.rule_type in ("link", "static_library", "shared_library"):
                _write_link_sig(rule.output, compute_link_signature(rule))

    def _create_compile_rule(self, filename: str) -> BuildRule:
        """Create a compile BuildRule for a single source file."""
        deplist = self.hunter.header_dependencies(filename)
        prerequisites = [filename] + sorted([str(dep) for dep in deplist])

        # Compute include_weight for SLURM memory estimation.
        # len(quoted_headers) from FileAnalyzer correlates with peak RSS (r=0.85)
        # because each quoted include transitively pulls in framework templates.
        # analyze_file is already cached from the header dep walk -- zero cost.
        from compiletools.file_analyzer import analyze_file
        from compiletools.global_hash_registry import get_file_hash

        try:
            content_hash = get_file_hash(filename, self.context)
            analysis = analyze_file(content_hash, self.context)
            include_weight = len(analysis.quoted_headers)
        except (FileNotFoundError, OSError, RuntimeError) as e:
            print(
                f"WARNING: could not analyze {filename!r} for include_weight ({type(e).__name__}: {e}); "
                "SLURM memory estimate will be 0 for this rule.",
                file=sys.stderr,
            )
            include_weight = 0

        import stringzilla as sz

        magicflags = self.hunter.magicflags(filename)

        # Add PCH .gch dependency if this source uses a precompiled header.
        # Collect -I flags for the shared pchdir so GCC finds the cached .gch.
        pch_include_flags: list[str] = []
        for pch_header in magicflags.get(sz.Str("PCH"), []):
            pch_header_str = str(pch_header)
            gch_path = self._pch_gch_paths.get(pch_header_str, _gch_path(pch_header_str))
            if gch_path not in prerequisites:
                prerequisites.append(gch_path)
            include_dir = self._pch_include_dirs.get(pch_header_str)
            if include_dir:
                pch_include_flags.extend(["-I", include_dir])

        macro_state_hash = self.hunter.macro_state_hash(filename)
        dep_hash = self.namer.compute_dep_hash(deplist)
        obj_name = self.namer.object_pathname(filename, macro_state_hash, dep_hash)

        magic_cpp_flags = magicflags.get(sz.Str("CPPFLAGS"), [])
        if compiletools.utils.is_c_source(filename):
            magic_c_flags = magicflags.get(sz.Str("CFLAGS"), [])
            compile_cmd = (
                [self.args.CC]
                + compiletools.utils.split_command_cached(self.args.CFLAGS)
                + pch_include_flags
                + [str(flag) for flag in magic_cpp_flags]
                + [str(flag) for flag in magic_c_flags]
            )
        else:
            magic_cxx_flags = magicflags.get(sz.Str("CXXFLAGS"), [])
            compile_cmd = (
                [self.args.CXX]
                + compiletools.utils.split_command_cached(self.args.CXXFLAGS)
                + pch_include_flags
                + [str(flag) for flag in magic_cpp_flags]
                + [str(flag) for flag in magic_cxx_flags]
            )

        if self.args.dynamic and filename in self._dynamic_sources:
            compile_cmd.append("-fPIC")

        compile_cmd.extend(["-c", filename, "-o", obj_name])

        return BuildRule(
            output=obj_name,
            inputs=prerequisites,
            command=compile_cmd,
            rule_type="compile",
            order_only_deps=[self.args.objdir],
            include_weight=include_weight,
        )

    def _merge_ldflags_for_sources(self, sources: list[str]) -> list[str]:
        """Collect per-file LDFLAGS and hard orderings, then merge via topo sort.

        Consumer side of the ``_HARD_ORDERINGS_KEY`` contract. The producer
        is ``magicflags._handle_pkg_config`` (see the comment block above
        ``magicflags._HARD_ORDERINGS_KEY`` for the full producer-side
        contract).

        Per-file invariants:

        * The ``_HARD_ORDERINGS_KEY`` sentinel MUST be popped (or filtered)
          out of the per-file ``magic_flags`` dict before that dict is
          consumed elsewhere as a flat flag list — otherwise the sentinel
          leaks out as a fake compiler flag. This method reads the key
          via ``magic_flags.get(_HARD_ORDERINGS_KEY, [])``; any other
          consumer of the per-file flags dict must do the same.
        * The aggregated value forwarded to
          ``utils.merge_ldflags_with_topo_sort(hard_orderings=...)`` is a
          ``list[tuple[str, str]]`` of pairwise ``(pred_lib, succ_lib)``
          constraints. Library names appear without the ``-l`` prefix,
          matching what ``_handle_pkg_config`` produces.
        * Source-file provenance is preserved in a parallel
          ``hard_ordering_sources`` list whose indices align 1:1 with the
          flattened ``hard_orderings`` list. ``merge_ldflags_with_topo_sort``
          uses these source paths in cycle-error messages so the user
          can find the contradictory ``//#PKG-CONFIG=`` annotations.
        """
        import stringzilla as sz

        per_file_ldflags = []
        ldflags_source_files = []
        hard_orderings = []
        hard_ordering_sources = []
        for s in sources:
            magic_flags = self.hunter.magicflags(s)
            file_ldflags = magic_flags.get(sz.Str("LDFLAGS"), [])
            if file_ldflags:
                per_file_ldflags.append(list(file_ldflags))
                ldflags_source_files.append(s)
            for pred, succ in magic_flags.get(_HARD_ORDERINGS_KEY, []):
                hard_orderings.append((str(pred), str(succ)))
                hard_ordering_sources.append(s)

        return compiletools.utils.merge_ldflags_with_topo_sort(
            per_file_ldflags,
            source_files=ldflags_source_files,
            hard_orderings=hard_orderings or None,
            hard_ordering_sources=hard_ordering_sources or None,
        )

    def _create_link_rule(self, source: str, library_outputs: list[str] | None = None) -> BuildRule:
        """Create a link BuildRule for a source file (executable target)."""
        completesources = self.hunter.required_source_files(source)
        exename = self.namer.executable_pathname(compiletools.wrappedos.realpath(source))

        object_names = compiletools.utils.ordered_unique(
            [
                self.namer.object_pathname(
                    s,
                    self.hunter.macro_state_hash(s),
                    self.namer.compute_dep_hash(self.hunter.header_dependencies(s)),
                )
                for s in completesources
            ]
        )

        merged_ldflags = self._merge_ldflags_for_sources(completesources)
        link_cmd = [self.args.LD, "-o", exename] + list(object_names) + merged_ldflags

        inputs = list(object_names)
        if library_outputs:
            exe_dir = self.namer.executable_dir()
            link_cmd.append(f"-L{exe_dir}")
            for lib_output in library_outputs:
                lib_basename = os.path.basename(lib_output)
                if lib_basename.startswith("lib"):
                    lib_name = lib_basename[3:]  # strip "lib" prefix
                    lib_name = os.path.splitext(lib_name)[0]  # strip extension
                    link_cmd.append(f"-l{lib_name}")
                inputs.append(lib_output)

        if self.args.LDFLAGS:
            link_cmd.extend(compiletools.utils.split_command_cached(self.args.LDFLAGS))

        exe_dir = self.namer.executable_dir()

        return BuildRule(
            output=exename,
            inputs=inputs,
            command=link_cmd,
            rule_type="link",
            order_only_deps=[exe_dir],
        )

    def _get_library_object_names(self, sources: list[str]) -> tuple[list[str], list[str]]:
        """Get object file names and source files for library targets.

        Returns:
            (object_names, all_source_files) tuple.
        """
        # Use ordered_union instead of set() to preserve deterministic
        # ordering — required for stable link commands and CA cache hits.
        all_source_files = compiletools.utils.ordered_union(
            *(self.hunter.required_source_files(source) for source in sources)
        )

        object_names = compiletools.utils.ordered_unique(
            [
                self.namer.object_pathname(
                    s,
                    self.hunter.macro_state_hash(s),
                    self.namer.compute_dep_hash(self.hunter.header_dependencies(s)),
                )
                for s in all_source_files
            ]
        )
        return object_names, all_source_files

    def _create_static_library_rule(self) -> BuildRule:
        """Create a static library BuildRule from args.static sources."""
        object_names, _ = self._get_library_object_names(self.args.static)
        lib_path = self.namer.staticlibrary_pathname()

        lib_cmd = ["ar", "-src", lib_path] + list(object_names)

        return BuildRule(
            output=lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="static_library",
            order_only_deps=[self.namer.executable_dir()],
        )

    def _create_shared_library_rule(self) -> BuildRule:
        """Create a shared library BuildRule from args.dynamic sources."""
        object_names, all_source_files = self._get_library_object_names(self.args.dynamic)
        lib_path = self.namer.dynamiclibrary_pathname()

        merged_ldflags = self._merge_ldflags_for_sources(all_source_files)
        lib_cmd = [self.args.LD, "-shared", "-o", lib_path] + list(object_names)
        lib_cmd.extend(merged_ldflags)
        if self.args.LDFLAGS:
            lib_cmd.extend(compiletools.utils.split_command_cached(self.args.LDFLAGS))

        return BuildRule(
            output=lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="shared_library",
            order_only_deps=[self.namer.executable_dir()],
        )


def _gch_path(header: str, pchdir: str | None = None, command_hash: str | None = None) -> str:
    """Return the precompiled header output path for a header file.

    When *pchdir* and *command_hash* are provided the .gch is placed under
    ``<pchdir>/<command_hash>/<basename>.gch`` so that GCC can find it via
    ``-I <pchdir>/<command_hash>/``.  Otherwise falls back to the legacy
    ``header.gch`` path next to the header.
    """
    if pchdir and command_hash:
        return os.path.join(pchdir, command_hash, os.path.basename(header) + ".gch")
    return header + ".gch"


_PCHDIR_WARNED: set[str] = set()


def _warn_if_pchdir_not_cross_user_safe(pchdir: str, verbose: int) -> None:
    """Emit a one-time warning if pchdir's parent isn't group-writable + SGID.

    The shared PCH cache is intended to be readable across users (I-B2):
    user A creates ``<pchdir>/<cmd_hash>/stdafx.h.gch``, user B should be
    able to consume it. With a default ``umask 0077`` and no SGID on the
    parent, A's directory is mode ``0700`` and B silently re-builds the
    PCH every time. Warn early so the operator can fix the parent dir
    permissions (typically ``chmod 2775`` + ``chgrp <build-group>``).

    The warning is one-time per (pchdir) per process to avoid spam in
    multi-target builds.
    """
    if pchdir in _PCHDIR_WARNED:
        return
    _PCHDIR_WARNED.add(pchdir)

    parent = os.path.dirname(os.path.abspath(pchdir)) or "."
    target = pchdir if os.path.isdir(pchdir) else parent
    try:
        st = os.stat(target)
    except OSError:
        return  # No parent yet, mkdir will create it; nothing useful to warn about

    mode = st.st_mode
    issues = []
    # Group-write needed so user B can create new <cmd_hash>/ subdirs.
    if not (mode & 0o020):
        issues.append("not group-writable (need at least mode 2775)")
    # SGID needed so children inherit the parent's group, not the creator's
    # primary group.
    if not (mode & 0o2000):
        issues.append("missing SGID bit (chmod g+s)")

    if issues and verbose >= 1:
        joined = "; ".join(issues)
        print(
            f"WARNING: shared PCH directory {target!r} is {joined}. "
            "Cross-user PCH cache hits will silently miss. Fix with: "
            f"chmod 2775 {target!r} && chgrp <build-group> {target!r}",
            file=sys.stderr,
        )


@functools.lru_cache(maxsize=64)
def _compiler_identity(cxx: str) -> str:
    """Return a stable identity string for a compiler binary.

    Used as part of the PCH cache key (I-B1): two users on the same shared
    filesystem with different ``$PATH``s could otherwise collide on the
    same key while resolving ``args.CXX`` (e.g. bare ``g++``) to different
    binaries (different versions, different stdlibs). GCC's PCH stamp
    catches this at *consume* time — but the slow fallback compile
    defeats the cache. By including binary realpath + (st_size, st_mtime),
    we make distinct compilers produce distinct cache entries.

    Falls back to the original string when the binary cannot be stat'd
    (e.g. user passed a non-path command like ``ccache g++``).
    """
    resolved = shutil.which(cxx) or cxx
    try:
        st = os.stat(resolved)
        # Use nanosecond mtime so a sub-second compiler swap (e.g.
        # ``cp new-g++ /usr/local/bin/g++`` followed immediately by a
        # build) does not collide on the cache key.
        return f"{os.path.realpath(resolved)}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return resolved


def _pch_command_hash(
    args,
    pch_header: str,
    magic_cpp_flags: list,
    magic_cxx_flags: list,
) -> str:
    """Compute a content-addressable hash for a PCH compile command.

    The hash captures compiler identity (binary realpath + size + mtime,
    not just the user-supplied command name), all flags, and the realpath
    of the header so that different compilers / flags / headers produce
    distinct cache entries while identical configurations share a single
    .gch file. Uses ``json.dumps`` rather than space-join so flag values
    containing literal spaces (``-DFOO="a b"``) cannot collide with
    space-separated flag pairs.
    """
    # M-A11: 64 bits (16 hex chars) of SHA-256 — birthday-collision risk at
    # ~4 billion entries, fine in practice. PCH cache validity is also
    # guarded by GCC's PCH stamp at consume time, so a hash collision
    # would only cause a slow rebuild, not a miscompile.
    # M-B6: Cache key includes the immediate header's realpath but NOT the
    # transitive header content. Two users whose stdafx.h includes
    # <config.h> with different content (e.g. different worktrees of the
    # same project) collide on cmd_hash; GCC's PCH stamp then refuses the
    # wrong .gch and the user pays a slow rebuild. Acceptable today;
    # consider sidecar manifest with content hashes if cross-user-mixed-
    # content workloads become common.
    canonical = {
        "compiler_identity": _compiler_identity(args.CXX),
        "cxx_command": args.CXX,
        "CXXFLAGS": args.CXXFLAGS,
        "magic_cpp_flags": [str(f) for f in magic_cpp_flags],
        "magic_cxx_flags": [str(f) for f in magic_cxx_flags],
        "header": os.path.realpath(pch_header),
        "stage": "c++-header",
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def _native_flock_available() -> bool:
    """Check if native flock binary (util-linux) is available."""
    return shutil.which("flock") is not None


def _build_lock_env_prefix(strategy: str, args, filesystem_type: str) -> str:
    """Build the CT_LOCK_* environment variable prefix for ct-lock-helper.

    Args:
        strategy: Lock strategy (lockdir, fcntl, cifs, flock)
        args: Namespace with sleep_interval_lockdir, sleep_interval_cifs,
              sleep_interval_flock_fallback, lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Space-terminated env var prefix string, or empty string if no vars needed.
    """
    import compiletools.filesystem_utils

    env_vars = []

    if strategy == "lockdir":
        if args.sleep_interval_lockdir is not None:
            sleep_interval = args.sleep_interval_lockdir
        else:
            sleep_interval = compiletools.filesystem_utils.get_lockdir_sleep_interval(filesystem_type)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL={sleep_interval}")
    elif strategy == "fcntl":
        pass  # fcntl.lockf() blocks in kernel, no sleep interval needed
    elif strategy == "cifs":
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_CIFS={args.sleep_interval_cifs}")
    else:  # flock (fallback when native flock unavailable)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_FLOCK={args.sleep_interval_flock_fallback}")

    env_vars.append(f"CT_LOCK_WARN_INTERVAL={args.lock_warn_interval}")
    env_vars.append(f"CT_LOCK_TIMEOUT={args.lock_cross_host_timeout}")

    return " ".join(env_vars) + " " if env_vars else ""


def wrap_compile_with_lock(compile_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a compile command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per compilation.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Shared by Make and Ninja backends. When args.file_locking is False,
    returns the command with ``-o target`` appended unchanged.

    Args:
        compile_cmd: Compile command without -o flag (e.g., "gcc -c file.c")
        target: Target file (e.g., "$@" for Make, or an actual path for Ninja)
        args: Namespace with file_locking, sleep_interval_lockdir,
              sleep_interval_cifs, sleep_interval_flock_fallback,
              lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return compile_cmd + " -o " + target

    import compiletools.filesystem_utils

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup).
    # Compile to a temp file then atomically rename — same pattern and same
    # rationale as locking.atomic_compile (which the helper-mode path below
    # routes through). DO NOT 'optimize' back to a bare `flock <target>
    # gcc -o <target>` form: the flock serialises concurrent compiles of
    # the same target, but link rules read .o files WITHOUT any lock, so a
    # peer linker would mmap-read a half-written .o under `make -j N` or
    # two concurrent ct-cake invocations on the same objdir, producing
    # sporadic 'undefined reference to main' / 'undefined symbol' errors.
    # Temp+rename eliminates the race for all readers without needing
    # read-side locks. The flock keeps a deterministic ".compiletools.tmp"
    # suffix collision-free across peer writers.
    # See locking.atomic_compile() for the full DO-NOT-REVERT story.
    if strategy == "flock" and _native_flock_available():
        target_q = shlex.quote(target)
        temp_q = shlex.quote(f"{target}.compiletools.tmp")
        # $$ escapes to $ at Make-recipe expansion so the shell sees $? / $ec.
        inner = f"{compile_cmd} -o {temp_q} && mv -f {temp_q} {target_q}; ec=$$?; rm -f {temp_q}; exit $$ec"
        return f"flock {target_q} sh -c {shlex.quote(inner)}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper compile --target={target} --strategy={strategy} -- {compile_cmd}"


def wrap_link_with_lock(link_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a link/ar command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per link.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Unlike wrap_compile_with_lock, the command is passed through unchanged
    (including any -o flag) since atomic_link does not manipulate output paths.

    Args:
        link_cmd: Complete link command string (e.g., "g++ -o bin/foo obj/foo.o")
        target: Target file for locking (e.g., "$@" for Make, or an actual path)
        args: Namespace with file_locking, sleep_interval_lockdir, etc.
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return link_cmd

    import compiletools.filesystem_utils

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup)
    if strategy == "flock" and _native_flock_available():
        return f"flock {target} {link_cmd}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper link --target={target} --strategy={strategy} -- {link_cmd}"


def check_lock_helper_available() -> bool:
    """Check if ct-lock-helper is on PATH. Returns True if found."""
    return shutil.which("ct-lock-helper") is not None


def report_lock_helper_missing() -> None:
    """Raise RuntimeError when ct-lock-helper is not found on PATH."""
    raise RuntimeError(
        "ct-lock-helper not found in PATH\n"
        "\n"
        "The --file-locking flag requires ct-lock-helper to be installed.\n"
        "\n"
        "Solutions:\n"
        "  1. Install compiletools: pip install compiletools\n"
        "  2. Install from source: pip install -e .\n"
        "  3. Add ct-lock-helper to your PATH\n"
        "\n"
        "Or disable file locking with: --no-file-locking"
    )


_REGISTRY: dict[str, type[BuildBackend]] = {}

_BackendT = TypeVar("_BackendT", bound="BuildBackend")


def register_backend(cls: type[_BackendT]) -> type[_BackendT]:
    """Register a backend class. Can be used as a decorator.

    Adding a new backend should be a single drop-in: implement
    BuildBackend, declare ``@staticmethod tool_command()`` if the backend
    needs an external tool (return None / ``("a", "b")`` for fallbacks),
    and register. The registry is the single source of truth for
    discovery, availability, and CLI argument registration (I-A3).
    """
    _REGISTRY[cls.name()] = cls
    return cls


def get_backend_class(name: str) -> type[BuildBackend]:
    """Look up a backend class by name. Raises ValueError if not found."""
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
        raise ValueError(f"Unknown backend '{name}'. Available: {available}")
    return _REGISTRY[name]


def available_backends() -> list[str]:
    """Return sorted list of registered backend names."""
    return sorted(_REGISTRY.keys())


def backend_tool_command(name: str) -> str | None:
    """Return the external tool command for a backend, or None if
    self-executing. Reads ``cls.tool_command()`` from the registered
    backend; first element of any tuple is canonical."""
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return None
    if isinstance(tool, tuple):
        return tool[0]
    return tool


def is_backend_available(name: str) -> bool:
    """Check whether the external tool for a backend is installed.

    Backends declare their tool requirement via the optional
    ``tool_command()`` classmethod, which may return:

    * ``None``        — self-executing, always available
    * ``"name"``      — single binary; available iff on PATH
    * ``("a", "b")``  — alternates; available iff at least one on PATH
    """
    import shutil

    cls = _REGISTRY.get(name)
    if cls is None:
        return False
    tool = getattr(cls, "tool_command", lambda: None)()
    if tool is None:
        return True  # self-executing backends
    candidates = (tool,) if isinstance(tool, str) else tuple(tool)
    return any(shutil.which(t) for t in candidates)


def detect_available_backends(requested: list[str]) -> list[str]:
    """Filter requested backends to those whose build tool is installed."""
    available = []
    for backend in requested:
        if is_backend_available(backend):
            available.append(backend)
        else:
            tool = backend_tool_command(backend) or backend
            print(f"  Skipping backend '{backend}': '{tool}' not found on PATH")
    return available


def register_backend_cli_arguments(cap) -> None:
    """Call ``cls.add_arguments(cap)`` on every registered backend that
    declares one (I-A4). Replaces the v8.0.2 pattern of cake.py
    hardcoding which backends contributed CLI args, which silently
    dropped any add_arguments() declared on ninja/cmake/bazel/tup/shake.
    """
    for cls in _REGISTRY.values():
        adder = getattr(cls, "add_arguments", None)
        if callable(adder):
            adder(cap)
