import os
import functools

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

    def _extractSOURCE(self, realpath):
        sources = self.magicparser.parse(realpath).get("SOURCE", [])
        cwd = compiletools.wrappedos.dirname(realpath)
        ess = {compiletools.wrappedos.realpath(os.path.join(cwd, es)) for es in sources}
        if self.args.verbose >= 2 and ess:
            print("Hunter::_extractSOURCE. realpath=", realpath, " SOURCE flag:", ess)
        return ess

    @functools.lru_cache(maxsize=None)
    def _required_files_cached(self, realpath, macro_hash):
        """Cached dependency resolution keyed by file path + macro state.
        
        Args:
            realpath: The real path to the file
            macro_hash: Hash of the macro state affecting this file's dependencies
            
        Returns:
            List of all files (headers and sources) that this file depends on
        """
        return self._required_files_impl_uncached(realpath)
    
    def _required_files_impl_uncached(self, realpath, processed=None):
        """ The recursive implementation that finds the source files.
            This function returns all headers and source files encountered.
            If you only need the source files then post process the result.
            It is a precondition that realpath actually is a realpath.
            
            This is the uncached version - normally called via _required_files_cached.
        """
        if not processed:
            processed = set()
        if self.args.verbose >= 7:
            print("Hunter::_required_files_impl. Finding header deps for ", realpath)

        # Don't try and collapse these lines.
        # We don't want todo as a handle to the headerdeps.process object.
        todo = list(self.headerdeps.process(realpath))

        # One of the magic flags is SOURCE.  If that was present, add to the
        # file list.
        if self.args.allow_magic_source_in_header or compiletools.utils.issource(realpath):
            todo.extend(self._extractSOURCE(realpath))

        # The header deps and magic flags have been parsed at this point so it
        # is now safe to mark the realpath as processed.
        processed.add(realpath)

        # Note that the implied source file of an actual source file is itself
        implied = compiletools.utils.implied_source(realpath)
        if implied:
            todo.append(implied)
            todo.extend(self.headerdeps.process(implied))

        todo = [f for f in compiletools.utils.ordered_unique(todo) if f not in processed]
        while todo:
            if self.args.verbose >= 9:
                print(
                    "Hunter::_required_files_impl. ", realpath, " remaining todo:", todo
                )
            morefiles = []
            for nextfile in todo:
                morefiles.extend(self._required_files_impl_uncached(nextfile, processed))
            todo = [f for f in compiletools.utils.ordered_unique(morefiles) if f not in processed]

        if self.args.verbose >= 9:
            print("Hunter::_required_files_impl. ", realpath, " Returning ", processed)
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
                if compiletools.utils.issource(filename)
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
        
        # Use macro-hash-aware caching for performance
        try:
            # Ensure magic flags are processed to get macro hash
            self.magicflags(filename)
            macro_hash = self.macro_hash(filename)
            
            if self.args.verbose >= 8:
                print(f"Hunter::required_files using cached lookup with macro_hash {macro_hash} for {filename}")
            
            return self._required_files_cached(realpath, macro_hash)
            
        except RuntimeError:
            # Fallback: macro hash not available (shouldn't happen in normal usage)
            if self.args.verbose >= 5:
                print(f"Hunter::required_files falling back to uncached for {filename} (macro hash not available)")
            return self._required_files_impl_uncached(realpath)

    @staticmethod
    def clear_cache():
        # print("Hunter::clear_cache")
        compiletools.wrappedos.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        # Clear class-level cache
        Hunter._magic_cache.clear()
        # Note: Cannot clear instance-level _parse_magic caches from static method
        # Each Hunter instance will retain its own cache until the instance is destroyed

    def clear_instance_cache(self):
        """Clear this instance's caches."""
        if hasattr(self, '_parse_magic'):
            self._parse_magic.cache_clear()
        if hasattr(self, '_required_files_cached'):
            self._required_files_cached.cache_clear()
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

    def macro_hash(self, filename):
        """Get final converged macro hash for the given file.
        
        Raises:
            RuntimeError: If parse() hasn't been called for this file yet
        """
        final_hash = self.magicparser.get_final_macro_hash(filename)
        if final_hash is None:
            raise RuntimeError(
                f"macro_hash() called for {filename} but parse() hasn't been called yet. "
                f"Call magicflags() first to process the file."
            )
        return final_hash

    def header_dependencies(self, source_filename):
        if self.args.verbose >= 8:
            print("Hunter asking for header dependencies for ", source_filename)
        return self.headerdeps.process(source_filename)

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
        self._hunted_sources = sorted(compiletools.wrappedos.realpath(src) for src in all_sources)


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
