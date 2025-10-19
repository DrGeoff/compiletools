from __future__ import annotations

import os
import inspect
import functools
import shlex
import argparse
from pathlib import Path
from typing import Any, Union
from collections.abc import Iterable
from itertools import chain
import compiletools.wrappedos

# Public API
__all__ = [
    'CPP_SOURCE_EXTS', 'C_SOURCE_EXTS', 'ALL_SOURCE_EXTS', 'HEADER_EXTS',
    'is_non_string_iterable', 'split_command_cached',
    'is_header', 'is_cpp_source', 'is_c_source', 'is_source', 'is_executable',
    'implied_source', 'implied_header', 'clear_cache',
    'extract_init_args', 'to_bool', 'add_boolean_argument', 'add_flag_argument',
    'remove_mount', 'ordered_unique', 'ordered_union', 'ordered_difference',
    'deduplicate_compiler_flags', 'combine_and_deduplicate_compiler_flags'
]

# Module-level constant for C++ source extensions (lowercase)
CPP_SOURCE_EXTS = frozenset({
    '.cpp', '.cxx', '.cc', '.c++', '.cp', '.mm', '.ixx'
})

C_SOURCE_EXTS = frozenset({".c"})

# Combined source extensions for C and C++
ALL_SOURCE_EXTS = CPP_SOURCE_EXTS | C_SOURCE_EXTS

# Header file extensions (lowercase)
HEADER_EXTS = frozenset({'.h', '.hpp', '.hxx', '.hh', '.inl'})

# Source extensions with case variations for implied_source function
SOURCE_EXTS_WITH_CASE = frozenset({'.cpp', '.cxx', '.cc', '.c++', '.cp', '.mm', '.ixx', '.c', '.C', '.CC'})

# Header extensions with case variations for implied_header function
HEADER_EXTS_WITH_CASE = frozenset({'.h', '.hpp', '.hxx', '.hh', '.inl', '.H', '.HH'})

# Boolean conversion mapping for to_bool function
BOOL_MAP = {
    # True values
    "yes": True, "y": True, "true": True, "t": True, "1": True, "on": True,
    # False values
    "no": False, "n": False, "false": False, "f": False, "0": False, "off": False
}

@functools.lru_cache(maxsize=None)
def _get_lower_ext(filename: str) -> str:
    """Fast extension extraction and lowercase conversion."""
    idx = filename.rfind('.')
    if idx == -1 or idx == len(filename) - 1:
        return ""
    return filename[idx:].lower()

def is_non_string_iterable(obj: Any) -> bool:
    """Check if an object is an iterable but not a string.

    Args:
        obj: Object to check

    Returns:
        True if object is iterable but not a string-like type
    """
    return isinstance(obj, Iterable) and not isinstance(obj, (str, bytes, bytearray))

@functools.lru_cache(maxsize=None)
def split_command_cached(command_line: str) -> list[str]:
    """Cache shlex parsing results"""
    return shlex.split(command_line)


@functools.lru_cache(maxsize=None)
def split_command_cached_sz(command_line_sz) -> list:
    """StringZilla-aware version returning StringZilla.Str list"""
    import stringzilla as sz
    str_results = shlex.split(command_line_sz.decode('utf-8'))
    return [sz.Str(s) for s in str_results]


@functools.lru_cache(maxsize=None)
def is_header(filename: str) -> bool:
    """Is filename a header file?"""
    return _get_lower_ext(filename) in HEADER_EXTS

@functools.lru_cache(maxsize=None)
def is_cpp_source(path: str) -> bool:
    """Lightweight C++ source detection by extension (case-insensitive)."""
    # Fast path: split once
    _, ext = os.path.splitext(path)
    # Handle .C (uppercase) as C++, but regular extensions use lowercase
    if ext == ".C":
        return True
    return ext.lower() in CPP_SOURCE_EXTS

@functools.lru_cache(maxsize=None)
def is_c_source(path: str) -> bool:
    """Test if the given file has a .c extension (but not .C which is C++)."""
    _, ext = os.path.splitext(path)
    # .c (lowercase) is C, but .C (uppercase) is C++
    return ext == ".c"

@functools.lru_cache(maxsize=None)
def is_source(filename: str) -> bool:
    """Is the filename a source file?"""
    return _get_lower_ext(filename) in ALL_SOURCE_EXTS



def is_executable(filename: str) -> bool:
    return os.path.isfile(filename) and os.access(filename, os.X_OK)


def _find_file_with_extensions(filename: str, extensions: frozenset[str]) -> str | None:
    """Generic helper to find a file with different extensions.

    Args:
        filename: Base filename to search for
        extensions: Tuple of extensions to try

    Returns:
        Real path of found file, or None if no file exists
    """
    if not filename:
        return None

    basename = os.path.splitext(filename)[0]
    for ext in extensions:
        trialpath = basename + ext
        if compiletools.wrappedos.isfile(trialpath):
            return compiletools.wrappedos.realpath(trialpath)
    return None


@functools.lru_cache(maxsize=None)
def implied_source(filename: str) -> str | None:
    """Find the source file corresponding to a header file.

    If a header file is included in a build, find the corresponding
    C or C++ source file that should also be built.

    Args:
        filename: Header filename to find source for

    Returns:
        Path to corresponding source file, or None if not found
    """
    return _find_file_with_extensions(filename, SOURCE_EXTS_WITH_CASE)


@functools.lru_cache(maxsize=None)
def implied_header(filename: str) -> str | None:
    """Find the header file corresponding to a source file.

    Args:
        filename: Source filename to find header for

    Returns:
        Path to corresponding header file, or None if not found
    """
    return _find_file_with_extensions(filename, HEADER_EXTS_WITH_CASE)


def clear_cache() -> None:
    """Clear all function caches."""
    _get_lower_ext.cache_clear()
    split_command_cached.cache_clear()
    split_command_cached_sz.cache_clear()
    is_header.cache_clear()
    is_cpp_source.cache_clear()
    is_c_source.cache_clear()
    is_source.cache_clear()
    implied_source.cache_clear()
    implied_header.cache_clear()


def extract_init_args(args: argparse.Namespace, classname: type) -> dict[str, Any]:
    """Extract the arguments that classname.__init__ needs out of args.

    Args:
        args: Namespace containing parsed arguments
        classname: Class whose __init__ method signature to inspect

    Returns:
        Dictionary of arguments needed by classname.__init__
    """
    sig = inspect.signature(classname.__init__)
    # Filter out 'self' and get only the parameters we care about
    params = {
        p.name for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY) and p.name != "self"
    }
    return {key: value for key, value in vars(args).items() if key in params}


def to_bool(value: Any) -> bool:
    """Convert a wide variety of values to a boolean.

    Args:
        value: Value to convert to boolean

    Returns:
        bool: Converted boolean value

    Raises:
        ValueError: If value cannot be converted to boolean
    """
    # Handle boolean values directly
    if isinstance(value, bool):
        return value

    str_value = str(value).strip().lower()
    if str_value in BOOL_MAP:
        return BOOL_MAP[str_value]

    # Better error message showing acceptable values
    acceptable = sorted(BOOL_MAP.keys())
    raise ValueError(f"Cannot convert {value!r} to boolean. Expected one of: {', '.join(acceptable)} or True/False.")


def add_boolean_argument(
    parser: argparse.ArgumentParser,
    name: str,
    dest: str | None = None,
    default: bool = False,
    help: str | None = None,
    allow_value_conversion: bool = True
) -> None:
    """Add a boolean argument to an ArgumentParser instance.

    Args:
        parser: ArgumentParser to add the argument to
        name: Name of the argument (without --)
        dest: Destination attribute name (defaults to name)
        default: Default value
        help: Help text
        allow_value_conversion: If True, allows value conversion (e.g., --flag=yes),
                               if False, treats as simple flag (--flag or --no-flag only)
    """
    dest = dest or name
    group = parser.add_mutually_exclusive_group()
    suffix = f"Use --no-{name} to turn the feature off."
    bool_help = f"{help} {suffix}" if help else suffix

    if allow_value_conversion:
        group.add_argument(
            f"--{name}",
            metavar="",
            nargs="?",
            dest=dest,
            default=default,
            const=True,
            type=to_bool,
            help=bool_help,
        )
    else:
        group.add_argument(
            f"--{name}", dest=dest, default=default, action="store_true", help=bool_help
        )

    group.add_argument(f"--no-{name}", dest=dest, action="store_false")


def add_flag_argument(
    parser: argparse.ArgumentParser,
    name: str,
    dest: str | None = None,
    default: bool = False,
    help: str | None = None
) -> None:
    """Add a flag argument to an ArgumentParser instance.

    This is a convenience wrapper around add_boolean_argument with
    allow_value_conversion=False for simple flag behavior.
    """
    add_boolean_argument(parser, name, dest, default, help, allow_value_conversion=False)


def remove_mount(absolutepath: Union[str, Path]) -> str:
    """Remove the mount point from an absolute path.

    Args:
        absolutepath: Absolute path to process

    Returns:
        Path with mount point removed

    Examples:
        >>> remove_mount("/home/user/file.txt")
        "home/user/file.txt"
        >>> remove_mount("C:\\Users\\user\\file.txt")  # Windows
        "Users\\user\\file.txt"
    """
    path = Path(absolutepath)
    if not path.is_absolute():
        raise ValueError(f"Path must be absolute: {absolutepath}")

    # Get parts and skip the root/anchor
    parts = path.parts[1:]  # Skip root ('/' on Unix, 'C:\\' on Windows)
    return str(Path(*parts)) if parts else ""


def ordered_unique(iterable: Iterable[Any]) -> list[Any]:
    """Return unique items from iterable preserving insertion order.

    Uses dict.fromkeys() which is guaranteed to preserve insertion
    order in Python 3.7+. This replaces OrderedSet for most use cases.
    """
    return list(dict.fromkeys(iterable))


def ordered_union(*iterables: Iterable[Any]) -> list[Any]:
    """Return union of multiple iterables preserving order.

    Uses dict.fromkeys() to maintain insertion order and uniqueness.
    This replaces OrderedSet union operations.
    """
    return list(dict.fromkeys(chain(*iterables)))


def deduplicate_compiler_flags(flags: list[str]) -> list[str]:
    """Deduplicate compiler flags with smart handling for flag-argument pairs.

    Handles both single flags and flag-argument pairs like:
    - '-I path', '-isystem path', '-L path', '-D macro'
    - '-Ipath', '-isystempath', '-Lpath', '-Dmacro'

    Preserves order and removes duplicates based on the argument/path portion.
    """
    if not flags:
        return flags

    # Flags that take arguments (both separate and combined forms)
    # Ordered longest-first to ensure correct prefix matching (-framework before -F)
    FLAG_WITH_ARGS = ('-framework', '-isystem', '-I', '-L', '-l', '-D', '-U', '-F')

    deduplicated = []
    seen_flag_args = {}  # flag -> set of seen arguments
    seen_simple_flags = set()
    i = 0

    while i < len(flags):
        flag = flags[i]

        # Find matching flag prefix efficiently
        matched_flag = None
        for flag_prefix in FLAG_WITH_ARGS:
            if flag == flag_prefix or (flag.startswith(flag_prefix) and len(flag) > len(flag_prefix)):
                matched_flag = flag_prefix
                break

        if matched_flag:
            if flag == matched_flag and i + 1 < len(flags):
                # Separate form: '-I path'
                arg = flags[i + 1]
                if matched_flag not in seen_flag_args:
                    seen_flag_args[matched_flag] = set()
                if arg not in seen_flag_args[matched_flag]:
                    deduplicated.extend([flag, arg])
                    seen_flag_args[matched_flag].add(arg)
                i += 2
            elif flag.startswith(matched_flag):
                # Combined form: '-Ipath'
                arg = flag[len(matched_flag):]
                if matched_flag not in seen_flag_args:
                    seen_flag_args[matched_flag] = set()
                if arg not in seen_flag_args[matched_flag]:
                    deduplicated.append(flag)
                    seen_flag_args[matched_flag].add(arg)
                i += 1
            else:
                i += 1
        else:
            # Regular flag - use set-based deduplication for O(1) lookup
            if flag not in seen_simple_flags:
                deduplicated.append(flag)
                seen_simple_flags.add(flag)
            i += 1

    return deduplicated


def _process_flag_source(source: Union[str, list[str], tuple[str, ...]]) -> list[str]:
    """Process a single flag source into a list of individual flags."""
    if not source:
        return []

    if isinstance(source, str):
        return split_command_cached(source)

    if isinstance(source, (list, tuple)):
        flags = []
        for item in source:
            if isinstance(item, str):
                # Check if item might be a multi-flag string
                if ' ' in item and not item.startswith('/'):
                    flags.extend(split_command_cached(item))
                else:
                    flags.append(item)
            else:
                flags.append(str(item))
        return flags

    return [str(source)]


def combine_and_deduplicate_compiler_flags(*flag_sources: Union[str, list[str], tuple[str, ...]]) -> list[str]:
    """Combine multiple sources of compiler flags and deduplicate intelligently.

    Takes multiple flag sources (lists or strings) and:
    1. Converts strings to flag lists using shlex_split
    2. Combines all sources preserving order
    3. Deduplicates using smart compiler flag logic

    Args:
        *flag_sources: Multiple sources of flags - can be lists of strings or single strings

    Returns:
        Combined and deduplicated list of flags
    """
    combined_flags = []
    for source in flag_sources:
        combined_flags.extend(_process_flag_source(source))

    return deduplicate_compiler_flags(combined_flags)


def ordered_difference(iterable: Iterable[Any], subtract: Iterable[Any]) -> list[Any]:
    """Return items from iterable not in subtract, preserving order.

    This replaces OrderedSet difference operations.
    """
    subtract_set = set(subtract)
    return [item for item in dict.fromkeys(iterable) if item not in subtract_set]
