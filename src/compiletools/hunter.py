import json
import os
import sys
from collections.abc import Callable

import compiletools.apptools
import compiletools.diagnostics
import compiletools.file_analyzer
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.utils
import compiletools.wrappedos
from compiletools.cmdline_macro_index import CmdlineMacroIndex
from compiletools.global_hash_registry import (
    get_file_hash,
    get_filepath_by_hash,
    get_tracked_files,
)
from compiletools.utils import instance_cache


def add_arguments(cap):
    """Add the command line arguments that the Hunter classes require.

    Safe to call more than once on the same parser.
    """
    if compiletools.apptools._parser_has_option(cap, "--allow-magic-source-in-header"):
        return
    compiletools.apptools.add_common_arguments(cap)
    compiletools.headerdeps.add_arguments(cap)
    compiletools.magicflags.add_arguments(cap)

    compiletools.utils.add_boolean_argument(
        parser=cap,
        name="allow-magic-source-in-header",
        dest="allow_magic_source_in_header",
        default=False,
        help="Set this to true if you want to use the //#SOURCE=foo.cpp magic flag in your "
        "header files. Defaults to false because it is significantly slower.",
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="scope-diagnostics",
        dest="scope_diagnostics",
        default=False,
        help="Write a per-TU JSON sidecar under <diagnostics-dir>/<invocation-id>/scope/ "
        "listing which cmdline -D macros were included vs excluded from the TU's "
        "macro_state_hash. Useful for auditing the per-TU cache-key scope filter. "
        "Silently skipped when no diagnostics dir is resolvable.",
    )


class Hunter:
    """Deeply inspect files to understand what are the header dependencies,
    other required source files, other required compile/link flags.
    """

    def __init__(self, args, headerdeps, magicparser, context):
        compiletools.apptools.check_flag_string_drift(args)
        self.args = args
        self.headerdeps = headerdeps
        self.magicparser = magicparser
        self.context = context

    @instance_cache
    def _extractSOURCE(self, realpath):
        import stringzilla as sz

        # Use magicflags() (which goes through _parse_magic cache) so that
        # _parse() runs for every file during dependency expansion.  This
        # ensures _final_macro_states carries the effective compile flags
        # (global + per-file magic) before macro_state_hash() is ever called.
        flags = self.magicflags(realpath)

        source_flags = flags.get(sz.Str("SOURCE"), [])
        cwd = compiletools.wrappedos.dirname(realpath)
        ess = frozenset(compiletools.wrappedos.realpath(os.path.join(cwd, str(es))) for es in source_flags)
        if self.args.verbose >= 2 and ess:
            print("Hunter::_extractSOURCE. realpath=", realpath, " SOURCE flag:", ess)
        return ess

    @instance_cache
    def _get_immediate_deps(self, realpath, macro_state_key):
        """Get immediate dependencies for a single file (cached by realpath + macro_state_key).

        Returns:
            Tuple of (headers, sources) where each is a tuple of absolute paths
        """
        if self.args.verbose >= 7:
            print(f"Hunter::_get_immediate_deps for {realpath} (macro_state_key={macro_state_key})")

        # Pass macro_state_key to preserve file-level macro context when analyzing headers
        headers = tuple(self.headerdeps.process(realpath, macro_state_key))

        sources = ()
        if self.args.allow_magic_source_in_header or compiletools.utils.is_source(realpath):
            sources = tuple(self._extractSOURCE(realpath))

        # Check for implied source (e.g., .cpp for .h)
        implied = compiletools.utils.implied_source(realpath)
        if implied:
            # Pass macro_state_key for implied source too
            implied_headers = tuple(self.headerdeps.process(implied, macro_state_key))
            headers = headers + (implied,) + implied_headers

        # C++20 modules: every `import M;` in this TU pulls the file that
        # declares `export module M;` into the dependency graph as a source.
        # The interface unit must compile before this TU; that ordering is
        # enforced by the backend via BMI/stamp edges on the importer's
        # inputs (see build_backend._wire_module_inputs).
        module_iface_sources = self._module_interface_sources_for(realpath)
        if module_iface_sources:
            sources = sources + module_iface_sources

        return (headers, sources)

    def _module_interface_sources_for(self, realpath: str) -> tuple[str, ...]:
        """Return module-related source paths for every module this TU imports.

        For each imported module name we pull in:
          1. The interface unit (the file with ``export module NAME;``), so
             gcc can produce / read the CMI before this TU is compiled.
          2. Every implementation unit (``module NAME;`` without ``export``),
             so the symbols defined in those units end up in the link.

        For partition imports (``import :basic;``) the partition-only form
        is resolved against the importer's own module name, harvested from
        the file's own ``export module M[:P];`` or ``module M[:P];``
        declaration. ``import math:basic;`` (fully-qualified form) is
        looked up directly.

        Returns an empty tuple when the file imports nothing.
        """
        result = self._file_analysis_result(realpath)
        if result is None or not result.module_imports:
            return ()
        own_module = self._own_module_name(result)
        registry = self._module_interface_registry()
        impl_registry = self._module_implementation_registry()
        # Multiple files exporting the same module name is tolerated at
        # registry-build time (warning only, see docstring on
        # ``_module_interface_registry``). Hard-fail HERE -- when an
        # importer actually depends on a name with multiple exporters --
        # so the diagnostic carries the importer's path and the user
        # gets actionable context. Without this gate a stale duplicate
        # in an unrelated subtree could silently be picked up.
        conflicts = getattr(self, "_module_export_conflicts", {})
        out: list[str] = []
        seen: set[str] = set()
        for raw_name in result.module_imports:
            resolved = self._resolve_module_import(raw_name, own_module)
            if resolved is None:
                continue
            if resolved in conflicts:
                paths = conflicts[resolved]
                raise ValueError(
                    f"Duplicate `export module {resolved};` declaration "
                    f"reached by `import {raw_name};` in {realpath}: "
                    f"candidates are {', '.join(repr(p) for p in paths)}. "
                    "A module name must have exactly one interface unit "
                    "in any single build's source set."
                )
            iface = registry.get(resolved)
            if iface is not None and iface != realpath and iface not in seen:
                out.append(iface)
                seen.add(iface)
            for impl in impl_registry.get(resolved, ()):
                if impl != realpath and impl not in seen:
                    out.append(impl)
                    seen.add(impl)
            # When the importer pulls in a primary module M, also pull in
            # every partition of M -- the primary may not list them
            # explicitly via `export import :P;` and the link still needs
            # the partition objects. Cheap to over-include.
            if ":" not in resolved:
                for part_name, part_iface in registry.items():
                    if part_name.startswith(resolved + ":"):
                        if part_iface != realpath and part_iface not in seen:
                            out.append(part_iface)
                            seen.add(part_iface)
                        for part_impl in impl_registry.get(part_name, ()):
                            if part_impl != realpath and part_impl not in seen:
                                out.append(part_impl)
                                seen.add(part_impl)
        return tuple(out)

    @staticmethod
    def _own_module_name(file_result) -> str | None:
        """Return the *primary* module name (no partition suffix) the TU
        belongs to, or None if it doesn't belong to any module.

        A TU declares its module via ``export module M[:P];`` or
        ``module M[:P];`` -- we strip the partition because a partition
        import like ``import :other;`` resolves to the same primary
        module, not the same partition.
        """
        for spec in tuple(file_result.module_exports) + tuple(file_result.module_implements):
            base = spec.split(":", 1)[0]
            if base:
                return base
        return None

    @staticmethod
    def _resolve_module_import(raw_name: str, own_module: str | None) -> str | None:
        """Resolve a raw import-spec to a full module name suitable for
        the registry lookup.

        - ``M`` -> ``M``
        - ``M:P`` -> ``M:P``
        - ``:P`` -> ``own_module:P`` if the importer belongs to a module,
          else None (the import is illegal C++; we skip rather than
          guessing).
        """
        if not raw_name:
            return None
        if raw_name.startswith(":"):
            if not own_module:
                return None
            return own_module + raw_name
        return raw_name

    def _file_analysis_result(self, realpath: str):
        """Return the FileAnalysisResult for `realpath`, or None on error."""
        # Module discovery is reachable from arbitrary call sites (Hunter,
        # build_backend, tests) -- not just from FindTargets which is the
        # usual init path for analyzer_args. Set it lazily here so the
        # module path doesn't require a particular caller.
        if self.context.analyzer_args is None:
            compiletools.file_analyzer.set_analyzer_args(self.args, self.context)
        try:
            content_hash = get_file_hash(realpath, self.context)
        except FileNotFoundError:
            return None
        try:
            return compiletools.file_analyzer.analyze_file(content_hash, self.context)
        except (FileNotFoundError, RuntimeError):
            return None

    def _module_interface_registry(self) -> dict[str, str]:
        """Lazily build a `module_name -> interface_filepath` map.

        Scans every tracked source file (the global hash registry walks the
        repo once at startup, so this is cheap) and reads each file's
        FileAnalysisResult to find ``export module NAME;`` declarations.
        Two files exporting the same module name is a hard error.
        """
        cached = getattr(self, "_module_iface_registry_cached", None)
        if cached is not None:
            return cached

        # Same lazy-init rationale as in _file_analysis_result.
        if self.context.analyzer_args is None:
            compiletools.file_analyzer.set_analyzer_args(self.args, self.context)

        registry: dict[str, str] = {}
        # Implementation-unit registry is built in the same scan to avoid a
        # second pass over the tree.
        impl_registry: dict[str, list[str]] = {}

        # All exporters per module name. The registry exposed below
        # picks one (lexicographically-first path) but we keep the
        # full list for the duplicate-detection warning below. Two
        # unrelated subtrees in a monorepo can legitimately both
        # export ``module math`` -- the duplicate matters only when
        # both are pulled into the SAME build's source set, and at
        # that point the lookup-time disambiguation in
        # ``_module_interface_sources_for`` will surface the issue
        # against an importer we can name. Failing eagerly here would
        # break otherwise-fine builds in monorepos with sample trees.
        all_exporters: dict[str, list[str]] = {}

        def consider(filepath: str, content_hash: str) -> None:
            if not compiletools.utils.is_source(filepath):
                return
            try:
                result = compiletools.file_analyzer.analyze_file(content_hash, self.context)
            except (FileNotFoundError, RuntimeError):
                return
            for name in result.module_exports:
                exporters = all_exporters.setdefault(name, [])
                if filepath not in exporters:
                    exporters.append(filepath)
            for name in result.module_implements:
                impls = impl_registry.setdefault(name, [])
                if filepath not in impls:
                    impls.append(filepath)

        # Seed from the tracked-files registry (git-fast-path), then
        # walk any user-specified ``--include`` directories to pick up
        # sources outside the registry. The cwd fallback (walking ``.``
        # when no include dirs are set) is gated on ``tracked`` being
        # empty: in a git tree we trust the registry; in a non-git tree
        # (the test-fixture case) we fall back to walking cwd so a build
        # without ``--include`` can still discover module sources.
        # Include dirs are walked unconditionally because they may point
        # outside the git tree (e.g., a tmp_path test fixture, or an
        # external code drop).
        tracked = get_tracked_files(self.context)
        seen_files: set[str] = set()
        for filepath, content_hash in tracked.items():
            consider(filepath, content_hash)
            seen_files.add(filepath)

        roots: list[str] = []
        include_dirs = getattr(self.args, "INCLUDE", None) or getattr(self.args, "include", None) or []
        if isinstance(include_dirs, str):
            include_dirs = [include_dirs]
        for inc in include_dirs:
            if isinstance(inc, str) and compiletools.wrappedos.isdir(inc):
                roots.append(compiletools.wrappedos.realpath(inc))
        # ``.`` is the last-resort fallback for non-git builds with no
        # ``--include``. Skip it when the git registry already has
        # something to say -- avoids walking the entire cwd subtree
        # (which can be a monorepo) for a module discovery that the
        # registry already covers.
        if not roots and not tracked:
            roots.append(compiletools.wrappedos.realpath("."))
        seen_roots: set[str] = set()
        for root in roots:
            if root in seen_roots:
                continue
            seen_roots.add(root)
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    full = compiletools.wrappedos.realpath(os.path.join(dirpath, fname))
                    if full in seen_files or not compiletools.utils.is_source(full):
                        continue
                    try:
                        content_hash = get_file_hash(full, self.context)
                    except FileNotFoundError:
                        continue
                    consider(full, content_hash)
                    seen_files.add(full)

        # Collapse the multi-exporter map to a single registered path
        # per name, picking the lexicographically-first path for
        # determinism. Warn on collisions at verbose >= 1 -- a real
        # build-time conflict is surfaced by the importer compile
        # failing against an ambiguously-resolved module, which gives
        # the user actionable file-line context that an eager error
        # here couldn't.
        verbose = getattr(self.args, "verbose", 0)
        for name, paths in all_exporters.items():
            paths.sort()
            registry[name] = paths[0]
            if len(paths) > 1 and verbose >= 1:
                others = ", ".join(paths[1:])
                print(
                    f"WARNING: module '{name}' is exported by multiple files; "
                    f"using {paths[0]!r} (also seen at: {others}). "
                    "Disambiguate by giving each module a distinct name or "
                    "ensure only one of these files is in your build's "
                    "source set.",
                    file=sys.stderr,
                )
        # Stash the full multi-exporter map so a build that actually
        # ends up needing a duplicated name can raise with full
        # context (see ``_check_module_conflicts_for``).
        self._module_export_conflicts: dict[str, list[str]] = {
            name: paths for name, paths in all_exporters.items() if len(paths) > 1
        }

        # System-provided modules: if no user file exports the standard
        # library module, fall back to the compiler's shipped source so
        # `import std;` JustWorks(tm). The path is recorded in the main
        # registry (so dep resolution treats it like any other interface
        # unit). `system_modules()` exposes the same mapping without
        # forcing a registry build, so callers that only need to know
        # whether a system std exists don't trigger a full source scan.
        for name, path in self.system_modules().items():
            if name not in registry:
                registry[name] = path

        self._module_iface_registry_cached = registry
        # Freeze the impl lists so callers can't mutate the cached map.
        self._module_impl_registry_cached = {k: tuple(v) for k, v in impl_registry.items()}
        return registry

    def system_modules(self) -> dict[str, str]:
        """Return the `module_name -> system_source_path` map.

        Computed independently of the user-file registry so callers that
        only need to detect whether a system std module is in scope
        don't pay for a full source-tree scan -- and don't trip
        cross-project duplicate-exporter errors when the build doesn't
        actually use the colliding modules.
        """
        cached = getattr(self, "_system_modules_cached", None)
        if cached is not None:
            return cached
        kind = compiletools.apptools.compiler_kind(self.args.CXX)
        std_path = compiletools.apptools.find_system_std_module_source(self.args.CXX, kind)
        result = {"std": std_path} if std_path else {}
        self._system_modules_cached = result
        return result

    def _module_implementation_registry(self) -> dict[str, tuple[str, ...]]:
        """Return the `module_name -> (impl_filepath, ...)` map.

        Side-builds the map on first access by going through
        ``_module_interface_registry`` so both registries are populated by
        the same single tree walk.
        """
        if not hasattr(self, "_module_impl_registry_cached"):
            self._module_interface_registry()
        return self._module_impl_registry_cached

    def _expand_deps_recursive(self, realpath, macro_state_key, processed):
        """Recursively expand dependencies (internal helper).

        processed is a dict used as an insertion-ordered set (Python 3.7+).
        Using a dict rather than a set ensures the link command has
        deterministic argument ordering across runs, which is required for
        the content-addressable build cache in trace_backend to produce
        stable cache keys.
        """
        if realpath in processed:
            return

        processed[realpath] = None
        headers, sources = self._get_immediate_deps(realpath, macro_state_key)

        for dep in headers + sources:
            if dep not in processed:
                self._expand_deps_recursive(dep, macro_state_key, processed)

    @instance_cache
    def _required_files_impl(self, realpath, macro_state_key):
        """Get all transitive dependencies for a file (cached by realpath + macro_state_key)."""
        if self.args.verbose >= 7:
            print(f"Hunter::_required_files_impl for {realpath}")

        # Use dict as insertion-ordered set (Python 3.7+) rather than set()
        # to ensure deterministic ordering of dependencies.  Non-deterministic
        # ordering causes the link command to change on every invocation,
        # defeating the content-addressable cache and forcing a re-link (~2s).
        processed = {}
        self._expand_deps_recursive(realpath, macro_state_key, processed)

        if self.args.verbose >= 9:
            print(f"Hunter::_required_files_impl returning {len(processed)} files")

        return list(processed)

    def required_source_files(self, filename):
        """Create the list of source files that also need to be compiled
        to complete the linkage of the given file. If filename is a source
        file itself then the returned set will contain the given filename.
        As a side effect, the magic //#... flags are cached.
        """
        if self.args.verbose >= 9:
            print("Hunter::required_source_files for " + filename)
        return compiletools.utils.ordered_unique(
            [f for f in self.required_files(filename) if compiletools.utils.is_source(f)]
        )

    def required_files(self, filename):
        """Create the list of files (both header and source)
        that are either directly or indirectly utilised by the given file.
        The returned set will contain the original filename.
        As a side effect, examine the files to determine the magic //#... flags
        """
        if self.args.verbose >= 9:
            print("Hunter::required_files for " + filename)

        realpath = compiletools.wrappedos.realpath(filename)

        # Ensure magic flags are processed to get macro state key
        try:
            self.magicflags(filename)
            macro_state_key = self.macro_state_key(filename)
        except RuntimeError as e:
            # This should not happen in normal usage - indicates magicflags() succeeded
            # but macro_state_key isn't available, suggesting a bug in our code
            print(f"ERROR in required_files: {e}")
            raise

        if self.args.verbose >= 8:
            print(f"Hunter::required_files for {filename} (macro_state_key={macro_state_key})")

        return self._required_files_impl(realpath, macro_state_key)

    @staticmethod
    def clear_cache():
        compiletools.wrappedos.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()

    def clear_instance_cache(self):
        """Clear this instance's caches."""
        for method in (self._parse_magic, self._get_immediate_deps, self._required_files_impl, self._extractSOURCE):
            # ``cache_attr`` is set on the @instance_cache wrapper function; bound
            # methods forward attribute access to the underlying function at
            # runtime. Cast through ``__func__`` so the type checker sees the
            # function object directly rather than the MethodType wrapper.
            self.__dict__.pop(method.__func__.cache_attr, None)  # type: ignore[attr-defined]
        # Clear project-level source discovery caches (these are set dynamically)
        if hasattr(self, "_hunted_sources"):
            del self._hunted_sources
        if hasattr(self, "_test_sources"):
            del self._test_sources
        # Clear the lazily-built CmdlineMacroIndex so it picks up any
        # subsequent change to magicparser._initial_macro_state.cmdline_origin
        # (e.g. when args are reparsed mid-test).
        # Drop dynamically-set caches with ``__dict__.pop`` so the
        # call-site is one line each and absent-key isn't an error.
        for attr in (
            "_cmdline_macro_index_cached",
            # Modules state -- dropped so a subsequent reparse picks up
            # new/removed .cppm files and re-evaluates conflicts.
            "_module_iface_registry_cached",
            "_module_impl_registry_cached",
            "_system_modules_cached",
            "_module_export_conflicts",
        ):
            self.__dict__.pop(attr, None)

    @instance_cache
    def _parse_magic(self, filename):
        """Cache the magic parse result to avoid duplicate processing."""
        return self.magicparser.parse(filename)

    def magicflags(self, filename):
        """Get magic flags dict from cached parse result."""
        return self._parse_magic(filename)

    def macro_state_key(self, filename):
        """Get final converged macro state key for the given file.

        Returns frozenset (variable macros only) for dependency caching.
        For object file naming, use macro_state_hash().

        Raises:
            KeyError: If parse() hasn't been called for this file yet
        """
        return self.magicparser.get_final_macro_state_key(filename)

    def macro_state_hash(self, filename, dep_hash: str | None = None) -> str:
        """Get full macro state hash (core + variable) for object file naming.

        Returns 16-character hex hash including both compiler/cmdline macros
        and file-defined macros. Different compilers or flags produce different hashes.

        When ``dep_hash`` is provided AND the magicparser has a non-empty
        ``cmdline_origin`` (i.e., there are some cmdline ``-D`` macros in scope),
        a per-TU scope filter is built via :class:`CmdlineMacroIndex` and forwarded
        to ``magicflags.get_final_macro_state_hash``. Cmdline ``-D`` macros that
        are NOT referenced by this TU or any of its transitive headers are
        excluded from the hash, preventing cache-key pollution.

        When ``dep_hash`` is ``None`` or ``cmdline_origin`` is empty, behaviour
        is unchanged: every cmdline ``-D`` macro contributes to the hash. This
        keeps old call sites working without regression.

        Args:
            filename: The TU file path.
            dep_hash: Optional content-addressed dep-set hash from ``Namer``.
                When provided, gates the per-TU scope-filter cache so distinct
                dep sets don't collide.

        Raises:
            KeyError: If parse() hasn't been called for this file yet.
        """
        cmdline_origin = self.magicparser._initial_macro_state.cmdline_origin
        if dep_hash is None or not cmdline_origin:
            return self.magicparser.get_final_macro_state_hash(filename)

        tu_hash = get_file_hash(filename, self.context)
        transitive = self._transitive_content_hashes(filename)
        scope_filter = self._get_cmdline_macro_index().tu_referenced_macros(
            tu_filename=filename,
            tu_content_hash=tu_hash,
            dep_hash=dep_hash,
            transitive_content_hashes=transitive,
        )

        if getattr(self.args, "scope_diagnostics", False):
            self._write_scope_diagnostic(filename, cmdline_origin, scope_filter, dep_hash)

        return self.magicparser.get_final_macro_state_hash(filename, scope_filter=scope_filter)

    def _write_scope_diagnostic(self, filename, cmdline_origin, scope_filter, dep_hash):
        """Write per-TU scope diagnostics JSON when --scope-diagnostics is on.

        File path: ``<diagnostics_dir>/scope/<basename>.<dep_hash>.json``

        ``dep_hash`` is part of the filename because the same TU may be processed
        in multiple variant builds (different macro states across backends or
        in test loops); ``dep_hash`` discriminates them so sidecars don't collide.

        Silently no-ops when no diagnostics dir is resolvable -- callers without
        ``--diagnostics-dir`` or ``--bindir`` set must not crash.
        """
        try:
            diagnostics_dir = compiletools.diagnostics.resolve_diagnostics_dir(self.args)
        except RuntimeError:
            return  # No diagnostics dir resolvable -- silently skip

        scope_dir = os.path.join(diagnostics_dir, "scope")
        os.makedirs(scope_dir, exist_ok=True)

        excluded = sorted(str(n) for n in cmdline_origin if n not in scope_filter)
        included = sorted(str(n) for n in scope_filter if n in cmdline_origin)

        payload = {
            "tu": filename,
            "dep_hash": dep_hash,
            "cmdline_d_macros_total": len(cmdline_origin),
            "cmdline_d_macros_in_hash": included,
            "cmdline_d_macros_excluded": excluded,
        }

        basename = os.path.basename(filename)
        out_path = os.path.join(scope_dir, f"{basename}.{dep_hash}.json")
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)

    def _bytes_provider(self) -> Callable[[str], bytes]:
        """Return a callable that maps content_hash -> file bytes.

        Uses :func:`global_hash_registry.get_filepath_by_hash` to find a
        file path for the hash, then reads the file. Suitable for handing
        to :class:`CmdlineMacroIndex`, which caches results by content
        hash so a given file is read at most once.
        """

        def provider(content_hash: str) -> bytes:
            try:
                path = get_filepath_by_hash(content_hash, self.context)
            except FileNotFoundError:
                # Stale registry or hash that no longer maps to a tracked
                # file. Returning empty bytes is safe -- the caller
                # (CmdlineMacroIndex._scan) treats empty data as "not
                # referenced", which falls back to the old behaviour
                # (macro stays in the hash) for that file.
                return b""
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return b""

        return provider

    def _get_cmdline_macro_index(self) -> CmdlineMacroIndex:
        """Lazily build the :class:`CmdlineMacroIndex` (one per Hunter)."""
        if not hasattr(self, "_cmdline_macro_index_cached"):
            cmdline_origin = self.magicparser._initial_macro_state.cmdline_origin
            self._cmdline_macro_index_cached = CmdlineMacroIndex(
                cmdline_d_macro_names=cmdline_origin,
                bytes_provider=self._bytes_provider(),
            )
        return self._cmdline_macro_index_cached

    def _transitive_content_hashes(self, filename: str) -> list[str]:
        """Content hashes of transitive headers for ``filename`` (NOT the TU itself).

        Uses the same dep walk as :meth:`header_dependencies` so the
        cache-key scope matches what flows into ``namer.compute_dep_hash``.
        The TU's own content hash is NOT included -- :meth:`macro_state_hash`
        passes it separately as ``tu_content_hash`` to
        :meth:`CmdlineMacroIndex.tu_referenced_macros`.
        """
        headers = self.header_dependencies(filename)
        return [get_file_hash(str(h), self.context) for h in headers]

    def header_dependencies(self, source_filename):
        """Get header dependencies for a file with proper macro context.

        This is a public API method - ensure we compute macro state first
        so conditional includes are resolved correctly.
        """
        if self.args.verbose >= 8:
            print("Hunter asking for header dependencies for ", source_filename)

        # Compute macro state for this file first to get correct conditional includes
        self.magicflags(source_filename)
        macro_state_key = self.macro_state_key(source_filename)

        headers = self.headerdeps.process(source_filename, macro_state_key)

        return headers

    def huntsource(self):
        """Discover all source files from command line arguments and their dependencies.

        This method analyzes the files specified in args.filename, args.static,
        args.dynamic, and args.tests, then expands each to include all source
        files it depends on. Results are cached for subsequent getsources() calls.
        """
        # For simplicity and test reliability, always recompute
        # This prevents test isolation issues while maintaining functionality
        if hasattr(self, "_hunted_sources"):
            del self._hunted_sources
        if hasattr(self, "_test_sources"):
            del self._test_sources

        if self.args.verbose >= 5:
            print("Hunter::huntsource - Discovering all project sources")

        # Get initial sources from command line arguments
        initial_sources = []
        if getattr(self.args, "static", None):
            initial_sources.extend(self.args.static)
        if getattr(self.args, "dynamic", None):
            initial_sources.extend(self.args.dynamic)
        if getattr(self.args, "filename", None):
            initial_sources.extend(self.args.filename)
        if getattr(self.args, "tests", None):
            initial_sources.extend(self.args.tests)

        if not initial_sources:
            self._hunted_sources = []
            if self.args.verbose >= 5:
                print("Hunter::huntsource - No initial sources found")
            return

        initial_sources = compiletools.utils.ordered_unique(initial_sources)
        if self.args.verbose >= 6:
            print(f"Hunter::huntsource - Initial sources: {initial_sources}")

        # Expand each source to include its dependencies
        all_sources = set()
        for source in initial_sources:
            try:
                realpath_source = compiletools.wrappedos.realpath(source)

                # Skip files that don't exist
                if not os.path.exists(realpath_source):
                    if self.args.verbose >= 2:
                        print(f"Hunter::huntsource - Source file does not exist: {source} -> {realpath_source}")
                    continue

                required_sources = self.required_source_files(realpath_source)
                all_sources.update(required_sources)

                if self.args.verbose >= 7:
                    print(f"Hunter::huntsource - {source} expanded to {len(required_sources)} sources")

            except Exception as e:
                if self.args.verbose >= 2:
                    print(f"Warning: Error expanding source {source}: {e}")
                # Include the original source even if expansion fails, but only if it exists
                if os.path.exists(source):
                    all_sources.add(compiletools.wrappedos.realpath(source))

        # Cache the results as sorted absolute paths
        self._hunted_sources = sorted(all_sources)  # all_sources already contains realpaths

        if self.args.verbose >= 5:
            print(f"Hunter::huntsource - Discovered {len(self._hunted_sources)} total sources")

    def getsources(self):
        """Get all discovered source files.

        Returns the list of source files discovered by huntsource().
        Calls huntsource() automatically if not already called.

        Returns:
            List of absolute paths to all source files
        """
        if not hasattr(self, "_hunted_sources"):
            self.huntsource()
        return self._hunted_sources

    def gettestsources(self):
        """Get test source files specifically.

        Returns only the source files that came from args.tests expansion.
        Calls huntsource() automatically if not already called.

        Returns:
            List of absolute paths to test source files
        """
        if not hasattr(self, "_test_sources"):
            # Expand only test sources
            test_sources = set()
            if getattr(self.args, "tests", None):
                for source in self.args.tests:
                    try:
                        realpath_source = compiletools.wrappedos.realpath(source)
                        required_sources = self.required_source_files(realpath_source)
                        test_sources.update(required_sources)
                    except Exception as e:
                        if self.args.verbose >= 2:
                            print(f"Warning: Error expanding test source {source}: {e}")
                        test_sources.add(compiletools.wrappedos.realpath(source))

            self._test_sources = sorted(compiletools.wrappedos.realpath(src) for src in test_sources)

        return self._test_sources
