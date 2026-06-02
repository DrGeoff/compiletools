"""Compiler-probe helpers (leaf module).

Extracted from :mod:`compiletools.apptools` as a behavior-preserving facade
split. This module is a leaf: it imports only stdlib plus
:mod:`compiletools.wrappedos`, :mod:`compiletools.utils`, and
:mod:`compiletools.apptools_canonicalize` (themselves leaves). It MUST NOT
import ``compiletools.apptools`` — doing so would reintroduce the very cycle
this split removes.

It groups the functions that interrogate a compiler binary on disk:

* :func:`compiler_identity` -- stable ``realpath|size|mtime_ns`` cache-key
  identity for a compiler binary (gitroot-canonicalised via
  :func:`compiletools.apptools_canonicalize.canonicalize_path_for_cache_key`).
* :func:`compiler_kind` -- classify a binary as ``gcc`` / ``clang`` /
  ``unknown``.
* :func:`compiler_default_cxx_std` -- the compiler's natural default
  ``-std=gnu++NN`` dialect, for PCH/BMI alignment.
* :func:`find_system_std_module_source` -- locate the compiler-shipped
  ``std`` module source (``std.cc`` / ``std.cppm``).
* :func:`get_functional_cxx_compiler` /
  :func:`_get_functional_cxx_compiler_cached` /
  :func:`_test_compiler_functionality` /
  :func:`derive_c_compiler_from_cxx` -- the fallback functional-compiler
  auto-detect used when ``args.CXX`` is unset.
* :func:`tool_version` -- generic ``<tool> --version`` ``(major, minor)``
  probe.
* :func:`_compiler_major_version` -- ``(family, major)`` probe used by
  :func:`compiler_kind` to disambiguate Termux-style ``g++ -> clang``
  symlinks.

``apptools.py`` re-exports every name here by binding so its existing
``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
importers, and test/patch targets keep working with identical object
identity.
"""

import functools
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap

import compiletools.wrappedos
from compiletools.apptools_canonicalize import canonicalize_path_for_cache_key
from compiletools.utils import split_command_cached


@functools.lru_cache(maxsize=64)
def compiler_identity(cxx: str, *, anchor_root: str = "") -> str:
    """Return a stable identity string for a compiler binary.

    Used as part of cache keys (PCH cache key in ``build_backend`` and the
    per-TU object cache key via ``MacroState.compiler_identity``). Two users
    on the same shared filesystem with different ``$PATH``s could otherwise
    collide on the same key while resolving ``args.CXX`` (e.g. bare ``g++``)
    to different binaries (different versions, different stdlibs). GCC's PCH
    stamp catches this at *consume* time -- but the slow fallback compile
    defeats the cache. By including binary realpath + (st_size, st_mtime),
    we make distinct compilers produce distinct cache entries.

    When the resolved binary lives under ``anchor_root``, the realpath
    *segment* of the returned ``<realpath>|<size>|<mtime_ns>`` triple is
    rewritten to ``<GITROOT>/<relpath>`` via
    :func:`canonicalize_path_for_cache_key` so two CI checkouts at
    different absolute prefixes share the same cache key. The
    ``|<size>|<mtime_ns>`` tail is unchanged. The default
    ``anchor_root=""`` is a graceful no-op (identity) for backward
    compatibility and ad-hoc test fixtures — **production call sites
    must always pass an anchor**, otherwise the workspace prefix leaks
    into every downstream cache key (PCH / PCM / per-TU object / link / ar).

    Falls back to the original string when the binary cannot be stat'd
    (e.g. user passed a non-path command like ``ccache g++``). The fallback
    string is also canonicalised against ``anchor_root`` when it parses
    as an absolute path under the anchor — otherwise the leak would
    survive the fallback path. Returns ``""`` when ``cxx`` is None /
    empty so unconfigured ``args.CXX`` (some unit-test fixtures) doesn't
    crash the helper.

    Side effect: any tool that bumps the compiler binary's mtime (e.g.
    a no-op ``touch /usr/bin/g++``) will invalidate the cache. This is
    acceptable because the false positive forces a rebuild -- a slow
    correct outcome -- whereas a false negative (which this helper is
    designed to prevent) would silently produce a stale ``.o``.
    """
    if not cxx:
        return ""
    resolved = shutil.which(cxx) or cxx
    try:
        st = os.stat(resolved)
        # Use nanosecond mtime so a sub-second compiler swap (e.g.
        # ``cp new-g++ /usr/local/bin/g++`` followed immediately by a
        # build) does not collide on the cache key.
        real = compiletools.wrappedos.realpath(resolved)
        canonical = canonicalize_path_for_cache_key(real, anchor_root)
        return f"{canonical}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return canonicalize_path_for_cache_key(resolved, anchor_root)


@functools.lru_cache(maxsize=64)
def find_system_std_module_source(cxx: str | None, kind: str) -> str | None:
    """Locate the compiler-provided source for the standard library module.

    Returns an absolute filesystem path to the file the build system can
    feed back into the compiler to materialize the ``std`` module's
    ``.gcm`` (gcc) or ``.pcm`` (clang). Returns ``None`` when the
    requested toolchain doesn't ship one (or we can't find it).

    Search strategy:

    - **gcc**: parse ``g++ -print-search-dirs`` for the compiler install
      root, then look for ``<root>/include/c++/<version>/bits/std.cc``.
      This is what the GNU toolchain ships starting with gcc 15+ as the
      canonical std-module source.
    - **clang**: walk up from the binary path two levels (``bin/`` ->
      install root) and look for ``share/libc++/v1/std.cppm``. This is
      what clang ships when built against libc++.

    Both probes are pure filesystem operations -- no compiler invocation
    -- so they are cheap and safe to call from cache-key paths.
    """
    if not cxx or kind not in ("gcc", "clang"):
        return None
    # Handle compiler-wrapper strings like ``ccache g++`` / ``distcc clang++``:
    # ``shutil.which("ccache g++")`` returns None (no binary literally named
    # ``"ccache g++"``), so falling back to the original string would feed
    # subprocess.run an unfindable argv0 (gcc branch) or os.path.realpath an
    # unresolvable path (clang branch) and silently return None. Mirror
    # ``compiler_kind``'s raw-string fallback: if the bare string isn't on
    # PATH, retry with the last whitespace-separated token (the real driver
    # after the wrapper).
    resolved = shutil.which(cxx)
    if resolved is None and " " in cxx:
        last = cxx.rsplit(None, 1)[-1]
        resolved = shutil.which(last) or last
    elif resolved is None:
        resolved = cxx
    if kind == "gcc":
        # `g++ -print-search-dirs` reports `install: <path-to-bin>/../lib/gcc/<triple>/<ver>/`
        try:
            r = subprocess.run(
                [resolved, "-print-search-dirs"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        install_dir = None
        for line in r.stdout.splitlines():
            if line.startswith("install:"):
                install_dir = line.split(":", 1)[1].strip()
                break
        if not install_dir:
            return None
        # install_dir = .../bin/../lib/gcc/<triple>/<version>/
        # Walk up to <install root> = .../bin/.., then look for include/c++/<ver>/bits/std.cc
        # The version is the same as the install_dir's last directory.
        version = os.path.basename(install_dir.rstrip(os.sep))
        # Normalize the install root (drop the .../lib/gcc/<triple>/<ver>/
        # tail) by going up four levels and resolving symlinks/.. in
        # one shot. The four-level count assumes the canonical
        # ``<root>/lib/gcc/<triple>/<version>/`` layout reported by
        # ``-print-search-dirs``; if a distro symlinks ``bin/g++``
        # somewhere unconventional and ``-print-search-dirs`` returns
        # a non-canonical install path, the candidate file won't exist
        # and we return None (graceful: caller falls back to no-cache
        # behaviour).
        gcc_root = os.path.realpath(os.path.join(install_dir, "..", "..", "..", ".."))
        candidate = os.path.join(gcc_root, "include", "c++", version, "bits", "std.cc")
        return candidate if os.path.isfile(candidate) else None
    # clang
    bin_dir = os.path.dirname(os.path.realpath(resolved))
    install_root = os.path.dirname(bin_dir)  # bin/.. -> install root
    candidate = os.path.join(install_root, "share", "libc++", "v1", "std.cppm")
    return candidate if os.path.isfile(candidate) else None


@functools.lru_cache(maxsize=64)
def compiler_kind(cxx: str | None) -> str:
    """Classify a C++ compiler binary as ``"gcc"`` / ``"clang"`` / ``"unknown"``.

    Used to pick compiler-specific code paths (e.g., the C++20 modules
    flag set: gcc needs ``-fmodules-ts`` while clang doesn't, and clang
    uses ``--precompile`` / ``-fprebuilt-module-path=`` for the BMI flow).

    Detection resolves the binary via ``shutil.which`` and inspects the
    basename. A gcc-ish basename (``g++``/``gcc``) is then verified
    against the binary's ``--version`` banner -- on Termux ``g++`` is a
    symlink to ``clang-21``, and dispatching gcc-only flags like
    ``-fmodules-ts`` at it fails the compile. Symmetric reverse case
    (``clang`` symlinked to gcc) is exceedingly rare and not probed; the
    basename wins for the clang side. The probe happens at most once per
    unique input string because the function is ``lru_cache``-d.

    Falls back to scanning the original string for ``clang`` / ``gcc`` /
    ``g++`` substrings when the binary can't be located -- callers that
    hand us a compound string like ``ccache clang++`` should still get
    the right answer.

    Returns ``"unknown"`` for ``None`` / empty input or when the basename
    matches neither toolchain. Callers must handle the unknown case
    rather than guessing.
    """
    if not cxx:
        return "unknown"
    resolved = shutil.which(cxx) or cxx
    base = os.path.basename(resolved).lower()
    # Strip versions/wrappers like ``g++-15`` or ``clang++-22.1.3``.
    if "clang" in base:
        return "clang"
    if "g++" in base or "gcc" in base:
        # Verify against --version: a gcc-ish basename on a binary that
        # actually reports clang (Termux ships ``g++`` -> ``clang-21``)
        # must be classified as clang, otherwise we dispatch gcc-only
        # flags at it and the compile fails. Use the resolved path so
        # ``--version`` is the binary on disk; fall through to "gcc" if
        # the probe can't parse a recognised banner.
        probe = _compiler_major_version(resolved)
        if probe is not None and probe[0] == "clang":
            return "clang"
        return "gcc"
    # Fall back to scanning the raw string -- handles ``ccache g++`` and
    # similar wrappers that point at a shim with no toolchain hint in
    # its name but mention the real compiler in the original argv0.
    raw = cxx.lower()
    if "clang" in raw:
        return "clang"
    if "g++" in raw or "gcc" in raw:
        return "gcc"
    return "unknown"


@functools.lru_cache(maxsize=64)
def compiler_default_cxx_std(cxx: str | None) -> str | None:
    """Return the ``-std=`` flag matching the compiler's natural default
    C++ dialect, e.g. ``-std=gnu++20`` for gcc-16, ``-std=gnu++17`` for
    clang-21. Returns ``None`` if the default cannot be determined.

    Used to align PCH/BMI prebuilt artefacts with downstream consumer
    compiles when the user hasn't explicitly set ``-std=`` in CXXFLAGS.
    Different compilers (and different versions of the same compiler)
    pick different defaults — gcc-16 ships ``gnu++20``, clang-21 ships
    ``gnu++17`` — and a hardcoded fallback would silently desync one
    of them. Bazel's ``rules_cc`` autoconfig appends its own
    ``-std=c++17`` to every C++ action; without aligning to the
    compiler's actual default, the prebuilt artefact and the bazel-
    spawned consumer end up at different dialects and gcc rejects the
    PCH (``__cpp_impl_three_way_comparison not defined``) or the BMI
    (``language dialect differs 'C++20', expected 'C++17'``).

    Always returns the ``gnu++`` mode (preserving non-ISO built-ins
    like ``unix``, ``linux``, ``__unix__``) rather than strict
    ``c++`` mode — gcc/clang both default to gnu mode, and switching
    to strict mode would itself invalidate PCH (``unix not defined``).

    Implementation: invokes ``<cxx> -dM -E -x c++ /dev/null`` and
    parses the ``__cplusplus`` macro value. Cached by ``cxx`` string.
    """
    if not cxx or not isinstance(cxx, str):
        return None
    cmd = shlex.split(cxx) + ["-dM", "-E", "-x", "c++", os.devnull]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # Parse `#define __cplusplus 202002L` etc.
    cplusplus_value: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("#define __cplusplus "):
            tok = line.split()[-1].rstrip("Ll")
            try:
                cplusplus_value = int(tok)
            except ValueError:
                return None
            break
    if cplusplus_value is None:
        return None
    # Map __cplusplus value → gnu++NN dialect string. Values are the
    # ISO C++ feature-test macro: 199711 (C++98), 201103 (C++11),
    # 201402 (C++14), 201703 (C++17), 202002 (C++20), 202302 (C++23),
    # and forward-compat for unreleased standards.
    _STD_MAP = {
        199711: "gnu++98",
        201103: "gnu++11",
        201402: "gnu++14",
        201703: "gnu++17",
        202002: "gnu++20",
        202302: "gnu++23",
        202602: "gnu++26",
    }
    dialect = _STD_MAP.get(cplusplus_value)
    if dialect is None:
        # Unknown future value — pick the closest known dialect ≤ value.
        # gnu++NN is forward-compatible (a c++23 compiler accepts
        # `-std=gnu++23` even if it predates the c++23 spec).
        known = sorted(k for k in _STD_MAP if k <= cplusplus_value)
        if not known:
            return None
        dialect = _STD_MAP[known[-1]]
    return f"-std={dialect}"


@functools.lru_cache(maxsize=8)
def _get_functional_cxx_compiler_cached(env_cxx=None, env_cc=None, env_path=None):
    """Internal cached implementation of functional C++ compiler detection.

    This function tests compiler candidates to ensure they can:
    - Execute basic version checks
    - Compile C++20 code with -std=c++20

    Args:
        These are only used for test cases.  For normal use, call get_functional_cxx_compiler()
        env_cxx: Value of CXX environment variable (or None)
        env_cc: Value of CC environment variable (or None)
        env_path: Value of PATH environment variable (for cache invalidation)

    Returns:
        str: Path to working C++ compiler executable, or None if none found
    """
    # Compiler candidates to test, in priority order
    candidates = []

    # Check environment variables first (user preference)
    if env_cxx and env_cxx.strip():
        candidates.append(env_cxx.strip())
    if env_cc and env_cc.strip():
        # Try adding ++ suffix for C compilers that might have C++ versions
        cc = env_cc.strip()
        candidates.append(cc)
        if cc.endswith("gcc"):
            candidates.append(cc.replace("gcc", "g++"))
        elif cc.endswith("clang"):
            candidates.append(cc.replace("clang", "clang++"))

    # Common system compiler names
    common_compilers = ["g++", "clang++", "gcc", "clang"]
    for compiler in common_compilers:
        if compiler not in candidates:
            candidates.append(compiler)

    # Test each candidate
    for compiler_name in candidates:
        if _test_compiler_functionality(compiler_name):
            return compiler_name

    return None


def derive_c_compiler_from_cxx(cxx_compiler):
    """Derive a C compiler from a C++ compiler name.

    Args:
        cxx_compiler (str): C++ compiler name (e.g., 'g++', 'clang++')

    Returns:
        str: Corresponding C compiler name (e.g., 'gcc', 'clang')
    """
    cxx_to_c_map = {
        "g++": "gcc",
        "clang++": "clang",
    }

    return cxx_to_c_map.get(cxx_compiler, cxx_compiler)


def get_functional_cxx_compiler():
    """Detect and return a fully functional C++ compiler that supports C++20.

    IMPORTANT: This is a FALLBACK mechanism for when args.CXX is not set.
    Production code should rely on args.CXX being properly configured by
    parseargs() rather than calling this function directly.

    This function tests compiler candidates to ensure they can:
    - Execute basic version checks
    - Compile C++20 code with -std=c++20

    The result is cached for performance since compiler detection is expensive.
    The cache key includes environment variables so changes are detected.

    Returns:
        str: Path to working C++ compiler executable, or None if none found

    Usage:
        # PREFERRED - rely on parseargs() setting args.CXX:
        args = parseargs(cap, argv)
        compiler = args.CXX  # Already validated and set

        # FALLBACK - only when args.CXX is not available:
        if not hasattr(args, 'CXX') or args.CXX is None:
            compiler = get_functional_cxx_compiler()
    """
    return _get_functional_cxx_compiler_cached()


# Expose the cache_clear method for tests
get_functional_cxx_compiler.cache_clear = _get_functional_cxx_compiler_cached.cache_clear  # type: ignore[attr-defined]


def _test_compiler_functionality(compiler_name):
    """Test if a compiler supports the functionality needed by the test suite.

    Args:
        compiler_name: Name or path of compiler to test

    Returns:
        bool: True if compiler is fully functional, False otherwise
    """
    try:
        # Test 1: Basic version check
        # Split compiler_name to handle multi-word commands like "ccache g++"
        result = subprocess.run(
            split_command_cached(compiler_name) + ["--version"], capture_output=True, timeout=5, text=True
        )
        if result.returncode != 0:
            return False

        # Test 2: C++20 compilation test
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cpp", delete=False) as f:
            # Write a simple C++20 test program
            f.write(
                textwrap.dedent("""
                #include <iostream>
                #include <string_view>
                #include <optional>
                #include <concepts>
                template<typename T>
                concept Integral = std::integral<T>;
                int main() {
                    std::string_view sv = "C++20 test";
                    std::optional<int> opt = 42;
                    return 0;
                }
            """).strip()
            )
            test_cpp = f.name

        obj_path = None
        try:
            # Try to compile with C++20
            with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as obj_file:
                obj_path = obj_file.name

            result = subprocess.run(
                split_command_cached(compiler_name) + ["-std=c++20", "-c", test_cpp, "-o", obj_path],
                capture_output=True,
                timeout=10,
                text=True,
            )

            success = result.returncode == 0

        finally:
            # Cleanup test files
            try:
                os.unlink(test_cpp)
            except OSError:
                pass
            if obj_path is not None:
                try:
                    os.unlink(obj_path)
                except OSError:
                    pass

        return success

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


@functools.cache
def tool_version(tool: str, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Probe ``<tool> --version`` for ``(major, minor)``.

    Returns ``default`` if the tool is missing, exits non-zero, or the
    first output line does not contain a ``\\d+\\.\\d+`` token. Cached so
    repeated probes during one process are free.
    """
    try:
        line = subprocess.check_output([tool, "--version"], text=True).splitlines()[0]
    except (subprocess.CalledProcessError, OSError, IndexError):
        return default
    m = re.search(r"(\d+)\.(\d+)", line)
    if not m:
        return default
    return (int(m.group(1)), int(m.group(2)))


def _compiler_major_version(compiler_path: str) -> tuple[str, int] | None:
    """Probe a compiler binary for its (family, major version).

    Runs ``<compiler> --version`` (one shot, ~10 ms) and parses gcc/clang
    output formats. Returns ``("gcc", N)`` / ``("clang", N)`` or ``None``
    if the binary isn't a recognised compiler driver. Wrapper scripts
    (coverage/sccache shims) that forward to a real gcc/clang typically
    pass-through ``--version`` and parse just like the real binary, so
    this is intentionally permissive.
    """
    import re as _re
    import subprocess

    # Tokenize so wrapper invocations like "ccache g++" forward --version
    # to the real compiler. Feeding the compound string as argv0 raises
    # OSError and silently degrades the check to "unknown driver, skip".
    argv = split_command_cached(compiler_path) if " " in compiler_path else [compiler_path]
    try:
        proc = subprocess.run(
            argv + ["--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    # gcc:   "g++ (GCC) 16.1.1 20260501 ..."  or "gcc-13 (Debian 13.2.0-...) 13.2.0"
    # clang: "clang version 22.1.4 (Fedora 22.1.4-1.fc44)"
    m = _re.search(r"clang version (\d+)", out)
    if m:
        return ("clang", int(m.group(1)))
    m = (
        _re.search(r"\(GCC\)\s+(\d+)", out)
        or _re.search(r"\bg\+\+ \(.*?\) (\d+)\.", out)
        or _re.search(r"\bgcc\b.*?(\d+)\.\d+", out)
    )
    if m:
        return ("gcc", int(m.group(1)))
    return None


def clear_cache():
    """Clear the compiler-probe caches owned by this module.

    Mirrors exactly the subset of ``cache_clear()`` calls the original
    monolithic :func:`compiletools.apptools.clear_cache` performed on the
    functions that now live here: ``_get_functional_cxx_compiler_cached``,
    ``compiler_identity``, ``compiler_kind``, ``compiler_default_cxx_std``,
    and ``find_system_std_module_source``.

    Note: ``tool_version`` is ``@functools.cache``-decorated but was NOT
    cleared by the original ``apptools.clear_cache`` — that omission is
    preserved here (no ``tool_version.cache_clear()`` call) so the net
    behaviour is identical. ``apptools.clear_cache`` calls this helper for
    the moved functions and continues to clear the non-moved
    ``cached_pkg_config`` directly.
    """
    _get_functional_cxx_compiler_cached.cache_clear()
    compiler_identity.cache_clear()
    compiler_kind.cache_clear()
    compiler_default_cxx_std.cache_clear()
    find_system_std_module_source.cache_clear()
