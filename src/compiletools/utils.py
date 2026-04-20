from __future__ import annotations

import argparse
import functools
import inspect
import os
import shlex
from collections.abc import Iterable
from itertools import chain
from pathlib import Path
from typing import Any, Union

import compiletools.wrappedos

# Public API
__all__ = [
    "ALL_SOURCE_EXTS",
    "CPP_SOURCE_EXTS",
    "C_SOURCE_EXTS",
    "HEADER_EXTS",
    "add_boolean_argument",
    "add_flag_argument",
    "clear_cache",
    "combine_and_deduplicate_compiler_flags",
    "deduplicate_compiler_flags",
    "extract_init_args",
    "implied_header",
    "implied_source",
    "is_c_source",
    "is_cpp_source",
    "is_executable",
    "is_header",
    "is_non_string_iterable",
    "is_source",
    "merge_ldflags_with_topo_sort",
    "ordered_difference",
    "ordered_union",
    "ordered_unique",
    "remove_mount",
    "split_command_cached",
    "to_bool",
]

# Module-level constant for C++ source extensions (lowercase)
CPP_SOURCE_EXTS = frozenset({".cpp", ".cxx", ".cc", ".c++", ".cp", ".mm", ".ixx"})

C_SOURCE_EXTS = frozenset({".c"})

# Combined source extensions for C and C++
ALL_SOURCE_EXTS = CPP_SOURCE_EXTS | C_SOURCE_EXTS

# Header file extensions (lowercase)
HEADER_EXTS = frozenset({".h", ".hpp", ".hxx", ".hh", ".inl"})

# Source extensions with case variations for implied_source function
SOURCE_EXTS_WITH_CASE = frozenset({".cpp", ".cxx", ".cc", ".c++", ".cp", ".mm", ".ixx", ".c", ".C", ".CC"})

# Header extensions with case variations for implied_header function
HEADER_EXTS_WITH_CASE = frozenset({".h", ".hpp", ".hxx", ".hh", ".inl", ".H", ".HH"})

# Boolean conversion mapping for to_bool function
BOOL_MAP = {
    # True values
    "yes": True,
    "y": True,
    "true": True,
    "t": True,
    "1": True,
    "on": True,
    # False values
    "no": False,
    "n": False,
    "false": False,
    "f": False,
    "0": False,
    "off": False,
}


@functools.cache
def _get_lower_ext(filename: str) -> str:
    """Fast extension extraction and lowercase conversion."""
    idx = filename.rfind(".")
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


@functools.cache
def split_command_cached(command_line: str) -> list[str]:
    """Cache shlex parsing results"""
    return shlex.split(command_line)


@functools.cache
def split_command_cached_sz(command_line_sz) -> list:
    """StringZilla-aware version returning StringZilla.Str list"""
    import stringzilla as sz

    str_results = shlex.split(command_line_sz.decode("utf-8"))
    return [sz.Str(s) for s in str_results]


@functools.cache
def is_header(filename: str) -> bool:
    """Is filename a header file?"""
    return _get_lower_ext(filename) in HEADER_EXTS


@functools.cache
def is_cpp_source(path: str) -> bool:
    """Lightweight C++ source detection by extension (case-insensitive)."""
    # Fast path: split once
    _, ext = os.path.splitext(path)
    # Handle .C (uppercase) as C++, but regular extensions use lowercase
    if ext == ".C":
        return True
    return ext.lower() in CPP_SOURCE_EXTS


@functools.cache
def is_c_source(path: str) -> bool:
    """Test if the given file has a .c extension (but not .C which is C++)."""
    _, ext = os.path.splitext(path)
    # .c (lowercase) is C, but .C (uppercase) is C++
    return ext == ".c"


@functools.cache
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


@functools.cache
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


@functools.cache
def implied_header(filename: str) -> str | None:
    """Find the header file corresponding to a source file.

    Args:
        filename: Source filename to find header for

    Returns:
        Path to corresponding header file, or None if not found
    """
    return _find_file_with_extensions(filename, HEADER_EXTS_WITH_CASE)


def instance_cache(method):
    """Decorator that caches method results per-instance (not per-class).

    Unlike @functools.cache on instance methods (which creates a class-level
    cache shared across all instances), this stores the cache dict on each
    instance via self.__dict__.

    The decorated method gets a ``cache_attr`` attribute holding the name of
    the dict stored on each instance, useful for clearing::

        self.__dict__.pop(method.cache_attr, None)
    """
    cache_attr = f"_cache_{method.__name__}"

    @functools.wraps(method)
    def wrapper(self, *args):
        cache = self.__dict__.get(cache_attr)
        if cache is None:
            cache = {}
            self.__dict__[cache_attr] = cache
        try:
            return cache[args]
        except KeyError:
            result = method(self, *args)
            cache[args] = result
            return result

    wrapper.cache_attr = cache_attr
    return wrapper


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
        p.name
        for p in sig.parameters.values()
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
    allow_value_conversion: bool = True,
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
        group.add_argument(f"--{name}", dest=dest, default=default, action="store_true", help=bool_help)

    group.add_argument(f"--no-{name}", dest=dest, action="store_false")


def add_flag_argument(
    parser: argparse.ArgumentParser, name: str, dest: str | None = None, default: bool = False, help: str | None = None
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
    FLAG_WITH_ARGS = ("-framework", "-isystem", "-I", "-L", "-l", "-D", "-U", "-F")

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
                arg = flag[len(matched_flag) :]
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


def _find_cycle(graph: dict[str, set[str]], remaining: set[str]) -> list[str]:
    """Find one cycle in the subgraph induced by *remaining* using DFS.

    Returns a list like [a, b, c, a] showing the cycle path.
    """
    visited: set[str] = set()
    on_stack: set[str] = set()
    parent: dict[str, str] = {}

    for start in sorted(remaining):  # sorted for determinism
        if start in visited:
            continue
        stack = [start]
        while stack:
            node = stack[-1]
            if node not in visited:
                visited.add(node)
                on_stack.add(node)
            pushed = False
            for succ in sorted(graph.get(node, [])):
                if succ not in remaining:
                    continue
                if succ not in visited:
                    parent[succ] = node
                    stack.append(succ)
                    pushed = True
                    break
                elif succ in on_stack:
                    # Found a cycle — reconstruct the path
                    cycle = [succ, node]
                    cur = node
                    while cur != succ:
                        cur = parent[cur]
                        cycle.append(cur)
                    cycle.reverse()
                    return cycle
            if not pushed:
                on_stack.discard(node)
                stack.pop()

    # Should not reach here if remaining is truly cyclic
    return sorted(remaining) + [sorted(remaining)[0]]  # pragma: no cover


class LDFLAGSCycleError(ValueError):
    """Raised when merge_ldflags_with_topo_sort cannot break a cycle.

    A subclass of ValueError so cake.py's outer error handler can match
    only this specific error (M-C7) rather than rendering every random
    ValueError through the cycle-error formatter.
    """


def _format_cycle_error(
    cycle_path: list[str],
    edge_sources: dict[tuple[str, str], list[str]],
    source_files: list[str] | None,
) -> str:
    """Format a human-readable error message for a hard library cycle."""
    cycle_str = " -> ".join(cycle_path)
    lines = [
        "Cyclic library dependency detected — link order cannot be determined.",
        "",
        f"  Cycle: {cycle_str}",
    ]
    if source_files is not None:
        cycle_files_seen: list[str] = []
        seen_set: set[str] = set()
        for i in range(len(cycle_path) - 1):
            edge = (cycle_path[i], cycle_path[i + 1])
            for f in edge_sources.get(edge, []):
                if f not in seen_set:
                    seen_set.add(f)
                    cycle_files_seen.append(f)
        common_root = ""
        if cycle_files_seen:
            try:
                common_root = os.path.commonpath(cycle_files_seen)
            except ValueError:
                pass  # different drives on Windows
            if common_root and not os.path.isdir(common_root):
                common_root = os.path.dirname(common_root)
        if common_root:
            lines.append("")
            lines.append(f"  Root: {common_root}/")

        def _shorten(filepath: str) -> str:
            if common_root:
                return os.path.relpath(filepath, common_root)
            return filepath

        lines.append("")
        lines.append("  Constraints contributing to the cycle:")
        for i in range(len(cycle_path) - 1):
            edge = (cycle_path[i], cycle_path[i + 1])
            # M-C6: dedupe files per-edge so a single source contributing
            # the same edge multiple times doesn't get listed N times.
            files = list(dict.fromkeys(edge_sources.get(edge, [])))
            if files:
                file_list = ", ".join(_shorten(f) for f in files)
                lines.append(f"    {edge[0]} must precede {edge[1]}  (from {file_list})")
            else:
                lines.append(f"    {edge[0]} must precede {edge[1]}")
    lines.append("")
    lines.append("Fix the LDFLAGS annotations in the source files above to remove the contradictory ordering.")
    return "\n".join(lines)


def merge_ldflags_with_topo_sort(
    per_file_ldflags: list[list],
    source_files: list[str] | None = None,
    hard_orderings: list[tuple[str, str]] | None = None,
    hard_ordering_sources: list[str] | None = None,
) -> list[str]:
    """Merge per-file LDFLAGS using topological sort for -l flag ordering.

    Each file's -l flag sequence defines pairwise ordering constraints:
    [-llibnext, -llibbase] means libnext must appear before libbase.
    Non -l flags are deduplicated and placed before the sorted -l flags.

    Constraints from per_file_ldflags are "soft" — when two files assert
    opposite orderings for the same pair (A before B in one, B before A
    in another), both edges are cancelled since neither is authoritative.
    This commonly happens when different pkg-config packages list shared
    transitive dependencies in different orders.

    Constraints from hard_orderings are "hard" — they represent explicit
    cross-package orderings from multi-package PKG-CONFIG annotations
    (e.g. PKG-CONFIG=libssh2 numa means ssh2 must precede numa).

    Raises ValueError if a genuine cycle exists after soft mutual edges
    are cancelled.

    Args:
        per_file_ldflags: Per-file lists of LDFLAGS (e.g. ["-llibnext", "-llibbase"]).
        source_files: Optional parallel list of source file paths (one per entry
            in per_file_ldflags) used to produce better error messages on cycles.
        hard_orderings: Optional list of (pred_lib, succ_lib) pairs representing
            hard ordering constraints (lib names without -l prefix).
        hard_ordering_sources: Optional parallel list of source file paths
            for hard_orderings (used in cycle error messages).
    """
    if not per_file_ldflags:
        # M-C11: hard_orderings without per_file_ldflags is impossible
        # in practice — multi-package PKG-CONFIG always populates LDFLAGS
        # alongside the hard ordering. If it ever changes, an empty
        # return here would silently lose the hard constraints.
        assert not hard_orderings, (
            "merge_ldflags_with_topo_sort: cannot honor hard_orderings "
            "without per_file_ldflags. The two are produced together by "
            "magicflags._handle_pkg_config; one without the other is a bug."
        )
        return []

    from collections import defaultdict

    non_l_flags: list[str] = []
    per_file_l_names: list[list[str]] = []

    for file_flags in per_file_ldflags:
        file_l_names: list[str] = []
        str_flags = [str(f) for f in file_flags]
        i = 0
        while i < len(str_flags):
            flag = str_flags[i]
            if flag == "-l" and i + 1 < len(str_flags):
                file_l_names.append(str_flags[i + 1])
                i += 2
            elif flag.startswith("-l") and len(flag) > 2:
                file_l_names.append(flag[2:])
                i += 1
            else:
                non_l_flags.append(flag)
                i += 1
        if file_l_names:
            per_file_l_names.append(file_l_names)

    # Build constraint graph from pairwise orderings
    graph: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = defaultdict(int)
    all_libs: list[str] = []
    seen_libs: set[str] = set()
    # Track which source files contributed each edge (for cycle diagnostics)
    edge_sources: dict[tuple[str, str], list[str]] = defaultdict(list)

    for file_idx, file_l_names in enumerate(per_file_l_names):
        for name in file_l_names:
            if name not in seen_libs:
                all_libs.append(name)
                seen_libs.add(name)
        for j in range(len(file_l_names) - 1):
            pred, succ = file_l_names[j], file_l_names[j + 1]
            if source_files is not None:
                edge_sources[(pred, succ)].append(source_files[file_idx])
            if succ not in graph[pred]:
                graph[pred].add(succ)
                in_degree[succ] = in_degree.get(succ, 0) + 1
            # Ensure pred has an in_degree entry
            if pred not in in_degree:
                in_degree[pred] = 0

    # Add hard ordering constraints (from multi-package PKG-CONFIG annotations)
    hard_edges: set[tuple[str, str]] = set()
    if hard_orderings:
        for idx, (pred, succ) in enumerate(hard_orderings):
            hard_edges.add((pred, succ))
            if pred not in seen_libs:
                all_libs.append(pred)
                seen_libs.add(pred)
            if succ not in seen_libs:
                all_libs.append(succ)
                seen_libs.add(succ)
            if hard_ordering_sources is not None:
                edge_sources[(pred, succ)].append(hard_ordering_sources[idx])
            if succ not in graph[pred]:
                graph[pred].add(succ)
                in_degree[succ] = in_degree.get(succ, 0) + 1
            if pred not in in_degree:
                in_degree[pred] = 0

    if not all_libs:
        return list(dict.fromkeys(non_l_flags))

    # Cancel soft mutual edges.  When both A→B and B→A exist:
    #   - Both soft: cancel both (ambiguous pkg-config transitive dep ordering)
    #   - One hard: keep the hard direction, remove the soft one
    #   - Both hard: keep both (genuine conflict, detected as cycle below)
    to_remove: set[tuple[str, str]] = set()
    processed: set[tuple[str, str]] = set()
    for node in list(graph):
        for succ in list(graph.get(node, set())):
            if node in graph.get(succ, set()):
                pair = (min(node, succ), max(node, succ))
                if pair in processed:
                    continue
                processed.add(pair)
                a, b = pair
                ab_hard = (a, b) in hard_edges
                ba_hard = (b, a) in hard_edges
                if ab_hard and ba_hard:
                    pass  # genuine conflict, keep both
                elif ab_hard:
                    to_remove.add((b, a))
                elif ba_hard:
                    to_remove.add((a, b))
                else:
                    to_remove.add((a, b))
                    to_remove.add((b, a))

    for pred, succ in to_remove:
        graph[pred].discard(succ)

    # Recompute in_degree after edge removal
    in_degree = {lib: 0 for lib in all_libs}
    for node in graph:
        for succ in graph[node]:
            in_degree[succ] = in_degree.get(succ, 0) + 1

    # Kahn's algorithm with alphabetical tie-breaking for determinism
    sorted_libs: list[str] = []
    remaining = set(all_libs)

    def _drain_kahn() -> None:
        """Drain all zero-in-degree nodes from *remaining* into *sorted_libs*."""
        queue = sorted(lib for lib in remaining if in_degree.get(lib, 0) == 0)
        while queue:
            node = queue.pop(0)
            sorted_libs.append(node)
            remaining.discard(node)
            next_ready = []
            for succ in sorted(graph.get(node, [])):
                if succ in remaining:
                    in_degree[succ] -= 1
                    if in_degree[succ] == 0:
                        next_ready.append(succ)
            queue = sorted(queue + next_ready)

    _drain_kahn()

    # Break cycles that contain soft edges.  Soft constraints are hints
    # from per-file flag ordering — when they form a cycle (even without
    # mutual contradictions), we drop them and let the topological sort
    # proceed.  Only purely hard cycles are genuine conflicts.
    while remaining:
        cycle_path = _find_cycle(graph, remaining)

        soft_in_cycle = [
            (cycle_path[i], cycle_path[i + 1])
            for i in range(len(cycle_path) - 1)
            if (cycle_path[i], cycle_path[i + 1]) not in hard_edges
        ]

        if not soft_in_cycle:
            # Purely hard cycle — genuine conflict, error out.
            raise LDFLAGSCycleError(_format_cycle_error(cycle_path, edge_sources, source_files))

        # Break cycle by removing soft edges, recompute in_degree, and drain.
        for pred, succ in soft_in_cycle:
            graph[pred].discard(succ)
        in_degree = {lib: 0 for lib in remaining}
        for node in remaining:
            for succ in graph.get(node, ()):
                if succ in remaining:
                    in_degree[succ] = in_degree.get(succ, 0) + 1
        _drain_kahn()

    deduped_non_l = list(dict.fromkeys(non_l_flags))
    return deduped_non_l + [f"-l{name}" for name in sorted_libs]


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
                if " " in item and not item.startswith("/"):
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
