import os
import re
import functools

# At deep verbose levels pprint is used
from pprint import pprint

import compiletools.wrappedos
import compiletools.apptools
import compiletools.tree as tree
import compiletools.preprocessor
import compiletools.compiler_macros
from compiletools.simple_preprocessor import SimplePreprocessor
from compiletools.file_analyzer import create_file_analyzer
import compiletools.timing



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
        """Return the set of dependencies for a given filename"""
        realpath = compiletools.wrappedos.realpath(filename)
        with compiletools.timing.time_operation(f"header_dependency_analysis_{os.path.basename(filename)}"):
            try:
                result = self._process_impl(realpath)
            except IOError:
                # If there was any error the first time around, an error correcting removal would have occured
                # So strangely, the best thing to do is simply try again
                result = None

            if not result:
                result = self._process_impl(realpath)

        return result

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
        #pat = re.compile(r"-(?:I|isystem)\s+([\S]+)")
        # Handle both -I src and -Isrc formats
        pat = re.compile(r"-(?:I)(?:\s+|)([^\s]+)")
        self.includes = pat.findall(self.args.CPPFLAGS)

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

    def _process_conditional_compilation(self, text, directive_positions):
        """Process conditional compilation directives and return only active sections"""
        preprocessor = SimplePreprocessor(self.defined_macros, self.args.verbose)
        
        # Always pass FileAnalyzer's pre-computed directive positions for maximum performance
        processed_text = preprocessor.process(text, directive_positions)
        
        # Update our defined_macros dict with any changes from the preprocessor
        # This allows macro accumulation within a single dependency analysis
        self.defined_macros.clear()
        self.defined_macros.update(preprocessor.macros)
        
        return processed_text

    def _create_include_list(self, realpath):
        """Internal use. Create the list of includes for the given file"""
        # Import modules outside timing block
        import compiletools.dirnamer
        
        with compiletools.timing.time_operation(f"include_analysis_{os.path.basename(realpath)}"):
            max_read_size = getattr(self.args, 'max_file_read_size', 0)
            
            # Use FileAnalyzer for efficient file reading and pattern detection  
            # Note: create_file_analyzer() handles StringZilla/Legacy fallback internally
            
            with compiletools.timing.time_operation(f"file_read_{os.path.basename(realpath)}"):
                analyzer = create_file_analyzer(realpath, max_read_size, self.args.verbose, cache=self.file_analyzer_cache)
                analysis_result = analyzer.analyze()
                text = analysis_result.text
                
                # Potential optimization: FileAnalyzer already found include_positions  
                # We could potentially use these to optimize regex processing later
                if self.args.verbose >= 9 and analysis_result.include_positions:
                    print(f"DirectHeaderDeps::analyze - FileAnalyzer pre-found {len(analysis_result.include_positions)} includes in {realpath}")

            # Process conditional compilation - this updates self.defined_macros as it encounters #define
            with compiletools.timing.time_operation(f"conditional_compilation_{os.path.basename(realpath)}"):
                # Pass FileAnalyzer's pre-computed directive positions for optimization
                processed_text = self._process_conditional_compilation(
                    text, 
                    analysis_result.directive_positions
                )

            # The pattern is intended to match all include statements but
            # not the ones with either C or C++ commented out.
            with compiletools.timing.time_operation(f"pattern_matching_{os.path.basename(realpath)}"):
                import re
                # Optimization: Use FileAnalyzer's pre-computed include positions when available
                includes = []
                if analysis_result.include_positions:
                    # Extract include filenames from positions that survived conditional compilation
                    original_lines = text.split('\n')
                    for pos in analysis_result.include_positions:
                        line_num = text[:pos].count('\n')
                        if line_num < len(original_lines):
                            include_line = original_lines[line_num]
                            # Check if this include line survived preprocessing
                            if include_line.strip() in processed_text:
                                # Extract filename using simpler regex on single line
                                match = re.search(r'#include[\s]*["<][\s]*([\S]*)[\s]*[">]', include_line)
                                if match:
                                    includes.append(match.group(1))
                
                # Fallback to full text search if no includes found or no position data
                if not includes:
                    pat = re.compile(
                        r'/\*.*?\*/|//.*?$|^[\s]*#include[\s]*["<][\s]*([\S]*)[\s]*[">]',
                        re.MULTILINE | re.DOTALL,
                    )
                    includes = [group for group in pat.findall(processed_text) if group]
                
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

    def _process_impl_recursive(self, realpath, results):
        results.add(realpath)
        cwd = compiletools.wrappedos.dirname(realpath)
        for include in self._create_include_list(realpath):
            trialpath = self._find_include(include, cwd)
            if trialpath and trialpath not in results:
                if self.args.verbose >= 9:
                    print(
                        "DirectHeaderDeps::_process_impl_recursive about to follow ",
                        trialpath,
                    )
                self._process_impl_recursive(trialpath, results)

    # TODO: Stop writing to the same cache as CPPHeaderDeps.
    # Because the magic flags rely on the .deps cache, this hack was put in
    # place.
    # NOTE: Cache removed due to macro state dependency - cache was keyed only on file path
    # but results depend on self.defined_macros which can change between calls
    def _process_impl(self, realpath):
        if self.args.verbose >= 9:
            print("DirectHeaderDeps::_process_impl: " + realpath)

        # Reset macro state at the beginning of each top-level dependency analysis
        # This ensures consistent results across multiple calls while allowing
        # macro accumulation within a single analysis
        self._initialize_includes_and_macros()

        results = set()
        self._process_impl_recursive(realpath, results)
        results.discard(realpath)
        return results


    @staticmethod
    def clear_cache():
        # print("DirectHeaderDeps::clear_cache")
        DirectHeaderDeps._search_project_includes.cache_clear()
        DirectHeaderDeps._find_include.cache_clear()


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
        # Handle both -isystem path and -isystempath formats
        regex = r"-isystem(?:\s+|)([^\s]+)"  # Regex to find paths following -isystem
        system_paths = re.findall(regex, self.args.CPPFLAGS)
        system_paths = tuple(item for pth in system_paths for item in (pth, compiletools.wrappedos.realpath(pth)))
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
