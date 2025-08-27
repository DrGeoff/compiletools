import sys
import os
import subprocess
import re
from collections import defaultdict
import compiletools.utils

import compiletools.git_utils
import compiletools.headerdeps
import compiletools.wrappedos
import compiletools.configutils
import compiletools.apptools
import compiletools.compiler_macros
import compiletools.dirnamer
from compiletools.file_analyzer import create_file_analyzer
import compiletools.timing


def create(args, headerdeps):
    """MagicFlags Factory"""
    classname = args.magic.title() + "MagicFlags"
    if args.verbose >= 4:
        print("Creating " + classname + " to process magicflags.")
    magicclass = globals()[classname]
    magicobject = magicclass(args, headerdeps)
    return magicobject


def add_arguments(cap, variant=None):
    """Add the command line arguments that the MagicFlags classes require"""
    compiletools.apptools.add_common_arguments(cap, variant=variant)
    compiletools.preprocessor.PreProcessor.add_arguments(cap)
    alldepscls = [
        st[:-10].lower() for st in dict(globals()) if st.endswith("MagicFlags")
    ]
    cap.add(
        "--magic",
        choices=alldepscls,
        default="direct",
        help="Methodology for reading file when processing magic flags",
    )
    cap.add(
        "--max-file-read-size",
        type=int,
        default=0,
        help="Maximum bytes to read from files (0 = entire file)",
    )


class MagicFlagsBase:
    """A magic flag in a file is anything that starts
    with a //# and ends with an =
    E.g., //#key=value1 value2

    Note that a magic flag is a C++ comment.

    This class is a map of filenames
    to the map of all magic flags for that file.
    Each magic flag has a list of values preserving order.
    E.g., { '/somepath/libs/base/somefile.hpp':
               {'CPPFLAGS':['-D', 'MYMACRO', '-D', 'MACRO2'],
                'CXXFLAGS':['-fsomeoption'],
                'LDFLAGS':['-lsomelib']}}
    This function will extract all the magics flags from the given
    source (and all its included headers).
    source_filename must be an absolute path
    """

    def __init__(self, args, headerdeps):
        self._args = args
        self._headerdeps = headerdeps
        
        # Always use the file analyzer cache from HeaderDeps
        self.file_analyzer_cache = self._headerdeps.get_file_analyzer_cache()

        # The magic pattern is //#key=value with whitespace ignored
        self.magicpattern = re.compile(
            r"^[\s]*//#([\S]*?)[\s]*=[\s]*(.*)", re.MULTILINE
        )

    def readfile(self, filename):
        """Derived classes implement this method"""
        raise NotImplementedError

    def __call__(self, filename):
        with compiletools.timing.time_file_operation("magic_flags_analysis", filename):
            return self.parse(filename)

    def _handle_source(self, flag, text, filename, magic):
        # Find the include before the //#SOURCE=
        result = re.search(
            r'# \d.* "(/\S*?)".*?//#SOURCE\s*=\s*' + flag, text, re.DOTALL
        )
        # Now adjust the flag to include the full path
        newflag = compiletools.wrappedos.realpath(
            os.path.join(compiletools.wrappedos.dirname(result.group(1)), flag.strip())
        )
        if self._args.verbose >= 9:
            print(
                " ".join(
                    [
                        "Adjusting source magicflag from flag=",
                        flag,
                        "to",
                        newflag,
                    ]
                )
            )

        if not compiletools.wrappedos.isfile(newflag):
            raise IOError(
                filename
                + " specified "
                + magic
                + "='"
                + newflag
                + "' but it does not exist"
            )

        return newflag

    def _handle_include(self, flag):
        flagsforfilename = {}
        flagsforfilename.setdefault("CPPFLAGS", []).append("-I " + flag)
        flagsforfilename.setdefault("CFLAGS", []).append("-I " + flag)
        flagsforfilename.setdefault("CXXFLAGS", []).append("-I " + flag)
        if self._args.verbose >= 9:
            print("Added -I {} to CPPFLAGS, CFLAGS, and CXXFLAGS".format(flag))
        return flagsforfilename

    def _handle_pkg_config(self, flag):
        flagsforfilename = defaultdict(list)
        for pkg in flag.split():
            # TODO: when we move to python 3.7, use text=True rather than universal_newlines=True and capture_output=True,
            with compiletools.timing.time_operation(f"pkg_config_cflags_{pkg}"):
                cflags_raw = subprocess.run(
                    ["pkg-config", "--cflags", pkg],
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                ).stdout.rstrip()
                
                # Replace -I flags with -isystem, but only when -I is a standalone flag
                # This helps the CppHeaderDeps avoid searching packages
                cflags = re.sub(r'-I(?=\s|/|$)', '-isystem', cflags_raw)
            
            with compiletools.timing.time_operation(f"pkg_config_libs_{pkg}"):
                libs = subprocess.run(
                    ["pkg-config", "--libs", pkg],
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                ).stdout.rstrip()
            flagsforfilename["CPPFLAGS"].append(cflags)
            flagsforfilename["CFLAGS"].append(cflags)
            flagsforfilename["CXXFLAGS"].append(cflags)
            flagsforfilename["LDFLAGS"].append(libs)
            if self._args.verbose >= 9:
                print(f"Magic PKG-CONFIG = {pkg}:")
                print(f"\tadded {cflags} to CPPFLAGS, CFLAGS, and CXXFLAGS")
                print(f"\tadded {libs} to LDFLAGS")
        return flagsforfilename

    def _handle_readmacros(self, flag, source_filename):
        """Handle READMACROS magic flag by adding file to explicit macro processing list"""
        # First try to resolve as a system header
        if not os.path.isabs(flag):
            # Try to find in system include paths first
            resolved_flag = self._find_system_header(flag)
            if not resolved_flag:
                # Fall back to resolving relative to source file directory
                source_dir = compiletools.wrappedos.dirname(source_filename)
                resolved_flag = compiletools.wrappedos.realpath(os.path.join(source_dir, flag))
        else:
            resolved_flag = compiletools.wrappedos.realpath(flag)
        
        # Check if file exists
        if not compiletools.wrappedos.isfile(resolved_flag):
            raise IOError(
                f"{source_filename} specified READMACROS='{flag}' but resolved file '{resolved_flag}' does not exist"
            )
        
        # Add to explicit macro files set
        self._explicit_macro_files.add(resolved_flag)
        
        if self._args.verbose >= 5:
            print(f"READMACROS: Will process '{resolved_flag}' for macro extraction (from {source_filename})")

    def _process_magic_flag(self, magic, flag, flagsforfilename, text, filename):
        """Override to handle READMACROS in DirectMagicFlags only"""
        # Handle READMACROS specifically for DirectMagicFlags - don't add to output
        if magic == "READMACROS":
            self._handle_readmacros(flag, filename)
            return  # Don't call parent - READMACROS shouldn't appear in final output
        
        # Call parent implementation for all other magic flags
        super()._process_magic_flag(magic, flag, flagsforfilename, text, filename)

    def _parse(self, filename):
        if self._args.verbose >= 4:
            print("Parsing magic flags for " + filename)

        # We assume that headerdeps _always_ exist
        # before the magic flags are called.
        # When used in the "usual" fashion this is true.
        # However, it is possible to call directly so we must
        # ensure that the headerdeps exist manually.
        with compiletools.timing.time_file_operation("magic_flags_headerdeps", filename):
            self._headerdeps.process(filename)

        with compiletools.timing.time_file_operation("magic_flags_readfile", filename):
            text = self.readfile(filename)
        
        with compiletools.timing.time_file_operation("magic_flags_parsing", filename):
            flagsforfilename = defaultdict(list)

            for match in self.magicpattern.finditer(text):
                magic, flag = match.groups()
                self._process_magic_flag(magic, flag, flagsforfilename, text, filename)

        # Deduplicate all flags while preserving order
        for key in flagsforfilename:
            flagsforfilename[key] = compiletools.utils.ordered_unique(flagsforfilename[key])

        return flagsforfilename

    def _process_magic_flag(self, magic, flag, flagsforfilename, text, filename):
        """Process a single magic flag entry"""
        # If the magic was SOURCE then fix up the path in the flag
        if magic == "SOURCE":
            flag = self._handle_source(flag, text, filename, magic)

        # If the magic was INCLUDE then modify that into the equivalent CPPFLAGS, CFLAGS, and CXXFLAGS
        if magic == "INCLUDE":
            with compiletools.timing.time_operation(f"magic_flags_include_handling_{flag}"):
                extrafff = self._handle_include(flag)
                for key, values in extrafff.items():
                    for value in values:
                        flagsforfilename[key].append(value)

        # If the magic was PKG-CONFIG then call pkg-config
        if magic == "PKG-CONFIG":
            with compiletools.timing.time_operation(f"magic_flags_pkgconfig_{flag}"):
                extrafff = self._handle_pkg_config(flag)
                for key, values in extrafff.items():
                    for value in values:
                        flagsforfilename[key].append(value)

        flagsforfilename[magic].append(flag)
        if self._args.verbose >= 5:
            print(
                "Using magic flag {0}={1} extracted from {2}".format(
                    magic, flag, filename
                )
            )

    @staticmethod
    def clear_cache():
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        compiletools.wrappedos.clear_cache()
        compiletools.apptools.clear_cache()
        DirectMagicFlags.clear_cache()
        CppMagicFlags.clear_cache()


class DirectMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        # Track defined macros with values during processing (unified storage)
        self.defined_macros = {}
        # Store FileAnalyzer results for potential optimization in parsing
        self._file_analyzer_results = {}
        # Cache for system include paths
        self._system_include_paths = None
        # Track files specified by PARSEMACROS magic flags
        self._explicit_macro_files = set()

    def _add_macros_from_command_line_flags(self):
        """Extract -D macros from command-line CPPFLAGS and CXXFLAGS and add them to defined_macros"""
        import compiletools.apptools
        
        # Extract macros from CPPFLAGS and CXXFLAGS only (excluding CFLAGS to match original behavior)
        macros = compiletools.apptools.extract_command_line_macros(
            self._args,
            flag_sources=['CPPFLAGS', 'CXXFLAGS'],
            include_compiler_macros=False,  # Don't include compiler macros here, done separately
            verbose=self._args.verbose
        )
        
        # Direct assignment - no copying overhead
        self.defined_macros.update(macros)

    def _get_system_include_paths(self):
        """Extract -I/-isystem include paths from command-line flags"""
        if self._args.verbose >= 9:
            print(f"DEBUG: _get_system_include_paths called")
        if self._system_include_paths is not None:
            return self._system_include_paths
            
        include_paths = []
        
        # Extract from CPPFLAGS and CXXFLAGS  
        for flag_name in ['CPPFLAGS', 'CXXFLAGS']:
            flag_value = getattr(self._args, flag_name, '')
            if not flag_value:
                continue
                
            # Split the flag string into individual tokens
            import shlex
            try:
                tokens = shlex.split(flag_value)
            except ValueError:
                # Fall back to simple split if shlex fails
                tokens = flag_value.split()
            
            # Process tokens to find -I and -isystem flags
            i = 0
            while i < len(tokens):
                token = tokens[i]
                
                if token == '-I' or token == '-isystem':
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
                elif token.startswith('-isystem'):
                    # -isystempath format (though this is unusual)
                    path = token[8:]
                    if path:  # Make sure it's not just "-isystem"
                        include_paths.append(path)
                    i += 1
                else:
                    i += 1
            
        # Remove duplicates while preserving order
        seen = set()
        unique_paths = []
        for path in include_paths:
            if path not in seen:
                seen.add(path)
                unique_paths.append(path)
                
        self._system_include_paths = unique_paths
        
        if self._args.verbose >= 9 and unique_paths:
            print(f"DirectMagicFlags extracted system include paths: {unique_paths}")
            
        return self._system_include_paths

    def _find_system_header(self, include_name):
        """Find a system header in the -I/-isystem include paths"""
        for include_path in self._get_system_include_paths():
            candidate = os.path.join(include_path, include_name)
            if compiletools.wrappedos.isfile(candidate):
                return compiletools.wrappedos.realpath(candidate)
        return None

    def _extract_macros_from_file(self, filename):
        """Extract #define macros from a file using cached FileAnalyzer results"""
        try:
            max_read_size = getattr(self._args, 'max_file_read_size', 0)
            analyzer = create_file_analyzer(filename, max_read_size, self._args.verbose, cache=self.file_analyzer_cache)
            analysis_result = analyzer.analyze()
            
            # Use FileAnalyzer's pre-computed directive positions for efficiency
            define_positions = analysis_result.directive_positions.get("define", [])
            if not define_positions:
                return
                
            lines = analysis_result.text.split('\n')
            
            # Extract #define directives from known positions
            import re
            for pos in define_positions:
                # Find which line this position is on
                line_num = analysis_result.text[:pos].count('\n')
                if line_num < len(lines):
                    line = lines[line_num].strip()
                    
                    # Parse #define directive
                    match = re.match(r'^\s*#\s*define\s+(\w+)(?:\s+(.+?))?$', line)
                    if match:
                        macro_name = match.group(1)
                        macro_value = match.group(2)
                        
                        # Clean up the macro value
                        if macro_value:
                            macro_value = macro_value.strip()
                            # Remove trailing comments
                            if '//' in macro_value:
                                macro_value = macro_value.split('//')[0].strip()
                            if '/*' in macro_value:
                                macro_value = re.sub(r'/\*.*?\*/', '', macro_value).strip()
                        else:
                            macro_value = "1"  # Default value for macros without explicit values
                        
                        # Skip function-like macros for simplicity
                        if '(' not in macro_name:
                            self.defined_macros[macro_name] = macro_value
                            if self._args.verbose >= 9:
                                print(f"DirectMagicFlags: extracted macro {macro_name} = {macro_value} from {filename}")
                        
        except Exception as e:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags warning: could not extract macros from {filename}: {e}")

    def _get_system_headers_from_source(self, filename):
        """Extract system headers from a source file and find them in include paths
        
        Returns list of (include_name, resolved_path) tuples for system includes found
        """
        system_headers = []
        
        # Read the file to find #include directives
        try:
            max_read_size = getattr(self._args, 'max_file_read_size', 0)
            analyzer = create_file_analyzer(filename, max_read_size, self._args.verbose, cache=self.file_analyzer_cache)
            analysis_result = analyzer.analyze()
            
            # Look for #include <system_header> directives in the raw file content
            # Use angle brackets to identify system includes
            system_include_pattern = re.compile(r'^\s*#\s*include\s*<([^>]+)>', re.MULTILINE)
            for match in system_include_pattern.finditer(analysis_result.text):
                include_name = match.group(1)
                resolved_path = self._find_system_header(include_name)
                if resolved_path:
                    system_headers.append((include_name, resolved_path))
                    if self._args.verbose >= 9:
                        print(f"DirectMagicFlags found system header: {include_name} -> {resolved_path}")
                        
        except Exception as e:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags warning: could not scan {filename} for system headers: {e}")
                
        return system_headers

    def _process_conditional_compilation(self, text, directive_positions):
        """Process conditional compilation directives and return only active sections"""
        from compiletools.simple_preprocessor import SimplePreprocessor
        
        # Use our macro state directly for SimplePreprocessor
        preprocessor = SimplePreprocessor(self.defined_macros, verbose=self._args.verbose)
        
        # Always pass FileAnalyzer's pre-computed directive positions for maximum performance
        processed_text = preprocessor.process(text, directive_positions)
        
        # Update our internal state from preprocessor results
        self.defined_macros.clear()
        self.defined_macros.update(preprocessor.macros)
        
        return processed_text

    def readfile(self, filename):
        """Read the first chunk of the file and all the headers it includes"""
        if self._args.verbose >= 9:
            print(f"DEBUG: DirectMagicFlags.readfile called with {filename}")
        # Reset defined macros for each new parse
        self.defined_macros = {}
        # Reset explicit macro files for each new parse
        self._explicit_macro_files = set()
        
        # Add macros from command-line CPPFLAGS and CXXFLAGS (e.g., from --append-CPPFLAGS/--append-CXXFLAGS)
        self._add_macros_from_command_line_flags()
        
        # Get compiler, platform, and architecture macros dynamically
        compiler = getattr(self._args, 'CXX', 'g++')
        macros = compiletools.compiler_macros.get_compiler_macros(compiler, self._args.verbose)
        self.defined_macros.update(macros)
        
        headers = self._headerdeps.process(filename)
        
        # First pass: scan all files for READMACROS flags to collect explicit macro files
        all_source_files = [filename] + headers
        if self._args.verbose >= 9:
            print(f"DirectMagicFlags: First pass - scanning {len(all_source_files)} files for READMACROS flags")
        for source_file in all_source_files:
            if self._args.verbose >= 9:
                print(f"First pass - scanning {source_file} for READMACROS flags")
            try:
                max_read_size = getattr(self._args, 'max_file_read_size', 0)
                analyzer = create_file_analyzer(source_file, max_read_size, self._args.verbose, cache=self.file_analyzer_cache)
                analysis_result = analyzer.analyze()
                
                # Look for READMACROS magic flags
                for match in self.magicpattern.finditer(analysis_result.text):
                    magic, flag = match.groups()
                    if magic == "READMACROS":
                        self._handle_readmacros(flag, source_file)
            except Exception as e:
                if self._args.verbose >= 5:
                    print(f"DirectMagicFlags warning: could not scan {source_file} for READMACROS: {e}")
        
        if self._args.verbose >= 5 and self._explicit_macro_files:
            print(f"DirectMagicFlags will process {len(self._explicit_macro_files)} files specified by READMACROS: {self._explicit_macro_files}")
        
        # CRITICAL: Extract macros from explicitly specified files BEFORE processing conditional compilation
        # This ensures macros are available for #if evaluation
        for macro_file in self._explicit_macro_files:
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: extracting macros from READMACROS file {macro_file}")
            self._extract_macros_from_file(macro_file)
        
        # Process files iteratively until no new macros are discovered
        # This handles cases where macros defined in one file affect conditional
        # compilation in other files
        previous_macros = {}
        max_iterations = 5  # Prevent infinite loops
        iteration = 0
        
        while set(previous_macros.keys()) != set(self.defined_macros.keys()) and iteration < max_iterations:
            previous_macros = self.defined_macros.copy()
            iteration += 1
            
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags::readfile iteration {iteration}, known macros: {set(self.defined_macros.keys())}")
            
            text = ""
            # Process files in preprocessor order to match CppMagicFlags:
            # 1. Explicit macro files first (from PARSEMACROS flags)
            # 2. Main file, then project headers (matching cpp -E behavior)
            all_files = list(self._explicit_macro_files) + [filename] + [h for h in headers if h != filename]
            for fname in all_files:
                if self._args.verbose >= 9:
                    print("DirectMagicFlags::readfile is processing " + fname)
                
                # To match the output of the C Pre Processor we insert
                # the filename before the text
                file_header = '# 1 "' + compiletools.wrappedos.realpath(fname) + '"\n'
                
                # Read file content using FileAnalyzer respecting max_file_read_size configuration
                max_read_size = getattr(self._args, 'max_file_read_size', 0)
                
                # Use FileAnalyzer with shared cache from DirectHeaderDeps
                # The cache automatically handles deduplication if DirectHeaderDeps already analyzed this file
                analyzer = create_file_analyzer(fname, max_read_size, self._args.verbose, cache=self.file_analyzer_cache)
                analysis_result = analyzer.analyze()
                file_content = analysis_result.text
                
                # Store FileAnalyzer results for potential optimization during parsing
                self._file_analyzer_results[fname] = analysis_result
                
                # Potential optimization: FileAnalyzer already found magic_positions
                # We could potentially use these to optimize regex processing later
                if self._args.verbose >= 9 and analysis_result.magic_positions:
                    print(f"DirectMagicFlags::readfile - FileAnalyzer pre-found {len(analysis_result.magic_positions)} magic flags in {fname}")
                
                # Process conditional compilation for this file
                # Pass FileAnalyzer's pre-computed directive positions for optimization
                
                processed_content = self._process_conditional_compilation(
                    file_content, 
                    analysis_result.directive_positions
                )
                
                text += file_header + processed_content + "\n"

        return text

    def parse(self, filename):
        # Leverage FileAnalyzer data for optimization and validation
        result = self._parse(filename)
        
        # Optimization: Validate results using FileAnalyzer pre-computed data
        if self._args.verbose >= 9:
            total_original_magic_flags = sum(len(analysis.magic_positions) 
                                           for analysis in self._file_analyzer_results.values())
            total_found_flags = sum(len(flags) for flags in result.values())
            if total_original_magic_flags > 0:
                print(f"DirectMagicFlags::parse - FileAnalyzer found {total_original_magic_flags} raw magic flags, "
                      f"after conditional compilation: {total_found_flags} active flags")
        
        return result

    @staticmethod
    def clear_cache():
        pass


class CppMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        self.preprocessor = compiletools.preprocessor.PreProcessor(args)

    def readfile(self, filename):
        """Preprocess the given filename but leave comments"""
        extraargs = "-C -E"
        return self.preprocessor.process(
            realpath=filename, extraargs=extraargs, redirect_stderr_to_stdout=True
        )

    def parse(self, filename):
        return self._parse(filename)

    @staticmethod
    def clear_cache():
        pass


class NullStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        print("{}: {}".format(self.adjust(realpath), str(magicflags)))


class PrettyStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        sys.stdout.write("\n{}".format(self.adjust(realpath)))
        try:
            for key in magicflags:
                sys.stdout.write("\n\t{}:".format(key))
                for flag in magicflags[key]:
                    sys.stdout.write(" {}".format(flag))
        except TypeError:
            sys.stdout.write("\n\tNone")


def main(argv=None):
    cap = compiletools.apptools.create_parser(
        "Parse a file and show the magicflags it exports", argv=argv
    )
    compiletools.headerdeps.add_arguments(cap)
    add_arguments(cap)
    cap.add("filename", help='File/s to extract magicflags from"', nargs="+")

    # Figure out what style classes are available and add them to the command
    # line options
    styles = [st[:-5].lower() for st in dict(globals()) if st.endswith("Style")]
    cap.add("--style", choices=styles, default="pretty", help="Output formatting style")

    args = compiletools.apptools.parseargs(cap, argv)
    headerdeps = compiletools.headerdeps.create(args)
    magicparser = create(args, headerdeps)

    styleclass = globals()[args.style.title() + "Style"]
    styleobject = styleclass(args)

    for fname in args.filename:
        realpath = compiletools.wrappedos.realpath(fname)
        styleobject(realpath, magicparser.parse(realpath))

    print()
    return 0
