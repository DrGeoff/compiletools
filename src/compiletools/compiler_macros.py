"""Dynamic compiler macro detection module.

This module queries compilers for their predefined macros rather than
hardcoding them, allowing automatic adaptation to new compiler versions.
"""

import os
import subprocess
from functools import lru_cache

import compiletools.utils

# Built-in include dirs parsed from the macro probe's ``-Wp,-v`` stderr.
# Keyed on compiler_path; populated by every :func:`get_compiler_macros` run
# so :func:`builtin_include_dirs` reads the SAME single invocation (plan
# finding F11: one compiler subprocess for macros + dirs, not two).
_builtin_include_dirs_memo: dict[str, tuple[str, ...]] = {}

_SEARCH_START_MARKER = "#include <...> search starts here:"
_SEARCH_END_MARKER = "End of search list."


def _parse_builtin_include_dirs(stderr_text: str) -> tuple[str, ...]:
    """Parse the ``-Wp,-v`` include search list out of compiler stderr.

    GCC and clang both print this block when preprocessing with ``-v``
    passed to the preprocessor::

        #include <...> search starts here:
         /usr/lib/gcc/.../include
         /usr/include
        End of search list.

    Only the angle-bracket block is taken; the ``#include "..."`` block the
    compilers print above it is the quoted-include list. Entries are
    stripped, realpath'd (gcc prints ``..``-chains), kept only when they
    exist on disk, and de-duplicated preserving order.
    """
    dirs: list[str] = []
    in_block = False
    for line in stderr_text.splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped == _SEARCH_START_MARKER:
                in_block = True
            continue
        if stripped == _SEARCH_END_MARKER:
            break
        # macOS clang annotates framework dirs with this suffix.
        stripped = stripped.removesuffix(" (framework directory)")
        if not stripped:
            continue
        # NOT cached: realpath of an already-absolute probe entry
        # (documented skip case #3), guarded by an existence check so probe
        # entries for dirs removed since the run never leak in.
        real = os.path.realpath(stripped)
        # NOT cached: isdir of a freshly probed compiler-reported dir.
        if os.path.isdir(real) and real not in dirs:
            dirs.append(real)
    return tuple(dirs)


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
        # Use -dM to dump macros, -E to preprocess only, - to read from stdin.
        # -Wp,-v asks the preprocessor to print its include search list to
        # stderr so ONE invocation yields both the macros (stdout) and the
        # built-in include dirs (stderr) -- F11.
        # Split compiler_path to handle multi-word commands like "ccache g++"
        base_cmd = compiletools.utils.split_compiler_command(compiler_path, slot="CXX")
        result = subprocess.run(
            base_cmd + ["-Wp,-v", "-dM", "-E", "-"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )

        # Some compiler drivers reject -Wp,-v outright, failing the whole
        # probe. Retry once without the flag so the macro half survives (the
        # pre-F11 behaviour); the fallback run prints no include search
        # list, so on success the include-dir memo records () instead of a
        # parsed stderr.
        used_fallback = False
        if result.returncode != 0:
            if verbose >= 4:
                print(
                    f"Compiler {compiler_path} returned non-zero exit code {result.returncode} "
                    "with -Wp,-v; retrying macro probe without it"
                )
            result = subprocess.run(
                base_cmd + ["-dM", "-E", "-"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
            )
            used_fallback = True

        if result.returncode != 0:
            if verbose >= 4:
                print(f"Compiler {compiler_path} returned non-zero exit code: {result.returncode}")
            _builtin_include_dirs_memo[compiler_path] = ()
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

        if used_fallback:
            # The retry ran without -Wp,-v, so no include search list was
            # printed: record () rather than parsing a stderr that cannot
            # contain one.
            _builtin_include_dirs_memo[compiler_path] = ()
        else:
            # Probe tests may stub subprocess.run with a mock whose stderr is
            # not a string; guard so a parse always sees probe-shaped text.
            stderr_text = result.stderr if isinstance(result.stderr, str) else ""
            _builtin_include_dirs_memo[compiler_path] = _parse_builtin_include_dirs(stderr_text)

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
        _builtin_include_dirs_memo[compiler_path] = ()
        return {}


def builtin_include_dirs(compiler_path: str, verbose: int = 0) -> tuple[str, ...]:
    """Directories the compiler searches for angle-bracket includes.

    Shares the single ``-Wp,-v`` macro probe invocation with
    :func:`get_compiler_macros` (F11): whichever of the two functions runs
    first for a compiler triggers the one subprocess and both memoise their
    half of the parsed result, so calling the pair costs exactly one
    compiler invocation. Cleared by :func:`clear_cache`. Any failure yields
    ``()``.
    """
    if not compiler_path:
        if verbose >= 2:
            print("No compiler specified, returning empty include-dir tuple")
        return ()

    cached = _builtin_include_dirs_memo.get(compiler_path)
    if cached is not None:
        return cached

    # One run populates both the macro cache and the include-dir memo.
    get_compiler_macros(compiler_path, verbose)

    return _builtin_include_dirs_memo.get(compiler_path, ())


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
        cmd = compiletools.utils.split_compiler_command(compiler_path, slot="CXX")
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
    _builtin_include_dirs_memo.clear()
