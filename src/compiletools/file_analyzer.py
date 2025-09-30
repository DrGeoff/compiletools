"""File analysis module for efficient pattern detection in source files.

This module provides SIMD-optimized file analysis with StringZilla when available,
falling back to traditional regex-based analysis for compatibility.
"""

import os
import mmap
import bisect
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Set, FrozenSet
from io import open

import stringzilla
import compiletools.wrappedos
from compiletools.stringzilla_utils import (
    strip_sz,
    ends_with_backslash_sz,
    is_alpha_or_underscore_sz,
    join_lines_strip_backslash_sz
)


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


def find_magic_positions_simd_bulk(str_text, line_byte_offsets: List[int]) -> List[int]:
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


def find_directive_positions_simd_bulk(str_text, line_byte_offsets: List[int]) -> Dict[str, List[int]]:
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


# Module-level cached file analysis - no object creation needed
@lru_cache(maxsize=None)
def analyze_file(filepath: str, max_read_size: int = 0, verbose: int = 0) -> 'FileAnalysisResult':
    """Direct file analysis with caching - no object creation.

    Raises:
        FileNotFoundError: If filepath does not exist
    """
    # Get content hash from global registry - raises FileNotFoundError if file missing
    from compiletools.global_hash_registry import get_file_hash
    content_hash = get_file_hash(filepath)

    filepath = compiletools.wrappedos.realpath(filepath)

    from stringzilla import Str, File

    file_size = os.path.getsize(filepath)

    # Handle empty files - StringZilla cannot memory-map zero-byte files
    if file_size == 0:
        str_text = Str("")
        bytes_analyzed = 0
        was_truncated = False
    else:
        read_entire_file = (max_read_size == 0) or (file_size <= max_read_size)

        if read_entire_file:
            # Memory-map entire file and keep as Str for SIMD operations
            str_text = Str(File(filepath))
            bytes_analyzed = len(str_text)
            was_truncated = False
        else:
            # Read limited amount using mmap for better performance
            text, bytes_analyzed, was_truncated = read_file_mmap(filepath, max_read_size)
            str_text = Str(text)

    # Use StringZilla's splitlines for optimal line processing
    lines = str_text.splitlines()

    # Calculate line_byte_offsets using StringZilla's accelerated find operations
    line_byte_offsets = [0]  # First line starts at position 0
    pos = 0
    while True:
        pos = str_text.find('\n', pos)
        if pos == -1:
            break
        line_byte_offsets.append(pos + 1)  # Next line starts after newline
        pos += 1

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
            line_str = str(lines[line_num]) if line_num < len(lines) else ""
            line = Str(line_str)

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
                        'full_line': line_str,
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

    # Detect include guard first so we can exclude it from defines
    include_guard = detect_include_guard(directives)

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

    return FileAnalysisResult(
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
        conditional_macros=conditional_macros
    )


def read_file_mmap(filepath, max_size=0):
    """Use memory-mapped I/O for large files with fallback to traditional reading.
    
    Args:
        filepath: Path to file to read
        max_size: Maximum bytes to read (0 = entire file)
        
    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = os.path.getsize(filepath)
        
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
        file_size = os.path.getsize(filepath)
        
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

                    # Found start of identifier
                    start = i
                    while i < len(cond) and (is_alpha_or_underscore_sz(cond, i) or
                                           (i > start and cond[i:i+1].find_first_not_of('0123456789') == -1)):
                        i += 1

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

            # Check if the next directive is #define with the same name
            if (i + 1 < len(directives) and
                directives[i + 1].directive_type == 'define' and
                directives[i + 1].macro_name and
                directives[i + 1].macro_name == guard_candidate):

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
    
    def __init__(self, filepath: str, max_read_size: int = 0, verbose: int = 0):
        """Initialize file analyzer.
        
        Args:
            filepath: Path to file to analyze (required)
            max_read_size: Maximum bytes to read (0 = entire file)
            verbose: Verbosity level for debugging
        """
        if filepath is None:
            raise ValueError("filepath must be provided")
            
        self.filepath = compiletools.wrappedos.realpath(filepath)
        self.max_read_size = max_read_size
        self.verbose = verbose
        
        # StringZilla is now mandatory - no fallbacks
        import stringzilla as sz
        self.Str = sz.Str
        
    # StringZilla utility methods now use the shared stringzilla_utils module

    def _should_read_entire_file(self, file_size: Optional[int] = None) -> bool:
        """Determine if entire file should be read based on configuration."""
        if self.max_read_size == 0:
            return True
        if file_size and file_size <= self.max_read_size:
            return True
        return False
    
    def analyze(self) -> FileAnalysisResult:
        """Analyze file using shared module-level cache."""
        return analyze_file(self.filepath, self.max_read_size, self.verbose)
    
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
        




