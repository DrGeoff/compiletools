"""StringZilla utility functions for SIMD-optimized string operations.

This module provides reusable StringZilla helper functions that are used across
multiple modules for efficient string processing and preprocessor analysis.
"""

from typing import List
import stringzilla


def strip_sz(sz_str: 'stringzilla.Str', chars: str = ' \t\r\n') -> 'stringzilla.Str':
    """Custom strip implementation for StringZilla.Str using character set operations."""
    start = sz_str.find_first_not_of(chars)
    if start == -1:
        return sz_str[0:0]  # Return empty Str if all characters are whitespace
    end = sz_str.find_last_not_of(chars)
    return sz_str[start:end + 1]


def ends_with_backslash_sz(sz_str: 'stringzilla.Str') -> bool:
    """Check if StringZilla.Str ends with backslash after trimming whitespace."""
    # Find last non-whitespace character
    last_non_ws = sz_str.find_last_not_of(' \t\r\n')
    if last_non_ws == -1:
        return False
    return sz_str[last_non_ws] == '\\'


def is_alpha_or_underscore_sz(sz_str: 'stringzilla.Str', pos: int = 0) -> bool:
    """Check if character at position is alphabetic or underscore using StringZilla."""
    if pos >= len(sz_str):
        return False
    # Use StringZilla character set operations for validation
    return (sz_str[pos:pos+1].find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_') == -1)


def join_lines_strip_backslash_sz(lines: List['stringzilla.Str']) -> 'stringzilla.Str':
    """Join lines stripping backslashes using StringZilla operations.

    Performance note: Despite the str/Str conversions, this approach is optimal because:
    1. strip_sz() provides SIMD-accelerated whitespace trimming
    2. Python's str.join() is dramatically faster than StringZilla concatenation
    3. The single conversion at the end minimizes overhead
    """
    from stringzilla import Str
    if not lines:
        return Str('')

    result_parts = []
    for line in lines:
        # Use SIMD-optimized strip_sz for whitespace removal
        trimmed = strip_sz(line, ' \t\r\n')
        if len(trimmed) > 0 and trimmed[-1] == '\\':
            trimmed = trimmed[:-1]  # Remove backslash
        trimmed = strip_sz(trimmed, ' \t')  # Remove trailing whitespace
        result_parts.append(str(trimmed))  # Convert to str for fast joining

    # Use Python's highly optimized string join, then convert once to StringZilla
    return Str(' '.join(result_parts))


def concat_sz(*parts) -> 'stringzilla.Str':
    """Efficient StringZilla.Str concatenation.

    Performance note: Uses Python's optimized string joining then converts once
    to StringZilla.Str to minimize conversion overhead while maintaining SIMD benefits.
    """
    from stringzilla import Str
    return Str(''.join(str(p) for p in parts))


def join_sz(separator: str, items) -> str:
    """Join StringZilla.Str items with separator, converting to str for compatibility.

    This helper enables tests and other consumers to use str.join() patterns
    with StringZilla.Str objects without manual conversion.
    """
    return separator.join(str(item) for item in items)
