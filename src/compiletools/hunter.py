import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.utils
import compiletools.wrappedos
from compiletools.utils import instance_cache

if TYPE_CHECKING:
    from compiletools.cmdline_macro_index import CmdlineMacroIndex


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


class Hunter:
    """Deeply inspect files to understand what are the header dependencies,
    other required source files, other required compile/link flags.
    """

    def __init__(self, args, headerdeps, magicparser, context):
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

        return (headers, sources)

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
            self.__dict__.pop(method.cache_attr, None)
        # Clear project-level source discovery caches (these are set dynamically)
        if hasattr(self, "_hunted_sources"):
            del self._hunted_sources
        if hasattr(self, "_test_sources"):
            del self._test_sources
        # Clear the lazily-built CmdlineMacroIndex so it picks up any
        # subsequent change to magicparser._initial_macro_state.cmdline_origin
        # (e.g. when args are reparsed mid-test).
        if hasattr(self, "_cmdline_macro_index_cached"):
            del self._cmdline_macro_index_cached

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

        from compiletools.global_hash_registry import get_file_hash

        tu_hash = get_file_hash(filename, self.context)
        transitive = self._transitive_content_hashes(filename)
        scope_filter = self._get_cmdline_macro_index().tu_referenced_macros(
            tu_filename=filename,
            tu_content_hash=tu_hash,
            dep_hash=dep_hash,
            transitive_content_hashes=transitive,
        )
        return self.magicparser.get_final_macro_state_hash(filename, scope_filter=scope_filter)

    def _bytes_provider(self) -> Callable[[str], bytes]:
        """Return a callable that maps content_hash -> file bytes.

        Uses :func:`global_hash_registry.get_filepath_by_hash` to find a
        file path for the hash, then reads the file. Suitable for handing
        to :class:`CmdlineMacroIndex`, which caches results by content
        hash so a given file is read at most once.
        """
        from compiletools.global_hash_registry import get_filepath_by_hash

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

    def _get_cmdline_macro_index(self) -> "CmdlineMacroIndex":
        """Lazily build the :class:`CmdlineMacroIndex` (one per Hunter)."""
        if not hasattr(self, "_cmdline_macro_index_cached"):
            from compiletools.cmdline_macro_index import CmdlineMacroIndex

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
        from compiletools.global_hash_registry import get_file_hash

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
