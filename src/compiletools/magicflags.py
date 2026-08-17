import argparse
import sys
from collections import defaultdict
from types import SimpleNamespace
from typing import Optional, Union

import stringzilla as sz

import compiletools.apptools
import compiletools.apptools_pkgconfig
import compiletools.build_apply
import compiletools.compiler_macros
import compiletools.flag_ops
import compiletools.git_utils
import compiletools.headerdeps
import compiletools.namer
import compiletools.preprocessing_cache
import compiletools.preprocessor
import compiletools.utils
import compiletools.wrappedos
from compiletools.file_analyzer import FileAnalysisResult
from compiletools.global_hash_registry import get_file_hash, get_filepath_by_hash
from compiletools.preprocessing_cache import MacroState, get_or_compute_preprocessing
from compiletools.simple_preprocessor import (
    MacroVerdictConflictError,
    SimplePreprocessor,
    converging,
    verdict_root,
    verdict_session,
)
from compiletools.stringzilla_utils import strip_sz
from compiletools.utils import instance_cache

# Internal sentinel key for storing hard library ordering constraints inside
# the per-file magic flags dict.
#
# Contract (M-6):
#   Producer: ``magicflags._handle_pkg_config()`` — when a single
#       ``//#PKG-CONFIG=a b c`` annotation lists 2+ libraried packages,
#       it stores pairwise ``(pred_lib, succ_lib)`` tuples (lib names,
#       no ``-l`` prefix) under this key.
#   Consumer: ``build_backend._merge_ldflags_for_sources()`` — pops these
#       entries out of the per-file flags dict, aggregates them across
#       all source files, and forwards the result to
#       ``utils.merge_ldflags_with_topo_sort(..., hard_orderings=...)``.
#   Value type: ``list[tuple[str, str]]``.
#   The key is a sentinel: it MUST be filtered out of normal flag-key
#       iteration (see the ``_HARD_ORDERINGS_KEY`` skip in ``_parse``'s
#       deduplication loop). Using ``sz.Str`` ensures it sorts/hashes
#       like the other dict keys without colliding with any real flag
#       key (no compiler flag starts with an underscore).
_HARD_ORDERINGS_KEY = sz.Str("_HARD_ORDERINGS")


class MacroConvergenceError(Exception):
    """Macro state failed to settle within the convergence iteration bound.

    Raised when the final `_converge_macro_state` call exhausts its iteration
    budget while the macro state is still changing — a macro chain deeper than
    the budget. Without this error the build would silently emit the flags of
    whatever intermediate state the cap happened to land on (measured: a
    10-deep reverse-order chain links the wrong library with no diagnostic).
    """


# Type aliases for clarity
MacroDict = dict[sz.Str, sz.Str]
FlagsDict = dict[sz.Str, list[sz.Str]]


def _define_body(define_info: dict) -> sz.Str:
    """Body a #define contributes to the macro state.

    A bodyless object-like macro is worth 1 in an #if; a bodyless function-like
    macro expands to nothing, so its invocation must vanish rather than become
    a literal 1. Mirrors SimplePreprocessor._handle_define_structured.
    """
    if define_info["value"] is not None:
        return define_info["value"]
    return sz.Str("") if define_info["is_function_like"] else sz.Str("1")


def create(args, headerdeps, context):
    """MagicFlags Factory"""
    classname = args.magic.title() + "MagicFlags"
    if args.verbose >= 4:
        print("Creating " + classname + " to process magicflags.")
    magicclass = globals()[classname]
    return magicclass(args, headerdeps, context=context)


def add_arguments(cap, variant=None):
    """Add the command line arguments that the MagicFlags classes require"""
    if compiletools.apptools._parser_has_option(cap, "--magic"):
        return
    compiletools.apptools.add_common_arguments(cap, variant=variant)
    compiletools.preprocessor.PreProcessor.add_arguments(cap)
    alldepscls = [st[:-10].lower() for st in dict(globals()) if st.endswith("MagicFlags")]
    cap.add_argument(
        "--magic",
        choices=alldepscls,
        default="direct",
        help="Methodology for reading file when processing magic flags",
    )
    if not compiletools.apptools._parser_has_option(cap, "--max-file-read-size"):
        cap.add_argument(
            "--max-file-read-size",
            type=int,
            default=0,
            help="Maximum bytes to read from files (0 = entire file)",
        )
    # Registered here rather than per-tool: every entry point that walks
    # more than one target through magicflags classifies verdict conflicts
    # (simple_preprocessor.verdict_session), and the knob must mean the same
    # thing under all of them.
    cap.add_argument(
        "--macro-verdict-conflict",
        dest="macro_verdict_conflict",
        choices=["error", "warn"],
        default="error",
        help="What to do when one target resolves a shared #if TRUE while another target "
        "cannot evaluate it and assumes false: refuse the run (error, default) or "
        "report and continue (warn). The coinciding FALSE case always warns.",
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

    def __init__(
        self,
        args: argparse.Namespace,
        headerdeps: compiletools.headerdeps.HeaderDepsBase,
        context,
    ) -> None:
        self._args = args
        self._headerdeps = headerdeps
        self.context = context

        # Set analyzer args for file_analyzer caching
        from compiletools.file_analyzer import set_analyzer_args

        set_analyzer_args(args, context)

        # Store final converged MacroState objects by filename.
        # After _parse() runs, the MacroState carries effective compile flags
        # (global + per-file magic) so get_hash(include_core=True) captures everything.
        self._final_macro_states: dict = {}

        # Populated by subclasses; declared here for type checkers.
        self._explicit_macro_files: set = set()

        # (source file, READMACROS value) pairs already warned about. PASS 1
        # and PASS 2 of get_structured_data each re-collect the same file, so
        # without this the unconditional warning prints twice per entry.
        self._warned_unresolved_readmacros: set = set()

    def parse(self, filename):
        """Parse magic flags for the given file. Implemented by subclasses."""
        raise NotImplementedError

    def get_structured_data(self, filename):
        """Return structured file-analysis data. Implemented by subclasses."""
        raise NotImplementedError

    def _initialize_macro_state(self) -> MacroState:
        """Initialize MacroState with command-line and compiler macros as core.

        Returns:
            MacroState: Initialized with core (compiler + cmdline) and empty variable macros
        """
        core_macros = {}

        # Get compiler built-in macros - these are the stable base (~378 macros)
        compiler_macros = compiletools.compiler_macros.get_compiler_macros(self._args.CXX, self._args.verbose)
        core_macros.update({sz.Str(k): sz.Str(v) for k, v in compiler_macros.items()})

        # Add command-line macros to core - they're also static for the
        # entire build. Both helpers read the stashed BuildState's flag
        # tokens (flag_sources names select state slots).
        cmd_macros = compiletools.apptools.extract_command_line_macros(
            self._args, flag_sources=["CPPFLAGS", "CXXFLAGS"], include_compiler_macros=False, verbose=self._args.verbose
        )
        core_macros.update({sz.Str(k): sz.Str(v) for k, v in cmd_macros.items()})

        # Cache-key scoping universe: macros that came from cmdline -D flags
        # (the rest of `core` is compiler builtins). Used by per-TU
        # scope_filter to drop irrelevant cmdline macros from the hash.
        cmdline_origin = compiletools.apptools.cmdline_d_macro_names(
            self._args,
            flag_sources=["CPPFLAGS", "CFLAGS", "CXXFLAGS"],
            verbose=self._args.verbose,
        )

        # All flag state (raw strings, structured Flags, compiler identity)
        # comes from the stashed BuildState -- the one authoritative
        # artifact; the raw args attrs hold only the pre-gather values.
        #
        # Hashing tokens (instead of raw strings) lets the scope filter
        # actually take effect: cmdline -D macros are hashed via core, and
        # stripping them from the token list keeps them from leaking back
        # into the build-context portion of the hash. ``compiler_identity``
        # folds the binary's realpath/size/mtime_ns into the same hash
        # (symmetric with the PCH cache key) so an in-place toolchain swap
        # that leaves args.CXX unchanged still invalidates the cache.
        state = compiletools.build_apply.get_build_state(self._args)
        cppflags_tokens = compiletools.apptools.strip_d_u_tokens(state.flags.cpp)
        cflags_tokens = compiletools.apptools.strip_d_u_tokens(state.flags.c)
        cxxflags_tokens = compiletools.apptools.strip_d_u_tokens(state.flags.cxx)
        compiler_identity = state.flags.compiler_identity

        # Create MacroState with core macros, empty variable macros.
        # anchor_root is the gitroot used to canonicalize -I/etc. tokens
        # in the cache-key hash (decouples cache from absolute workspace
        # path so identical TUs in /run-1/... and /run-2/... share entries).
        return MacroState(
            core_macros,
            {},
            compiler_path=self._args.CXX,
            cppflags=state.cppflags,
            cflags=state.cflags,
            cxxflags=state.cxxflags,
            cmdline_origin=cmdline_origin,
            cppflags_tokens=cppflags_tokens,
            cflags_tokens=cflags_tokens,
            cxxflags_tokens=cxxflags_tokens,
            compiler_identity=compiler_identity,
            anchor_root=compiletools.git_utils.find_git_root(),
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

    def get_final_macro_state_hash(
        self,
        filename: str,
        scope_filter: Optional[frozenset] = None,
    ) -> str:
        """Get the full macro state hash (core + variable + build context) for object file naming.

        The MacroState carries all compile-relevant state:
        - Core macros (compiler built-ins + cmdline -D flags)
        - Variable macros (from file #defines)
        - Build context: compiler_path, cppflags, cflags, cxxflags
          (includes both global flags and per-file magic flags after _parse())

        Args:
            filename: The file path to get the full macro state hash for
            scope_filter: Optional set of cmdline -D macro names to include
                from `core`. When None (default), behavior is unchanged --
                every core macro is hashed. When a frozenset, cmdline-origin
                macros NOT in the set are dropped from the hash, so only
                cmdline -D macros actually referenced by this TU contribute.

        Returns:
            str: 16-character hex hash of full macro state

        Raises:
            KeyError: If file hasn't been processed yet
        """
        abs_filename = compiletools.wrappedos.realpath(filename)
        macro_state = self._final_macro_states.get(abs_filename)
        if macro_state is None:
            raise KeyError(f"No macro state found for {filename} - file not processed")

        return macro_state.get_hash(include_core=True, scope_filter=scope_filter)

    def _get_file_analyzer_result(self, filename: str) -> FileAnalysisResult:
        """Get FileAnalysisResult for a file, using module-level cache.

        Args:
            filename: Path to file to analyze

        Returns:
            FileAnalysisResult: Analysis result for the file
        """
        from compiletools.file_analyzer import analyze_file

        content_hash = get_file_hash(filename, self.context)
        return analyze_file(content_hash, self.context)

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
        context_file = magic_flag_data.get("source_file_context") or filename

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
            raise OSError(f"{filename} specified {magic}='{newflag}' but it does not exist")

        return newflag

    def _handle_include(self, flag):
        flagsforfilename = defaultdict(list)
        # Use canonical separate-token form for compatibility with deduplicate_compiler_flags
        flagsforfilename[sz.Str("CPPFLAGS")].extend([sz.Str("-I"), flag])
        flagsforfilename[sz.Str("CFLAGS")].extend([sz.Str("-I"), flag])
        flagsforfilename[sz.Str("CXXFLAGS")].extend([sz.Str("-I"), flag])
        if self._args.verbose >= 9:
            print(f"Added -I {flag} to CPPFLAGS, CFLAGS, and CXXFLAGS")
        return flagsforfilename

    def _handle_pkg_config(self, flag, filename, expander=None):
        """Expand a ``//#PKG-CONFIG=pkg1 pkg2 ...`` annotation.

        Args:
            flag: The (already-expanded) value of the magic flag.
            filename: Path of the file the annotation came from, used as
                the ``FlagTokenizeError`` source attribution below.
            expander: Optional ``SimplePreprocessor`` used to expand macros
                inside the pkg-config output (e.g. ``$LIB_SUFFIX``). Passed
                explicitly rather than read from ``self._expander`` so this
                method has no hidden mutable per-call state on ``self``.

        The annotation is split into package specs by
        ``apptools.tokenize_pkg_config_specs`` — the same tokenizer the
        conf-file ``pkg-config = ...`` surface uses, so the two surfaces
        agree on what counts as one package. A version-constrained spec
        (``zlib >= 1.2``) is one element, not three, and is passed to
        pkg-config intact so the version floor is enforced.

        This tokenize call is the FIRST thing this method does, before any
        pkg-config subprocess runs -- load-bearing ordering. It used to
        silently degrade a malformed spec into an invented package name
        that got queried before the generic ``//#`` tokenizer at the
        bottom of ``_process_magic_flag`` could raise its diagnostic;
        tokenizing eagerly here (and letting the caller's
        ``FlagTokenizeError`` handler render it) means the good
        diagnostic wins and no subprocess ever runs for the bad value.
        """
        flagsforfilename = defaultdict(list)

        packages = compiletools.apptools.tokenize_pkg_config_specs([str(flag)], slot="//#PKG-CONFIG", source=filename)

        first_l_per_pkg = []  # Track first -l per package for hard orderings

        for pkg in packages:
            # pkg is str. Call cached_pkg_config directly to avoid unnecessary sz conversions
            cflags_raw = compiletools.apptools.cached_pkg_config(pkg, "--cflags")

            # Use the shared filtering logic from apptools
            cflags_str = compiletools.apptools.filter_pkg_config_cflags(cflags_raw, self._args.verbose, package=pkg)
            cflags_sz = sz.Str(cflags_str)
            if cflags_str and expander:
                cflags_sz = expander._recursive_expand_macros_sz(cflags_sz)
            # Post-macro-expansion, so partially user-influenced -- but this
            # site keeps degrading identically to the other two pkg-config
            # output sites (never hard-fail on a malformed .pc/expansion
            # result); Task 9's //#PKG-CONFIG spec-side validation already
            # catches a malformed *package spec* before any query runs.
            cflags_list = compiletools.apptools_pkgconfig.tokenize_pkg_config_output_sz(
                cflags_sz, package=pkg, option="--cflags", verbose=self._args.verbose
            )

            libs_raw = compiletools.apptools.cached_pkg_config(pkg, "--libs")
            libs_sz = sz.Str(libs_raw)
            if libs_raw and expander:
                libs_sz = expander._recursive_expand_macros_sz(libs_sz)
            libs_list = compiletools.apptools_pkgconfig.tokenize_pkg_config_output_sz(
                libs_sz, package=pkg, option="--libs", verbose=self._args.verbose
            )

            # Extract first -l from expanded libs — must use the same
            # post-expansion list that feeds LDFLAGS so names match.
            # Packages whose --libs contain no -l (header-only / linker-script-
            # only / -L-only packages) are silently omitted from
            # first_l_per_pkg: there is no library to express an ordering
            # against. Multi-package PKG-CONFIG=a b c with b library-less
            # therefore degenerates to a single hard edge (A_first, C_first)
            # — i.e. ONE edge per ADJACENT-PAIR-OF-LIBRARIED-PACKAGES, not
            # one per literal package position.
            for token in libs_list:
                token_str = str(token)
                if token_str.startswith("-l") and len(token_str) > 2:
                    first_l_per_pkg.append(token_str[2:])
                    break

            # Add cflags to all C/C++ flag categories
            for key in (sz.Str("CPPFLAGS"), sz.Str("CFLAGS"), sz.Str("CXXFLAGS")):
                flagsforfilename[key].extend(cflags_list)
            flagsforfilename[sz.Str("LDFLAGS")].extend(libs_list)

            if self._args.verbose >= 9:
                print(f"Magic PKG-CONFIG = {pkg}:")
                print(f"\tadded {cflags_list} to CPPFLAGS, CFLAGS, and CXXFLAGS")
                print(f"\tadded {libs_list} to LDFLAGS")

        # For multi-package annotations, store pairwise hard orderings between
        # adjacent libraried packages. See the loop above for what happens
        # when an interior package contributes no -l flag.
        if len(first_l_per_pkg) >= 2:
            for i in range(len(first_l_per_pkg) - 1):
                flagsforfilename[_HARD_ORDERINGS_KEY].append((first_l_per_pkg[i], first_l_per_pkg[i + 1]))

        return flagsforfilename

    def _file_declared_include_paths(self, analysis_result: FileAnalysisResult) -> list[str]:
        """Include directories a single file declares through its own magic flags.

        Harvests ``-I`` / ``-isystem`` from that file's ``CPPFLAGS`` /
        ``CFLAGS`` / ``CXXFLAGS`` annotations, the directory an ``INCLUDE``
        annotation names outright, and the ``--cflags`` of every package a
        ``PKG-CONFIG`` annotation lists. Declaration order is preserved and
        duplicates dropped.

        The result feeds ``READMACROS`` resolution, both for this file's own
        entries and, through ``_tu_declared_include_paths``, for the entries
        of every other file in the same translation unit. Three limits are
        worth stating:

        * Values are used unexpanded. READMACROS collection runs in PASS 1 of
          ``get_structured_data``, before any macro state converges, so a path
          assembled from a macro will not resolve.
        * Every annotation in the file counts, including ones sitting in a
          preprocessor branch that turns out to be inactive. That matches how
          ``_collect_explicit_macro_files`` already treats READMACROS itself.
        * A relative directory is taken as written, i.e. relative to the
          invocation cwd, exactly as the compiler would read it -- not
          relative to the declaring file.

        Repeat calls are cheap without a cache of their own: the pkg-config
        queries behind them are memoised process-wide
        (``apptools_pkgconfig.cached_pkg_config``) and so is the flag
        tokenizer.
        """
        include_paths: list[str] = []

        for magic_flag in analysis_result.magic_flags:
            key = str(magic_flag["key"])
            value = str(magic_flag["value"])

            if key in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
                try:
                    tokens = compiletools.utils.split_command_cached(value)
                except ValueError:
                    # A malformed value gets its diagnostic from the
                    # authoritative tokenize in _process_magic_flag; path
                    # discovery just skips it.
                    continue
                include_paths.extend(compiletools.flag_ops.system_include_paths_from_tokens(tokens))
            elif key == "INCLUDE":
                include_paths.append(value)
            elif key == "PKG-CONFIG":
                include_paths.extend(self._pkg_config_include_paths(value))

        return list(dict.fromkeys(include_paths))

    def _tu_declared_include_paths(self, source_files: list[str]) -> list[str]:
        """Include directories the whole translation unit declares.

        The union of every file's ``_file_declared_include_paths``, in the
        order the files are scanned, duplicates dropped. This is what the
        compile line looks like: magic flags from every file of the TU are
        aggregated onto one command, so a ``//#READMACROS=`` in one header
        must be able to resolve through an ``-isystem`` declared in another.

        A file that cannot be analysed contributes nothing;
        ``_collect_explicit_macro_files`` reports that failure when it reaches
        the file itself.
        """
        include_paths: list[str] = []

        for source_file in source_files:
            try:
                analysis_result = self._get_file_analyzer_result(source_file)
            except OSError:
                continue
            include_paths.extend(self._file_declared_include_paths(analysis_result))

        return list(dict.fromkeys(include_paths))

    def _pkg_config_include_paths(self, flag_value: str) -> list[str]:
        """Include directories the packages of one ``//#PKG-CONFIG=`` contribute.

        Best-effort by design: every failure mode here already has an
        authoritative diagnostic on the real flag-processing path
        (``_handle_pkg_config`` via ``_process_magic_flag``), and raising from
        PASS 1 would replace that curated message with a traceback. In
        particular a ``PkgConfigError`` -- what ``--pkg-config-errors=error``
        promotes a missing package to -- is swallowed so the later
        ``SystemExit(1)`` with the install hint is still what the user sees.
        """
        try:
            packages = compiletools.apptools.tokenize_pkg_config_specs([flag_value])
        except (compiletools.utils.FlagTokenizeError, ValueError):
            return []

        include_paths: list[str] = []
        for pkg in packages:
            try:
                cflags_raw = compiletools.apptools.cached_pkg_config(pkg, "--cflags")
            except compiletools.apptools_pkgconfig.PkgConfigError:
                continue
            if not cflags_raw:
                continue
            cflags_str = compiletools.apptools.filter_pkg_config_cflags(cflags_raw, self._args.verbose, package=pkg)
            tokens = compiletools.apptools_pkgconfig.tokenize_pkg_config_output(
                cflags_str, package=pkg, option="--cflags", verbose=self._args.verbose
            )
            include_paths.extend(compiletools.flag_ops.system_include_paths_from_tokens(tokens))

        return include_paths

    def _resolve_readmacros_path(self, flag, source_filename, extra_include_paths=None):
        """Resolve READMACROS flag to absolute path (pure path resolution logic).

        Resolution order, in full:

        1. An absolute value is taken as-is.
        2. The global ``-I`` / ``-isystem`` set (command line plus conf files)
           via ``apptools.find_system_header``.
        3. ``extra_include_paths``, in the order given. From PASS 1 that is
           the whole translation unit's declared include directories, the
           declaring file's own first (see ``_tu_declared_include_paths``).
        4. The declaring file's own directory.

        Global before magic-declared mirrors the compiler's own search order:
        per-file magic flags are appended after the global flags on the real
        command line. Step 3 beating step 4 is deliberate and observable: a
        header reachable through some TU file's include path wins over a
        same-named header sitting beside the declaring file, which is the
        answer the compiler gives an ``#include <...>``.

        Args:
            flag: The flag value from READMACROS magic flag
            source_filename: The file containing the READMACROS flag
            extra_include_paths: Optional additional directories to search
                after the global set

        Returns:
            str: Absolute path to the resolved file

        Raises:
            OSError: If resolved file doesn't exist
        """
        # Absolute path - use as-is
        if compiletools.wrappedos.isabs_sz(flag):
            resolved_flag = compiletools.wrappedos.realpath_sz(flag)
        else:
            # Try to resolve as a system header using apptools
            resolved_flag_str = compiletools.apptools.find_system_header(
                str(flag), self._args, verbose=self._args.verbose
            )
            if not resolved_flag_str:
                resolved_flag_str = compiletools.apptools.find_header_in_paths(
                    str(flag),
                    extra_include_paths or [],
                    verbose=self._args.verbose,
                    label="READMACROS",
                    paths_label="magic-declared include paths",
                    warn_on_empty=False,
                )

            if resolved_flag_str:
                resolved_flag = sz.Str(resolved_flag_str)
            else:
                # Fall back to resolving relative to source file directory
                source_dir = compiletools.wrappedos.dirname(source_filename)
                resolved_flag = compiletools.wrappedos.realpath_sz(
                    compiletools.wrappedos.join_sz(sz.Str(source_dir), flag)
                )

        # Check if file exists
        if not compiletools.wrappedos.isfile_sz(resolved_flag):
            raise OSError(
                f"{source_filename} specified READMACROS='{flag}' but resolved file '{resolved_flag}' does not exist"
            )

        return str(resolved_flag)

    def _collect_explicit_macro_files(self, source_files: list[str]) -> set:
        """Scan files for READMACROS flags and return set of explicit macro files.

        A relative entry resolves against the include paths declared by the
        WHOLE translation unit, not just the declaring file: the compile line
        these flags land on aggregates every file's magic flags, so an
        ``-isystem`` in one header has to reach a ``//#READMACROS=`` in
        another. The declaring file's own paths are searched first.

        Each entry is resolved and reported independently: an unresolvable
        entry costs that entry alone, never the remaining entries of the same
        file. It is also warned about unconditionally, because the failure is
        otherwise indistinguishable from success -- the macros the header
        would have defined are simply absent, and the ``#if`` guarding them
        reads as false, so the build gets the wrong branch's flags and exits
        zero.

        Args:
            source_files: List of source files to scan

        Returns:
            set: Set of resolved paths (str) to files specified by READMACROS flags
        """
        explicit_files = set()

        # The TU-wide harvest can run pkg-config, so it is deferred until the
        # first relative READMACROS is seen and then reused for the rest of
        # this call.
        tu_include_paths: list[str] | None = None

        for source_file in source_files:
            try:
                analysis_result = self._get_file_analyzer_result(source_file)
            except OSError as e:
                if self._args.verbose >= 1:
                    print(
                        f"DirectMagicFlags warning: could not scan {source_file} for READMACROS: {e}",
                        file=sys.stderr,
                    )
                continue

            readmacros_flags = [mf for mf in analysis_result.magic_flags if mf["key"] == sz.Str("READMACROS")]
            if not readmacros_flags:
                continue

            # Only files carrying a relative READMACROS need the harvest, and
            # it may run pkg-config, so defer it until one is seen.
            declared_include_paths = None

            for magic_flag in readmacros_flags:
                if declared_include_paths is None and not compiletools.wrappedos.isabs_sz(magic_flag["value"]):
                    if tu_include_paths is None:
                        tu_include_paths = self._tu_declared_include_paths(source_files)
                    own_include_paths = self._file_declared_include_paths(analysis_result)
                    declared_include_paths = list(dict.fromkeys(own_include_paths + tu_include_paths))

                try:
                    resolved_path = self._resolve_readmacros_path(
                        magic_flag["value"], source_file, extra_include_paths=declared_include_paths
                    )
                except OSError as e:
                    warn_key = (source_file, str(magic_flag["value"]))
                    if warn_key not in self._warned_unresolved_readmacros:
                        self._warned_unresolved_readmacros.add(warn_key)
                        print(
                            f"DirectMagicFlags warning: could not resolve READMACROS in {source_file}: {e}",
                            file=sys.stderr,
                        )
                    continue

                explicit_files.add(resolved_path)

                if self._args.verbose >= 5:
                    print(f"READMACROS: Will process '{resolved_path}' for macro extraction (from {source_file})")

        return explicit_files

    def _parse(self, filename):
        if self._args.verbose >= 4:
            print("Parsing magic flags for " + filename)

        # We assume that headerdeps _always_ exist
        # before the magic flags are called.
        # When used in the "usual" fashion this is true.
        # However, it is possible to call directly so we must
        # ensure that the headerdeps exist manually.
        # Pass empty frozenset since we haven't computed macros for this file yet
        #
        # Both this walk and the convergence below evaluate the file before its
        # controlling macros are all known, so unevaluable-condition reports are
        # held until the macro state settles and retracted if a later pass
        # resolves them (see simple_preprocessor.converging). The verdict_root
        # span attributes every verdict to THIS target's partition: retraction
        # is legitimate between passes over one target (approximations of the
        # same settled answer) and illegitimate between two targets' settled
        # states (distinct final answers that coexist in the product).
        with verdict_root(self.context, compiletools.wrappedos.realpath(filename)), converging(self.context):
            self._headerdeps.process(filename, frozenset())

            # Both DirectMagicFlags and CppMagicFlags now use structured data approach
            flagsforfilename = defaultdict(list)

            file_analysis_data = self.get_structured_data(filename)

        # Expand macros in magic flag values (e.g., LIB_SUFFIX -> O2)
        abs_filename = compiletools.wrappedos.realpath(filename)
        macro_state = self._final_macro_states.get(abs_filename)
        expander = None
        if macro_state:
            # Filter out legacy compiler macros (e.g. 'linux', 'unix')
            # that lack a leading underscore — they corrupt path
            # components in pkg-config output.  Conditional compilation
            # is unaffected because it uses a separate preprocessor
            # instance that retains the full macro set.
            all_macros = macro_state.all_macros()
            compiler_predefined = compiletools.compiler_macros.get_compiler_macros(
                getattr(self._args, "CXX", ""), self._args.verbose
            )
            legacy_names = {sz.Str(k) for k in compiler_predefined if not k.startswith("_")}
            if legacy_names:
                all_macros = {k: v for k, v in all_macros.items() if k not in legacy_names or k in macro_state.variable}

            expander = SimplePreprocessor(
                all_macros,
                verbose=self._args.verbose,
                compiler_path=getattr(self._args, "CXX", ""),
                cppflags=compiletools.build_apply.get_build_state(self._args).cppflags,
                function_params=macro_state.function_params,
            )

        for file_data in file_analysis_data:
            content_hash = file_data["content_hash"]
            filepath = get_filepath_by_hash(content_hash, self.context)
            active_magic_flags = file_data["active_magic_flags"]

            for magic_flag in active_magic_flags:
                magic = magic_flag["key"]
                flag = magic_flag["value"]
                if expander:
                    flag = expander._recursive_expand_macros_sz(flag)
                # Pass magic_flag data, filepath, and expander explicitly so
                # the per-call state has no hidden dependency on `self`.
                self._process_magic_flag(magic, flag, flagsforfilename, magic_flag, filepath, expander=expander)

        # Merge deprecated LINKFLAGS into LDFLAGS before deduplication
        if sz.Str("LINKFLAGS") in flagsforfilename:
            flagsforfilename[sz.Str("LDFLAGS")].extend(flagsforfilename[sz.Str("LINKFLAGS")])
            del flagsforfilename[sz.Str("LINKFLAGS")]

        # Unify CPPFLAGS and CXXFLAGS in magic flags (unless opted out)
        if not getattr(self._args, "separate_flags_CPP_CXX", False):
            cpp_key = sz.Str("CPPFLAGS")
            cxx_key = sz.Str("CXXFLAGS")
            if cpp_key in flagsforfilename or cxx_key in flagsforfilename:
                combined = flagsforfilename[cpp_key] + flagsforfilename[cxx_key]
                flagsforfilename[cpp_key] = list(combined)
                flagsforfilename[cxx_key] = list(combined)

        # Deduplicate all flags while preserving order, with smart compiler flag handling
        for key in flagsforfilename:
            if key == _HARD_ORDERINGS_KEY:
                continue
            flagsforfilename[key] = compiletools.utils.deduplicate_compiler_flags(flagsforfilename[key])

        # Update _final_macro_states to carry effective compile flags
        # (global from self._args + per-file magic flags).  Always computed
        # from self._args.* so this is idempotent if _parse() runs twice.
        abs_filename = compiletools.wrappedos.realpath(filename)
        old_ms = self._final_macro_states.get(abs_filename)
        if old_ms is None:
            raise RuntimeError(
                f"_final_macro_states not populated for {filename} before _parse() update. "
                f"get_structured_data() should have been called first."
            )

        # Magic-flag entries are already structured (lists of sz.Str / str)
        # from the magic-flag pipeline. Build the effective token lists by
        # extending the parse-time global tokens directly, eliminating the
        # list -> join -> concat -> tokenize round-trip that the previous
        # implementation performed once per TU.
        magic_cpp_tokens = [str(f) for f in flagsforfilename.get(sz.Str("CPPFLAGS"), [])]
        magic_c_tokens = [str(f) for f in flagsforfilename.get(sz.Str("CFLAGS"), [])]
        magic_cxx_tokens = [str(f) for f in flagsforfilename.get(sz.Str("CXXFLAGS"), [])]

        # The stashed BuildState's flags are the canonical source (via
        # parseargs / testhelper.finalize_flag_state). list(...) wraps the
        # tuples so the +-with-magic-tokens concat stays a list operation.
        state = compiletools.build_apply.get_build_state(self._args)
        args_cpp_tokens = list(state.flags.cpp)
        args_c_tokens = list(state.flags.c)
        args_cxx_tokens = list(state.flags.cxx)

        # Strip -D/-U so per-file magic `-D`s don't smuggle themselves into
        # the build-context portion of the hash. Magic-flag macros are
        # file-private; they are NOT cmdline-origin, so cmdline_origin is
        # propagated unchanged from the initial state.
        effective_cppflags_tokens = compiletools.apptools.strip_d_u_tokens(args_cpp_tokens + magic_cpp_tokens)
        effective_cflags_tokens = compiletools.apptools.strip_d_u_tokens(args_c_tokens + magic_c_tokens)
        effective_cxxflags_tokens = compiletools.apptools.strip_d_u_tokens(args_cxx_tokens + magic_cxx_tokens)

        # Raw string fields on MacroState are still needed for `__has_*`
        # queries that grep them as text; rebuild from the unstripped
        # token lists so all flags (including magic -D entries) are present.
        # This token round-trip happens once per file -- a strict reduction
        # from the prior 3-step round-trip on globals + magic strings.
        effective_cppflags = " ".join(args_cpp_tokens + magic_cpp_tokens)
        effective_cflags = " ".join(args_c_tokens + magic_c_tokens)
        effective_cxxflags = " ".join(args_cxx_tokens + magic_cxx_tokens)

        self._final_macro_states[abs_filename] = MacroState(
            old_ms.core,
            old_ms.variable,
            compiler_path=old_ms.compiler_path,
            cppflags=effective_cppflags,
            cflags=effective_cflags,
            cxxflags=effective_cxxflags,
            cmdline_origin=old_ms.cmdline_origin,
            cppflags_tokens=effective_cppflags_tokens,
            cflags_tokens=effective_cflags_tokens,
            cxxflags_tokens=effective_cxxflags_tokens,
            compiler_identity=old_ms.compiler_identity,
            function_params=old_ms.function_params,
            anchor_root=old_ms.anchor_root,
        )

        return flagsforfilename

    def _extend_flags_from_dict(self, flagsforfilename, extra_flags_dict):
        """Helper to extend flags from a dict of flag lists."""
        for key, values in extra_flags_dict.items():
            flagsforfilename[key].extend(values)

    def _process_magic_flag(self, magic, flag, flagsforfilename, magic_flag_data, filename, expander=None):
        """Process a single magic flag entry.

        Args:
            magic: Magic flag key (e.g. ``LDFLAGS``, ``PKG-CONFIG``).
            flag: Magic flag value (already macro-expanded by the caller).
            flagsforfilename: Output dict to append flags into.
            magic_flag_data: Raw structured magic-flag dict (for SOURCE
                context resolution etc.).
            filename: Path of the file the magic flag came from.
            expander: Optional ``SimplePreprocessor`` forwarded to
                downstream handlers (notably ``_handle_pkg_config``) that
                need to expand macros inside subprocess output. Passed
                explicitly to avoid hidden ``self`` mutation between calls.
        """
        # READMACROS is handled during DirectMagicFlags first pass, don't add to output
        if magic == sz.Str("READMACROS"):
            return

        # PCH=path/to/header.h marks a header for precompilation.
        # Resolve the path relative to the file containing the annotation,
        # matching SOURCE semantics.
        if magic == sz.Str("PCH"):
            if compiletools.wrappedos.isabs_sz(flag):
                resolved = compiletools.wrappedos.realpath_sz(flag)
            else:
                context_dir = compiletools.wrappedos.dirname(filename)
                resolved = compiletools.wrappedos.realpath_sz(
                    compiletools.wrappedos.join_sz(sz.Str(context_dir), strip_sz(flag))
                )
            flagsforfilename[magic].append(resolved)
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
            try:
                self._extend_flags_from_dict(
                    flagsforfilename, self._handle_pkg_config(flag, filename, expander=expander)
                )
            except compiletools.apptools_pkgconfig.PkgConfigError as exc:
                # Verbosity must not invert the policy. SystemExit is the
                # termination that survives the deliberately broad
                # ``except Exception`` in Hunter's source expansion; a
                # PkgConfigError there is downgraded to a warning and the
                # caller gets a source list missing the package's flags.
                message = compiletools.apptools_pkgconfig.render_pkg_config_error(
                    exc, f"PKG-CONFIG requested by {filename}."
                )
                print(message, file=sys.stderr)
                raise SystemExit(1) from None
            except compiletools.utils.FlagTokenizeError as exc:
                # Same carve-out as the PkgConfigError case just above: a
                # malformed //#PKG-CONFIG= spec (caught inside
                # _handle_pkg_config, before any pkg-config subprocess
                # runs) must not be downgraded to a warning by Hunter's
                # broad except Exception.
                print(str(exc), file=sys.stderr)
                raise SystemExit(1) from None
            # PKG-CONFIG generates flags for other keys AND adds itself to PKG-CONFIG key

        # Split flag string into individual flags - all magic flags can contain multiple values
        try:
            individual_flags = compiletools.utils.tokenize_flags_sz_or_raise(flag, slot=f"//#{magic}", source=filename)
        except compiletools.utils.FlagTokenizeError as exc:
            # Verbosity must not invert this, matching the PKG-CONFIG carve-out
            # just above: SystemExit is the termination that survives the
            # deliberately broad ``except Exception`` in Hunter's source
            # expansion (hunter.py), which would otherwise catch this
            # RuntimeError subclass and downgrade the failure to a warning.
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from None
        flagsforfilename[magic].extend(individual_flags)
        if self._args.verbose >= 5:
            print(f"Using magic flag {magic}={flag} extracted from {filename}")

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


class DirectMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps, context):
        MagicFlagsBase.__init__(self, args, headerdeps, context=context)
        # Create namer instance for dependency hash computation
        self._namer = compiletools.namer.Namer(args, context=context)
        # Compute initial macro state once (compiler built-ins + command-line macros)
        # Computed once and shared directly (MacroState is immutable)
        self._initial_macro_state = self._initialize_macro_state()
        # Track defined macros with values during processing (MacroState with core + variable)
        self.defined_macros = self._initial_macro_state
        # Track files specified by READMACROS magic flags
        self._explicit_macro_files = set()
        # Cache structured data results by (file_hash, input_macro_key, deps_hash) to avoid redundant convergence
        # deps_hash: XOR of dependency file content hashes (headers + READMACROS)
        # Cached result stores content_hash (not filepath) - current paths resolved via global hash registry
        self._structured_data_cache = {}

    def _extract_macros_from_magic_flags(self, magic_flags_result):
        """Extract -D macros from magic flag CPPFLAGS and CXXFLAGS."""
        # Create minimal args object with magic flag values
        flag_sources = [sz.Str("CPPFLAGS"), sz.Str("CXXFLAGS")]
        temp_args = SimpleNamespace(
            CPPFLAGS=magic_flags_result.get(flag_sources[0], []), CXXFLAGS=magic_flags_result.get(flag_sources[1], [])
        )
        macros = compiletools.apptools.extract_command_line_macros_sz(
            temp_args, flag_sources_sz=flag_sources, verbose=self._args.verbose
        )

        # Update macro state immutably (use empty core since these are variable macros)
        self.defined_macros = self.defined_macros.with_updates(macros)

    @instance_cache
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
            Tuple of (active_magic_flags, magic_macros, effects) where
            magic_macros holds the -D macros extracted from active CPPFLAGS/
            CXXFLAGS magic flags and effects is a FileEffects over the
            active #define/#undef directives. None if the file cannot be
            processed.

            The effects sets are deliberately NOT the ProcessingResult's own:
            magicflags extracts from every active #define line (including
            include guards the preprocessor strips from file_defines), and
            makes the define/undef sets disjoint per the positional verdict
            (see the comment at the extraction below). Its occurrence tuple
            is empty because get_or_compute_preprocessing already replayed
            this computation's occurrences.
        """
        try:
            file_result = self._get_file_analyzer_result(fname)
        except OSError as e:
            # Soft-fail (a header may legitimately not exist yet, e.g. it is
            # generated later in the build) — but say so: a silently skipped
            # file drops its #defines from the macro state and the miscompile
            # shows up far downstream with no pointer back here.
            if self._args.verbose >= 1:
                print(
                    f"DirectMagicFlags warning: could not process {fname} for macro extraction: {e}",
                    file=sys.stderr,
                )
            return None

        # Process conditional compilation to get active lines using current macro state
        result = get_or_compute_preprocessing(
            file_result,
            self.defined_macros,
            self._args.verbose,
            context=self.context,
        )
        active_line_set = set(result.active_lines)

        # Extract macros from active magic flag CPPFLAGS and CXXFLAGS
        active_magic_flags = [
            magic_flag for magic_flag in file_result.magic_flags if magic_flag["line_num"] in active_line_set
        ]

        # Collect macros from active magic flags for caching. CPPFLAGS
        # entries first, then CXXFLAGS, so a name present in both slots
        # keeps the CXXFLAGS value regardless of line order (the merge
        # order dict(cpp + cxx) always had).
        cppflags_macros = []
        cxxflags_macros = []
        if active_magic_flags:
            for magic_flag in active_magic_flags:
                key = magic_flag["key"]
                value = magic_flag["value"]
                if key == sz.Str("CPPFLAGS") or key == sz.Str("CXXFLAGS"):
                    # Extract -D macros using StringZilla operations
                    from compiletools.stringzilla_utils import parse_d_flags_sz

                    for macro_name, macro_value in parse_d_flags_sz(value):
                        if key == sz.Str("CPPFLAGS"):
                            cppflags_macros.append((macro_name, macro_value))
                        else:
                            cxxflags_macros.append((macro_name, macro_value))
        magic_macros = dict(cppflags_macros + cxxflags_macros)

        # Extract variable macros from active #define directives. A name can
        # be both actively #define'd and actively #undef'd, and which wins is
        # positional ('#undef X/#define X 2' keeps X, '#define X 2/#undef X'
        # loses it) — information a consumer applying two flat sets in a fixed
        # order cannot recover. The preprocessor's final state carries the
        # verdict, so the two sets are made disjoint here: a define a later
        # #undef killed is dropped, and an #undef a later #define superseded
        # is dropped.
        final_variable_macros = result.updated_macros.variable
        extracted_variable_macros = {}
        extracted_function_params = {}
        for define_info in file_result.defines:
            if define_info["line_num"] not in active_line_set:
                continue

            macro_name = define_info["name"]
            if macro_name in result.file_undefs and macro_name not in final_variable_macros:
                continue
            extracted_variable_macros[macro_name] = _define_body(define_info)
            if define_info["is_function_like"]:
                extracted_function_params[macro_name] = tuple(define_info["params"])

        file_undefs = frozenset(u for u in result.file_undefs if u not in extracted_variable_macros)

        effects = compiletools.preprocessing_cache.FileEffects(
            content_hash=file_result.content_hash,
            file_defines=extracted_variable_macros,
            file_undefs=file_undefs,
            file_function_params=extracted_function_params,
        )
        return (active_magic_flags, magic_macros, effects)

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

        active_magic_flags, magic_macros, effects = cached_result

        # Store active magic flags for this file to avoid redundant final pass
        self._stored_active_magic_flags[fname] = active_magic_flags

        # Layer the magic-flag-derived macros first, then the file's own
        # directive effects through the shared apply() contract. Magic
        # macros are a magicflags-only concern, so they stay outside
        # FileEffects; applying them first means a file's #define overrides
        # a magic -D of the same name (as the previous single merged update
        # did) and a #undef still removes a magic macro the file never
        # redefines (apply's undefs run after this).
        self.defined_macros = self.defined_macros.with_updates(magic_macros)
        self.defined_macros = effects.apply(self.defined_macros, self.context)

    def _extract_macros_from_file(self, filename):
        """Extract #define macros from a file (unconditionally, no preprocessor evaluation)."""
        try:
            file_result = self._get_file_analyzer_result(filename)
        except OSError as e:
            # Same soft-fail contract as _compute_file_processing_result: skip
            # the file but leave a breadcrumb, since its macros go missing.
            if self._args.verbose >= 1:
                print(
                    f"DirectMagicFlags warning: could not extract macros from {filename}: {e}",
                    file=sys.stderr,
                )
            return

        updates = {}
        function_params = {}
        # Extract macros directly from file_analyzer's structured defines data
        for define_info in file_result.defines:
            macro_name = define_info["name"]
            updates[macro_name] = _define_body(define_info)
            if define_info["is_function_like"]:
                function_params[macro_name] = tuple(define_info["params"])

        if updates:
            self.defined_macros = self.defined_macros.with_updates(updates, function_params)

    def _build_all_files_list(self, filename, headers):
        """Build deduplicated list of all files to process (explicit macros + main + headers)."""
        return compiletools.utils.ordered_unique(
            list(self._explicit_macro_files) + [filename] + [h for h in headers if h != filename]
        )

    def _reset_state(self):
        """Reset state for new file processing."""
        self.defined_macros = self._initial_macro_state
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
        self.defined_macros = self._final_macro_states[abs_filename]

        # Verify state consistency in debug mode
        if __debug__:
            expected_key = self._final_macro_states[abs_filename].get_cache_key()
            actual_key = self.defined_macros.get_cache_key()
            assert expected_key == actual_key, (
                f"Macro state restoration failed for {filename}: expected key {expected_key}, got {actual_key}"
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

        Returns: (iterations taken, converged). converged is False when the
        state was still moving after `max_iterations` state-CHANGING passes —
        the caller of the FINAL convergence must treat that as an error,
        because the emitted flags come from whatever intermediate point the
        budget landed on and can be silently wrong.

        Only changing passes count against the budget, and exhaustion is
        detected by the pass AFTER the budget is spent: a state that settles
        exactly on the last budgeted pass is confirmed by one further
        (no-change, cache-warm) pass rather than misreported as exhausted
        (measured: a 9-deep reverse-order chain settles on the final budgeted
        pass and links the right library). This moves the raise boundary by
        one, it does not remove it: a chain exactly one deeper still raises
        even when its flags happen to land right, the conservative side of
        the trade.
        """
        file_last_macro_version = {}
        iteration = 0
        changing_passes = 0
        converged = False

        while True:
            iteration += 1
            # Track state object identity to detect convergence
            # with_updates() returns self if no effective changes occur
            macro_state_before = self.defined_macros

            # Determine which files need processing (those not yet processed with current macro state)
            # Use cache key as proxy for version since immutable state usually means different cache key
            current_macro_key = self.defined_macros.get_cache_key()

            files_to_process = [fname for fname in all_files if file_last_macro_version.get(fname) != current_macro_key]

            if not files_to_process:
                converged = True
                break

            # Process files that need reprocessing
            for fname in files_to_process:
                self._process_file_for_macros(fname, current_macro_key)
                # Record current key to avoid reprocessing in next iteration
                # (files that mutate macros are already cached by their input state)
                file_last_macro_version[fname] = current_macro_key

            # Check convergence - identity check works because with_updates returns
            # self if no changes occurred
            if self.defined_macros is macro_state_before:
                converged = True
                break

            changing_passes += 1
            if changing_passes > max_iterations:
                break

        return iteration, converged

    def _raise_if_not_converged(self, converged: bool, iterations: int, filename: str) -> None:
        """Fail loudly when the FINAL convergence for `filename` exhausted its bound.

        Only the last `_converge_macro_state` call needs this: pass-1
        exhaustion is benign when Pass 2 follows, since Pass 2 re-converges
        from the pass-1 state with a fresh budget (a chain of depth 6-9
        exhausts pass 1 and still settles correctly in pass 2).
        """
        if converged:
            return
        raise MacroConvergenceError(
            f"Macro state for {filename} did not converge after {iterations} iterations. "
            f"A macro dependency chain deeper than the iteration bound would silently "
            f"resolve conditional magic flags (//#CXXFLAGS, //#LDFLAGS, ...) from an "
            f"unsettled intermediate state. Break up the chain, or include the headers "
            f"closer to dependency order so each pass resolves more of it."
        )

    def _finalize_and_cache_result(self, filename, headers, cache_key):
        """Store final macro state and build cached result."""
        # Store final converged MacroState (for both cache key and full hash)
        abs_filename = compiletools.wrappedos.realpath(filename)
        self._final_macro_states[abs_filename] = self.defined_macros

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

    def get_structured_data(
        self, filename: str
    ) -> list[dict[str, Union[str, sz.Str, list[dict[str, Union[int, sz.Str]]]]]]:
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
        if self._args.verbose >= 4:
            print("DirectMagicFlags: Setting up structured data with macro processing")

        # Reset state to initial (core) macros
        self._reset_state()

        # Get file hash and initial macro state
        file_hash = get_file_hash(filename, self.context)
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
        iterations, converged = self._converge_macro_state(all_files)

        # PASS 2: Re-discover if macros changed during convergence
        pass1_macro_key = self.defined_macros.get_cache_key()
        if pass1_macro_key != input_macro_key:
            if self._args.verbose >= 5:
                print("DirectMagicFlags: Macros changed during convergence, re-discovering headers (Pass 2)")
                print(f"DirectMagicFlags: input_macro_key had {len(input_macro_key)} macros")
                print(f"DirectMagicFlags: pass1_macro_key has {len(pass1_macro_key)} macros")
                if len(pass1_macro_key) <= 10:
                    for k, v in sorted(pass1_macro_key)[:10]:
                        print(f"DirectMagicFlags:   {k} = {v}")

            # Clear caches that depend on macro state for Pass 2
            # Invariant caches are preserved - those files have no conditionals
            if self._args.verbose >= 7:
                print("DirectMagicFlags: Clearing variant caches before Pass 2")
            compiletools.headerdeps.clear_include_list_cache(context=self.context)
            compiletools.preprocessing_cache.clear_variant_cache(context=self.context)

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
            iterations, converged = self._converge_macro_state(all_files)

        # Only the FINAL convergence must be enforced. When Pass 2 runs, its
        # verdict (fresh budget over the pass-1 state) supersedes pass-1
        # exhaustion; when it does not run, Pass 1 IS the final convergence
        # and its verdict is the one to enforce — including the oscillation
        # edge case where an exhausted Pass 1 lands back on the input key.
        self._raise_if_not_converged(converged, iterations, filename)

        # Finalize with FINAL dependency list
        final_all_deps = sorted(set(headers) | self._explicit_macro_files)
        final_deps_hash = self._namer.compute_dep_hash(final_all_deps)
        final_cache_key = (file_hash, input_macro_key, final_deps_hash)

        if self._args.verbose >= 5:
            print(f"DirectMagicFlags: Final deps_hash={final_deps_hash} from {len(final_all_deps)} dependency files")

        return self._finalize_and_cache_result(filename, headers, final_cache_key)

    def _build_structured_result(self, all_files: list[str], stored_active_flags: dict) -> list:
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
        if self._args.verbose >= 7:
            print(f"DirectMagicFlags: Building result from {len(all_files)} stored files")

        result = []
        for filepath in all_files:
            active_magic_flags = stored_active_flags.get(filepath, [])

            if self._args.verbose >= 9:
                print(f"DirectMagicFlags: Using stored magic flags for {filepath}: {len(active_magic_flags)} active")

            content_hash = get_file_hash(filepath, self.context)
            result.append({"content_hash": content_hash, "active_magic_flags": active_magic_flags})

        return result

    # DirectMagicFlags doesn't implement readfile() - it uses structured data processing only
    # All processing goes through get_structured_data() -> FileAnalysisResult

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
                    f"Converged macros: {set(converged_macro_state.variable.keys())}\n"
                    f"Current macros:   {set(self.defined_macros.variable.keys())}"
                )

    def parse(self, filename):
        # Leverage file_analyzer data for optimization and validation
        result = self._parse(filename)

        # Verify macro state hasn't been corrupted during parsing
        if __debug__:
            self._verify_macro_state_unchanged("parse() completion", filename)

        return result

    @staticmethod
    def clear_cache():
        pass  # Instance caches are per-instance; nothing class-level to clear


class CppMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps, context):
        MagicFlagsBase.__init__(self, args, headerdeps, context=context)
        # Reuse preprocessor from CppHeaderDeps if available to avoid duplicate instances.
        # getattr() over hasattr() lets the type checker see the access path narrow on
        # the not-None branch.
        shared_pre = getattr(headerdeps, "preprocessor", None)
        if shared_pre is not None and headerdeps.__class__.__name__ == "CppHeaderDeps":
            self.preprocessor = shared_pre
        else:
            self.preprocessor = compiletools.preprocessor.PreProcessor(args)

        # Compute initial macro state once (compiler built-ins + command-line macros)
        self._initial_macro_state = self._initialize_macro_state()

    def _readfile(self, filename):
        """Preprocess the given filename but leave comments"""
        extraargs = ["-C", "-E"]
        return self.preprocessor.process(realpath=filename, extraargs=extraargs, redirect_stderr_to_stdout=True)

    def get_structured_data(self, filename: str) -> list[dict[str, Union[str, list[dict]]]]:
        """Get magic flags directly from preprocessed text using StringZilla SIMD operations.

        Returns:
            List of dicts with structure: [{'content_hash': str, 'active_magic_flags': List[Dict]}]
            See MagicFlagsBase docstring for magic flag dict structure.
        """

        if self._args.verbose >= 4:
            print("CppMagicFlags: Getting structured data from preprocessed C++ output")

        # Use initial macro state (core macros only) for CppMagicFlags.
        # In cpp mode, the C++ preprocessor handles all conditional compilation,
        # so we don't need to track variable macros. The magic flags are extracted
        # from preprocessed output where all conditionals are already resolved.
        # This avoids an expensive additional preprocessor call (-dM -E).
        abs_filename = compiletools.wrappedos.realpath(filename)
        if abs_filename not in self._final_macro_states:
            self._final_macro_states[abs_filename] = self._initial_macro_state

        # Get preprocessed text (existing logic)
        preprocessed_text = self._readfile(filename)

        # Bulk-find scan: locate linemarkers and magic flags directly via SIMD find
        # instead of splitting into ~697K lines and checking each one.
        text = sz.Str(preprocessed_text)
        text_len = len(text)
        magic_flags = []
        current_source_file = None

        # Patterns to search for in the bulk text
        linemarker_pat = sz.Str("\n# ")
        magic_pat = sz.Str("//#")

        # Handle edge case: text starts with a linemarker (no preceding newline)
        scan_pos = 0
        if text_len > 2 and text[0] == "#" and text[1] == " ":
            line_end = text.find("\n", 0)
            if line_end < 0:
                line_end = text_len
            line_sz = text[:line_end]
            first_quote = line_sz.find('"')
            if first_quote >= 0:
                second_quote = line_sz.find('"', first_quote + 1)
                if second_quote > first_quote:
                    current_source_file = str(line_sz[first_quote + 1 : second_quote])

        # Main interleaved scan
        next_lm = text.find(linemarker_pat, scan_pos)
        next_mg = text.find(magic_pat, scan_pos)

        while next_lm >= 0 or next_mg >= 0:
            # Determine which match comes first (-1 means no more matches)
            if next_mg < 0 or (next_lm >= 0 and next_lm < next_mg):
                # Process linemarker: extract filename from # N "file"
                line_start = next_lm + 1  # skip the \n
                line_end = text.find("\n", line_start)
                if line_end < 0:
                    line_end = text_len
                line_sz = text[line_start:line_end]
                first_quote = line_sz.find('"')
                if first_quote >= 0:
                    second_quote = line_sz.find('"', first_quote + 1)
                    if second_quote > first_quote:
                        current_source_file = str(line_sz[first_quote + 1 : second_quote])
                scan_pos = line_end
                next_lm = text.find(linemarker_pat, scan_pos)
            else:
                # Process magic flag: extract full line, then key=value
                line_start_pos = text.rfind("\n", 0, next_mg)
                line_start = 0 if line_start_pos < 0 else line_start_pos + 1
                line_end = text.find("\n", next_mg)
                if line_end < 0:
                    line_end = text_len
                line_sz = text[line_start:line_end]
                magic_offset = next_mg - line_start

                after_marker = line_sz[magic_offset + 3 :]
                eq_pos = after_marker.find("=")
                if eq_pos >= 0:
                    key_trimmed = strip_sz(after_marker[:eq_pos])
                    value_trimmed = strip_sz(after_marker[eq_pos + 1 :])

                    if key_trimmed:
                        magic_flag = {
                            "line_num": -1,
                            "byte_pos": -1,
                            "full_line": line_sz,
                            "key": key_trimmed,
                            "value": value_trimmed,
                        }
                        if current_source_file:
                            magic_flag["source_file_context"] = current_source_file
                        magic_flags.append(magic_flag)

                scan_pos = line_end
                next_mg = text.find(magic_pat, scan_pos)

        if self._args.verbose >= 9:
            print(f"CppMagicFlags: Found {len(magic_flags)} magic flags in preprocessed output")

        content_hash = get_file_hash(filename, self.context)

        return [{"content_hash": content_hash, "active_magic_flags": magic_flags}]

    def parse(self, filename):
        return self._parse(filename)

    @staticmethod
    def clear_cache():
        pass


class NullStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        print(f"{self.adjust(realpath)}: {magicflags!s}")


class PrettyStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        sys.stdout.write(f"\n{self.adjust(realpath)}")
        try:
            for key in magicflags:
                sys.stdout.write(f"\n\t{key}:")
                for flag in magicflags[key]:
                    sys.stdout.write(f" {flag}")
        except TypeError:
            sys.stdout.write("\n\tNone")


def main(argv=None):
    cap = compiletools.apptools.create_parser("Parse a file and show the magicflags it exports", argv=argv)
    compiletools.headerdeps.add_arguments(cap)
    add_arguments(cap)
    cap.add_argument("filename", help='File/s to extract magicflags from"', nargs="+")

    # Figure out what style classes are available and add them to the command
    # line options
    styles = [st[:-5].lower() for st in dict(globals()) if st.endswith("Style")]
    cap.add_argument("--style", choices=styles, default="pretty", help="Output formatting style")

    from compiletools.build_context import BuildContext

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)
    headerdeps = compiletools.headerdeps.create(args, context=context)
    magicparser = create(args, headerdeps, context=context)

    styleclass = globals()[args.style.title() + "Style"]
    styleobject = styleclass(args)

    # Same session shape as ct-cake: with more than one filename this tool
    # is a multi-target walk, and a cross-target verdict conflict must
    # surface here exactly as a build over the same files would surface it.
    try:
        with verdict_session(context, mode=getattr(args, "macro_verdict_conflict", "error"), tool="ct-magicflags"):
            for fname in args.filename:
                realpath = compiletools.wrappedos.realpath(fname)
                styleobject(realpath, magicparser.parse(realpath))
    except MacroVerdictConflictError as err:
        # The message is complete end-user prose (remedies included);
        # matching cake.main's rendering, not a traceback.
        if args.verbose >= 2:
            raise
        print(f"Error: {err}", file=sys.stderr)
        return 1

    print()
    return 0
