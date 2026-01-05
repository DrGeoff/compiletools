import sys
import os
import re
import functools
from collections import defaultdict
from typing import Dict, List, Union
from types import SimpleNamespace
import argparse
import stringzilla as sz
import compiletools.utils

import compiletools.git_utils
import compiletools.headerdeps
import compiletools.wrappedos
import compiletools.configutils
import compiletools.apptools
import compiletools.compiler_macros
import compiletools.namer
from compiletools.preprocessing_cache import get_or_compute_preprocessing, MacroState
from compiletools.stringzilla_utils import strip_sz
from compiletools.file_analyzer import FileAnalysisResult

# Type aliases for clarity
MacroDict = Dict[sz.Str, sz.Str]
FlagsDict = Dict[sz.Str, List[sz.Str]]



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

    Magic Flag Dict Structure:
        Each magic flag is represented as a dict with the following fields:

        {
            'line_num': int,              # Line number in file (0-based)
            'key': stringzilla.Str,       # Magic flag key (e.g., 'LDFLAGS', 'CPPFLAGS')
            'value': stringzilla.Str,     # Magic flag value
            'full_line': stringzilla.Str, # Complete source line containing the magic flag
            'byte_pos': int,              # Byte position in original file
                                          # DirectMagicFlags: actual file position
                                          # CppMagicFlags: -1 (unavailable in preprocessed output)
            'source_file_context': str    # (Optional, CppMagicFlags only) Original source file
                                          # for preprocessed output. Used for SOURCE path resolution.
                                          # DirectMagicFlags: not present
        }
    """

    def __init__(self, args: argparse.Namespace, headerdeps: compiletools.headerdeps.HeaderDepsBase) -> None:
        self._args = args
        self._headerdeps = headerdeps

        # Set global analyzer args for FileAnalyzer caching
        from compiletools.file_analyzer import set_analyzer_args
        set_analyzer_args(args)

        # The magic pattern is //#key=value with whitespace ignored
        self.magicpattern = re.compile(
            r"^[\s]*//#([\S]*?)[\s]*=[\s]*(.*)", re.MULTILINE
        )

    def get_final_macro_state_key(self, filename: str):
        """Get the final converged macro state key for a specific file.

        Returns the frozenset cache key (variable macros only) for use in
        dependency caching. For object file naming, use get_final_macro_state_hash().

        Args:
            filename: The file path to get the macro state key for

        Returns:
            frozenset: Cache key of variable macros

        Raises:
            KeyError: If file hasn't been processed yet
        """
        abs_filename = compiletools.wrappedos.realpath(filename)
        macro_state = self._final_macro_states.get(abs_filename)
        if macro_state is None:
            raise KeyError(f"No macro state found for {filename} - file not processed")
        return macro_state.get_cache_key()

    def get_final_macro_state_hash(self, filename: str) -> str:
        """Get the full macro state hash (core + variable) for object file naming.

        This includes BOTH core macros (compiler built-ins + cmdline flags) AND
        variable macros (from file #defines). Different compilers or cmdline flags
        will produce different hashes, ensuring proper object file separation.

        Args:
            filename: The file path to get the full macro state hash for

        Returns:
            str: 16-character hex hash of full macro state (core + variable)

        Raises:
            KeyError: If file hasn't been processed yet
        """
        abs_filename = compiletools.wrappedos.realpath(filename)
        macro_state = self._final_macro_states.get(abs_filename)
        if macro_state is None:
            raise KeyError(f"No macro state found for {filename} - file not processed")
        return macro_state.get_hash(include_core=True)

    def _get_file_analyzer_result(self, filename: str) -> FileAnalysisResult:
        """Get FileAnalysisResult for a file, using module-level cache.

        Args:
            filename: Path to file to analyze

        Returns:
            FileAnalysisResult: Analysis result for the file
        """
        from compiletools.file_analyzer import analyze_file
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(filename)
        return analyze_file(content_hash)

    def __call__(self, filename: str) -> FlagsDict:
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
        context_file = magic_flag_data.get('source_file_context') or filename

        # Resolve SOURCE path relative to context file
        if compiletools.wrappedos.isabs_sz(flag):
            # Absolute path - use as-is
            newflag = compiletools.wrappedos.realpath_sz(flag)
        else:
            # Relative path - resolve relative to context file's directory
            context_dir = compiletools.wrappedos.dirname(context_file)
            joined_path = compiletools.wrappedos.join_sz(sz.Str(context_dir), strip_sz(flag))
            newflag = compiletools.wrappedos.realpath_sz(joined_path)

        if self._args.verbose >= 9:
            context_info = f", context_file={context_file}" if context_file != filename else ""
            print(f"SOURCE: flag={flag}{context_info} -> {newflag}")

        if not compiletools.wrappedos.isfile_sz(newflag):
            raise IOError(f"{filename} specified {magic}='{newflag}' but it does not exist")

        return newflag

    def _handle_include(self, flag):
        flagsforfilename = defaultdict(list)
        # Use canonical separate-token form for compatibility with deduplicate_compiler_flags
        flagsforfilename[sz.Str("CPPFLAGS")].extend([sz.Str("-I"), flag])
        flagsforfilename[sz.Str("CFLAGS")].extend([sz.Str("-I"), flag])
        flagsforfilename[sz.Str("CXXFLAGS")].extend([sz.Str("-I"), flag])
        if self._args.verbose >= 9:
            print("Added -I {} to CPPFLAGS, CFLAGS, and CXXFLAGS".format(flag))
        return flagsforfilename

    def _handle_pkg_config(self, flag):
        flagsforfilename = defaultdict(list)
        
        # Convert to string for splitting, as we need to iterate over packages
        flag_str = str(flag)

        for pkg in flag_str.split():
            # pkg is str. Call cached_pkg_config directly to avoid unnecessary sz conversions
            cflags_raw = compiletools.apptools.cached_pkg_config(pkg, "--cflags")
            
            # Use the shared filtering logic from apptools
            cflags_str = compiletools.apptools.filter_pkg_config_cflags(cflags_raw, self._args.verbose)
            cflags_list = compiletools.utils.split_command_cached_sz(sz.Str(cflags_str))
            
            libs_raw = compiletools.apptools.cached_pkg_config(pkg, "--libs")
            libs_list = compiletools.utils.split_command_cached_sz(sz.Str(libs_raw))

            # Add cflags to all C/C++ flag categories
            for key in (sz.Str("CPPFLAGS"), sz.Str("CFLAGS"), sz.Str("CXXFLAGS")):
                flagsforfilename[key].extend(cflags_list)
            flagsforfilename[sz.Str("LDFLAGS")].extend(libs_list)

            if self._args.verbose >= 9:
                print(f"Magic PKG-CONFIG = {pkg}:")
                print(f"\tadded {cflags_list} to CPPFLAGS, CFLAGS, and CXXFLAGS")
                print(f"\tadded {libs_list} to LDFLAGS")
        return flagsforfilename

    def _resolve_readmacros_path(self, flag, source_filename):
        """Resolve READMACROS flag to absolute path (pure path resolution logic).

        Args:
            flag: The flag value from READMACROS magic flag
            source_filename: The file containing the READMACROS flag

        Returns:
            str: Absolute path to the resolved file

        Raises:
            IOError: If resolved file doesn't exist
        """
        # Absolute path - use as-is
        if compiletools.wrappedos.isabs_sz(flag):
            resolved_flag = compiletools.wrappedos.realpath_sz(flag)
        else:
            # Try to resolve as a system header using apptools
            resolved_flag_str = compiletools.apptools.find_system_header(str(flag), self._args, verbose=self._args.verbose)
            if resolved_flag_str:
                resolved_flag = sz.Str(resolved_flag_str)
            else:
                # Fall back to resolving relative to source file directory
                source_dir = compiletools.wrappedos.dirname(source_filename)
                resolved_flag = compiletools.wrappedos.realpath_sz(compiletools.wrappedos.join_sz(sz.Str(source_dir), flag))

        # Check if file exists
        if not compiletools.wrappedos.isfile_sz(resolved_flag):
            raise IOError(f"{source_filename} specified READMACROS='{flag}' but resolved file '{resolved_flag}' does not exist")

        return str(resolved_flag)

    def _collect_explicit_macro_files(self, source_files: List[str]) -> set:
        """Scan files for READMACROS flags and return set of explicit macro files.

        Args:
            source_files: List of source files to scan

        Returns:
            set: Set of resolved paths (str) to files specified by READMACROS flags
        """
        explicit_files = set()

        for source_file in source_files:
            try:
                analysis_result = self._get_file_analyzer_result(source_file)

                for magic_flag in analysis_result.magic_flags:
                    if magic_flag['key'] == sz.Str("READMACROS"):
                        resolved_path = self._resolve_readmacros_path(magic_flag['value'], source_file)
                        explicit_files.add(resolved_path)

                        if self._args.verbose >= 5:
                            print(f"READMACROS: Will process '{resolved_path}' for macro extraction (from {source_file})")
            except Exception as e:
                if self._args.verbose >= 5:
                    print(f"DirectMagicFlags warning: could not scan {source_file} for READMACROS: {e}")

        return explicit_files

    def _handle_readmacros(self, flag, source_filename):
        """Handle READMACROS magic flag by adding file to explicit macro processing list"""
        resolved_flag = self._resolve_readmacros_path(flag, source_filename)

        # Add to explicit macro files set (store as str for consistency with filename/headers)
        self._explicit_macro_files.add(resolved_flag)

    def _parse(self, filename):
        if self._args.verbose >= 4:
            print("Parsing magic flags for " + filename)

        # We assume that headerdeps _always_ exist
        # before the magic flags are called.
        # When used in the "usual" fashion this is true.
        # However, it is possible to call directly so we must
        # ensure that the headerdeps exist manually.
        # Pass empty frozenset since we haven't computed macros for this file yet
        self._headerdeps.process(filename, frozenset())

        # Both DirectMagicFlags and CppMagicFlags now use structured data approach
        from compiletools.global_hash_registry import get_filepath_by_hash

        flagsforfilename = defaultdict(list)

        file_analysis_data = self.get_structured_data(filename)

        for file_data in file_analysis_data:
            content_hash = file_data['content_hash']
            filepath = get_filepath_by_hash(content_hash)
            active_magic_flags = file_data['active_magic_flags']

            for magic_flag in active_magic_flags:
                magic = magic_flag['key']
                flag = magic_flag['value']
                # Pass magic_flag data and filepath for structured processing
                self._process_magic_flag(magic, flag, flagsforfilename, magic_flag, filepath)

        # Merge deprecated LINKFLAGS into LDFLAGS before deduplication
        if sz.Str("LINKFLAGS") in flagsforfilename:
            flagsforfilename[sz.Str("LDFLAGS")].extend(flagsforfilename[sz.Str("LINKFLAGS")])
            del flagsforfilename[sz.Str("LINKFLAGS")]

        # Deduplicate all flags while preserving order, with smart compiler flag handling
        for key in flagsforfilename:
            flagsforfilename[key] = compiletools.utils.deduplicate_compiler_flags(flagsforfilename[key])

        return flagsforfilename

    def _extend_flags_from_dict(self, flagsforfilename, extra_flags_dict):
        """Helper to extend flags from a dict of flag lists."""
        for key, values in extra_flags_dict.items():
            flagsforfilename[key].extend(values)

    def _process_magic_flag(self, magic, flag, flagsforfilename, magic_flag_data, filename):
        """Process a single magic flag entry"""
        # READMACROS is handled during DirectMagicFlags first pass, don't add to output
        if magic == sz.Str("READMACROS"):
            return

        # If the magic was SOURCE then fix up the path in the flag
        if magic == sz.Str("SOURCE"):
            flag = self._handle_source(flag, magic_flag_data, filename, magic)

        # If the magic was INCLUDE then modify that into the equivalent CPPFLAGS, CFLAGS, and CXXFLAGS
        if magic == sz.Str("INCLUDE"):
            self._extend_flags_from_dict(flagsforfilename, self._handle_include(flag))
            # INCLUDE generates flags for other keys, but also falls through to add to INCLUDE key

        # If the magic was PKG-CONFIG then call pkg-config
        if magic == sz.Str("PKG-CONFIG"):
            self._extend_flags_from_dict(flagsforfilename, self._handle_pkg_config(flag))
            # PKG-CONFIG generates flags for other keys AND adds itself to PKG-CONFIG key

        # Split flag string into individual flags - all magic flags can contain multiple values
        individual_flags = compiletools.utils.split_command_cached_sz(flag)
        flagsforfilename[magic].extend(individual_flags)
        if self._args.verbose >= 5:
            print("Using magic flag {0}={1} extracted from {2}".format(magic, flag, filename))

    @staticmethod
    def clear_cache():
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        compiletools.wrappedos.clear_cache()
        compiletools.apptools.clear_cache()
        DirectMagicFlags.clear_cache()
        CppMagicFlags.clear_cache()
        # Clear LRU caches
        compiletools.utils.split_command_cached.cache_clear()
        compiletools.utils.split_command_cached_sz.cache_clear()
        # Clear FileAnalyzer module-level cache
        from compiletools.file_analyzer import analyze_file
        analyze_file.cache_clear()


class DirectMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        # Create namer instance for dependency hash computation
        self._namer = compiletools.namer.Namer(args)
        # Compute initial macro state once (compiler built-ins + command-line macros)
        # This is computed once and reused via copy() to avoid redundant initialization
        self._initial_macro_state = self._initialize_macro_state()
        # Track defined macros with values during processing (MacroState with core + variable)
        self.defined_macros = self._initial_macro_state.copy()
        # Track files specified by READMACROS magic flags
        self._explicit_macro_files = set()
        # Store final converged MacroState objects by filename
        self._final_macro_states = {}
        # Cache structured data results by (file_hash, input_macro_key, deps_hash) to avoid redundant convergence
        # deps_hash: XOR of dependency file content hashes (headers + READMACROS)
        # Cached result stores content_hash (not filepath) - current paths resolved via global hash registry
        self._structured_data_cache = {}

    def _initialize_macro_state(self) -> MacroState:
        """Initialize MacroState with command-line and compiler macros as core.

        Returns:
            MacroState: Initialized with core (compiler + cmdline) and empty variable macros
        """
        core_macros = {}

        # Get compiler built-in macros - these are the stable base (~378 macros)
        compiler_macros = compiletools.compiler_macros.get_compiler_macros(self._args.CXX, self._args.verbose)
        core_macros.update({sz.Str(k): sz.Str(v) for k, v in compiler_macros.items()})

        # Add command-line macros to core - they're also static for the entire build
        cmd_macros = compiletools.apptools.extract_command_line_macros(
            self._args,
            flag_sources=['CPPFLAGS', 'CXXFLAGS'],
            include_compiler_macros=False,
            verbose=self._args.verbose
        )
        core_macros.update({sz.Str(k): sz.Str(v) for k, v in cmd_macros.items()})

        # Create MacroState with core macros, empty variable macros
        return MacroState(core_macros, {})

    def _extract_macros_from_magic_flags(self, magic_flags_result):
        """Extract -D macros from magic flag CPPFLAGS and CXXFLAGS."""
        # Create minimal args object with magic flag values
        flag_sources = [sz.Str('CPPFLAGS'), sz.Str('CXXFLAGS')]
        temp_args = SimpleNamespace(
            CPPFLAGS=magic_flags_result.get(flag_sources[0], []),
            CXXFLAGS=magic_flags_result.get(flag_sources[1], [])
        )
        macros = compiletools.apptools.extract_command_line_macros_sz(
            temp_args,
            flag_sources_sz=flag_sources,
            verbose=self._args.verbose
        )

        # Wrap dict in MacroState for update (use empty core since these are variable macros)
        from compiletools.preprocessing_cache import MacroState
        macro_state = MacroState(core={}, variable=macros)
        self.defined_macros.update(macro_state)


    @functools.lru_cache(maxsize=None)
    def _compute_file_processing_result(self, fname: str, macro_key):
        """Pure function: compute file processing result without mutating state.

        Cacheable by (fname, macro_key) to avoid reprocessing shared headers.
        Uses frozenset macro_key as cache key since it's hashable and more efficient.

        NOTE: This method accesses self.defined_macros to get the actual MacroState.
        The macro_key parameter is only used as a cache key for lru_cache.

        Args:
            fname: File path to process
            macro_key: Frozenset cache key of current macro state (from MacroState.get_cache_key())

        Returns:
            Tuple of (active_magic_flags, extracted_variable_macros_dict, cppflags_macros, cxxflags_macros)
            or None if file cannot be processed
        """
        try:
            file_result = self._get_file_analyzer_result(fname)
        except Exception as e:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags warning: could not process {fname} for macro extraction: {e}")
            return None

        # Process conditional compilation to get active lines using current macro state
        result = get_or_compute_preprocessing(file_result, self.defined_macros, self._args.verbose)
        active_line_set = set(result.active_lines)

        # Extract macros from active magic flag CPPFLAGS and CXXFLAGS
        active_magic_flags = [
            magic_flag for magic_flag in file_result.magic_flags
            if magic_flag['line_num'] in active_line_set
        ]

        # Collect macros from active magic flags for caching
        cppflags_macros = []
        cxxflags_macros = []
        if active_magic_flags:
            for magic_flag in active_magic_flags:
                key = magic_flag['key']
                value = magic_flag['value']
                if key == sz.Str('CPPFLAGS') or key == sz.Str('CXXFLAGS'):
                    # Extract -D macros using StringZilla operations
                    from compiletools.stringzilla_utils import parse_d_flags_sz
                    for macro_name, macro_value in parse_d_flags_sz(value):
                        if key == sz.Str('CPPFLAGS'):
                            cppflags_macros.append((macro_name, macro_value))
                        else:
                            cxxflags_macros.append((macro_name, macro_value))

        # Extract variable macros from active #define directives
        extracted_variable_macros = {}
        for define_info in file_result.defines:
            if define_info['line_num'] not in active_line_set:
                continue
            if define_info['is_function_like']:
                continue

            macro_name = define_info['name']
            macro_value = define_info['value'] if define_info['value'] is not None else sz.Str("1")
            extracted_variable_macros[macro_name] = macro_value

        return (active_magic_flags, extracted_variable_macros, cppflags_macros, cxxflags_macros)

    def _process_file_for_macros(self, fname: str, macro_key=None) -> None:
        """Process a single file to extract macros and active magic flags (mutates state).

        Updates self.defined_macros and self._stored_active_magic_flags based on
        conditional compilation with current macro state. Uses caching to avoid
        reprocessing the same file with the same macro state.

        Args:
            fname: File path to process
            macro_key: Optional pre-computed cache key for current macro state.
                      If None, will compute from self.defined_macros.
        """
        # Get cache key (frozenset) - reuse if provided to avoid redundant computation
        if macro_key is None:
            macro_key = self.defined_macros.get_cache_key()

        # Use cached computation - pass key, function accesses self.defined_macros
        cached_result = self._compute_file_processing_result(fname, macro_key)

        if cached_result is None:
            return

        active_magic_flags, extracted_variable_macros, cppflags_macros, cxxflags_macros = cached_result

        # Store active magic flags for this file to avoid redundant final pass
        self._stored_active_magic_flags[fname] = active_magic_flags

        # Apply extracted macros from magic flags to state
        for macro_name, macro_value in cppflags_macros + cxxflags_macros:
            self.defined_macros[macro_name] = macro_value

        # Apply extracted variable macros to state
        for macro_name, macro_value in extracted_variable_macros.items():
            self.defined_macros[macro_name] = macro_value

    def _extract_macros_from_file(self, filename):
        """Extract #define macros from a file (unconditionally, no preprocessor evaluation)."""
        try:
            file_result = self._get_file_analyzer_result(filename)
        except Exception as e:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags warning: could not extract macros from {filename}: {e}")
            return

        # Extract macros directly from FileAnalyzer's structured defines data
        for define_info in file_result.defines:
            if define_info['is_function_like']:
                continue

            macro_name = define_info['name']
            macro_value = define_info['value'] if define_info['value'] is not None else sz.Str("1")
            self.defined_macros[macro_name] = macro_value

    def _build_all_files_list(self, filename, headers):
        """Build deduplicated list of all files to process (explicit macros + main + headers)."""
        return compiletools.utils.ordered_unique(
            list(self._explicit_macro_files) + [filename] + [h for h in headers if h != filename]
        )

    def _reset_state(self):
        """Reset state for new file processing."""
        self.defined_macros = self._initial_macro_state.copy()
        self._explicit_macro_files = set()
        self._stored_active_magic_flags = {}

    def _check_cache(self, filename, cache_key):
        """Check cache and restore state if hit. Returns cached result or None."""
        if cache_key not in self._structured_data_cache:
            return None

        # Restore macro state from previous convergence using absolute path
        abs_filename = compiletools.wrappedos.realpath(filename)

        # Ensure _final_macro_states is populated (required for hunter.macro_state_key())
        if abs_filename not in self._final_macro_states:
            raise RuntimeError(
                f"Cache hit for {filename} but _final_macro_states not populated. "
                f"This indicates a bug in the caching logic."
            )

        # Restore the converged macro state
        self.defined_macros = self._final_macro_states[abs_filename].copy()

        # Verify state consistency in debug mode
        if __debug__:
            expected_key = self._final_macro_states[abs_filename].get_cache_key()
            actual_key = self.defined_macros.get_cache_key()
            assert expected_key == actual_key, (
                f"Macro state restoration failed for {filename}: "
                f"expected key {expected_key}, got {actual_key}"
            )

        return self._structured_data_cache[cache_key]

    def _setup_explicit_macro_files(self, all_source_files):
        """Collect and process READMACROS files."""
        self._explicit_macro_files = self._collect_explicit_macro_files(all_source_files)

        # Extract macros from explicitly specified files BEFORE processing conditional compilation
        for macro_file in self._explicit_macro_files:
            self._extract_macros_from_file(macro_file)

    def _converge_macro_state(self, all_files, max_iterations=5):
        """Iteratively process files until macro state converges.

        Returns: number of iterations taken
        """
        file_last_macro_version = {}
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            macro_version_before = self.defined_macros.get_version()

            # Determine which files need processing (those not yet processed with current macro version)
            files_to_process = [
                fname for fname in all_files
                if file_last_macro_version.get(fname) != macro_version_before
            ]

            if not files_to_process:
                break

            # Process files that need reprocessing
            # Pass cache key to avoid redundant get_cache_key() calls within file processing
            current_macro_key = self.defined_macros.get_cache_key()
            for fname in files_to_process:
                self._process_file_for_macros(fname, current_macro_key)
                # Record current version to avoid reprocessing in next iteration
                # (files that mutate macros are already cached by their input state)
                file_last_macro_version[fname] = macro_version_before

            # Check convergence - version unchanged means macros converged
            macro_version_after = self.defined_macros.get_version()
            if macro_version_after == macro_version_before:
                break

        return iteration

    def _finalize_and_cache_result(self, filename, headers, cache_key):
        """Store final macro state and build cached result."""
        # Store final converged MacroState (for both cache key and full hash)
        abs_filename = compiletools.wrappedos.realpath(filename)
        self._final_macro_states[abs_filename] = self.defined_macros.copy()

        if self._args.verbose >= 5:
            final_macro_key = self.defined_macros.get_cache_key()
            print(f"DirectMagicFlags: Final converged macro key for {filename}: {final_macro_key}")

        # Build result from stored data
        all_files = self._build_all_files_list(filename, headers)
        result = self._build_structured_result(all_files, self._stored_active_magic_flags)

        # Verify macro state integrity
        if __debug__:
            self._verify_macro_state_unchanged("get_structured_data() completion", filename)

        # Cache result
        self._structured_data_cache[cache_key] = result

        return result

    def get_structured_data(self, filename: str) -> List[Dict[str, Union[str, sz.Str, List[Dict[str, Union[int, sz.Str]]]]]]:
        """Override to handle DirectMagicFlags complex macro processing.

        Cache key: (file_hash, input_macro_key, deps_hash) where:
        - file_hash: SHA1 of source file content (40-char hex)
        - input_macro_key: Initial macro state frozenset
        - deps_hash: 14-char hex hash of dependencies via namer.compute_dep_hash()

        Cached result structure:
            List of dicts: [{'content_hash': str, 'active_magic_flags': List[Dict]}]

            Note: Result stores content_hash (not filepath). Use global_hash_registry.get_filepath_by_hash()
            to resolve current file paths when processing magic flags.

        Returns:
            List of dicts with structure per file (see above)
            See MagicFlagsBase docstring for magic flag dict structure.
        """
        from compiletools.global_hash_registry import get_file_hash

        if self._args.verbose >= 4:
            print("DirectMagicFlags: Setting up structured data with macro processing")

        # Reset state to initial (core) macros
        self._reset_state()

        # Get file hash and initial macro state
        file_hash = get_file_hash(filename)
        input_macro_key = self.defined_macros.get_cache_key()

        # PASS 1: Initial discovery with core macros (compiler built-ins + command-line)
        headers = self._headerdeps.process(filename, input_macro_key)

        if self._args.verbose >= 9:
            print(f"DirectMagicFlags: PASS 1 headers from headerdeps: {headers}")

        all_source_files = [filename] + headers

        # Collect READMACROS file paths from Pass 1 headers
        explicit_macro_files = self._collect_explicit_macro_files(all_source_files)

        # CRITICAL: Store to instance var - _build_all_files_list() reads from self._explicit_macro_files
        self._explicit_macro_files = explicit_macro_files

        # Check cache with initial deps (optimistic - may be incomplete if Pass 2 needed)
        all_deps = sorted(set(headers) | explicit_macro_files)
        deps_hash = self._namer.compute_dep_hash(all_deps)
        cache_key = (file_hash, input_macro_key, deps_hash)

        if self._args.verbose >= 5:
            print(f"DirectMagicFlags: PASS 1 deps_hash={deps_hash} from {len(all_deps)} dependency files")

        cached_result = self._check_cache(filename, cache_key)
        if cached_result is not None:
            return cached_result

        # Cache miss - extract macros from READMACROS files
        for macro_file in explicit_macro_files:
            self._extract_macros_from_file(macro_file)

        # Converge macro state with Pass 1 file set
        all_files = self._build_all_files_list(filename, headers)
        self._converge_macro_state(all_files)

        # PASS 2: Re-discover if macros changed during convergence
        pass1_macro_key = self.defined_macros.get_cache_key()
        if pass1_macro_key != input_macro_key:
            if self._args.verbose >= 5:
                print(f"DirectMagicFlags: Macros changed during convergence, re-discovering headers (Pass 2)")
                print(f"DirectMagicFlags: input_macro_key had {len(input_macro_key)} macros")
                print(f"DirectMagicFlags: pass1_macro_key has {len(pass1_macro_key)} macros")
                if len(pass1_macro_key) <= 10:
                    for k, v in sorted(pass1_macro_key)[:10]:
                        print(f"DirectMagicFlags:   {k} = {v}")

            # CRITICAL: Clear ALL caches to ensure Pass 2 isn't using Pass 1 cached results
            # Even though caches use macro_key, we need to ensure fresh evaluation
            if self._args.verbose >= 7:
                print(f"DirectMagicFlags: Clearing all caches before Pass 2")
            import compiletools.headerdeps
            import compiletools.preprocessing_cache
            compiletools.headerdeps._include_list_cache.clear()
            compiletools.preprocessing_cache._variant_cache.clear()
            compiletools.preprocessing_cache._invariant_cache.clear()

            # Re-discover headers with converged macros (includes file-defined macros)
            headers = self._headerdeps.process(filename, pass1_macro_key)

            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: PASS 2 headers from headerdeps: {headers}")

            all_source_files = [filename] + headers

            # Re-collect READMACROS with expanded header set
            explicit_macro_files = self._collect_explicit_macro_files(all_source_files)

            # CRITICAL: Update instance var with expanded READMACROS set
            self._explicit_macro_files = explicit_macro_files

            # Re-extract macros from any new READMACROS files
            for macro_file in explicit_macro_files:
                self._extract_macros_from_file(macro_file)

            # Re-converge with expanded file set
            all_files = self._build_all_files_list(filename, headers)
            self._converge_macro_state(all_files)

        # Finalize with FINAL dependency list
        final_all_deps = sorted(set(headers) | self._explicit_macro_files)
        final_deps_hash = self._namer.compute_dep_hash(final_all_deps)
        final_cache_key = (file_hash, input_macro_key, final_deps_hash)

        if self._args.verbose >= 5:
            print(f"DirectMagicFlags: Final deps_hash={final_deps_hash} from {len(final_all_deps)} dependency files")

        return self._finalize_and_cache_result(filename, headers, final_cache_key)

    def _build_structured_result(self, all_files: List[str], stored_active_flags: dict) -> list:
        """Build final structured result from stored active magic flags (pure data transformation).

        Args:
            all_files: List of file paths in desired order
            stored_active_flags: Dict mapping filepath -> list of active magic flags

        Returns:
            list: Structured result with content_hash and active_magic_flags for each file
                  [{'content_hash': str, 'active_magic_flags': List[Dict]}]

        Note:
            Stores content_hash (not filepath) to prevent path staleness. Current file paths
            can be resolved via global_hash_registry.get_filepath_by_hash(content_hash).
        """
        from compiletools.global_hash_registry import get_file_hash

        if self._args.verbose >= 7:
            print(f"DirectMagicFlags: Building result from {len(all_files)} stored files")

        result = []
        for filepath in all_files:
            active_magic_flags = stored_active_flags.get(filepath, [])

            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: Using stored magic flags for {filepath}: {len(active_magic_flags)} active")

            content_hash = get_file_hash(filepath)
            result.append({
                'content_hash': content_hash,
                'active_magic_flags': active_magic_flags
            })

        return result

    # DirectMagicFlags doesn't implement readfile() - it uses structured data processing only
    # All processing goes through get_structured_data() -> FileAnalyzerResults

    def _verify_macro_state_unchanged(self, context, filename):
        """Verify that the macro state hasn't changed after convergence for a specific file."""
        if __debug__:
            abs_filename = compiletools.wrappedos.realpath(filename)
            if abs_filename in self._final_macro_states:
                current_key = self.defined_macros.get_cache_key()
                converged_macro_state = self._final_macro_states[abs_filename]
                converged_key = converged_macro_state.get_cache_key()
                assert current_key == converged_key, (
                    f"MACRO STATE CORRUPTION DETECTED in {context} for file {filename}!\n"
                    f"Converged key: {converged_key}\n"
                    f"Current key:   {current_key}\n"
                    f"Converged macros: {set(converged_macro_state.keys())}\n"
                    f"Current macros:   {set(self.defined_macros.keys())}"
                )

    def parse(self, filename):
        # Leverage FileAnalyzer data for optimization and validation
        result = self._parse(filename)
        
        # Verify macro state hasn't been corrupted during parsing
        if __debug__:
            self._verify_macro_state_unchanged("parse() completion", filename)

        return result

    @staticmethod
    def clear_cache():
        # Clear instance method lru_caches on the class
        # These are shared across all instances
        try:
            DirectMagicFlags._compute_file_processing_result.cache_clear()
        except AttributeError:
            pass  # Method doesn't exist yet


class CppMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        # Reuse preprocessor from CppHeaderDeps if available to avoid duplicate instances
        if hasattr(headerdeps, 'preprocessor') and headerdeps.__class__.__name__ == 'CppHeaderDeps':
            self.preprocessor = headerdeps.preprocessor
        else:
            self.preprocessor = compiletools.preprocessor.PreProcessor(args)

    def _readfile(self, filename):
        """Preprocess the given filename but leave comments"""
        extraargs = "-C -E"
        return self.preprocessor.process(
            realpath=filename, extraargs=extraargs, redirect_stderr_to_stdout=True
        )

    def get_structured_data(self, filename: str) -> List[Dict[str, Union[str, List[Dict]]]]:
        """Get magic flags directly from preprocessed text using StringZilla SIMD operations.

        Returns:
            List of dicts with structure: [{'content_hash': str, 'active_magic_flags': List[Dict]}]
            See MagicFlagsBase docstring for magic flag dict structure.
        """
        
        if self._args.verbose >= 4:
            print("CppMagicFlags: Getting structured data from preprocessed C++ output")

        # Get preprocessed text (existing logic)
        preprocessed_text = self._readfile(filename)

        # Use StringZilla for SIMD-optimized processing with source file context tracking
        text = sz.Str(preprocessed_text)
        magic_flags = []
        
        line_num = 0
        current_source_file = None
        
        # Split into lines using StringZilla (SIMD optimized)
        for line_sz in text.split('\n'):
            # Track current source file from preprocessor # directives using StringZilla
            # Format: # <linenum> "<filepath>" <flags>
            if line_sz.startswith('# '):
                first_quote = line_sz.find('"')
                if first_quote >= 0:
                    second_quote = line_sz.find('"', first_quote + 1)
                    if second_quote > first_quote:
                        current_source_file = str(line_sz[first_quote + 1:second_quote])

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
                    key_trimmed = strip_sz(key_slice)
                    value_trimmed = strip_sz(value_slice)

                    if key_trimmed:  # Only add if key is non-empty
                        magic_flag = {
                            'line_num': line_num,
                            'byte_pos': -1,  # Not used for CppMagicFlags
                            'full_line': line_sz,
                            'key': key_trimmed,
                            'value': value_trimmed
                        }
                        
                        # Add source file context for SOURCE resolution
                        if current_source_file:
                            magic_flag['source_file_context'] = current_source_file
                        
                        magic_flags.append(magic_flag)
            
            line_num += 1
        
        if self._args.verbose >= 9:
            print(f"CppMagicFlags: Found {len(magic_flags)} magic flags in preprocessed output")

        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(filename)

        return [{
            'content_hash': content_hash,
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
