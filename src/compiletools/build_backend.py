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
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys

import compiletools.filesystem_utils
import compiletools.namer
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_graph import BuildGraph, BuildRule


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
    """
    if not command:
        return []
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
        if arg in object_files:
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
        headers = rule.inputs[1:] if len(rule.inputs) > 1 else []
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
            from compiletools.build_context import BuildContext

            self.context = BuildContext()
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
            graph.add_rule(BuildRule(output="runtests", inputs=test_exe_paths, command=None, rule_type="phony"))
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
            # Collect results in submission order
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

        The command list is expected to end with [..., "-o", target].
        When file_locking is enabled, the -o and target are stripped and
        ct-lock-helper wraps the remainder.
        """
        if not getattr(self.args, "file_locking", False) or self._filesystem_type is None:
            return " ".join(command)

        try:
            o_idx = command.index("-o")
        except ValueError:
            return " ".join(command)

        compile_part = command[:o_idx]
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
        except (FileNotFoundError, OSError, RuntimeError):
            include_weight = 0

        import stringzilla as sz

        magicflags = self.hunter.magicflags(filename)
        macro_state_hash = self.hunter.macro_state_hash(filename)
        dep_hash = self.namer.compute_dep_hash(deplist)
        obj_name = self.namer.object_pathname(filename, macro_state_hash, dep_hash)

        magic_cpp_flags = magicflags.get(sz.Str("CPPFLAGS"), [])
        if compiletools.utils.is_c_source(filename):
            magic_c_flags = magicflags.get(sz.Str("CFLAGS"), [])
            compile_cmd = (
                [self.args.CC, self.args.CFLAGS]
                + [str(flag) for flag in magic_cpp_flags]
                + [str(flag) for flag in magic_c_flags]
            )
        else:
            magic_cxx_flags = magicflags.get(sz.Str("CXXFLAGS"), [])
            compile_cmd = (
                [self.args.CXX, self.args.CXXFLAGS]
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

        import stringzilla as sz

        all_magic_ldflags = []
        for s in completesources:
            magic_flags = self.hunter.magicflags(s)
            all_magic_ldflags.extend(magic_flags.get(sz.Str("LDFLAGS"), []))

        link_cmd = [self.args.LD, "-o", exename] + list(object_names) + [str(f) for f in all_magic_ldflags]

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
            link_cmd.append(self.args.LDFLAGS)

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

        import stringzilla as sz

        all_magic_ldflags = []
        for s in all_source_files:
            magic_flags = self.hunter.magicflags(s)
            all_magic_ldflags.extend(magic_flags.get(sz.Str("LDFLAGS"), []))

        lib_cmd = [self.args.LD, "-shared", "-o", lib_path] + list(object_names)
        lib_cmd.extend([str(f) for f in all_magic_ldflags])
        if self.args.LDFLAGS:
            lib_cmd.append(self.args.LDFLAGS)

        return BuildRule(
            output=lib_path,
            inputs=list(object_names),
            command=lib_cmd,
            rule_type="shared_library",
            order_only_deps=[self.namer.executable_dir()],
        )


def wrap_compile_with_lock(compile_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a compile command with ct-lock-helper for file locking.

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
    else:  # flock
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_FLOCK={args.sleep_interval_flock_fallback}")

    env_vars.append(f"CT_LOCK_WARN_INTERVAL={args.lock_warn_interval}")
    env_vars.append(f"CT_LOCK_TIMEOUT={args.lock_cross_host_timeout}")

    env_prefix = " ".join(env_vars) + " " if env_vars else ""

    return f"{env_prefix}ct-lock-helper compile --target={target} --strategy={strategy} -- {compile_cmd}"


def wrap_link_with_lock(link_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a link/ar command with ct-lock-helper for file locking.

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
    else:  # flock
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_FLOCK={args.sleep_interval_flock_fallback}")

    env_vars.append(f"CT_LOCK_WARN_INTERVAL={args.lock_warn_interval}")
    env_vars.append(f"CT_LOCK_TIMEOUT={args.lock_cross_host_timeout}")

    env_prefix = " ".join(env_vars) + " " if env_vars else ""

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

# Backend tools required for execution.  Backends not listed here (e.g. shake,
# which is self-executing) are assumed to need no external tool.
_BACKEND_TOOLS: dict[str, str] = {
    "make": "make",
    "ninja": "ninja",
    "cmake": "cmake",
    "bazel": "bazel",
    "tup": "tup",
}


def register_backend(cls: type[BuildBackend]) -> type[BuildBackend]:
    """Register a backend class. Can be used as a decorator."""
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
    """Return the external tool command for a backend, or None if self-executing."""
    return _BACKEND_TOOLS.get(name)


def is_backend_available(name: str) -> bool:
    """Check whether the external tool for a backend is installed."""
    import shutil

    tool = _BACKEND_TOOLS.get(name)
    if tool is None:
        if name == "slurm":
            return shutil.which("sbatch") is not None
        return True  # Self-executing backends (e.g. shake) need no external tool.
    if shutil.which(tool):
        return True
    if name == "bazel" and shutil.which("bazelisk"):
        return True
    return False


def detect_available_backends(requested: list[str]) -> list[str]:
    """Filter requested backends to those whose build tool is installed."""
    available = []
    for backend in requested:
        if is_backend_available(backend):
            available.append(backend)
        else:
            tool = _BACKEND_TOOLS.get(backend, backend)
            print(f"  Skipping backend '{backend}': '{tool}' not found on PATH")
    return available
