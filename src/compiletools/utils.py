import os
import inspect
import functools
import shlex
import argparse
from typing import Any, Iterable
from itertools import chain
import compiletools.wrappedos

# Module-level constant for C++ source extensions (lowercase)
_CPP_SOURCE_EXTS = frozenset({
    '.cpp', '.cxx', '.cc', '.c++', '.cp', '.mm', '.ixx'
})

_C_SOURCE_EXTS = frozenset({".c"})

# Combined source extensions for C and C++
_ALL_SOURCE_EXTS = _CPP_SOURCE_EXTS | _C_SOURCE_EXTS

# Header file extensions (lowercase)
_HEADER_EXTS = frozenset({'.h', '.hpp', '.hxx', '.hh', '.inl'})

# Source extensions with case variations for implied_source function
_SOURCE_EXTS_WITH_CASE = ('.cpp', '.cxx', '.cc', '.c++', '.cp', '.mm', '.ixx', '.c', '.C', '.CC')

# Header extensions with case variations for impliedheader function
_HEADER_EXTS_WITH_CASE = ('.h', '.hpp', '.hxx', '.hh', '.inl', '.H', '.HH')

def is_nonstr_iter(obj: Any) -> bool:
    """ A python 3 only method for deciding if the given variable
        is a non-string iterable
    """
    return not isinstance(obj, str) and hasattr(obj, "__iter__")

@functools.lru_cache(maxsize=None)
def cached_shlex_split(command_line: str) -> list[str]:
    """Cache shlex parsing results"""
    return shlex.split(command_line)


@functools.lru_cache(maxsize=None)
def isheader(filename: str) -> bool:
    """ Internal use.  Is filename a header file?"""
    _, ext = os.path.splitext(filename)
    return ext.lower() in _HEADER_EXTS

@functools.lru_cache(maxsize=None)
def is_cpp_source(path: str) -> bool:
    """Lightweight C++ source detection by extension (case-insensitive)."""
    # Fast path: split once
    _, ext = os.path.splitext(path)
    # Handle .C (uppercase) as C++, but regular extensions use lowercase
    if ext == ".C":
        return True
    return ext.lower() in _CPP_SOURCE_EXTS

@functools.lru_cache(maxsize=None)
def is_c_source(path: str) -> bool:
    """Test if the given file has a .c extension (but not .C which is C++)."""
    _, ext = os.path.splitext(path)
    # .c (lowercase) is C, but .C (uppercase) is C++
    return ext == ".c"

@functools.lru_cache(maxsize=None)
def issource(filename: str) -> bool:
    """ Internal use. Is the filename a source file?"""
    _, ext = os.path.splitext(filename)
    return ext.lower() in _ALL_SOURCE_EXTS


def isexecutable(filename: str) -> bool:
    return os.path.isfile(filename) and os.access(filename, os.X_OK)


@functools.lru_cache(maxsize=None)
def implied_source(filename: str) -> str | None:
    """ If a header file is included in a build then assume that the corresponding c or cpp file must also be build. """
    basename = os.path.splitext(filename)[0]
    for ext in _SOURCE_EXTS_WITH_CASE:
        trialpath = basename + ext
        if compiletools.wrappedos.isfile(trialpath):
            return compiletools.wrappedos.realpath(trialpath)
    return None


@functools.lru_cache(maxsize=None)
def impliedheader(filename: str) -> str | None:
    """ Guess what the header file is corresponding to the given source file """
    basename = os.path.splitext(filename)[0]
    for ext in _HEADER_EXTS_WITH_CASE:
        trialpath = basename + ext
        if compiletools.wrappedos.isfile(trialpath):
            return compiletools.wrappedos.realpath(trialpath)
    return None


def clear_cache() -> None:
    cached_shlex_split.cache_clear()
    isheader.cache_clear()
    is_cpp_source.cache_clear()
    is_c_source.cache_clear()
    issource.cache_clear()
    implied_source.cache_clear()
    impliedheader.cache_clear()


def extractinitargs(args: argparse.Namespace, classname: type) -> dict[str, Any]:
    """ Extract the arguments that classname.__init__ needs out of args """
    function_args = inspect.getfullargspec(classname.__init__).args
    return {key: value for key, value in vars(args).items() if key in function_args}


def tobool(value: Any) -> bool:
    """
    Tries to convert a wide variety of values to a boolean
    Raises an exception for unrecognised values
    """
    str_value = str(value).lower()
    if str_value in {"yes", "y", "true", "t", "1", "on"}:
        return True
    if str_value in {"no", "n", "false", "f", "0", "off"}:
        return False

    raise ValueError(f"Don't know how to convert {value} to boolean.")


def add_boolean_argument(
    parser: argparse.ArgumentParser,
    name: str,
    dest: str | None = None,
    default: bool = False,
    help: str | None = None
) -> None:
    """Add a boolean argument to an ArgumentParser instance."""
    dest = dest or name
    group = parser.add_mutually_exclusive_group()
    bool_help = f"{help} Use --no-{name} to turn the feature off."
    group.add_argument(
        f"--{name}",
        metavar="",
        nargs="?",
        dest=dest,
        default=default,
        const=True,
        type=tobool,
        help=bool_help,
    )
    group.add_argument(f"--no-{name}", dest=dest, action="store_false")


def add_flag_argument(
    parser: argparse.ArgumentParser,
    name: str,
    dest: str | None = None,
    default: bool = False,
    help: str | None = None
) -> None:
    """ Add a flag argument to an ArgumentParser instance.
        Either the --flag is present or the --no-flag is present.
        No trying to convert boolean values like the add_boolean_argument
    """
    dest = dest or name
    group = parser.add_mutually_exclusive_group()
    bool_help = f"{help} Use --no-{name} to turn the feature off."
    group.add_argument(
        f"--{name}", dest=dest, default=default, action="store_true", help=bool_help
    )
    group.add_argument(
        f"--no-{name}", dest=dest, action="store_false", default=not default
    )


def removemount(absolutepath: str) -> str:
    """ Remove the '/' on unix and (TODO) 'C:\' on Windows """
    return absolutepath[1:]


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


def ordered_difference(iterable: Iterable[Any], subtract: Iterable[Any]) -> list[Any]:
    """Return items from iterable not in subtract, preserving order.

    This replaces OrderedSet difference operations.
    """
    subtract_set = set(subtract)
    return [item for item in dict.fromkeys(iterable) if item not in subtract_set]
