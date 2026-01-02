"""File analysis module for efficient pattern detection in source files.

This module provides SIMD-optimized file analysis with StringZilla when available,
falling back to traditional regex-based analysis for compatibility.
"""

import mmap
import bisect
import resource
import sys
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Dict, List, Optional, Set, FrozenSet
from io import open

import stringzilla
import compiletools.wrappedos
import compiletools.filesystem_utils
from compiletools.stringzilla_utils import (
    strip_sz,
    ends_with_backslash_sz,
    is_alpha_or_underscore_sz,
    join_lines_strip_backslash_sz
)


class MarkerType(Enum):
    """Type of marker found in source file."""
    NONE = 0
    EXE = 1
    TEST = 2
    LIBRARY = 3


def is_position_commented_simd_optimized(str_text: 'stringzilla.Str', pos: int, line_byte_offsets: List[int]) -> bool:
    """Optimized comment detection using pre-computed line boundaries."""
    # Binary search for line start using precomputed line starts
    line_start_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
    line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0

    # Check for single-line comment on current line using StringZilla
    line_prefix_slice = str_text[line_start:pos]
    comment_pos = line_prefix_slice.find('//')
    if comment_pos != -1:
        return True

    # Check for multi-line block comment using StringZilla rfind
    last_block_start = str_text.rfind('/*', 0, pos)
    if last_block_start != -1:
        last_block_end = str_text.rfind('*/', last_block_start, pos)
        if last_block_end == -1:
            return True

    return False

def is_inside_block_comment_simd(str_text: 'stringzilla.Str', pos: int) -> bool:
    """Check if position is inside a multi-line block comment using StringZilla."""
    last_block_start = str_text.rfind('/*', 0, pos)
    if last_block_start != -1:
        last_block_end = str_text.rfind('*/', last_block_start, pos)
        if last_block_end == -1:
            return True

    return False


def find_include_positions_simd_bulk(str_text, line_byte_offsets: List[int]) -> List[int]:
    """Optimized include position finder using pre-computed line byte offsets.

    Vectorization: Minimizes Python-level loops by finding all positions in one pass.
    """
    positions = []

    # Single-pass search: find and validate in one loop
    pos = str_text.find('#include', 0)
    while pos != -1:
        if not is_position_commented_simd_optimized(str_text, pos, line_byte_offsets):
            positions.append(pos)
        pos = str_text.find('#include', pos + 8)  # Continue from next position

    return positions


def find_magic_positions_simd_bulk(str_text, line_byte_offsets: List[int]) -> List[int]:
    """Optimized magic position finder using pre-computed line byte offsets.

    Vectorization: Single-pass search avoiding intermediate list allocation.
    """
    positions = []

    # Single-pass search with inline validation
    pos = str_text.find('//#', 0)
    while pos != -1:
        # Binary search for line start
        line_start_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
        line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0

        # Check if only whitespace before //# using StringZilla slice
        if pos > line_start:
            line_prefix_slice = str_text[line_start:pos]
            # Use StringZilla's character set operations for efficient whitespace checking
            if line_prefix_slice.find_first_not_of(' \t\r\n') != -1:
                pos = str_text.find('//#', pos + 3)
                continue

        # Check if we're inside a block comment
        if is_inside_block_comment_simd(str_text, pos):
            pos = str_text.find('//#', pos + 3)
            continue

        # Look for KEY=value pattern after //# using StringZilla
        after_hash = pos + 3
        # Find the end of this line using line_byte_offsets
        current_line_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
        if current_line_idx + 1 < len(line_byte_offsets):
            line_end = line_byte_offsets[current_line_idx + 1] - 1  # End before next line starts
        else:
            line_end = len(str_text)  # Last line

        # Use StringZilla slice to find = efficiently
        line_content_slice = str_text[after_hash:line_end]
        equals_pos = line_content_slice.find('=')
        if equals_pos != -1:
            # Extract key part using StringZilla slice
            key_slice = line_content_slice[:equals_pos]

            # Use StringZilla's character set operations for efficient whitespace trimming
            start_pos = key_slice.find_first_not_of(' \t')
            if start_pos != -1:
                end_pos = key_slice.find_last_not_of(' \t')
                trimmed_key = key_slice[start_pos:end_pos + 1]
            else:
                trimmed_key = key_slice[0:0]  # Empty slice

            if len(trimmed_key) > 0:
                # Validate key format using StringZilla character set operations
                if trimmed_key.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-') == -1:
                    if is_alpha_or_underscore_sz(trimmed_key, 0):
                        positions.append(pos)

        # Continue search from next position
        pos = str_text.find('//#', pos + 3)

    return positions


def find_directive_positions_simd_bulk(str_text, line_byte_offsets: List[int]) -> Dict[str, List[int]]:
    """Optimized directive position finder using pre-computed newline positions.

    Vectorization: Single-pass search without intermediate list allocation.
    """
    directive_positions = {}

    # Pre-define common directives for faster lookup
    target_directives = {
        'include', 'ifdef', 'ifndef', 'define', 'undef', 'endif', 'else', 'elif',
        'pragma', 'error', 'warning', 'line', 'if'
    }

    # Single-pass search: find and process each # character
    hash_pos = str_text.find('#', 0)
    while hash_pos != -1:
        # Binary search for line start using precomputed line starts
        line_start_idx = bisect.bisect_right(line_byte_offsets, hash_pos) - 1
        line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0

        # Check if only whitespace before # using StringZilla slice
        if hash_pos > line_start:
            line_prefix_slice = str_text[line_start:hash_pos]
            # Use StringZilla's character set operations for efficient whitespace checking
            if line_prefix_slice.find_first_not_of(' \t\r\n') != -1:
                hash_pos = str_text.find('#', hash_pos + 1)
                continue

        # Extract directive name efficiently
        directive_start = hash_pos + 1
        # Skip whitespace after # using StringZilla
        directive_start = str_text.find_first_not_of(' \t', directive_start)
        if directive_start == -1:
            hash_pos = str_text.find('#', hash_pos + 1)
            continue

        # Find end of directive name using character set
        directive_end = str_text.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', directive_start)
        if directive_end == -1: # Directive takes up rest of string
            directive_end = len(str_text)

        if directive_end > directive_start:
            # Use StringZilla slice for directive name
            directive_slice = str_text[directive_start:directive_end]

            # Check if directive matches any target directive using StringZilla direct comparison
            for target_directive in target_directives:
                # Use StringZilla's efficient string comparison
                if directive_slice == target_directive:
                    if target_directive not in directive_positions:
                        directive_positions[target_directive] = []
                    directive_positions[target_directive].append(hash_pos)
                    break

        # Continue search from next position
        hash_pos = str_text.find('#', hash_pos + 1)

    return directive_positions



def parse_directive_struct(dtype: str, pos: int, line_num: int,
                          directive_lines: List['stringzilla.Str']) -> 'PreprocessorDirective':
    """Parse a directive into structured form using StringZilla operations."""
    full_text_str = join_lines_strip_backslash_sz(directive_lines)

    directive = PreprocessorDirective(
        line_num=line_num,
        byte_pos=pos,
        directive_type=dtype,
        continuation_lines=len(directive_lines) - 1
    )

    # Find start of content after directive
    content_start_pos = full_text_str.find(dtype)
    if content_start_pos == -1:
        return directive
    content_start_pos += len(dtype)

    # Skip whitespace after directive
    content_start_pos = full_text_str.find_first_not_of(' \t', content_start_pos)
    if content_start_pos == -1:
        return directive

    content_slice = full_text_str[content_start_pos:]

    if dtype in ('ifdef', 'ifndef', 'undef'):
        directive.macro_name = strip_sz(content_slice)

    elif dtype in ('if', 'elif'):
        directive.condition = strip_sz(content_slice)

    elif dtype == 'define':
        parts = content_slice.split(maxsplit=1)
        if len(parts) > 0:
            name_part = parts[0]
            # Handle function-like macros: extract name before '('
            paren_pos = name_part.find('(')
            if paren_pos != -1:
                directive.macro_name = name_part[:paren_pos]
            else:
                directive.macro_name = name_part

            if len(parts) > 1:
                directive.macro_value = strip_sz(parts[1])
            else:
                directive.macro_value = None

    return directive


# Global args storage for file analysis (set once per build)
_analyzer_args = None

# Module-level warning flags for one-time warnings
_warned_low_ulimit = False
_file_reading_strategy = None  # Cache the chosen strategy


def _warn_low_ulimit(total_files: int, soft_limit: int):
    """Warn once about low file descriptor limit."""
    global _warned_low_ulimit
    if _warned_low_ulimit:
        return
    if _analyzer_args and getattr(_analyzer_args, 'suppress_fd_warnings', False):
        return

    print(f"Warning: File descriptor limit too low for mmap mode (ulimit -n = {soft_limit})", file=sys.stderr)
    print(f"  Total files: {total_files}, available FDs (90% of limit): {int(soft_limit * 0.9)}", file=sys.stderr)
    print("  Using traditional file I/O instead of mmap to avoid 'Too many open files' errors", file=sys.stderr)
    print("  This is ~0.1-0.2ms slower per file but prevents EMFILE errors", file=sys.stderr)
    print(f"  To use faster mmap mode: ulimit -n {total_files * 2}", file=sys.stderr)
    print("  To suppress this warning: add '--suppress-fd-warnings' flag or config", file=sys.stderr)
    _warned_low_ulimit = True


_warned_mmap_failure = False

def _warn_mmap_failure(filepath: str, error: Exception):
    """Warn once about unexpected mmap failure and fallback."""
    global _warned_mmap_failure
    if _warned_mmap_failure:
        return
    if _analyzer_args and getattr(_analyzer_args, 'suppress_fd_warnings', False):
        return

    print(f"Warning: mmap failed for {filepath}: {error}", file=sys.stderr)
    print("  Falling back to traditional file reading for this and subsequent files", file=sys.stderr)
    print("  To suppress this warning: add '--suppress-fd-warnings' flag or config", file=sys.stderr)
    _warned_mmap_failure = True


def _determine_file_reading_strategy() -> str:
    """Determine which file reading strategy to use for this session.

    Called once at file_analyzer initialization.

    Returns:
        'mmap' - Use Str(File(filepath)) directly (best performance)
        'no_mmap' - Use traditional open()/read() (for low ulimit or problematic filesystems)
    """
    global _file_reading_strategy
    if _file_reading_strategy is not None:
        return _file_reading_strategy

    args = _analyzer_args

    # Check for manual overrides first
    if args and not getattr(args, 'use_mmap', True):
        _file_reading_strategy = 'no_mmap'
        return _file_reading_strategy

    if args and getattr(args, 'force_mmap', False):
        _file_reading_strategy = 'mmap'
        return _file_reading_strategy

    # Get total file count from global hash registry
    # IMPORTANT: Must load the registry first to get accurate file count
    try:
        from compiletools.global_hash_registry import load_hashes, get_registry_stats
        load_hashes()  # Ensure registry is loaded before checking stats
        stats = get_registry_stats()
        total_files = stats.get('total_files', 0)
    except (ImportError, AttributeError):
        total_files = 0

    # Query actual OS limit
    try:
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, AttributeError):
        soft_limit = 1024  # Reasonable fallback

    # If ulimit is dangerously low (< 100), always use no_mmap mode
    # This handles shells with ulimit 20, test environments, etc.
    if soft_limit < 100:
        if total_files > 0:
            _warn_low_ulimit(total_files, soft_limit)
        _file_reading_strategy = 'no_mmap'
        return _file_reading_strategy

    # Check filesystem type (use first file's filesystem as representative)
    # We'll check this per-file in actual implementation
    # For now, assume local filesystem and check per-file later

    # Compare file count to available fd limit
    # Use 90% of soft limit to leave headroom for Python/system overhead
    safe_fd_limit = int(soft_limit * 0.9)

    if total_files > 0 and total_files > safe_fd_limit:
        # Too many files for available fds - use traditional file I/O
        _warn_low_ulimit(total_files, soft_limit)
        _file_reading_strategy = 'no_mmap'
    else:
        # Default to mmap mode
        _file_reading_strategy = 'mmap'

    return _file_reading_strategy


def _read_file_with_strategy(filepath: str, strategy: str):
    """Read file using specified strategy.

    Args:
        filepath: Path to file to read
        strategy: 'mmap' or 'no_mmap' (based on ulimit/file count)

    Returns:
        stringzilla.Str object with file contents
    """
    # Strategy is about ulimit/resource constraints
    # Filesystem safety is handled by safe_read_text_file
    force_no_mmap = (strategy == 'no_mmap')

    return compiletools.filesystem_utils.safe_read_text_file(
        filepath,
        encoding='utf-8',
        force_no_mmap=force_no_mmap
    )


def set_analyzer_args(args):
    """Set global args for file analysis. Must be called once at build start.

    Args:
        args: Args object containing max_read_size, verbose, exemarkers, testmarkers, librarymarkers
    """
    global _analyzer_args, _file_reading_strategy
    _analyzer_args = args
    # Clear cached strategy to force re-evaluation with new args
    # This is important when tools like ct-cake call _createctobjs() multiple times
    _file_reading_strategy = None
    # Determine strategy with new args
    _determine_file_reading_strategy()

@lru_cache(maxsize=None)
def analyze_file(content_hash: str) -> 'FileAnalysisResult':
    """Direct file analysis with LRU caching - content hash based.

    Args:
        content_hash: Git blob hash of file content

    Raises:
        FileNotFoundError: If file with given hash not found
        RuntimeError: If analyzer args not set via set_analyzer_args()
    """
    if _analyzer_args is None:
        raise RuntimeError("analyze_file: analyzer args not set. Call set_analyzer_args() first.")

    args = _analyzer_args

    # Reverse lookup to get filepath (already realpath from registry)
    from compiletools.global_hash_registry import get_filepath_by_hash
    filepath = get_filepath_by_hash(content_hash)

    from stringzilla import Str

    # Extract parameters from args
    max_read_size = getattr(args, 'max_read_size', 0)
    exe_markers = getattr(args, 'exemarkers', [])
    test_markers = getattr(args, 'testmarkers', [])
    library_markers = getattr(args, 'librarymarkers', [])

    file_size = compiletools.wrappedos.getsize(filepath)

    # Determine file reading strategy
    strategy = _determine_file_reading_strategy()

    # Handle empty files - StringZilla cannot memory-map zero-byte files
    if file_size == 0:
        str_text = Str("")
        bytes_analyzed = 0
        was_truncated = False
    else:
        read_entire_file = (max_read_size == 0) or (file_size <= max_read_size)

        if read_entire_file:
            # Read entire file using appropriate strategy
            str_text = _read_file_with_strategy(filepath, strategy)
            bytes_analyzed = len(str_text)
            was_truncated = False
        else:
            # Read limited amount using mmap for better performance
            text, bytes_analyzed, was_truncated = read_file_mmap(filepath, max_read_size)
            try:
                str_text = Str(text)
            except UnicodeDecodeError:
                # This shouldn't happen since read_file_mmap decodes with errors='ignore'
                # But if it does, provide useful debugging info
                print(f"ERROR: Failed to create Str from text in {filepath}", file=sys.stderr)
                print(f"  text type: {type(text)}, len: {len(text)}", file=sys.stderr)
                print(f"  First 100 chars: {repr(text[:100])}", file=sys.stderr)
                raise

    # Use StringZilla's splitlines for optimal line processing
    lines = str_text.splitlines()

    # Build line_byte_offsets efficiently in a single pass
    # Vectorization: Avoid Python loop by using single-pass find with accumulation
    line_byte_offsets = [0]  # First line starts at position 0
    pos = str_text.find('\n', 0)
    while pos != -1:
        line_byte_offsets.append(pos + 1)  # Next line starts after newline
        pos = str_text.find('\n', pos + 1)  # Continue from next position

    # Find all pattern positions using optimized StringZilla bulk operations
    include_positions = find_include_positions_simd_bulk(str_text, line_byte_offsets)
    magic_positions = find_magic_positions_simd_bulk(str_text, line_byte_offsets)
    directive_positions = find_directive_positions_simd_bulk(str_text, line_byte_offsets)

    # Extract structured directive information
    directives = []
    directive_by_line = {}
    processed_lines = set()

    for dtype, positions in directive_positions.items():
        for pos in positions:
            # Use binary search on pre-computed line offsets for O(log n) performance
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            if line_num in processed_lines:
                continue

            # Extract directive with continuations using StringZilla
            directive_lines = []
            current_line = line_num
            while current_line < len(lines):
                line = lines[current_line]
                directive_lines.append(line)  # Already StringZilla.Str from splitlines()
                processed_lines.add(current_line)
                if not ends_with_backslash_sz(line):
                    break
                current_line += 1

            # Parse directive
            directive = parse_directive_struct(dtype, pos, line_num, directive_lines)
            directives.append(directive)
            directive_by_line[line_num] = directive

    # Extract includes with full information using bulk processing
    includes = []
    if include_positions:
        for pos in include_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else Str("")  # Already Str from splitlines()

            is_commented = is_position_commented_simd_optimized(str_text, pos, line_byte_offsets)

            # Extract filename and type using StringZilla, replacing regex
            include_keyword_pos = line.find('#include')
            if include_keyword_pos == -1:
                continue

            search_start = include_keyword_pos + 8  # len('#include')

            quote_pos = line.find('"', search_start)
            lt_pos = line.find('<', search_start)

            start_delim_pos = -1
            is_system = False
            end_delim = ''

            if quote_pos != -1 and (lt_pos == -1 or quote_pos < lt_pos):
                start_delim_pos = quote_pos
                end_delim = '"'
                is_system = False
            elif lt_pos != -1:
                start_delim_pos = lt_pos
                end_delim = '>'
                is_system = True

            if start_delim_pos != -1:
                end_delim_pos = line.find(end_delim, start_delim_pos + 1)
                if end_delim_pos != -1:
                    filename_slice = line[start_delim_pos + 1:end_delim_pos]
                    includes.append({
                        'line_num': line_num,
                        'byte_pos': pos,
                        'full_line': line,
                        'filename': filename_slice,
                        'is_system': is_system,
                        'is_commented': is_commented
                    })

    # Extract magic flags with full information using StringZilla operations
    magic_flags = []
    if magic_positions:
        for pos in magic_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else ""

            # Parse magic flag using StringZilla operations - ensure line is Str
            if not isinstance(line, Str):
                line = Str(line)
            hash_pos = line.find('//#')
            if hash_pos != -1:
                after_hash = line[hash_pos + 3:]  # Skip //#

                # Use StringZilla split for KEY=value parsing
                equals_parts = after_hash.split('=', maxsplit=1)
                if len(equals_parts) == 2:
                    key_part = equals_parts[0]
                    value_part = equals_parts[1]

                    # Trim whitespace using StringZilla character set operations
                    key_start = key_part.find_first_not_of(' \t')
                    if key_start != -1:
                        key_end = key_part.find_last_not_of(' \t')
                        key_trimmed = key_part[key_start:key_end + 1]

                        # Validate key format using StringZilla character set operations
                        if len(key_trimmed) > 0 and is_alpha_or_underscore_sz(key_trimmed, 0):
                            # Use StringZilla to check if all chars are valid (alphanumeric, _, -)
                            invalid_pos = key_trimmed.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-')
                            if invalid_pos == -1:  # No invalid characters found
                                # Trim value whitespace
                                value_start = value_part.find_first_not_of(' \t')
                                if value_start != -1:
                                    value_end = value_part.find_last_not_of(' \t\r\n')
                                    value_trimmed = value_part[value_start:value_end + 1]
                                else:
                                    value_trimmed = value_part[0:0]  # Empty Str

                                magic_flags.append({
                                    'line_num': line_num,
                                    'byte_pos': pos,
                                    'full_line': line,
                                    'key': key_trimmed,
                                    'value': value_trimmed
                                })

    # Sort directives by line number for correct guard detection
    # The directives list is built by iterating directive_positions.items()
    # which processes by directive TYPE, not line number order
    directives_sorted = sorted(directives, key=lambda d: d.line_num)

    # Detect include guard first so we can exclude it from defines
    include_guard = detect_include_guard(directives_sorted)

    # Extract defines with full information (excluding include guard)
    defines = []
    for pos in directive_positions.get('define', []):
        line_num = bisect.bisect_right(line_byte_offsets, pos) - 1

        # Get all lines including continuations using StringZilla
        define_lines = []
        current_line = line_num
        while current_line < len(lines):
            line = lines[current_line]
            define_lines.append(line)  # Already StringZilla.Str from splitlines()
            if not ends_with_backslash_sz(line):
                break
            current_line += 1

        # Parse define using StringZilla, replacing regex
        if not define_lines:
            continue

        first_line = define_lines[0]
        define_kw_pos = first_line.find('#define')
        if define_kw_pos == -1:
            continue

        # Find start of macro name
        name_start_pos = first_line.find_first_not_of(' \t', define_kw_pos + 7)
        if name_start_pos == -1:
            continue

        # Join lines for parsing complex defines using StringZilla
        full_define_str = join_lines_strip_backslash_sz(define_lines)

        # Find macro name part in the joined string
        name_part_start = full_define_str.find_first_not_of(' \t', full_define_str.find('#define') + 7)

        # Find end of name (space or parenthesis)
        paren_pos = full_define_str.find('(', name_part_start)
        space_pos = full_define_str.find_first_of(' \t', name_part_start)

        name_end_pos = -1
        if paren_pos != -1 and (space_pos == -1 or paren_pos < space_pos):
            name_end_pos = paren_pos
        else:
            name_end_pos = space_pos

        if name_end_pos == -1: # Macro without value
            name = full_define_str[name_part_start:]
            value = None
            is_function_like = False
            params = []
        else:
            name = full_define_str[name_part_start:name_end_pos]

            # Check for function-like macro
            is_function_like = (paren_pos == name_end_pos)
            if is_function_like:
                params_end_pos = full_define_str.find(')', paren_pos + 1)
                if params_end_pos != -1:
                    params_str = full_define_str[paren_pos + 1:params_end_pos]
                    params = [strip_sz(p) for p in params_str.split(',')] if params_str else []
                    value_start_pos = full_define_str.find_first_not_of(' \t', params_end_pos + 1)
                else: # Malformed
                    params = []
                    value_start_pos = -1
            else:
                params = []
                value_start_pos = full_define_str.find_first_not_of(' \t', name_end_pos)

            if value_start_pos != -1:
                value = strip_sz(full_define_str[value_start_pos:])
            else:
                value = None

        # Skip include guard - it's tracked separately and doesn't affect compilation
        if include_guard and name == include_guard:
            continue

        defines.append({
            'line_num': line_num,
            'byte_pos': pos,
            'lines': define_lines,
            'name': name,
            'value': value if value else None,
            'is_function_like': is_function_like,
            'params': params
        })

    # Extract unique headers
    system_headers = {inc['filename'] for inc in includes if inc['is_system']}
    quoted_headers = {inc['filename'] for inc in includes if not inc['is_system']}

    # Extract macros referenced in conditionals (for cache optimization)
    conditional_macros = _extract_conditional_macros(directives)

    # Detect marker type - check for exe, test, or library markers
    marker_type = MarkerType.NONE
    if exe_markers:
        for marker in exe_markers:
            if str_text.count(marker) > 0:
                marker_type = MarkerType.EXE
                break

    if marker_type == MarkerType.NONE and test_markers:
        for marker in test_markers:
            if str_text.count(marker) > 0:
                marker_type = MarkerType.TEST
                break

    if marker_type == MarkerType.NONE and library_markers:
        for marker in library_markers:
            if str_text.count(marker) > 0:
                marker_type = MarkerType.LIBRARY
                break

    result = FileAnalysisResult(
        line_count=len(lines),
        line_byte_offsets=line_byte_offsets,
        include_positions=include_positions,
        magic_positions=magic_positions,
        directive_positions=directive_positions,
        directives=directives,
        directive_by_line=directive_by_line,
        bytes_analyzed=bytes_analyzed,
        was_truncated=was_truncated,
        includes=includes,
        magic_flags=magic_flags,
        defines=defines,
        system_headers=system_headers,
        quoted_headers=quoted_headers,
        content_hash=content_hash,
        include_guard=include_guard,
        conditional_macros=conditional_macros,
        marker_type=marker_type
    )

    return result


def cache_clear():
    """Clear the file analysis cache and reset analyzer args."""
    global _analyzer_args
    _analyzer_args = None
    analyze_file.cache_clear()


def get_cache_stats():
    """Get cache statistics from LRU cache."""
    info = analyze_file.cache_info()
    return {
        'hits': info.hits,
        'misses': info.misses,
        'total_calls': info.hits + info.misses,
        'cache_size': info.currsize,
        'max_size': info.maxsize
    }


def print_cache_stats():
    """Print cache statistics."""
    stats = get_cache_stats()
    total = stats['total_calls']
    hits = stats['hits']
    misses = stats['misses']

    if total == 0:
        hit_rate = 0.0
    else:
        hit_rate = (hits / total) * 100

    print("\n=== FileAnalyzer Cache Statistics ===")
    print(f"Total calls:    {total:,}")
    print(f"Cache hits:     {hits:,} ({hit_rate:.1f}%)")
    print(f"Cache misses:   {misses:,}")
    print(f"Cache size:     {stats['cache_size']:,}")


# Attach cache management functions
analyze_file.get_cache_stats = get_cache_stats
analyze_file.print_cache_stats = print_cache_stats


def read_file_mmap(filepath, max_size=0):
    """Use memory-mapped I/O for large files with fallback to traditional reading.
    
    Args:
        filepath: Path to file to read
        max_size: Maximum bytes to read (0 = entire file)
        
    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = compiletools.wrappedos.getsize(filepath)
        
        # Handle empty files (mmap fails on zero-byte files)
        if file_size == 0:
            return "", 0, False
        
        with open(filepath, 'rb') as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if max_size > 0 and max_size < file_size:
                    data = mm[:max_size]
                    bytes_analyzed = max_size
                    was_truncated = True
                else:
                    data = mm[:]
                    bytes_analyzed = len(data)
                    was_truncated = False
                    
                text = data.decode('utf-8', errors='ignore')
                return text, bytes_analyzed, was_truncated
                
    except (OSError, IOError, ValueError):
        # Fallback to traditional reading on any mmap failure
        return read_file_traditional(filepath, max_size)


def read_file_traditional(filepath, max_size=0):
    """Traditional file reading fallback.
    
    Args:
        filepath: Path to file to read  
        max_size: Maximum bytes to read (0 = entire file)
        
    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = compiletools.wrappedos.getsize(filepath)
        
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            if max_size > 0 and max_size < file_size:
                text = f.read(max_size)
                bytes_analyzed = len(text.encode('utf-8'))
                was_truncated = True
            else:
                text = f.read()
                bytes_analyzed = len(text.encode('utf-8'))
                was_truncated = False
                
        return text, bytes_analyzed, was_truncated
        
    except (OSError, IOError, ValueError):
        # Return empty content on any error
        return "", 0, False

@dataclass
class PreprocessorDirective:
    """A preprocessor directive with all its content."""
    line_num: int                    # Starting line number (0-based)
    byte_pos: int                    # Byte position in original file
    directive_type: str              # 'if', 'ifdef', 'ifndef', 'elif', 'else', 'endif', 'define', 'undef', 'include'
    continuation_lines: int          # Number of continuation lines (for multi-line directives)
    condition: Optional['stringzilla.Str'] = None  # The condition expression (for if/ifdef/ifndef/elif)
    macro_name: Optional['stringzilla.Str'] = None # Macro name (for define/undef/ifdef/ifndef)
    macro_value: Optional['stringzilla.Str'] = None # Macro value (for define)


def _extract_conditional_macros(directives: List[PreprocessorDirective]) -> FrozenSet['stringzilla.Str']:
    """Extract all macro names referenced in conditional directives.

    Returns frozenset of sz.Str macro names from ifdef/ifndef/if/elif conditions.
    Used for cache optimization - files are effectively invariant when none
    of these macros are defined.
    """

    macros = set()

    for directive in directives:
        if directive.directive_type in ('ifdef', 'ifndef'):
            if directive.macro_name:
                macros.add(directive.macro_name)
        elif directive.directive_type in ('if', 'elif'):
            if directive.condition:
                # Extract identifiers from condition using stringzilla
                cond = directive.condition
                keywords = {'and', 'or', 'not', 'true', 'false', 'defined'}

                i = 0
                while i < len(cond):
                    # Skip non-identifier chars
                    if not is_alpha_or_underscore_sz(cond, i):
                        i += 1
                        continue

                    # Found start of identifier - vectorized
                    start = i
                    identifier_end = cond.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', start)
                    i = identifier_end if identifier_end != -1 else len(cond)

                    # Extract identifier
                    identifier = cond[start:i]
                    name = str(identifier)

                    if name not in keywords:
                        macros.add(identifier)

    return frozenset(macros)


def detect_include_guard(directives: List[PreprocessorDirective]) -> Optional['stringzilla.Str']:
    """Detect include guard macro from preprocessor directives.

    Supports both traditional include guards (#ifndef/#define) and #pragma once.
    Returns the guard macro name as StringZilla.Str or sz.Str("pragma_once") for #pragma once.
    """
    import stringzilla as sz

    if not directives:
        return None

    # Check for #pragma once first (simpler case)
    for directive in directives:
        if (directive.directive_type == 'pragma' and
            directive.condition and
            'once' in directive.condition):
            return sz.Str("pragma_once")

    # Check for traditional include guard pattern: #ifndef GUARD followed by #define GUARD
    for i, directive in enumerate(directives):
        if (directive.directive_type == 'ifndef' and
            directive.macro_name):

            guard_candidate = directive.macro_name

            # Look ahead up to 5 positions for the matching #define
            # This handles cases where comments or other directives appear between
            # the #ifndef and the matching #define
            for j in range(i + 1, min(i + 6, len(directives))):
                if (directives[j].directive_type == 'define' and
                    directives[j].macro_name and
                    directives[j].macro_name == guard_candidate):

                    # guard_candidate is already sz.Str from PreprocessorDirective.macro_name
                    return guard_candidate

    return None


@dataclass
class FileAnalysisResult:
    """Complete structured result without text field.
    
    Provides all information needed by consumers without requiring text reconstruction.
    """
    
    # Line-level data (for SimplePreprocessor) - required fields first
    line_count: int                         # Number of lines in the file
    line_byte_offsets: List[int]            # Byte offset where each line starts
    
    # Position arrays (for fast lookups) - required fields
    include_positions: List[int]            # Byte positions of #include directives
    magic_positions: List[int]              # Byte positions of //#KEY= patterns
    directive_positions: Dict[str, List[int]]  # Byte positions by directive type
    
    # Preprocessor directives (structured for SimplePreprocessor) - required fields
    directives: List[PreprocessorDirective]  # All directives with full context
    directive_by_line: Dict[int, PreprocessorDirective]  # Line number -> directive mapping
    
    # Metadata - required fields
    bytes_analyzed: int                     # Bytes analyzed from file
    was_truncated: bool                     # Whether file was truncated
    
    # Optional fields with defaults come last
    includes: List[Dict] = field(default_factory=list)
    # Each include dict contains:
    # {
    #   'line_num': int,                # Line number (0-based)
    #   'byte_pos': int,                # Byte position
    #   'full_line': str,               # Complete include line (str for compatibility)
    #   'filename': stringzilla.Str,    # Extracted filename
    #   'is_system': bool,              # True for <>, False for ""
    #   'is_commented': bool,           # True if in comment
    # }
    
    magic_flags: List[Dict] = field(default_factory=list)
    # Each magic flag dict contains:
    # {
    #   'line_num': int,           # Line number (0-based)
    #   'byte_pos': int,                 # Byte position
    #   'full_line': stringzilla.Str,   # Complete line with //#KEY=value
    #   'key': stringzilla.Str,          # The KEY part
    #   'value': stringzilla.Str,        # The value part
    # }
    
    defines: List[Dict] = field(default_factory=list)
    # Each define dict contains:
    # {
    #   'line_num': int,                        # Starting line number
    #   'byte_pos': int,                        # Byte position
    #   'lines': List[stringzilla.Str],         # All lines including continuations
    #   'name': stringzilla.Str,                # Macro name
    #   'value': Optional[stringzilla.Str],     # Macro value (if any)
    #   'is_function_like': bool,               # True for function-like macros
    #   'params': List[stringzilla.Str],        # Parameters for function-like macros
    # }
    
    system_headers: Set[str] = field(default_factory=set)  # Unique system headers found
    quoted_headers: Set[str] = field(default_factory=set)  # Unique quoted headers found
    content_hash: str = ""                  # SHA1 of original content
    include_guard: Optional['stringzilla.Str'] = None  # Include guard macro name (traditional) or sz.Str("pragma_once") for #pragma once
    conditional_macros: FrozenSet['stringzilla.Str'] = field(default_factory=frozenset)  # Macros referenced in conditionals (for cache optimization)
    marker_type: MarkerType = MarkerType.NONE  # Type of marker found in file (exe, test, library, or none)
    
    # Helper method for SimplePreprocessor compatibility
    def get_directive_line_numbers(self) -> Dict[str, Set[int]]:
        """Get line numbers for each directive type (for SimplePreprocessor)."""
        result = {}
        for dtype, positions in self.directive_positions.items():
            line_nums = set()
            for pos in positions:
                # Binary search in line_byte_offsets to find line number
                line_num = bisect.bisect_right(self.line_byte_offsets, pos) - 1
                line_nums.add(line_num)
            result[dtype] = line_nums
        return result


class FileAnalyzer:
    """SIMD-optimized implementation using StringZilla.

    IMPORTANT: FileAnalyzer provides an INVARIANT file summary - the same file
    should always produce the same analysis result regardless of external context
    like preprocessor flags, compiler settings, or magic mode. This ensures
    reliable caching and consistent behavior across different build configurations.

    Preprocessing and context-dependent analysis should be handled at higher levels
    (e.g., in MagicFlags classes) that can use FileAnalyzer's invariant results
    as a foundation.
    """

    @staticmethod
    def add_arguments(cap):
        """Add file analyzer specific arguments.

        Args:
            cap: ConfigArgParse parser instance
        """
        import compiletools.utils

        # Manual overrides for testing/debugging
        compiletools.utils.add_flag_argument(
            parser=cap,
            name="use-mmap",
            dest="use_mmap",
            default=True,
            help="Use mmap for file reading. Disable with --no-use-mmap for GPFS, SMB/CIFS, etc."
        )

        compiletools.utils.add_flag_argument(
            parser=cap,
            name="force-mmap",
            dest="force_mmap",
            default=False,
            help="Force mmap mode even on low ulimit systems (for testing/debugging)"
        )

        # Warning suppression
        compiletools.utils.add_flag_argument(
            parser=cap,
            name="suppress-fd-warnings",
            dest="suppress_fd_warnings",
            default=False,
            help="Suppress file descriptor limit warnings"
        )

        compiletools.utils.add_flag_argument(
            parser=cap,
            name="suppress-filesystem-warnings",
            dest="suppress_filesystem_warnings",
            default=False,
            help="Suppress filesystem compatibility warnings"
        )

    def __init__(self, content_hash: str, args):
        """Initialize file analyzer.

        Args:
            content_hash: Git blob hash of file content
            args: Args object containing max_read_size, verbose, exemarkers, testmarkers, librarymarkers
        """
        if content_hash is None:
            raise ValueError("content_hash must be provided")

        self.content_hash = content_hash
        self.args = args

        # Set global analyzer args for LRU caching
        set_analyzer_args(args)

        # StringZilla is now mandatory - no fallbacks
        import stringzilla as sz
        self.Str = sz.Str

    # StringZilla utility methods now use the shared stringzilla_utils module

    def _should_read_entire_file(self, file_size: Optional[int] = None) -> bool:
        """Determine if entire file should be read based on configuration."""
        max_read_size = getattr(self.args, 'max_read_size', 0)
        if max_read_size == 0:
            return True
        if file_size and file_size <= max_read_size:
            return True
        return False

    def analyze(self) -> FileAnalysisResult:
        """Analyze file using shared module-level LRU cache."""
        # Args must be set globally via set_analyzer_args before calling analyze
        return analyze_file(self.content_hash)
    
    # _parse_directive_struct method removed - now uses stringzilla_utils module
    
    
    def _find_include_positions_simd_bulk(self, str_text, line_byte_offsets: List[int]) -> List[int]:
        """Optimized include position finder using pre-computed line byte offsets."""
        # Pre-allocate using StringZilla count for better performance
        include_count = str_text.count('#include')
        include_positions = [0] * include_count  # Pre-allocate list
        pos_idx = 0
        
        # Find all '#include' occurrences in bulk
        start = 0
        while pos_idx < include_count:
            pos = str_text.find('#include', start)
            if pos == -1:
                break
            include_positions[pos_idx] = pos
            pos_idx += 1
            start = pos + 8  # len('#include')
        
        # Truncate list if we found fewer than expected
        if pos_idx < include_count:
            include_positions = include_positions[:pos_idx]
        
        positions = []
        
        # Batch process all include positions using pre-computed line starts
        for pos in include_positions:
            if not is_position_commented_simd_optimized(str_text, pos, line_byte_offsets):
                positions.append(pos)
        
        return positions
    
    def _find_magic_positions_simd_bulk(self, str_text, line_byte_offsets: List[int]) -> List[int]:
        """Optimized magic position finder using pre-computed line byte offsets."""
        positions = []
        
        # Pre-allocate using StringZilla count for better performance
        magic_count = str_text.count('//#')
        magic_positions = [0] * magic_count  # Pre-allocate list
        pos_idx = 0
        
        # Find all '//# occurrences in bulk
        start = 0
        while pos_idx < magic_count:
            pos = str_text.find('//#', start)
            if pos == -1:
                break
            magic_positions[pos_idx] = pos
            pos_idx += 1
            start = pos + 3  # len('//#')
        
        # Truncate list if we found fewer than expected
        if pos_idx < magic_count:
            magic_positions = magic_positions[:pos_idx]
        
        # Batch process all magic flag positions using pre-computed line starts
        for pos in magic_positions:
            # Binary search for line start
            line_start_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
            line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0
            
            # Check if only whitespace before //# using StringZilla slice
            if pos > line_start:
                line_prefix_slice = str_text[line_start:pos]
                # Use StringZilla's character set operations for efficient whitespace checking
                if line_prefix_slice.find_first_not_of(' \t\r\n') != -1:
                    continue
            
            # Check if we're inside a block comment
            if is_inside_block_comment_simd(str_text, pos):
                continue
            
            # Look for KEY=value pattern after //# using StringZilla
            after_hash = pos + 3
            # Find the end of this line using line_byte_offsets
            current_line_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
            if current_line_idx + 1 < len(line_byte_offsets):
                line_end = line_byte_offsets[current_line_idx + 1] - 1  # End before next line starts
            else:
                line_end = len(str_text)  # Last line
            
            # Use StringZilla slice to find = efficiently
            line_content_slice = str_text[after_hash:line_end]
            equals_pos = line_content_slice.find('=')
            if equals_pos != -1:
                # Extract key part using StringZilla slice
                key_slice = line_content_slice[:equals_pos]
                
                # Use StringZilla's character set operations for efficient whitespace trimming
                start_pos = key_slice.find_first_not_of(' \t')
                if start_pos != -1:
                    end_pos = key_slice.find_last_not_of(' \t')
                    trimmed_key = key_slice[start_pos:end_pos + 1]
                else:
                    trimmed_key = key_slice[0:0]  # Empty slice
                
                if len(trimmed_key) > 0:
                    # Validate key format using StringZilla character set operations
                    if trimmed_key.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-') == -1:
                        if is_alpha_or_underscore_sz(trimmed_key, 0):
                            positions.append(pos)
        
        return positions
    
    def _find_directive_positions_simd_bulk(self, str_text, line_byte_offsets: List[int]) -> Dict[str, List[int]]:
        """Optimized directive position finder using pre-computed newline positions."""
        directive_positions = {}
        
        # Pre-define common directives for faster lookup
        target_directives = {
            'include', 'ifdef', 'ifndef', 'define', 'undef', 'endif', 'else', 'elif',
            'pragma', 'error', 'warning', 'line', 'if'
        }
        
        # Pre-allocate using StringZilla count for better performance
        hash_count = str_text.count('#')
        hash_positions = [0] * hash_count  # Pre-allocate list
        pos_idx = 0
        
        # Find all # characters in bulk
        start = 0
        while pos_idx < hash_count:
            pos = str_text.find('#', start)
            if pos == -1:
                break
            hash_positions[pos_idx] = pos
            pos_idx += 1
            start = pos + 1
        
        # Truncate list if we found fewer than expected
        if pos_idx < hash_count:
            hash_positions = hash_positions[:pos_idx]
            
        # Process hash positions efficiently using pre-computed line boundaries
        for hash_pos in hash_positions:
            # Binary search for line start using precomputed line starts
            line_start_idx = bisect.bisect_right(line_byte_offsets, hash_pos) - 1
            line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0
            
            # Check if only whitespace before # using StringZilla slice
            if hash_pos > line_start:
                line_prefix_slice = str_text[line_start:hash_pos]
                # Use StringZilla's character set operations for efficient whitespace checking
                if line_prefix_slice.find_first_not_of(' \t\r\n') != -1:
                    continue
            
            # Extract directive name efficiently
            directive_start = hash_pos + 1
            # Skip whitespace after # using StringZilla
            directive_start = str_text.find_first_not_of(' \t', directive_start)
            if directive_start == -1:
                continue
            
            # Find end of directive name using character set
            directive_end = str_text.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', directive_start)
            if directive_end == -1: # Directive takes up rest of string
                directive_end = len(str_text)

            if directive_end > directive_start:
                # Use StringZilla slice for directive name
                directive_slice = str_text[directive_start:directive_end]
                
                # Check if directive matches any target directive using StringZilla direct comparison
                for target_directive in target_directives:
                    # Use StringZilla's efficient string comparison
                    if directive_slice == target_directive:
                        if target_directive not in directive_positions:
                            directive_positions[target_directive] = []
                        directive_positions[target_directive].append(hash_pos)
                        break
        
        return directive_positions
        
    # Note: _is_position_commented_simd_optimized and _is_inside_block_comment_simd methods
    # are now module-level functions in this file
        




