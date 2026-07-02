"""CMake backend — generates CMakeLists.txt from a BuildGraph.

Aggregates low-level compile/link rules back into high-level CMake
targets (add_executable, target_compile_options, etc.), since CMake
operates at a higher abstraction level than Make/Ninja.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import compiletools.filesystem_utils
import compiletools.git_utils
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


def _cmake_quote(token: str) -> str:
    """Quote *token* as a well-formed CMake quoted argument.

    CMake's quoted-argument syntax wraps the value in ``"..."`` and uses
    ``\\"`` as the escape sequence for a literal double-quote character
    inside the string.  Naively wrapping with ``f'"{token}"'`` breaks when
    *token* already contains ``"``, e.g. ``-DFOO="bar"`` would produce the
    malformed cmake syntax ``"-DFOO="bar""``.  This function escapes any
    embedded ``\\`` and ``"`` before adding the outer quotes, so EVERY
    value interpolated into the generated CMakeLists.txt (copts, paths,
    link options, test argv) should go through it rather than a bare
    ``f'"{token}"'``. ``$``-forms (``${CMAKE_COMMAND}``,
    ``$<TARGET_FILE:...>``) pass through unmodified — CMake expands them
    inside quoted arguments.
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

    Background: gcc < 15 does not recognise ``.cppm`` as C++ source, so
    ``build_backend._create_compile_rule`` injects ``-x c++`` immediately
    before the source filename.  ``extract_copts`` keeps the bare ``-x``
    token but silently drops ``c++`` (it does not start with ``-``), leaving
    an orphan ``-x`` in the aggregated copts.  When cmake puts that orphan
    in ``target_compile_options`` at the *global* scope, gcc interprets the
    next cmake-injected flag (``-MD``) as the language argument and raises::

        g++: error: language -MD not recognized

    (Verified 2026-05-13 across gcc-12.3.0, gcc-13.2.0, gcc-14.3.0,
    gcc-15.2.0, gcc-16.1.0: gcc-14.3.0 still requires the workaround;
    native ``.cppm`` recognition arrived in gcc 15.)

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

    2. **``COMPILE_OPTIONS "-x;c++"``** — gcc < 15 does not recognise
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
       scoped to this TU.  (Verified 2026-05-13 across gcc-12.3.0,
       gcc-13.2.0, gcc-14.3.0, gcc-15.2.0, gcc-16.1.0.)

    CMake uses semicolons as list separators inside property values, so the
    correct form is ``"-x;c++"`` (cmake list) rather than ``"-x" "c++"``
    (two separate arguments).

    *srcs* must already be sorted and deduplicated (callers pass ``rel_srcs``).
    """
    for src in srcs:
        if src.endswith(".cppm"):
            f.write(
                f'set_source_files_properties({_cmake_quote(src)} PROPERTIES LANGUAGE CXX COMPILE_OPTIONS "-x;c++")\n'
            )


def _cmake_src_rel(path: str) -> str:
    """Anchor a gitroot-relative path to ``${CMAKE_SOURCE_DIR}``.

    ``add_custom_command`` resolves a relative OUTPUT / COMMAND-argument
    against the *build* directory (cmake-build/), not the source tree — so a
    gitroot-relative rule.output would land the .result marker buried inside
    cmake-build/. Absolute paths pass through unchanged.
    """
    return path if os.path.isabs(path) else f"${{CMAKE_SOURCE_DIR}}/{path}"


def _cmake_cache_home_dir(cache_file: str) -> str | None:
    """Return the ``CMAKE_HOME_DIRECTORY`` recorded in a CMakeCache.txt.

    This is the source directory CMake bound the cache to at configure time.
    Returns ``None`` if the cache file is absent or the entry can't be read —
    callers treat ``None`` as "no pre-existing cache to conflict with".
    """
    try:
        with open(cache_file, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Format: CMAKE_HOME_DIRECTORY:INTERNAL=/abs/source/dir
                if line.startswith("CMAKE_HOME_DIRECTORY:"):
                    _, _, value = line.partition("=")
                    value = value.strip()
                    return value or None
    except OSError:
        return None
    return None


@register_backend
class CMakeBackend(BuildBackend):
    """Generate and execute CMake build files.

    Note: --file-locking is not applied to this backend. CMake manages its
    own build system with its own parallelism and output handling; external
    file locking would conflict with its internal coordination.
    """

    @classmethod
    def _self_manages_exe_placement(cls) -> bool:
        # CMake builds out-of-source under cas-objdir/cmake-build/ and
        # then post-build copies binaries to topbindir(). Routing
        # through compiletools' cas-exedir would just dangle (cmake
        # never writes there). Use the legacy single-rule shape.
        # NB: cmake's incremental rebuild is mtime/depgraph based, not
        # content-addressable — this predicate is about who manages
        # exe placement, not about whether the backend has a CAS.
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

        # Named-module interface .o files (e.g. math.cppm → math.o) are
        # pre-compiled locally by ``_prebuild_aux_artefacts`` before cmake
        # runs. They MUST be excluded from cmake's compile targets — if
        # the .cppm source appears in ``add_executable`` srcs, cmake's
        # ``--parallel`` build re-issues the interface compile command,
        # which truncates and rewrites the BMI (.gcm / .pcm) at the
        # mapper-resolved path while a peer importer compile in the same
        # build may simultaneously open it for reading. Race symptom
        # (observed on CI gcc-14.2.0 / make generator):
        #   math: error: failed to read compiled module: No such file or directory
        # Strip those .o entries from obj_info so ``aggregate_rule_sources``
        # drops the .cppm from srcs (and drops the per-rule orphan ``-x``
        # that would otherwise bleed into target_compile_options for a
        # target that no longer compiles any .cppm). The prebuilt .o paths
        # are added back as ``target_link_libraries`` entries — cmake
        # passes absolute path-like items in target_link_libraries
        # directly to the linker, so the module's definitions are still
        # linked into the final binary.
        # Module objects ct-cake prebuilt and the native tool must NOT
        # recompile: interface-unit objects PLUS implementation-unit objects
        # (the latter import modules / carry a global module fragment that
        # cmake's Unix Makefiles build can't drive the module mapper for).
        prebuilt_module_obj_paths: frozenset[str] = frozenset(self._module_iface_obj.values()) | self._module_impl_obj
        cmake_obj_info = {obj: info for obj, info in obj_info.items() if obj not in prebuilt_module_obj_paths}

        for lib_type in (RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
            for rule in graph.rules_by_type(lib_type):
                srcs, all_copts = aggregate_rule_sources(rule, cmake_obj_info)
                target_name = mangle_target_name(os.path.basename(rule.output))
                # Filter orphan -x flags from the global copts; .cppm sources
                # will receive -x c++ via set_source_files_properties instead.
                filtered_copts = _filter_x_lang_copts(all_copts)
                include_dirs, remaining_copts = _separate_include_dirs(filtered_copts)
                rel_srcs = sorted(set(srcs))
                cmake_type = "STATIC" if lib_type == RuleType.STATIC_LIBRARY else "SHARED"

                f.write(f"\nadd_library({target_name} {cmake_type}\n")
                for s in rel_srcs:
                    f.write(f"    {_cmake_quote(s)}\n")
                f.write(")\n")

                _emit_per_source_x_lang(f, rel_srcs)
                self._emit_compile_attrs(f, target_name, remaining_copts, include_dirs)
                prebuilt_objs = sorted(set(rule.inputs) & prebuilt_module_obj_paths)
                if prebuilt_objs:
                    quoted = " ".join(_cmake_quote(p) for p in prebuilt_objs)
                    f.write(f"target_link_libraries({target_name} PRIVATE {quoted})\n")

        for rule in graph.rules_by_type(RuleType.LINK):
            srcs, all_copts = aggregate_rule_sources(rule, cmake_obj_info)
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
                f.write(f"    {_cmake_quote(s)}\n")
            f.write(")\n")

            _emit_per_source_x_lang(f, rel_srcs)
            self._emit_compile_attrs(f, target_name, remaining_copts, include_dirs)

            prebuilt_objs = sorted(set(rule.inputs) & prebuilt_module_obj_paths)
            if prebuilt_objs:
                quoted = " ".join(_cmake_quote(p) for p in prebuilt_objs)
                f.write(f"target_link_libraries({target_name} PRIVATE {quoted})\n")

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
                    quoted = " ".join(_cmake_quote(d) for d in lib_dirs)
                    f.write(f"target_link_directories({target_name} PRIVATE {quoted})\n")
                if lib_names:
                    quoted = " ".join(_cmake_quote(n) for n in lib_names)
                    f.write(f"target_link_libraries({target_name} PRIVATE {quoted})\n")
                if other_opts:
                    quoted = " ".join(_cmake_quote(opt) for opt in other_opts)
                    f.write(f"target_link_options({target_name} PRIVATE {quoted})\n")

        # Each RuleType.TEST rule becomes an add_custom_command whose OUTPUT is
        # the rule's output (JUnit XML for framework tests, else the .result
        # marker). An aggregate ``runtests ALL`` target makes ``cmake --build``
        # run tests concurrently during the build — no ctest involved. All
        # graph paths are anchored via _cmake_src_rel (see module level).
        test_rules = graph.rules_by_type(RuleType.TEST)
        test_outputs_set = {r.output for r in test_rules}
        test_outputs: list[str] = []
        for rule in test_rules:
            if not (rule.inputs and rule.command):
                continue
            exe_path = rule.inputs[0]
            try:
                exe_idx = rule.command.index(exe_path)
            except ValueError:
                continue
            target_name = mangle_target_name(os.path.basename(exe_path))
            test_argv = (
                list(rule.command[:exe_idx]) + [f"$<TARGET_FILE:{target_name}>"] + list(rule.command[exe_idx + 1 :])
            )
            argv_str = " ".join(_cmake_quote(a) for a in test_argv)
            rule_output = _cmake_src_rel(rule.output)
            # --serialise-tests chains tests by injecting the previous test
            # rule's output into this rule's inputs/order_only_deps. Surface
            # any such chain dep (a sibling test rule's output) as a file
            # DEPENDS so cmake serialises the custom commands; the exe stays
            # a target DEPENDS.
            chain_deps = [d for d in (*rule.inputs[1:], *rule.order_only_deps) if d in test_outputs_set]
            depends = " ".join([target_name, *(_cmake_quote(_cmake_src_rel(d)) for d in chain_deps)])
            f.write("\nadd_custom_command(\n")
            f.write(f"    OUTPUT {_cmake_quote(rule_output)}\n")
            # -E make_directory is mkdir -p: a no-op when the dir already
            # exists, required when rule.output is a JUnit XML file under
            # <xml-dir>/<variant> that no link rule created.
            out_dir = os.path.dirname(rule_output)
            f.write(f'    COMMAND "${{CMAKE_COMMAND}}" -E make_directory {_cmake_quote(out_dir)}\n')
            f.write(f"    COMMAND {argv_str}\n")
            # success_marker is always set for test rules (see _build_graph);
            # touching rule.output instead would be wrong for framework tests
            # where output is the JUnit XML, not the .result stamp.
            assert rule.success_marker is not None, "test rules always carry a success_marker"
            f.write(f'    COMMAND "${{CMAKE_COMMAND}}" -E touch {_cmake_quote(_cmake_src_rel(rule.success_marker))}\n')
            f.write(f"    DEPENDS {depends}\n")
            f.write("    VERBATIM\n")
            f.write(")\n")
            test_outputs.append(rule_output)
        if test_outputs:
            deps = " ".join(_cmake_quote(o) for o in test_outputs)
            f.write(f"\nadd_custom_target(runtests ALL DEPENDS {deps})\n")

    @staticmethod
    def _emit_compile_attrs(
        f,
        target_name: str,
        remaining_copts: list[str],
        include_dirs: list[str],
    ) -> None:
        """Write ``target_compile_options`` and ``target_include_directories`` for a target."""
        if remaining_copts:
            quoted = " ".join(_cmake_quote(c) for c in remaining_copts)
            f.write(f"target_compile_options({target_name} PRIVATE {quoted})\n")
        if include_dirs:
            quoted = " ".join(_cmake_quote(d) for d in include_dirs)
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

        # Use out-of-source build in {objdir}/cmake-build-<src_key>
        # CMake binds CMakeCache.txt to one source dir — hash the source so two projects
        # sharing cas_objdir don't collide.
        # Hash workspace-invariant key (gitroot-basename | gitroot-relative source dir) so two checkouts of the
        # same project at different absolute paths produce byte-identical cmake-build dirs (preserves the
        # cross-workspace .o byte-identity guarantee of -ffile-prefix-map). Outside a real git repo,
        # find_git_root returns its cwd fallback — in that case fall back to the absolute-path hash
        # (byte-identity across workspaces isn't expected there anyway, and the cwd fallback would
        # spuriously equate unrelated trees that happen to live under the same parent).
        # build_filename() may be a bare ``CMakeLists.txt`` (relative) — abspath() it FIRST so the
        # wrappedos.realpath() result (and therefore the cache key) doesn't depend on the cwd the
        # caller happened to be in.
        source_dir = os.path.dirname(compiletools.wrappedos.realpath(os.path.abspath(self.build_filename())))
        # abspath() FIRST (same reason as source_dir above): find_git_root ->
        # wrappedos.realpath caches on the input *string*, so a bare relative
        # build_filename() would return the first-seen cwd's resolution on a
        # second call from a different cwd, yielding a stale anchor_root that
        # disagrees with the (already abspath'd) source_dir.
        anchor_root = compiletools.git_utils.find_git_root(os.path.abspath(self.build_filename()))
        # find_git_root returns its cwd-style fallback (the queried directory itself) when no real
        # .git marker is found — gate the workspace-invariant key on a *real* git marker so a stray
        # empty ``/tmp/.git`` doesn't poison the hash.
        if compiletools.git_utils.is_real_git_marker(anchor_root):
            anchor_real = compiletools.wrappedos.realpath(anchor_root)
            # Guard against ``source_dir`` not actually living under ``anchor_real`` (would yield
            # ``../../...`` traversal escapes that leak workspace structure into the cache key).
            # When not under, fall back to the absolute-source-dir hash.
            try:
                common = os.path.commonpath([source_dir, anchor_real])
            except ValueError:
                # Different drives on Windows etc. — treat as not-under.
                common = ""
            if common == anchor_real:
                rel_src = os.path.relpath(source_dir, anchor_real)
                # ``anchor_real == "/"`` would basename to ``""`` and produce a key like ``|rel`` —
                # substitute a sentinel so the key always has a nonempty gitroot name.
                gitroot_name = os.path.basename(os.path.normpath(anchor_real)) or "ROOT"
                key_material = f"{gitroot_name}|{rel_src}"
            else:
                key_material = source_dir
        else:
            key_material = source_dir
        # blake2b (truncated to 6 bytes = 12 hex chars) avoids sha1's historical baggage and is
        # faster. Width is pinned at 12 hex chars to match the prior layout exactly.
        src_key = hashlib.blake2b(key_material.encode("utf-8"), digest_size=6).hexdigest()
        build_dir = os.path.join(self.args.cas_objdir, f"cmake-build-{src_key}")
        # Cross-repo collision guard. The workspace-invariant key deliberately
        # collides two checkouts of the *same* repo (preserving .o byte-identity
        # under a shared cas_objdir), but two *distinct* repos that share a
        # gitroot basename AND gitroot-relative source path hash to the same
        # src_key. CMake binds CMakeCache.txt to one source dir and would abort
        # the second project with a "source directory has changed" error. If an
        # existing cache points at a different source dir, fall back to an
        # absolute-source-dir key (unique per checkout) for this tree only.
        cached_home = _cmake_cache_home_dir(os.path.join(build_dir, "CMakeCache.txt"))
        if cached_home is not None and compiletools.wrappedos.realpath(cached_home) != source_dir:
            fallback_key = hashlib.blake2b(source_dir.encode("utf-8"), digest_size=6).hexdigest()
            build_dir = os.path.join(self.args.cas_objdir, f"cmake-build-{fallback_key}")
        os.makedirs(build_dir, exist_ok=True)

        # Configure — pass the user-configured compilers so CMake does not
        # fall back to the system default (which may be too old). A wrapper
        # prefix (e.g. CXX="ccache g++") is split off into
        # CMAKE_*_COMPILER_LAUNCHER because CMAKE_*_COMPILER itself must be
        # a single executable path.
        #
        # CMAKE_BUILD_TYPE="" suppresses CMake's injected build-type flags
        # (CMake 4.x defaults to RelWithDebInfo → -O2 -g -DNDEBUG). The
        # variant axis owns optimization; a stray -O from CMake desyncs
        # __OPTIMIZE__ between the ct-cake-built PCH and the consumer compile.
        configure_cmd = [cmake, "-S", source_dir, "-B", build_dir, "-DCMAKE_BUILD_TYPE="]
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

        # Copy executables to namer paths (variant-specific dir) so the
        # user-visible bin/ stays in sync; cake._copyexes afterwards copies
        # args.filename executables to topbindir / --output. Tests already ran
        # during ``cmake --build`` via the runtests target's custom commands.
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
