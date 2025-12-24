import os
import functools
import stringzilla as sz
from pathlib import Path
from typing import List

# At deep verbose levels pprint is used
from pprint import pprint

import compiletools.wrappedos
import compiletools.apptools
import compiletools.utils
from compiletools.utils import split_command_cached
import compiletools.tree as tree
import compiletools.preprocessor
import compiletools.compiler_macros
import compiletools.file_analyzer
from compiletools.preprocessing_cache import get_or_compute_preprocessing, MacroState, MacroDict, MacroCacheKey
from compiletools.file_analyzer import analyze_file, set_analyzer_args

# Cache for filtered include lists: (content_hash, macro_cache_key) -> list of includes
_include_list_cache = {}



def create(args):
    """HeaderDeps Factory"""
    classname = args.headerdeps.title() + "HeaderDeps"
    if args.verbose >= 4:
        print("Creating " + classname + " to process header dependencies.")
    depsclass = globals()[classname]
    depsobject = depsclass(args)
    return depsobject


def add_arguments(cap):
    """Add the command line arguments that the HeaderDeps classes require"""
    compiletools.apptools.add_common_arguments(cap)
    alldepscls = [st[:-10].lower() for st in dict(globals()) if st.endswith("HeaderDeps")]
    cap.add(
        "--headerdeps",
        choices=alldepscls,
        default="direct",
        help="Methodology for determining header dependencies",
    )
    cap.add(
        "--max-file-read-size",
        type=int,
        default=0,
        help="Maximum bytes to read from files (0 = entire file)",
    )

    # Add FileAnalyzer arguments for file reading strategy control
    compiletools.file_analyzer.FileAnalyzer.add_arguments(cap)


class HeaderDepsBase(object):
    """Implement the common functionality of the different header
    searching classes.  This really should be an abstract base class.
    """

    def __init__(self, args):
        self.args = args
        # Set global analyzer args for FileAnalyzer caching
        set_analyzer_args(args)

    def _process_impl(self, realpath: str, macro_cache_key: MacroCacheKey) -> List[str]:
        """Derived classes implement this function"""
        raise NotImplementedError

    def process(self, filename: str, macro_cache_key: MacroCacheKey) -> List[str]:
        """Return an ordered list of header dependencies for the given file.

        Args:
            filename: File to analyze for dependencies
            macro_cache_key: Frozenset of (macro_name, macro_value) tuples for cache key.
                            Pass frozenset() for no variable macros (core only).

        Notes:
                - The list preserves discovery order (depth-first and preprocessor-respecting
                    for DirectHeaderDeps; cpp -MM derived for CppHeaderDeps) while removing
                    duplicates.
                - The input file itself is excluded from the returned list.
                - System include paths are excluded by default unless configured otherwise.
        """
        realpath = compiletools.wrappedos.realpath(filename)
        try:
            result = self._process_impl(realpath, macro_cache_key)
        except IOError:
            # If there was any error the first time around, an error correcting removal would have occured
            # So strangely, the best thing to do is simply try again
            result = None

        if not result:
            result = self._process_impl(realpath, macro_cache_key)

        return result

    def _extract_isystem_paths_from_flags(self, flag_value):
        """Extract -isystem paths from command-line flags using proper shell parsing.
        
        This replaces the regex-based approach to properly handle quoted paths with spaces.
        Shared utility method for both DirectHeaderDeps and CppHeaderDeps.
        """
        if not flag_value:
            return []
            
        isystem_paths = []
        
        # Split the flag string into individual tokens using shell parsing
        try:
            tokens = split_command_cached(flag_value)
        except ValueError:
            # Fall back to simple split if shlex fails
            tokens = flag_value.split()
        
        # Process tokens to find -isystem flags
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            if token == '-isystem':
                # Next token should be the path
                if i + 1 < len(tokens):
                    isystem_paths.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
            elif token.startswith('-isystem'):
                # -isystempath format (though this is unusual)
                path = token[8:]
                if path:  # Make sure it's not just "-isystem"
                    isystem_paths.append(path)
                i += 1
            else:
                i += 1
                
        return isystem_paths

    def _extract_include_paths_from_flags(self, flag_value):
        """Extract -I include paths from command-line flags using proper shell parsing.
        
        This replaces the regex-based approach to properly handle quoted paths with spaces.
        Shared utility method for both DirectHeaderDeps and CppHeaderDeps.
        """
        if not flag_value:
            return []

        include_paths = []

        # Handle both string and list inputs from configargparse
        if isinstance(flag_value, list):
            # Join list elements into a single string
            flag_string = ' '.join(flag_value)
        else:
            flag_string = flag_value

        # Split the flag string into individual tokens using shell parsing
        try:
            tokens = split_command_cached(flag_string)
        except ValueError:
            # Fall back to simple split if shlex fails
            tokens = flag_string.split()
        
        # Process tokens to find -I flags
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            if token == '-I':
                # Next token should be the path
                if i + 1 < len(tokens):
                    include_paths.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
            elif token.startswith('-I'):
                # -Ipath format
                path = token[2:]
                if path:  # Make sure it's not just "-I"
                    include_paths.append(path)
                i += 1
            else:
                i += 1
                
        return include_paths

    @staticmethod
    def clear_cache():
        # print("HeaderDepsBase::clear_cache")
        import compiletools.apptools
        compiletools.apptools.clear_cache()
        _include_list_cache.clear()
        DirectHeaderDeps.clear_cache()
        CppHeaderDeps.clear_cache()


class DirectHeaderDeps(HeaderDepsBase):
    """Create a tree structure that shows the header include tree"""

    def __init__(self, args):
        HeaderDepsBase.__init__(self, args)

        # Keep track of ancestor paths so that we can do header cycle detection
        self.ancestor_paths = []

        # Cache core macros (computed once, reused for all files)
        self._core_macros = None
        self._includes = None

        # Initialize includes and macros
        self._initialize_includes_and_macros({})

    def _initialize_includes_and_macros(self, variable_macros: MacroDict):
        """Initialize include paths and macro definitions from compile flags.

        Caches core macros and includes on first call for reuse across all files.

        Args:
            variable_macros: Dict of variable macros (from file #defines).
                            Pass {} for no variable macros (core only).
        """
        # Cache core macros and includes - they never change for this instance
        if self._core_macros is None:
            # Grab the include paths from the CPPFLAGS
            # By default, exclude system paths
            # TODO: include system paths if the user sets (the currently nonexistent) "use-system" flag

            # Use proper shell parsing instead of regex to handle quoted paths with spaces
            self._includes = self._extract_include_paths_from_flags(self.args.CPPFLAGS)

            if self.args.verbose >= 3:
                print("Includes=" + str(self._includes))

            # Extract macro definitions from command line flags and compiler
            # Both are static for the lifetime of this instance (core macros)
            import compiletools.apptools
            raw_macros = compiletools.apptools.extract_command_line_macros(
                self.args,
                flag_sources=['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'],
                include_compiler_macros=True,
                verbose=self.args.verbose
            )
            # Convert all keys and values to StringZilla.Str and place in core
            self._core_macros = {sz.Str(k): sz.Str(v) for k, v in raw_macros.items()}

        # Set includes and reset macro state (reuse cached core, use provided variable dict)
        self.includes = self._includes
        self.defined_macros = MacroState(self._core_macros, variable_macros)

    @functools.lru_cache(maxsize=None)
    def _search_project_includes(self, include: sz.Str):
        """Internal use.  Find the given include file in the project include paths"""
        for inc_dir in self.includes:
            trialpath_sz = compiletools.wrappedos.join_sz(sz.Str(inc_dir), include)
            if compiletools.wrappedos.isfile_sz(trialpath_sz):
                return str(compiletools.wrappedos.realpath_sz(trialpath_sz))

        # TODO: Try system include paths if the user sets (the currently nonexistent) "use-system" flag
        return None

    @functools.lru_cache(maxsize=None)
    def _find_include(self, include: sz.Str, cwd: str):
        """Internal use.  Find the given include file.
        Start at the current working directory then try the project includes
        """
        # Check if the file is referable from the current working directory
        # if that guess doesn't exist then try all the include paths
        trialpath_sz = compiletools.wrappedos.join_sz(sz.Str(cwd), include)
        if compiletools.wrappedos.isfile_sz(trialpath_sz):
            return str(compiletools.wrappedos.realpath_sz(trialpath_sz))

        return self._search_project_includes(include)

    @functools.lru_cache(maxsize=None)
    def _process_impl(self, realpath, initial_macro_key):
        """Process file with macro state in cache key.

        Args:
            realpath: File to process
            initial_macro_key: Frozenset cache key of initial macro state (part of cache key)

        Note: Assumes caller has already initialized macro state via process()
        """
        if self.args.verbose >= 9:
            print(f"DirectHeaderDeps::_process_impl: {realpath} (macro_key={initial_macro_key})")

        results_order = []
        results_set = set()
        self._process_impl_recursive(realpath, results_order, results_set)
        if realpath in results_order:
            results_order.remove(realpath)
        return results_order

    def process(self, filename: str, macro_cache_key: MacroCacheKey) -> List[str]:
        """Override to compute and pass initial macro cache key.

        Args:
            filename: File to analyze for dependencies
            macro_cache_key: Frozenset of (macro_name, macro_value) tuples for cache key.
                            Pass frozenset() for no variable macros (core only).
        """
        realpath = compiletools.wrappedos.realpath(filename)

        # Convert cache key frozenset to MacroDict for MacroState initialization
        # Ensure all keys/values are sz.Str for consistency (MacroState uses stringzilla exclusively)
        variable_macros: MacroDict = {
            sz.Str(k): sz.Str(v)
            for k, v in macro_cache_key
        }

        # Initialize with variable macros to ensure consistent initial_macro_key for LRU cache
        self._initialize_includes_and_macros(variable_macros)
        initial_macro_key = self.defined_macros.get_cache_key()

        try:
            result = self._process_impl(realpath, initial_macro_key)
        except IOError:
            result = None

        if not result:
            result = self._process_impl(realpath, initial_macro_key)

        return result

    def _create_include_list(self, realpath):
        """Internal use. Create the list of includes for the given file

        Caches filtered include lists to avoid redundant processing when the same
        file is encountered with the same macro state across different traversals.
        """
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(realpath)

        # Check cache using content_hash + macro state
        # Try to use cached key if available to avoid recomputation
        macro_key = self.defined_macros.get_cached_key_if_available()
        if macro_key is None:
            macro_key = self.defined_macros.get_cache_key()

        cache_key = (content_hash, macro_key)

        if cache_key in _include_list_cache:
            cached_includes, cached_file_defines = _include_list_cache[cache_key]
            # Reconstruct updated_macros from current input + file's defines
            self.defined_macros = self.defined_macros.with_updates(cached_file_defines)
            return cached_includes

        # Cache miss - compute the include list
        analysis_result = analyze_file(content_hash)

        if self.args.verbose >= 9 and analysis_result.include_positions:
            print(f"DirectHeaderDeps::analyze - FileAnalyzer pre-found {len(analysis_result.include_positions)} includes in {realpath}")

        # Use unified preprocessing cache for active line detection
        result = get_or_compute_preprocessing(analysis_result, self.defined_macros, self.args.verbose)
        active_line_set = set(result.active_lines)

        # Extract active includes from FileAnalyzer's structured results
        include_list = [
            sz.Str(inc['filename'])
            for inc in analysis_result.includes
            if inc['line_num'] in active_line_set and not inc['is_commented']
        ]

        # Replace macro state instead of mutating it.
        # result.updated_macros comes from preprocessing cache and may already have
        # its cache key computed. By replacing instead of mutating via update(),
        # we preserve the cached key and avoid recomputation during traversal.
        self.defined_macros = result.updated_macros

        # Cache the result - store file_defines instead of updated_macros
        # This prevents macro pollution across different traversal contexts
        _include_list_cache[cache_key] = (include_list, result.file_defines)

        return include_list

    def _generate_tree_impl(self, realpath, node=None):
        """Return a tree that describes the header includes
        The node is passed recursively, however the original caller
        does not need to pass it in.
        """

        if self.args.verbose >= 4:
            print("DirectHeaderDeps::_generate_tree_impl: ", realpath)

        if node is None:
            node = tree.tree()

        # Stop cycles
        if realpath in self.ancestor_paths:
            if self.args.verbose >= 7:
                print(
                    "DirectHeaderDeps::_generate_tree_impl is breaking the cycle on ",
                    realpath,
                )
            return node
        self.ancestor_paths.append(realpath)

        # This next line is how you create the node in the tree
        node[realpath]

        if self.args.verbose >= 6:
            print("DirectHeaderDeps inserted: " + realpath)
            pprint(tree.dicts(node))

        cwd = os.path.dirname(realpath)
        for include in self._create_include_list(realpath):
            trialpath = self._find_include(include, cwd)
            if trialpath:
                self._generate_tree_impl(trialpath, node[realpath])
                if self.args.verbose >= 5:
                    print("DirectHeaderDeps building tree: ")
                    pprint(tree.dicts(node))

        self.ancestor_paths.pop()
        return node

    def generatetree(self, filename):
        """Returns the tree of include files"""
        self.ancestor_paths = []
        realpath = compiletools.wrappedos.realpath(filename)
        return self._generate_tree_impl(realpath)

    def _process_impl_recursive(self, realpath, results_order, results_set):
        if realpath in results_set:
            return
        results_set.add(realpath)

        # Process includes first (depth-first traversal to match preprocessor)
        cwd = compiletools.wrappedos.dirname(realpath)
        for include in self._create_include_list(realpath):
            trialpath = self._find_include(include, cwd)
            if trialpath and trialpath not in results_set:
                if self.args.verbose >= 9:
                    print(
                        "DirectHeaderDeps::_process_impl_recursive about to follow ",
                        trialpath,
                    )
                self._process_impl_recursive(trialpath, results_order, results_set)

        # Add current file after processing includes (depth-first order)
        results_order.append(realpath)

    def clear_instance_cache(self):
        """Clear this instance's _process_impl cache."""
        self._process_impl.cache_clear()
        if self.args.verbose >= 5:
            print("DirectHeaderDeps::clear_instance_cache completed")
    
    @staticmethod
    def clear_cache():
        # print("DirectHeaderDeps::clear_cache")
        DirectHeaderDeps._search_project_includes.cache_clear()
        DirectHeaderDeps._find_include.cache_clear()
        # Note: Cannot clear instance-level _process_impl caches from static method
        # Each DirectHeaderDeps instance will retain its cache until destroyed


class CppHeaderDeps(HeaderDepsBase):
    """Using the C Pre Processor, create the list of headers that the given file depends upon."""

    def __init__(self, args):
        HeaderDepsBase.__init__(self, args)
        self.preprocessor = compiletools.preprocessor.PreProcessor(args)

    def process(self, filename: str, macro_cache_key: MacroCacheKey) -> List[str]:
        """Process using cpp -MM (raises error if macro_cache_key non-empty).

        Args:
            filename: File to analyze for dependencies
            macro_cache_key: Must be empty frozenset(). CppHeaderDeps does not support
                            file-level macro contexts (compiler determines macros).

        Raises:
            NotImplementedError: If macro_cache_key is non-empty
        """
        if macro_cache_key:
            raise NotImplementedError(
                "CppHeaderDeps does not support file-level macro contexts. "
                "Use --headerdeps=direct (default) for macro-aware dependency analysis."
            )
        realpath = compiletools.wrappedos.realpath(filename)
        return self._process_impl(realpath, macro_cache_key)

    def _process_impl(self, realpath: str, macro_cache_key: MacroCacheKey) -> List[str]:
        """Use the -MM option to the compiler to generate the list of dependencies
        If you supply a header file rather than a source file then
        a dummy, blank, source file will be transparently provided
        and the supplied header file will be included into the dummy source file.
        """
        # By default, exclude system paths
        # TODO: include system paths if the user sets (the currently nonexistent) "use-system" flag
        
        # Use proper shell parsing instead of regex to handle quoted paths with spaces
        isystem_paths = self._extract_isystem_paths_from_flags(self.args.CPPFLAGS)
        system_paths = tuple(item for pth in isystem_paths for item in (pth, compiletools.wrappedos.realpath(pth)))
        realpath_obj = Path(realpath)
        if any(realpath_obj.is_relative_to(syspath) for syspath in system_paths):
            return []

        output = self.preprocessor.process(realpath, extraargs="-MM")

        # output will be something like
        # test_direct_include.o: tests/test_direct_include.cpp
        # tests/get_numbers.hpp tests/get_double.hpp tests/get_int.hpp
        # We need to throw away the object file and only keep the dependency
        # list
        deplist = output.split(":")[1]

        # Strip non-space whitespace, remove any backslashes, and remove any empty strings
        # Also remove the initially given realpath and /dev/null from the list
        # Use a set to inherently remove any redundancies
        # Use realpath to get rid of  // and ../../ etc in paths (similar to normpath) and
        # to get the full path even to files in the current working directory
        return compiletools.utils.ordered_unique(
            [
                compiletools.wrappedos.realpath(x)
                for x in deplist.split()
                if x.strip("\\\t\n\r") and x not in [realpath, "/dev/null"] and not any(Path(x).is_relative_to(syspath) for syspath in system_paths)
            ]
        )

    @staticmethod
    def clear_cache():
        # print("CppHeaderDeps::clear_cache")
        pass
