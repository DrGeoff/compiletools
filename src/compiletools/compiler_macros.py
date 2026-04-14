"""Dynamic compiler macro detection module.

This module queries compilers for their predefined macros rather than
hardcoding them, allowing automatic adaptation to new compiler versions.
"""

import subprocess
from functools import lru_cache

import compiletools.utils


@lru_cache(maxsize=32)
def get_compiler_macros(compiler_path: str, verbose: int = 0) -> dict[str, str]:
    """Query a compiler for its predefined macros.

    Args:
        compiler_path: Path to the compiler executable (e.g., 'gcc', 'clang')
        verbose: Verbosity level for debug output

    Returns:
        Dictionary of macro names to their values
    """
    if not compiler_path:
        if verbose >= 2:
            print("No compiler specified, returning empty macro dict")
        return {}

    try:
        # Use -dM to dump macros, -E to preprocess only, - to read from stdin
        # Split compiler_path to handle multi-word commands like "ccache g++"
        result = subprocess.run(
            compiletools.utils.split_command_cached(compiler_path) + ["-dM", "-E", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            if verbose >= 4:
                print(f"Compiler {compiler_path} returned non-zero exit code: {result.returncode}")
            return {}

        macros = {}
        for line in result.stdout.splitlines():
            # Parse lines like: #define __GNUC__ 11
            if line.startswith("#define "):
                parts = line[8:].split(None, 1)  # Split after '#define '
                if parts:
                    macro_name = parts[0]
                    macro_value = parts[1] if len(parts) > 1 else "1"
                    # Remove surrounding quotes if present
                    if macro_value.startswith('"') and macro_value.endswith('"'):
                        macro_value = macro_value[1:-1]
                    macros[macro_name] = macro_value

        if verbose >= 3:
            print(f"Queried {len(macros)} macros from {compiler_path}")
            if verbose >= 8:
                import pprint

                print("Sample of detected macros:")
                pprint.pprint(dict(sorted(macros.items())[:20]))  # Show first 20

        return macros

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        if verbose >= 3:
            print(f"Failed to query macros from {compiler_path}: {e}")
        return {}


def filter_for_expansion(compiler_macros: dict[str, str]) -> dict[str, str]:
    """Filter compiler macros to those safe for string expansion.

    GCC predefines legacy macros like ``linux`` and ``unix`` (without a
    leading underscore) for backward compatibility.  These collide with
    path components in pkg-config output, e.g. turning
    ``clickhouse-linux-x64`` into ``clickhouse-1-x64``.

    The C/C++ standard reserves identifiers starting with ``_`` for the
    implementation, so all *standard* compiler macros (``__linux__``,
    ``__GNUC__``, ``_LP64``, etc.) are safe.  This function strips the
    non-conforming ones.
    """
    return {k: v for k, v in compiler_macros.items() if k.startswith("_")}


@lru_cache(maxsize=256)
def query_has_function(compiler_path: str, function_call: str, cppflags: str = "", verbose: int = 0) -> int:
    """Query the compiler to evaluate a __has_* preprocessor function call.

    Args:
        compiler_path: Path to the compiler executable (e.g., 'gcc', 'clang')
        function_call: The full function call expression (e.g., '__has_include(<iostream>)')
        cppflags: Additional preprocessor flags (e.g., '-I/usr/include')
        verbose: Verbosity level for debug output

    Returns:
        1 if the compiler evaluates the function call as true, 0 otherwise.
    """
    if not compiler_path:
        return 0

    # Build a snippet that the preprocessor can evaluate
    snippet = f"#if {function_call}\n1\n#else\n0\n#endif\n"

    try:
        cmd = compiletools.utils.split_command_cached(compiler_path)
        if cppflags:
            cmd = cmd + compiletools.utils.split_command_cached(cppflags)
        cmd = cmd + ["-E", "-x", "c++", "-"]

        result = subprocess.run(
            cmd,
            input=snippet,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )

        if result.returncode != 0:
            if verbose >= 4:
                print(f"query_has_function: compiler returned {result.returncode} for '{function_call}'")
            return 0

        # Parse output: look for standalone '1' or '0' line, skipping # lines
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped == "1":
                return 1
            if stripped == "0":
                return 0

        return 0

    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        if verbose >= 3:
            print(f"query_has_function: failed for '{function_call}': {e}")
        return 0


def clear_cache():
    """Clear the LRU caches for compiler macro functions."""
    get_compiler_macros.cache_clear()
    query_has_function.cache_clear()
