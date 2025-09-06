import sys
import os
import re
from collections import defaultdict
from functools import lru_cache
import compiletools.utils

import compiletools.git_utils
import compiletools.headerdeps
import compiletools.wrappedos
import compiletools.configutils
import compiletools.apptools
import compiletools.compiler_macros
import compiletools.dirnamer
from compiletools.file_analyzer import create_file_analyzer
from compiletools.simple_preprocessor import SimplePreprocessor
from compiletools.apptools import cached_pkg_config



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

    def _compute_macro_hash(self, macros_dict=None):
        """Compute hash of macro state."""
        import hashlib
        
        # Use provided macros or current state
        if macros_dict is None:
            macros_dict = getattr(self, 'defined_macros', {})
        
        # Create deterministic hash of sorted macro definitions
        macro_items = sorted(macros_dict.items())
        macro_string = "|".join(f"{k}={v}" for k, v in macro_items)
        return hashlib.sha256(macro_string.encode('utf-8')).hexdigest()[:12]

    def get_final_macro_hash(self, filename):
        """Get the final converged macro hash for a specific file.
        
        Args:
            filename: The file path to get the macro hash for
            
        Returns:
            str: 12-character hash of the final macro state for this file,
                 or None if the file hasn't been processed yet
        """
        return self._final_macro_hashes.get(filename)

    @lru_cache(maxsize=None)
    def _get_file_analyzer_result(self, filename):
        """Get FileAnalysisResult for a file, using shared headerdeps cache.
        
        Args:
            filename: Path to file to analyze
            
        Returns:
            FileAnalysisResult: Analysis result for the file
        """
        max_read_size = getattr(self._args, 'max_file_read_size', 0)
        analyzer = create_file_analyzer(filename, max_read_size, self._args.verbose, cache=self._headerdeps.get_file_analyzer_cache())
        return analyzer.analyze()

    def __call__(self, filename):
        return self.parse(filename)


    def _handle_source(self, flag, magic_flag_data, filename, magic):
        """Handle SOURCE magic flag using structured data.

        Args:
            flag: The relative path from the SOURCE magic flag
            magic_flag_data: Dict with magic flag info from FileAnalysisResult.magic_flags
            filename: The file containing the magic flag
            magic: The magic flag name ('SOURCE')
        """
        assert isinstance(magic_flag_data, dict), f"magic_flag_data must be dict, got {type(magic_flag_data)}"

        # Determine the context file for path resolution
        context_file = magic_flag_data['source_file_context'] if 'source_file_context' in magic_flag_data and magic_flag_data['source_file_context'] else filename

        # Resolve SOURCE path relative to context file
        if os.path.isabs(flag):
            # Absolute path - use as-is
            newflag = compiletools.wrappedos.realpath(flag)
        else:
            # Relative path - resolve relative to context file's directory
            context_dir = compiletools.wrappedos.dirname(context_file)
            newflag = compiletools.wrappedos.realpath(os.path.join(context_dir, flag.strip()))

        if self._args.verbose >= 9:
            context_info = f", context_file={context_file}" if context_file != filename else ""
            print(f"SOURCE: flag={flag}{context_info} -> {newflag}")

        if not compiletools.wrappedos.isfile(newflag):
            raise IOError(
                f"{filename} specified {magic}='{newflag}' but it does not exist"
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
            cflags_raw = cached_pkg_config(pkg, "--cflags")
            
            # Replace -I flags with -isystem, but only when -I is a standalone flag
            # This helps the CppHeaderDeps avoid searching packages
            cflags = re.sub(r'-I(?=\s|/|$)', '-isystem', cflags_raw)
            
            libs = cached_pkg_config(pkg, "--libs")
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
        # First try to resolve as a system header using apptools
        if not os.path.isabs(flag):
            resolved_flag = compiletools.apptools.find_system_header(flag, self._args, verbose=self._args.verbose)
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

    def _process_magic_flag(self, magic, flag, flagsforfilename, magic_flag_data, filename):
        """Override to handle READMACROS in DirectMagicFlags only"""
        # Handle READMACROS specifically for DirectMagicFlags - don't add to output
        if magic == "READMACROS":
            self._handle_readmacros(flag, filename)
            return  # Don't call parent - READMACROS shouldn't appear in final output
        
        # Call parent implementation for all other magic flags
        super()._process_magic_flag(magic, flag, flagsforfilename, magic_flag_data, filename)
    
    

    def _parse(self, filename):
        if self._args.verbose >= 4:
            print("Parsing magic flags for " + filename)

        # We assume that headerdeps _always_ exist
        # before the magic flags are called.
        # When used in the "usual" fashion this is true.
        # However, it is possible to call directly so we must
        # ensure that the headerdeps exist manually.
        self._headerdeps.process(filename)

        # Both DirectMagicFlags and CppMagicFlags now use structured data approach
        flagsforfilename = defaultdict(list)
        
        file_analysis_data = self.get_structured_data(filename)
        
        for file_data in file_analysis_data:
            filepath = file_data['filepath']
            active_magic_flags = file_data['active_magic_flags']
            
            for magic_flag in active_magic_flags:
                magic = magic_flag['key']
                flag = magic_flag['value']
                # Pass magic_flag data and filepath for structured processing
                self._process_magic_flag(magic, flag, flagsforfilename, magic_flag, filepath)

        # Deduplicate all flags while preserving order
        for key in flagsforfilename:
            flagsforfilename[key] = compiletools.utils.ordered_unique(flagsforfilename[key])

        return flagsforfilename

    def _process_magic_flag(self, magic, flag, flagsforfilename, magic_flag_data, filename):
        """Process a single magic flag entry"""
        # If the magic was SOURCE then fix up the path in the flag
        if magic == "SOURCE":
            flag = self._handle_source(flag, magic_flag_data, filename, magic)

        # If the magic was INCLUDE then modify that into the equivalent CPPFLAGS, CFLAGS, and CXXFLAGS
        if magic == "INCLUDE":
            extrafff = self._handle_include(flag)
            for key, values in extrafff.items():
                for value in values:
                    flagsforfilename[key].append(value)

        # If the magic was PKG-CONFIG then call pkg-config
        if magic == "PKG-CONFIG":
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
        # Clear LRU caches
        compiletools.utils.cached_shlex_split.cache_clear()


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
        # Store final converged macro hashes by filename
        self._final_macro_hashes = {}
        # Store converged macro states by filename for verification
        if __debug__:
            self._verification_final_macro_hashes = {}

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

    def _extract_macros_from_magic_flags(self, magic_flags_result):
        """Extract -D macros from magic flag CPPFLAGS and CXXFLAGS."""
        # Create minimal args object with magic flag values
        class MagicFlagArgs:
            def __init__(self, magic_flags_result):
                self.CPPFLAGS = magic_flags_result.get('CPPFLAGS', [])
                self.CXXFLAGS = magic_flags_result.get('CXXFLAGS', [])
        
        temp_args = MagicFlagArgs(magic_flags_result)
        macros = compiletools.apptools.extract_command_line_macros(
            temp_args,
            flag_sources=['CPPFLAGS', 'CXXFLAGS'],
            include_compiler_macros=False,
            verbose=self._args.verbose
        )
        
        # Update defined_macros with extracted macros
        self.defined_macros.update(macros)
        
        if self._args.verbose >= 9:
            print(f"DirectMagicFlags: extracted {len(macros)} macros from magic flags: {macros}")

    def _extract_macros_from_file(self, filename):
        """Extract #define macros from a file using cached FileAnalyzer results."""
        try:
            # Check if we already have FileAnalyzer results for this file
            if hasattr(self, '_file_analyzer_results') and filename in self._file_analyzer_results:
                file_result = self._file_analyzer_results[filename]
            else:
                # Get FileAnalyzer results using cached method
                file_result = self._get_file_analyzer_result(filename)
                
                # Cache for potential reuse
                if not hasattr(self, '_file_analyzer_results'):
                    self._file_analyzer_results = {}
                self._file_analyzer_results[filename] = file_result
            
            # Extract macros directly from FileAnalyzer's structured defines data
            for define_info in file_result.defines:
                macro_name = define_info['name']
                macro_value = define_info['value'] if define_info['value'] is not None else "1"
                
                # Skip function-like macros for simplicity
                if not define_info['is_function_like']:
                    self.defined_macros[macro_name] = macro_value
                    if self._args.verbose >= 9:
                        print(f"DirectMagicFlags: extracted macro {macro_name} = {macro_value} from {filename}")
                        
        except Exception as e:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags warning: could not extract macros from {filename}: {e}")

    def get_structured_data(self, filename):
        """Override to handle DirectMagicFlags complex macro processing"""
        if self._args.verbose >= 4:
            print("DirectMagicFlags: Setting up structured data with macro processing")
        
        # Reset state for each parse
        self.defined_macros = {}
        self._explicit_macro_files = set()
        
        # Add macros from command-line CPPFLAGS and CXXFLAGS
        self._add_macros_from_command_line_flags()
        
        # Get compiler, platform, and architecture macros dynamically
        macros = compiletools.compiler_macros.get_compiler_macros(self._args.CXX, self._args.verbose)
        self.defined_macros.update(macros)
        
        # Get headers from headerdeps
        headers = self._headerdeps.process(filename)
        all_source_files = [filename] + headers
        
        # First pass: scan all files for READMACROS flags to collect explicit macro files
        if self._args.verbose >= 9:
            print(f"DirectMagicFlags: First pass - scanning {len(all_source_files)} files for READMACROS flags")
        
        for source_file in all_source_files:
            try:
                analysis_result = self._get_file_analyzer_result(source_file)
                
                # Look for READMACROS magic flags in structured data
                for magic_flag in analysis_result.magic_flags:
                    if magic_flag['key'] == "READMACROS":
                        self._handle_readmacros(magic_flag['value'], source_file)
            except Exception as e:
                if self._args.verbose >= 5:
                    print(f"DirectMagicFlags warning: could not scan {source_file} for READMACROS: {e}")
        
        # Extract macros from explicitly specified files BEFORE processing conditional compilation
        for macro_file in self._explicit_macro_files:
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: extracting macros from READMACROS file {macro_file}")
            self._extract_macros_from_file(macro_file)
        
        # Process files iteratively until no new macros are discovered
        previous_macros = {}
        max_iterations = 5
        iteration = 0
        
        while (set(previous_macros.keys()) != set(self.defined_macros.keys()) or iteration == 0) and iteration < max_iterations:
            previous_macros = self.defined_macros.copy()
            iteration += 1
            
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags::get_structured_data iteration {iteration}, known macros: {set(self.defined_macros.keys())}")
            
            # Process each file and extract macros from defines
            all_files = list(self._explicit_macro_files) + [filename] + [h for h in headers if h != filename]
            for fname in all_files:
                if self._args.verbose >= 9:
                    print("DirectMagicFlags::get_structured_data processing " + fname)
                try:
                    file_result = self._get_file_analyzer_result(fname)
                    
                    # Process conditional compilation to get active lines
                    preprocessor = SimplePreprocessor(self.defined_macros, verbose=self._args.verbose)
                    active_lines = preprocessor.process_structured(file_result)
                    active_line_set = set(active_lines)
                    
                    # Extract macros from active magic flag CPPFLAGS and CXXFLAGS
                    active_magic_flags = []
                    for magic_flag in file_result.magic_flags:
                        if magic_flag['line_num'] in active_line_set:
                            active_magic_flags.append(magic_flag)
                    
                    # Extract -D macros from active magic flags
                    if active_magic_flags:
                        magic_flags_result = defaultdict(list)
                        for magic_flag in active_magic_flags:
                            magic_flags_result[magic_flag['key']].append(magic_flag['value'])
                        self._extract_macros_from_magic_flags(magic_flags_result)
                    
                    # Extract macros from active #define directives and update immediately
                    # so they're available for subsequent files in this iteration
                    for define_info in file_result.defines:
                        if define_info['line_num'] in active_line_set:
                            macro_name = define_info['name']
                            macro_value = define_info['value'] if define_info['value'] is not None else "1"
                            self.defined_macros[macro_name] = macro_value
                            if self._args.verbose >= 9:
                                print(f"DirectMagicFlags: extracted macro {macro_name} = {macro_value} from {fname}")
                            
                except Exception as e:
                    if self._args.verbose >= 5:
                        print(f"DirectMagicFlags warning: could not process {fname} for macro extraction: {e}")
        
        # CONVERGENCE ACHIEVED: Store final macro hash for this filename
        final_macro_hash = self._compute_macro_hash(self.defined_macros)
        
        # Check if we've already processed this file - verify consistency
        if filename in self._final_macro_hashes:
            previous_hash = self._final_macro_hashes[filename]
            if final_macro_hash == previous_hash:
                # Consistent reprocessing - this is OK, just return without updating storage
                if self._args.verbose >= 7:
                    print(f"DirectMagicFlags: Reprocessed {filename} - macro hash consistent: {final_macro_hash}")
                # Note: returning early without building result - this might need adjustment
                # TODO: Determine if we need to rebuild result or can skip entirely
                pass  # Continue to rebuild result for now
            else:
                # Hash mismatch - this indicates a real bug
                assert False, (
                    f"BUG: Reprocessing {filename} produced different macro hash! "
                    f"Previous: {previous_hash}, Current: {final_macro_hash}. "
                    f"Convergence should be deterministic."
                )
        
        self._final_macro_hashes[filename] = final_macro_hash
        
        if self._args.verbose >= 5:
            print(f"DirectMagicFlags: Final converged macro hash for {filename}: {final_macro_hash}")
        
        # Store the converged macro state for verification
        if __debug__:
            self._verification_final_macro_hashes[filename] = self.defined_macros.copy()
        
        # Now return structured data with converged macro state
        result = []
        
        # Get all files to process (main file + headers)
        all_files = list(self._explicit_macro_files) + [filename] + [h for h in headers if h != filename]
        
        for filepath in all_files:
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: Final processing of structured magic flags for {filepath}")
            
            try:
                # Get FileAnalysisResult using cached method
                file_result = self._get_file_analyzer_result(filepath)
                
                # Get active line numbers using final converged macro state
                preprocessor = SimplePreprocessor(self.defined_macros, verbose=self._args.verbose)
                active_lines = preprocessor.process_structured(file_result)
                active_line_set = set(active_lines)
                
                # Filter magic flags by active lines
                active_magic_flags = []
                for magic_flag in file_result.magic_flags:
                    if magic_flag['line_num'] in active_line_set:
                        active_magic_flags.append(magic_flag)
                
                if self._args.verbose >= 9:
                    print(f"DirectMagicFlags: Found {len(file_result.magic_flags)} total magic flags, {len(active_magic_flags)} active after preprocessing")
                
                result.append({
                    'filepath': filepath,
                    'active_magic_flags': active_magic_flags
                })
                
            except Exception as e:
                if self._args.verbose >= 5:
                    print(f"DirectMagicFlags warning: could not process structured data for {filepath}: {e}")
                # Add empty data for this file
                result.append({
                    'filepath': filepath,
                    'active_magic_flags': []
                })
        
        # Verify macro state hasn't been corrupted during final processing
        if __debug__:
            self._verify_macro_state_unchanged("get_structured_data() completion", filename)
        
        return result

    # DirectMagicFlags doesn't implement readfile() - it uses structured data processing only
    # All processing goes through get_structured_data() -> FileAnalyzerResults

    def _verify_macro_state_unchanged(self, context="unknown", filename=None):
        """Verify that the macro state hasn't changed after convergence for a specific file."""
        if __debug__:
            if filename and filename in self._verification_final_macro_hashes:
                current_hash = self._compute_macro_hash(self.defined_macros)
                converged_macro_state = self._verification_final_macro_hashes[filename]
                converged_hash = self._compute_macro_hash(converged_macro_state)
                assert current_hash == converged_hash, (
                    f"MACRO STATE CORRUPTION DETECTED in {context} for file {filename}!\n"
                    f"Converged hash: {converged_hash}\n"
                    f"Current hash:   {current_hash}\n"
                    f"Converged macros: {set(converged_macro_state.keys())}\n"
                    f"Current macros:   {set(self.defined_macros.keys())}"
                )

    def parse(self, filename):
        # Leverage FileAnalyzer data for optimization and validation
        result = self._parse(filename)
        
        # Verify macro state hasn't been corrupted during parsing
        if __debug__:
            self._verify_macro_state_unchanged("parse() completion", filename)
        
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
        # Reuse preprocessor from CppHeaderDeps if available to avoid duplicate instances
        if hasattr(headerdeps, 'preprocessor') and headerdeps.__class__.__name__ == 'CppHeaderDeps':
            self.preprocessor = headerdeps.preprocessor
        else:
            self.preprocessor = compiletools.preprocessor.PreProcessor(args)
    
    def _strip_sz(self, sz_str, chars: str = ' \t\r\n'):
        """Custom strip implementation for StringZilla.Str using character set operations."""
        start = sz_str.find_first_not_of(chars)
        if start == -1:
            return sz_str[0:0]  # Return empty Str if all characters are whitespace
        end = sz_str.find_last_not_of(chars)
        return sz_str[start:end + 1]

    def _readfile(self, filename):
        """Preprocess the given filename but leave comments"""
        extraargs = "-C -E"
        return self.preprocessor.process(
            realpath=filename, extraargs=extraargs, redirect_stderr_to_stdout=True
        )

    def get_structured_data(self, filename):
        """Get magic flags directly from preprocessed text using StringZilla SIMD operations"""
        import stringzilla as sz
        
        if self._args.verbose >= 4:
            print("CppMagicFlags: Getting structured data from preprocessed C++ output")

        # Get preprocessed text (existing logic)
        preprocessed_text = self._readfile(filename)
        
        # Store preprocessed text for SOURCE resolution
        self._preprocessed_text = preprocessed_text
        
        # Use StringZilla for SIMD-optimized processing with source file context tracking
        text = sz.Str(preprocessed_text)
        magic_flags = []
        
        line_num = 0
        current_source_file = None
        
        # Split into lines using StringZilla (SIMD optimized)
        for line_sz in text.split('\n'):
            line_str = str(line_sz)
            
            # Track current source file from preprocessor # directives
            if line_str.startswith('# ') and '"' in line_str:
                # Extract source file from preprocessor line directive
                import re
                match = re.search(r'# \d+ "([^"]+)"', line_str)
                if match:
                    current_source_file = match.group(1)
            
            # Use StringZilla to find "//#" pattern with SIMD search
            magic_start = line_sz.find('//#')
            if magic_start >= 0:
                # Extract everything after "//#" using StringZilla slicing
                after_marker = line_sz[magic_start + 3:]  # Skip "//#"
                
                # Find the "=" separator using StringZilla SIMD find
                eq_pos = after_marker.find('=')
                if eq_pos >= 0:
                    # Extract key and value using StringZilla character set operations
                    key_slice = after_marker[:eq_pos]
                    value_slice = after_marker[eq_pos + 1:]
                    
                    # Use StringZilla strip for better performance
                    key_trimmed = self._strip_sz(key_slice)
                    value_trimmed = self._strip_sz(value_slice)
                    
                    key_part = str(key_trimmed)
                    value_part = str(value_trimmed)
                    
                    if key_part:  # Only add if key is non-empty
                        magic_flag = {
                            'line_num': line_num,
                            'byte_pos': -1,  # Not used for CppMagicFlags
                            'full_line': line_str,
                            'key': key_part,
                            'value': value_part
                        }
                        
                        # Add source file context for SOURCE resolution
                        if current_source_file:
                            magic_flag['source_file_context'] = current_source_file
                        
                        magic_flags.append(magic_flag)
            
            line_num += 1
        
        if self._args.verbose >= 9:
            print(f"CppMagicFlags: Found {len(magic_flags)} magic flags in preprocessed output")
        
        return [{
            'filepath': filename,
            'active_magic_flags': magic_flags
        }]

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
