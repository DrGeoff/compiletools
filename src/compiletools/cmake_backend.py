"""CMake backend — generates CMakeLists.txt from a BuildGraph.

Aggregates low-level compile/link rules back into high-level CMake
targets (add_executable, target_compile_options, etc.), since CMake
operates at a higher abstraction level than Make/Ninja.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import compiletools.filesystem_utils
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_backend import (
    BuildBackend,
    aggregate_rule_sources,
    build_obj_info,
    extract_linkopts,
    mangle_target_name,
    register_backend,
)
from compiletools.build_graph import BuildGraph, RuleType


def _cmake_quote_copt(token: str) -> str:
    """Quote *token* for use as a CMake ``target_compile_options`` argument.

    CMake's quoted-argument syntax wraps the value in ``"..."`` and uses
    ``\\"`` as the escape sequence for a literal double-quote character
    inside the string.  Naively wrapping with ``f'"{token}"'`` breaks when
    *token* already contains ``"``, e.g. ``-DFOO="bar"`` would produce the
    malformed cmake syntax ``"-DFOO="bar""``.  This function escapes any
    embedded ``"`` before adding the outer quotes so the result is always
    a well-formed CMake quoted argument.
    """
    return '"' + token.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _separate_include_dirs(copts: list[str]) -> tuple[list[str], list[str]]:
    """Separate -I flags from other compiler options.

    Returns (include_dirs, remaining_copts) where include_dirs contains
    the directory paths (without -I prefix) and remaining_copts has
    everything else.
    """
    include_dirs = []
    remaining = []
    for opt in copts:
        if opt.startswith("-I"):
            include_dirs.append(opt[2:])
        else:
            remaining.append(opt)
    return include_dirs, remaining


def _filter_x_lang_copts(copts: list[str]) -> list[str]:
    """Remove ``-x`` and any following language argument from a copts list.

    Background: gcc < 14 does not recognise ``.cppm`` as C++ source, so
    ``build_backend._create_compile_rule`` injects ``-x c++`` immediately
    before the source filename.  ``extract_copts`` keeps the bare ``-x``
    token but silently drops ``c++`` (it does not start with ``-``), leaving
    an orphan ``-x`` in the aggregated copts.  When cmake puts that orphan
    in ``target_compile_options`` at the *global* scope, gcc interprets the
    next cmake-injected flag (``-MD``) as the language argument and raises::

        g++: error: language -MD not recognized

    This helper strips both the ``-x`` and any adjacent non-flag language
    argument so the caller can emit a clean global ``target_compile_options``
    and separately scope ``-x c++`` to the individual ``.cppm`` source via
    ``set_source_files_properties``.
    """
    result = []
    i = 0
    while i < len(copts):
        if copts[i] == "-x":
            i += 1
            # Also skip the following language token (e.g. "c++") if present
            if i < len(copts) and not copts[i].startswith("-"):
                i += 1
        else:
            result.append(copts[i])
            i += 1
    return result


def _emit_per_source_x_lang(f, srcs: list[str]) -> None:
    """Emit ``set_source_files_properties`` for each ``.cppm`` source.

    Two things are needed for cmake to correctly compile C++20 module
    interface units (``.cppm`` files):

    1. **``LANGUAGE CXX``** — cmake 3.20 does not recognise ``.cppm`` as a
       C++ source extension, so without this property cmake silently omits
       the file from the compile step and the link fails with
       ``undefined reference to 'func@module(...)'``.

    2. **``COMPILE_OPTIONS "-x;c++"``** — gcc < 14 does not recognise
       ``.cppm`` as C++, so it treats the file as a linker input under
       ``-c`` and produces no ``.o``.  The ``-x c++`` coercion (which
       ``build_backend._create_compile_rule`` injects immediately before the
       filename) must be scoped to this single source file.  ``extract_copts``
       keeps the bare ``-x`` token but drops ``c++`` (not flag-shaped), leaving
       an orphan ``-x`` in the aggregated copts.  At global
       ``target_compile_options`` scope that orphan causes gcc to consume
       cmake's own ``-MD`` flag as the language argument:
           g++: error: language -MD not recognized
       Setting it here via ``set_source_files_properties`` keeps it
       scoped to this TU.

    CMake uses semicolons as list separators inside property values, so the
    correct form is ``"-x;c++"`` (cmake list) rather than ``"-x" "c++"``
    (two separate arguments).
    """
    seen: set[str] = set()
    for src in srcs:
        if src.endswith(".cppm") and src not in seen:
            seen.add(src)
            f.write(f'set_source_files_properties("{src}" PROPERTIES LANGUAGE CXX COMPILE_OPTIONS "-x;c++")\n')


@register_backend
class CMakeBackend(BuildBackend):
    """Generate and execute CMake build files.

    Note: --file-locking is not applied to this backend. CMake manages its
    own build system with its own parallelism and output handling; external
    file locking would conflict with its internal coordination.
    """

    def _has_native_cas_exe(self) -> bool:
        # CMake builds out-of-source under cas-objdir/cmake-build/ and
        # then post-build copies binaries to topbindir(). Cmake's own
        # incremental tracking is the CAS-equivalent here; routing
        # through compiletools' cas-exedir would just dangle (cmake
        # never writes there). Use the legacy single-rule shape.
        return True

    @staticmethod
    def name() -> str:
        return "cmake"

    @staticmethod
    def tool_command() -> str:
        return "cmake"

    @staticmethod
    def build_filename() -> str:
        return "CMakeLists.txt"

    def generate(self, graph: BuildGraph, output=None) -> None:
        graph = self._apply_build_only_changed(graph)

        if output is not None:
            self._write_cmake(graph, output)
        else:
            filename = self.build_filename()
            with compiletools.filesystem_utils.atomic_output_file(filename, mode="w", encoding="utf-8") as f:
                self._write_cmake(graph, f)

    def _write_cmake(self, graph: BuildGraph, f) -> None:
        f.write("# CMakeLists.txt generated by compiletools\n")
        f.write("cmake_minimum_required(VERSION 3.15)\n")
        f.write("\n# Reject in-source builds\n")
        f.write('file(TO_CMAKE_PATH "${PROJECT_BINARY_DIR}/CMakeLists.txt" LOC_PATH)\n')
        f.write('if(EXISTS "${LOC_PATH}")\n')
        f.write('    message(FATAL_ERROR "In-source builds are not allowed. Use an out-of-source build directory.")\n')
        f.write("endif()\n")

        has_c = False
        has_cxx = False
        for rule in graph.rules_by_type(RuleType.COMPILE):
            if rule.inputs and compiletools.utils.is_c_source(rule.inputs[0]):
                has_c = True
            else:
                has_cxx = True
        if has_c and not has_cxx:
            lang = "C"
        elif has_c and has_cxx:
            lang = "C CXX"
        else:
            lang = "CXX"
        f.write(f"project(compiletools_build {lang})\n")

        obj_info = build_obj_info(graph)

        for lib_type in (RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
            for rule in graph.rules_by_type(lib_type):
                srcs, all_copts = aggregate_rule_sources(rule, obj_info)
                target_name = mangle_target_name(os.path.basename(rule.output))
                # Filter orphan -x flags from the global copts; .cppm sources
                # will receive -x c++ via set_source_files_properties instead.
                filtered_copts = _filter_x_lang_copts(all_copts)
                include_dirs, remaining_copts = _separate_include_dirs(filtered_copts)
                rel_srcs = sorted(set(srcs))
                cmake_type = "STATIC" if lib_type == RuleType.STATIC_LIBRARY else "SHARED"

                f.write(f"\nadd_library({target_name} {cmake_type}\n")
                for s in rel_srcs:
                    f.write(f'    "{s}"\n')
                f.write(")\n")

                _emit_per_source_x_lang(f, srcs)
                self._emit_compile_attrs(f, target_name, remaining_copts, include_dirs)

        for rule in graph.rules_by_type(RuleType.LINK):
            srcs, all_copts = aggregate_rule_sources(rule, obj_info)
            object_files = set(rule.inputs)
            linkopts = extract_linkopts(rule.command, object_files) if rule.command else []
            target_name = mangle_target_name(os.path.basename(rule.output))
            # Filter orphan -x flags from the global copts; .cppm sources
            # will receive -x c++ via set_source_files_properties instead.
            filtered_copts = _filter_x_lang_copts(all_copts)
            include_dirs, remaining_copts = _separate_include_dirs(filtered_copts)
            rel_srcs = sorted(set(srcs))

            f.write(f"\nadd_executable({target_name}\n")
            for s in rel_srcs:
                f.write(f'    "{s}"\n')
            f.write(")\n")

            _emit_per_source_x_lang(f, srcs)
            self._emit_compile_attrs(f, target_name, remaining_copts, include_dirs)

            if linkopts:
                # Split linkopts into CMake-native directives:
                # -L paths → link_directories (resolved to absolute since
                #   CMake runs the linker from cmake-build/, not CWD)
                # -l libs  → target_link_libraries (ensures correct order)
                # other    → target_link_options (raw flags)
                lib_dirs = []
                lib_names = []
                other_opts = []
                for opt in linkopts:
                    if opt.startswith("-L"):
                        path = opt[2:]
                        lib_dirs.append(compiletools.wrappedos.realpath(path) if not os.path.isabs(path) else path)
                    elif opt.startswith("-l"):
                        lib_names.append(opt[2:])
                    else:
                        other_opts.append(opt)
                if lib_dirs:
                    quoted = " ".join(f'"{d}"' for d in lib_dirs)
                    f.write(f"target_link_directories({target_name} PRIVATE {quoted})\n")
                if lib_names:
                    quoted = " ".join(f'"{n}"' for n in lib_names)
                    f.write(f"target_link_libraries({target_name} PRIVATE {quoted})\n")
                if other_opts:
                    quoted = " ".join(f'"{opt}"' for opt in other_opts)
                    f.write(f"target_link_options({target_name} PRIVATE {quoted})\n")

        # Register tests so `ctest` can run them standalone
        test_rules = graph.rules_by_type(RuleType.TEST)
        if test_rules:
            f.write("\nenable_testing()\n")
            for rule in test_rules:
                if rule.inputs:
                    exe_path = rule.inputs[0]
                    test_name = mangle_target_name(os.path.splitext(os.path.basename(exe_path))[0])
                    f.write(f'add_test(NAME {test_name} COMMAND "{exe_path}")\n')
            f.write("\n")

    @staticmethod
    def _emit_compile_attrs(
        f,
        target_name: str,
        remaining_copts: list[str],
        include_dirs: list[str],
    ) -> None:
        """Write ``target_compile_options`` and ``target_include_directories`` for a target."""
        if remaining_copts:
            quoted = " ".join(_cmake_quote_copt(c) for c in remaining_copts)
            f.write(f"target_compile_options({target_name} PRIVATE {quoted})\n")
        if include_dirs:
            quoted = " ".join(f'"{d}"' for d in include_dirs)
            f.write(f"target_include_directories({target_name} PRIVATE {quoted})\n")

    def _all_outputs_current(self, graph: BuildGraph) -> bool:
        """Always re-execute the build.

        The base-class pre-check looks for compile/link outputs at the
        ``namer``-derived objdir/exedir paths.  CMake builds in an
        out-of-source ``cmake-build/`` directory, so those paths are
        almost never populated by CMake itself — the pre-check would
        return False "by accident" and skip the post-build copy of the
        produced binaries to ``topbindir``.  Make the contract explicit:
        always defer to CMake, which has its own incremental-build
        logic.  This also guarantees ``_copy_built_executables`` runs
        every time so the user-visible ``bin/`` stays in sync.
        """
        return False

    def _execute_build(self, target: str) -> None:
        cmake = shutil.which("cmake")
        if cmake is None:
            raise RuntimeError("'cmake' not found on PATH")

        self._prebuild_aux_artefacts()

        # Use out-of-source build in {objdir}/cmake-build
        source_dir = os.path.dirname(compiletools.wrappedos.realpath(self.build_filename()))
        build_dir = os.path.join(self.args.cas_objdir, "cmake-build")
        os.makedirs(build_dir, exist_ok=True)

        # Configure — pass the user-configured compilers so CMake does not
        # fall back to the system default (which may be too old). A wrapper
        # prefix (e.g. CXX="ccache g++") is split off into
        # CMAKE_*_COMPILER_LAUNCHER because CMAKE_*_COMPILER itself must be
        # a single executable path.
        configure_cmd = [cmake, "-S", source_dir, "-B", build_dir]
        if hasattr(self.args, "CXX") and self.args.CXX:
            cxx_parts = compiletools.utils.split_command_cached(self.args.CXX)
            configure_cmd.append(f"-DCMAKE_CXX_COMPILER={cxx_parts[-1]}")
            if len(cxx_parts) > 1:
                configure_cmd.append("-DCMAKE_CXX_COMPILER_LAUNCHER=" + ";".join(cxx_parts[:-1]))
        if hasattr(self.args, "CC") and self.args.CC:
            cc_parts = compiletools.utils.split_command_cached(self.args.CC)
            configure_cmd.append(f"-DCMAKE_C_COMPILER={cc_parts[-1]}")
            if len(cc_parts) > 1:
                configure_cmd.append("-DCMAKE_C_COMPILER_LAUNCHER=" + ";".join(cc_parts[:-1]))
        subprocess.check_call(configure_cmd, text=True)

        # Build
        build_cmd = [cmake, "--build", build_dir]
        if hasattr(self.args, "parallel") and self.args.parallel:
            build_cmd.extend(["--parallel", str(self.args.parallel)])
        if target not in ("build", "all"):
            build_cmd.extend(["--target", target])
        subprocess.check_call(build_cmd, text=True)

        # Copy executables to namer paths (variant-specific dir) so that
        # _run_tests can find test executables; cake._copyexes afterwards
        # copies args.filename executables to topbindir / --output.
        self._copy_built_executables(build_dir)
        # Copy built libraries to namer library paths so the second
        # cake.main() (linking the exe) can find them via -L/-l flags.
        if self._graph is not None:
            self._copy_built_libraries(build_dir, self._graph)

    def _copy_built_libraries(self, build_dir: str, graph) -> None:
        """Copy built libraries from cmake-build to namer library paths.

        CMake builds to an out-of-source directory.  The namer expects
        libraries at paths like bin/<variant>/libfoo.a, so we walk
        cmake-build looking for .a/.so files and copy them to the
        graph-declared output paths.
        """
        lib_rules = list(graph.rules_by_type(RuleType.STATIC_LIBRARY)) + list(
            graph.rules_by_type(RuleType.SHARED_LIBRARY)
        )
        if not lib_rules:
            return

        # Build a lookup from CMake-mangled library name to graph output path
        mangled_to_dest: dict[str, str] = {}
        for rule in lib_rules:
            basename = os.path.basename(rule.output)
            mangled = mangle_target_name(basename)
            # CMake always produces lib<target>.a for STATIC, lib<target>.so for SHARED
            cmake_name = f"lib{mangled}.a"
            cmake_name_so = f"lib{mangled}.so"
            mangled_to_dest[cmake_name] = rule.output
            mangled_to_dest[cmake_name_so] = rule.output

        for dirpath, _dirs, files in os.walk(build_dir):
            for fname in files:
                if fname in mangled_to_dest:
                    src = os.path.join(dirpath, fname)
                    dest = mangled_to_dest[fname]
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    compiletools.filesystem_utils.atomic_copy(src, dest)
