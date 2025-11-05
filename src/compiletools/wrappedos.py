"""
Cached filesystem operations with StringZilla optimization.

Provides LRU-cached versions of common os.path operations. Callers choose the
appropriate function: standard versions for Python str, _sz versions for StringZilla.
Optimized for speed over memory - uses separate caches to avoid conversions.
"""
import os
import functools
from typing import Union
import stringzilla as sz

# Type aliases for clarity
PathInput = Union[str, sz.Str]


# Python str API - cached directly, no extra function call layer
@functools.lru_cache(maxsize=None)
def realpath(path: str) -> str:
    """Cached os.path.realpath for Python strings."""
    return os.path.realpath(path)

@functools.lru_cache(maxsize=None)
def abspath(path: str) -> str:
    """Cached os.path.abspath for Python strings."""
    return os.path.abspath(path)

@functools.lru_cache(maxsize=None)
def dirname(path: str) -> str:
    """Cached os.path.dirname for Python strings."""
    return os.path.dirname(path)

@functools.lru_cache(maxsize=None)
def basename(path: str) -> str:
    """Cached os.path.basename for Python strings."""
    return os.path.basename(path)

@functools.lru_cache(maxsize=None)
def getmtime(path: str) -> float:
    """Cached os.path.getmtime for Python strings."""
    return os.path.getmtime(path)

@functools.lru_cache(maxsize=None)
def isfile(path: str) -> bool:
    """Cached os.path.isfile for Python strings."""
    return os.path.isfile(path)

@functools.lru_cache(maxsize=None)
def isdir(path: str) -> bool:
    """Cached os.path.isdir for Python strings."""
    return os.path.isdir(path)

@functools.lru_cache(maxsize=None)
def isabs(path: str) -> bool:
    """Cached os.path.isabs for Python strings."""
    return os.path.isabs(path)

@functools.lru_cache(maxsize=None)
def getsize(path: str) -> int:
    """Cached os.path.getsize for Python strings."""
    return os.path.getsize(path)

def join(path: str, *paths: str) -> str:
    """os.path.join for Python strings."""
    return os.path.join(path, *paths)


# StringZilla API - cached directly, leverages shared Python str caches
@functools.lru_cache(maxsize=None)
def realpath_sz(path: sz.Str) -> sz.Str:
    """Cached realpath for StringZilla - avoids conversions when cached."""
    return sz.Str(realpath(path.decode('utf-8')))

@functools.lru_cache(maxsize=None)
def abspath_sz(path: sz.Str) -> sz.Str:
    """Cached abspath for StringZilla - avoids conversions when cached."""
    return sz.Str(abspath(path.decode('utf-8')))

@functools.lru_cache(maxsize=None)
def dirname_sz(path: sz.Str) -> sz.Str:
    """Cached dirname for StringZilla - avoids conversions when cached."""
    return sz.Str(dirname(path.decode('utf-8')))

@functools.lru_cache(maxsize=None)
def basename_sz(path: sz.Str) -> sz.Str:
    """Cached basename for StringZilla - avoids conversions when cached."""
    return sz.Str(basename(path.decode('utf-8')))

@functools.lru_cache(maxsize=None)
def getmtime_sz(path: sz.Str) -> float:
    """Cached getmtime for StringZilla - leverages shared cache."""
    return getmtime(path.decode('utf-8'))

@functools.lru_cache(maxsize=None)
def isfile_sz(path: sz.Str) -> bool:
    """Cached isfile for StringZilla - leverages shared cache."""
    return isfile(path.decode('utf-8'))

@functools.lru_cache(maxsize=None)
def isdir_sz(path: sz.Str) -> bool:
    """Cached isdir for StringZilla - leverages shared cache."""
    return isdir(path.decode('utf-8'))

@functools.lru_cache(maxsize=None)
def isabs_sz(path: sz.Str) -> bool:
    """Cached isabs for StringZilla - leverages shared cache."""
    return isabs(path.decode('utf-8'))

@functools.lru_cache(maxsize=None)
def getsize_sz(path: sz.Str) -> int:
    """Cached getsize for StringZilla - leverages shared cache."""
    return getsize(path.decode('utf-8'))

def join_sz(path: sz.Str, *paths: sz.Str) -> sz.Str:
    """os.path.join for StringZilla strings."""
    return sz.Str(join(path.decode('utf-8'), *(p.decode('utf-8') for p in paths)))


def clear_cache() -> None:
    """Clear all LRU caches to free memory."""
    # Python str API caches
    realpath.cache_clear()
    abspath.cache_clear()
    dirname.cache_clear()
    basename.cache_clear()
    getmtime.cache_clear()
    isfile.cache_clear()
    isdir.cache_clear()
    isabs.cache_clear()
    getsize.cache_clear()

    # StringZilla API caches
    realpath_sz.cache_clear()
    abspath_sz.cache_clear()
    dirname_sz.cache_clear()
    basename_sz.cache_clear()
    getmtime_sz.cache_clear()
    isfile_sz.cache_clear()
    isdir_sz.cache_clear()
    isabs_sz.cache_clear()
    getsize_sz.cache_clear()