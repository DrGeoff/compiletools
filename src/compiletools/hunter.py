import os
import functools

import compiletools.apptools
import compiletools.utils
import compiletools.wrappedos
import compiletools.headerdeps
import compiletools.magicflags


def add_arguments(cap):
    """ Add the command line arguments that the Hunter classes require """
    compiletools.apptools.add_common_arguments(cap)
    compiletools.headerdeps.add_arguments(cap)
    compiletools.magicflags.add_arguments(cap)

    compiletools.utils.add_boolean_argument(
        parser=cap,
        name="allow-magic-source-in-header",
        dest="allow_magic_source_in_header",
        default=False,
        help="Set this to true if you want to use the //#SOURCE=foo.cpp magic flag in your header files. Defaults to false because it is significantly slower.",
    )


class Hunter(object):

    """ Deeply inspect files to understand what are the header dependencies,
        other required source files, other required compile/link flags.
    """
    
    # Class-level cache for magic parsing results
    _magic_cache = {}

    def __init__(self, args, headerdeps, magicparser):
        self.args = args
        self.headerdeps = headerdeps
        self.magicparser = magicparser
        # Clear lru_cache on instance methods to prevent stale cache across builds
        # The cache is bound to the method, not the instance, so it persists
        if hasattr(Hunter, '_get_immediate_deps'):
            Hunter._get_immediate_deps.cache_clear()
        if hasattr(Hunter, '_parse_magic'):
            Hunter._parse_magic.cache_clear()
        if hasattr(Hunter, '_required_files_impl'):
            Hunter._required_files_impl.cache_clear()

    def _extractSOURCE(self, realpath):
        import stringzilla as sz
        # Call get_structured_data directly to leverage its cache (90%+ hit rate)
        # without paying the cost of parse() transforming all flags
        structured_data = self.magicparser.get_structured_data(realpath)

        sources = []
        for file_data in structured_data:
            for magic_flag in file_data['active_magic_flags']:
                if magic_flag['key'] == sz.Str("SOURCE"):
                    sources.append(magic_flag['value'])

        cwd = compiletools.wrappedos.dirname(realpath)
        ess = {compiletools.wrappedos.realpath(os.path.join(cwd, str(es))) for es in sources}
        if self.args.verbose >= 2 and ess:
            print("Hunter::_extractSOURCE. realpath=", realpath, " SOURCE flag:", ess)
        return ess

    @functools.lru_cache(maxsize=None)
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
        """Recursively expand dependencies (internal helper)."""
        if realpath in processed:
            return

        processed.add(realpath)
        headers, sources = self._get_immediate_deps(realpath, macro_state_key)

        for dep in headers + sources:
            if dep not in processed:
                self._expand_deps_recursive(dep, macro_state_key, processed)

    @functools.lru_cache(maxsize=None)
    def _required_files_impl(self, realpath, macro_state_key):
        """Get all transitive dependencies for a file (cached by realpath + macro_state_key)."""
        if self.args.verbose >= 7:
            print(f"Hunter::_required_files_impl for {realpath}")

        processed = set()
        self._expand_deps_recursive(realpath, macro_state_key, processed)

        if self.args.verbose >= 9:
            print(f"Hunter::_required_files_impl returning {len(processed)} files")

        return list(processed)

    def required_source_files(self, filename):
        """ Create the list of source files that also need to be compiled
            to complete the linkage of the given file. If filename is a source
            file itself then the returned set will contain the given filename.
            As a side effect, the magic //#... flags are cached.
        """
        if self.args.verbose >= 9:
            print("Hunter::required_source_files for " + filename)
        return compiletools.utils.ordered_unique(
            [
                filename
                for filename in self.required_files(filename)
                if compiletools.utils.is_source(filename)
            ]
        )

    def required_files(self, filename):
        """ Create the list of files (both header and source)
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
        # print("Hunter::clear_cache")
        compiletools.wrappedos.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        # Clear class-level cache
        # Hunter._magic_cache.clear()  # This cache doesn't exist
        # Note: Cannot clear instance-level _parse_magic caches from static method
        # Each Hunter instance will retain its own cache until the instance is destroyed

    def clear_instance_cache(self):
        """Clear this instance's caches."""
        if hasattr(self, '_parse_magic'):
            self._parse_magic.cache_clear()
        if hasattr(self, '_get_immediate_deps'):
            self._get_immediate_deps.cache_clear()
        if hasattr(self, '_required_files_impl'):
            self._required_files_impl.cache_clear()
        # Clear project-level source discovery caches
        if hasattr(self, '_hunted_sources'):
            del self._hunted_sources
        if hasattr(self, '_test_sources'):
            del self._test_sources

    @functools.lru_cache(maxsize=None)
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

    def macro_state_hash(self, filename):
        """Get full macro state hash (core + variable) for object file naming.

        Returns 16-character hex hash including both compiler/cmdline macros
        and file-defined macros. Different compilers or flags produce different hashes.

        Raises:
            KeyError: If parse() hasn't been called for this file yet
        """
        return self.magicparser.get_final_macro_state_hash(filename)

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
        if hasattr(self, '_hunted_sources'):
            del self._hunted_sources
        if hasattr(self, '_test_sources'):
            del self._test_sources

        if self.args.verbose >= 5:
            print("Hunter::huntsource - Discovering all project sources")

        # Get initial sources from command line arguments
        initial_sources = []
        if getattr(self.args, 'static', None):
            initial_sources.extend(self.args.static)
        if getattr(self.args, 'dynamic', None):
            initial_sources.extend(self.args.dynamic)
        if getattr(self.args, 'filename', None):
            initial_sources.extend(self.args.filename)
        if getattr(self.args, 'tests', None):
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
        if not hasattr(self, '_hunted_sources'):
            self.huntsource()
        return self._hunted_sources

    def gettestsources(self):
        """Get test source files specifically.

        Returns only the source files that came from args.tests expansion.
        Calls huntsource() automatically if not already called.

        Returns:
            List of absolute paths to test source files
        """
        if not hasattr(self, '_test_sources'):
            # Expand only test sources
            test_sources = set()
            if getattr(self.args, 'tests', None):
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
