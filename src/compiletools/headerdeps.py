import os
import functools

# At deep verbose levels pprint is used
from pprint import pprint

import compiletools.wrappedos
import compiletools.apptools
import compiletools.utils
from compiletools.utils import cached_shlex_split
import compiletools.tree as tree
import compiletools.preprocessor
import compiletools.compiler_macros
from compiletools.simple_preprocessor import SimplePreprocessor
from compiletools.file_analyzer import create_file_analyzer



def create(args, file_analyzer_cache=None):
    """HeaderDeps Factory"""
    classname = args.headerdeps.title() + "HeaderDeps"
    if args.verbose >= 4:
        print("Creating " + classname + " to process header dependencies.")
    depsclass = globals()[classname]
    depsobject = depsclass(args, file_analyzer_cache=file_analyzer_cache)
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


class HeaderDepsBase(object):
    """Implement the common functionality of the different header
    searching classes.  This really should be an abstract base class.
    """

    def __init__(self, args):
        self.args = args

    def _process_impl(self, realpath):
        """Derived classes implement this function"""
        raise NotImplementedError

    def process(self, filename):
        """Return an ordered list of header dependencies for the given file.

        Notes:
                - The list preserves discovery order (depth-first and preprocessor-respecting
                    for DirectHeaderDeps; cpp -MM derived for CppHeaderDeps) while removing
                    duplicates.
                - The input file itself is excluded from the returned list.
                - System include paths are excluded by default unless configured otherwise.
        """
        realpath = compiletools.wrappedos.realpath(filename)
        try:
            result = self._process_impl(realpath)
        except IOError:
            # If there was any error the first time around, an error correcting removal would have occured
            # So strangely, the best thing to do is simply try again
            result = None

        if not result:
            result = self._process_impl(realpath)

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
            tokens = cached_shlex_split(flag_value)
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
        
        # Split the flag string into individual tokens using shell parsing
        try:
            tokens = cached_shlex_split(flag_value)
        except ValueError:
            # Fall back to simple split if shlex fails
            tokens = flag_value.split()
        
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
        DirectHeaderDeps.clear_cache()
        CppHeaderDeps.clear_cache()


class DirectHeaderDeps(HeaderDepsBase):
    """Create a tree structure that shows the header include tree"""

    def __init__(self, args, file_analyzer_cache=None):
        HeaderDepsBase.__init__(self, args)

        # Keep track of ancestor paths so that we can do header cycle detection
        self.ancestor_paths = []
    
        # Use provided file analyzer cache or create one if none provided (for backward compatibility)
        if file_analyzer_cache is not None:
            self.file_analyzer_cache = file_analyzer_cache
        else:
            # Fallback for backward compatibility - create cache internally
            import compiletools.dirnamer
            cache_type = compiletools.dirnamer.get_cache_type(args=args)
            if cache_type:
                from compiletools.file_analyzer_cache import create_cache
                self.file_analyzer_cache = create_cache(cache_type)
            else:
                self.file_analyzer_cache = None
        
        # Initialize includes and macros
        self._initialize_includes_and_macros()
    
    def get_file_analyzer_cache(self):
        """Get the shared FileAnalyzer cache for reuse by other components."""
        return self.file_analyzer_cache
    
    def _initialize_includes_and_macros(self):
        """Initialize include paths and macro definitions from compile flags."""
        # Grab the include paths from the CPPFLAGS
        # By default, exclude system paths
        # TODO: include system paths if the user sets (the currently nonexistent) "use-system" flag
        
        # Use proper shell parsing instead of regex to handle quoted paths with spaces
        self.includes = self._extract_include_paths_from_flags(self.args.CPPFLAGS)

        if self.args.verbose >= 3:
            print("Includes=" + str(self.includes))
            
        # Extract macro definitions from command line flags and compiler
        import compiletools.apptools
        self.defined_macros = compiletools.apptools.extract_command_line_macros(
            self.args, 
            flag_sources=['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'],
            include_compiler_macros=True,
            verbose=self.args.verbose
        )

    @functools.lru_cache(maxsize=None)
    def _search_project_includes(self, include):
        """Internal use.  Find the given include file in the project include paths"""
        for inc_dir in self.includes:
            trialpath = os.path.join(inc_dir, include)
            if compiletools.wrappedos.isfile(trialpath):
                return compiletools.wrappedos.realpath(trialpath)

        # else:
        #    TODO: Try system include paths if the user sets (the currently nonexistent) "use-system" flag
        #    Only get here if the include file cannot be found anywhere
        #    raise FileNotFoundError("DirectHeaderDeps could not determine the location of ",include)
        return None

    @functools.lru_cache(maxsize=None)
    def _find_include(self, include, cwd):
        """Internal use.  Find the given include file.
        Start at the current working directory then try the project includes
        """
        # Check if the file is referable from the current working directory
        # if that guess doesn't exist then try all the include paths
        trialpath = os.path.join(cwd, include)
        if compiletools.wrappedos.isfile(trialpath):
            return compiletools.wrappedos.realpath(trialpath)
        else:
            return self._search_project_includes(include)


    def _create_include_list(self, realpath):
        """Internal use. Create the list of includes for the given file"""
        max_read_size = getattr(self.args, 'max_file_read_size', 0)
        
        # Use FileAnalyzer for efficient file reading and pattern detection  
        analyzer = create_file_analyzer(realpath, max_read_size, self.args.verbose, cache=self.file_analyzer_cache)
        analysis_result = analyzer.analyze()
        
        if self.args.verbose >= 9 and analysis_result.include_positions:
            print(f"DirectHeaderDeps::analyze - FileAnalyzer pre-found {len(analysis_result.include_positions)} includes in {realpath}")

        # Use SimplePreprocessor to get active line numbers (proper architecture)
        preprocessor = SimplePreprocessor(self.defined_macros, verbose=self.args.verbose)
        active_lines = preprocessor.process_structured(analysis_result)
        active_line_set = set(active_lines)
        
        # Update our macro state from preprocessor results
        self.defined_macros.update(preprocessor.macros)
        
        # Extract active includes from FileAnalyzer's structured results (no regex needed!)
        includes = []
        for include_info in analysis_result.includes:
            # When working with cached data that lacks line content, active_line_set may be empty
            # In that case, trust the cached structured data directly (oracle cache stores filtered results)
            if not analysis_result.lines and not active_line_set:
                # Use cached data directly - oracle cache already filtered during original analysis
                if not include_info['is_commented']:
                    includes.append(include_info['filename'])
            else:
                # Normal case: only include if this line is active after preprocessing and not commented
                if include_info['line_num'] in active_line_set and not include_info['is_commented']:
                    includes.append(include_info['filename'])
        
        return includes

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

    @functools.lru_cache(maxsize=None)
    def _process_impl(self, realpath):
        if self.args.verbose >= 9:
            print("DirectHeaderDeps::_process_impl: " + realpath)

        # Reset macro state at the beginning of each top-level dependency analysis
        # This ensures consistent results across multiple calls while allowing
        # macro accumulation within a single analysis
        self._initialize_includes_and_macros()

        results_order = []  # Maintain order of discovery
        results_set = set()  # Fast lookup for cycle detection
        self._process_impl_recursive(realpath, results_order, results_set)
        # Remove the original file from results while preserving order
        if realpath in results_order:
            results_order.remove(realpath)
        return results_order


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

    def __init__(self, args, file_analyzer_cache=None):
        HeaderDepsBase.__init__(self, args)
        self.preprocessor = compiletools.preprocessor.PreProcessor(args)
        
        # CppHeaderDeps doesn't use file analyzers directly, so cache is not needed
        # But we store it for API consistency
        self.file_analyzer_cache = file_analyzer_cache
    
    def get_file_analyzer_cache(self):
        """Get the shared FileAnalyzer cache for reuse by other components."""
        return self.file_analyzer_cache

    def _process_impl(self, realpath):
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
        if realpath.startswith(system_paths):
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
                if x.strip("\\\t\n\r") and x not in [realpath, "/dev/null"] and not x.startswith(system_paths)
            ]
        )

    @staticmethod
    def clear_cache():
        # print("CppHeaderDeps::clear_cache")
        pass
