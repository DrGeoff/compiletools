"""Auto pkg-config inference: guess which pkg-config package owns a header.

This module is the resolver core for the ``--auto-pkg-config`` feature (plan
``docs/superpowers/plans/2026-09-02-auto-pkg-config.md``). It answers one
question per ``#include``: *which* ``.pc`` package would a user most plausibly
have written in a ``//#PKG-CONFIG`` marker for this header? Inferred packages
later enter the CAS key and the link line, so ambiguity resolves to
"unresolved + diagnostic" -- never a guess.

Resolver chain (first hit wins; each stage sees only what earlier stages left
unresolved):

1. ``header_map`` -- user-supplied ``header -> package`` overrides win over
   everything (even standard headers). Resolver ``"map"``.
2. :func:`is_standard_header` -- fast-path static list (C89-C23, C++98-C++23,
   common POSIX, libc/kernel prefixes). Not the only std guard: the compiler
   built-in include-dir probe (Stage 2: ``compiletools.compiler_macros
   .builtin_include_dirs``) plus :func:`classify_location` complete it.
3. ``ignore_globs`` -- fnmatch on the include spelling.
4. :func:`locate_header` -- find the header under ``InferenceConfig.search_dirs``;
   not found anywhere -> ``REASON_NOT_FOUND``.
5. :func:`classify_location` -- the header lives under a compiler built-in
   include dir other than the default dirs -> ``REASON_COMPILER``. This
   completes the static std list (item 2) for compiler-shipped headers:
   ``bits/...``, ``c++/N/...``, ``immintrin.h``, ``omp.h`` and friends.
6. Covered-by-include-dir -- the header lives under an explicit ``-I``/
   ``-isystem`` dir -> ``REASON_COVERED``.
7. Resolver chain: pc-index (``"pc"`` in ``InferenceConfig.resolvers``) ->
   distro -> man. The pc-index resolver walks the header's parent dirs upward
   against :func:`build_pc_include_index`; a single candidate wins, several go
   through :func:`tie_break`. An ambiguity verdict is terminal for the chain:
   the diagnostic is the answer, and later stages would only overwrite it.
8. Post-demotions: an inferred package the user already names explicitly (as a
   pkg-config package or via its first ``-l`` stem) reports ``REASON_COVERED``
   instead of "inferred".

Never-raise contract (plan finding F8): a broad ``except Exception`` in
``hunter`` swallows per-TU errors by dropping the TU's entire magic-flag set,
so every public function here returns a value and never propagates. Each
resolver step is individually wrapped -- an exception means "unresolved for
that resolver, continue".

Memo keys:
* :func:`build_pc_include_index` -- module-level dict keyed on
  ``(PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR, compiler_builtin_dirs)``.
* :func:`pkg_config_exists_quiet` -- module-level dict keyed on
  ``(pkg, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR)``.
* :func:`infer` -- caller-supplied ``memo`` dict keyed on
  ``(header_path, PKG_CONFIG_PATH, config.resolvers)`` holding the pre-post
  resolver-chain verdict (post-demotions depend on the config, so they are
  re-applied per call). ``clear_cache()`` clears only the module-level memos;
  caller-supplied memo dicts are the caller's lifetime.

Subprocess boundaries (the ONLY functions here that invoke subprocess, all
wrapped so they never raise): :func:`_run_pkg_config_index` and
:func:`_run_pkg_config_exists`. The distro/man boundaries
(:func:`_run_distro_owner_query`, :func:`_run_distro_list_query`,
:func:`_run_man_lookup`) are stubs pending Stages 3-4. Everything else is pure
python; tests stub only the boundaries.

Import-surface note: leaf module. Imports stdlib plus
:mod:`compiletools.apptools_pkgconfig` and NOTHING else from compiletools.
``apptools_pkgconfig.clear_cache`` fans out into :func:`clear_cache` via a
function-local import to keep this module off that one's import-time graph.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from compiletools import apptools_pkgconfig as _ap

REASON_STD = "standard header"
REASON_COMPILER = "compiler-shipped header"
REASON_COVERED = "covered by explicit flags"
REASON_IGNORED = "matched auto-pkg-config-ignore"
REASON_NOT_FOUND = "header not found on any include path"
REASON_NO_OWNER = "no pkg-config package exports this header"
REASON_NO_PC = "owner package ships no .pc file"
REASON_AMBIGUOUS = "ambiguous"
REASON_MISSING_PC = "inferred package failed pkg-config --exists"


@dataclass(frozen=True)
class InferenceResult:
    """Verdict for one include spelling."""

    header: str  # spelling in the #include
    header_path: str | None  # realpath if located
    package: str | None  # inferred pkg-config name; None if unresolved
    resolver: str | None  # "map" | "pc-index" | "distro:rpm" | "distro:dpkg" | "distro:pacman" | "distro:apk" | "man"
    candidates: tuple[str, ...]  # viable packages before tie-break (sorted)
    reason: str  # "" on success; else a REASON_* constant + detail


@dataclass(frozen=True)
class InferenceConfig:
    """Per-TU knobs for :func:`infer`. Every field defaults so ``InferenceConfig()``
    is a valid no-op configuration."""

    resolvers: tuple[str, ...] = ("pc", "distro")  # subset of ("pc", "distro", "man"), ordered
    header_map: Mapping[str, str] = MappingProxyType({})  # "openssl/ssl.h" -> "libssl"
    ignore_globs: tuple[str, ...] = ()
    explicit_packages: frozenset[str] = frozenset()  # bare names, TU + conf/CLI
    explicit_lib_stems: frozenset[str] = frozenset()  # from explicit -l tokens (TU magic + state LDFLAGS)
    explicit_include_dirs: tuple[str, ...] = ()  # realpaths from explicit -I/-isystem (TU magic + state)
    search_dirs: tuple[str, ...] = ()  # where to locate headers, in order
    compiler_builtin_dirs: tuple[str, ...] = ()  # from the Stage 2 probe
    strict: bool = False  # --pkg-config-errors=error


# ---------------------------------------------------------------------------
# Subprocess boundaries -- the only functions in this module that invoke
# subprocess. Each is wrapped so it never raises.
# ---------------------------------------------------------------------------


def _run_pkg_config_index() -> tuple[list[str], dict[str, str]]:
    """Return (package names from ``pkg-config --list-all``, dict of
    ``pkg -> --cflags-only-I`` output).

    A single batched ``pkg-config --cflags-only-I <all packages>`` would merge
    every package's flags into one string, losing per-package attribution --
    and dirs exported by multiple packages ("families") are exactly what the
    index exists to report. So: one subprocess per package. Packages whose
    per-package query fails (returncode != 0, e.g. a broken ``Requires``) are
    omitted from the output dict, not fatal. Any exception (pkg-config absent,
    interrupted) degrades to ``([], {})``.
    """
    try:
        listing = subprocess.run(
            ["pkg-config", "--list-all"],
            capture_output=True,
            check=False,
            text=True,
        )
        if listing.returncode != 0:
            return ([], {})
        names = [line.split()[0] for line in listing.stdout.splitlines() if line.split()]
        outputs: dict[str, str] = {}
        for name in names:
            probe = subprocess.run(
                ["pkg-config", "--cflags-only-I", name],
                capture_output=True,
                check=False,
                text=True,
            )
            if probe.returncode == 0:
                outputs[name] = probe.stdout
        return (names, outputs)
    except Exception:
        return ([], {})


def _run_distro_owner_query(paths, backend):
    """Stub: the distro-DB resolver lands in Stage 3. Returns None."""
    return


def _run_distro_list_query(pkg, backend):
    """Stub: the distro-DB resolver lands in Stage 3. Returns None."""
    return


def _run_man_lookup(header):
    """Stub: the man-page resolver lands in Stage 4. Returns None."""
    return


def _run_pkg_config_exists(pkg: str) -> bool:
    """``pkg-config --exists`` probe. Never raises; failures are False."""
    try:
        result = subprocess.run(
            ["pkg-config", "--exists", pkg],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# pc include-dir index
# ---------------------------------------------------------------------------

# Include dirs pkg-config exports that mean "the header is a system/libc
# header", not a package one. Skipped (realpath-compared) when building the
# index so bare /usr/include installs do not claim every system header.
_DEFAULT_INCLUDE_DIRS = ("/usr/include", "/usr/local/include")

# Keyed on (PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR, compiler_builtin_dirs).
_pc_include_index_memo: dict[tuple[str, str, tuple[str, ...]], dict[str, tuple[str, ...]]] = {}


def _include_dirs_from_cflags(raw: str) -> list[str]:
    """Extract the directory of each ``-I`` flag in pkg-config output.

    Handles both the attached (``-I/usr/include``) and detached (``-I
    /usr/include``) forms; a trailing bare ``-I`` is dropped.
    """
    tokens = _ap.tokenize_pkg_config_output(raw, package="?", option="--cflags-only-I")
    dirs: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-I" and index + 1 < len(tokens):
            dirs.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith("-I") and len(token) > 2:
            dirs.append(token[2:])
        index += 1
    return dirs


def build_pc_include_index(compiler_builtin_dirs: Sequence[str] = ()) -> dict[str, tuple[str, ...]]:
    """Map realpath(include dir) -> sorted tuple of pkg-config packages that
    export it (``--cflags-only-I``).

    Dirs in :data:`_DEFAULT_INCLUDE_DIRS` (bare ``/usr/include`` installs are
    blind to this resolver by design) and in *compiler_builtin_dirs* are
    skipped, both compared after realpath. Memoised in a module-level dict
    keyed on ``(PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR, compiler_builtin_dirs)``
    (F10, same env sensitivity as ``pkg_config_query_cache``); cleared by
    :func:`clear_cache`. The returned dict is shared -- treat it as read-only.
    """
    builtin_key = tuple(compiler_builtin_dirs)
    key = (
        os.environ.get("PKG_CONFIG_PATH", ""),
        os.environ.get("PKG_CONFIG_LIBDIR", ""),
        builtin_key,
    )
    cached = _pc_include_index_memo.get(key)
    if cached is not None:
        return cached

    names, outputs = _run_pkg_config_index()
    # NOT cached: realpath of already-absolute pkg-config -I output
    # (documented skip case #3); the index memo keeps this off any hot path.
    skip_dirs = {os.path.realpath(d) for d in _DEFAULT_INCLUDE_DIRS}
    # NOT cached: same, for already-absolute compiler built-in dirs.
    skip_dirs.update(os.path.realpath(d) for d in builtin_key if d)

    by_dir: dict[str, set[str]] = {}
    for name in names:
        raw = outputs.get(name)
        if raw is None:
            continue
        for directory in _include_dirs_from_cflags(raw):
            real = os.path.realpath(directory)  # NOT cached (abs -I dirs)
            if real in skip_dirs:
                continue
            by_dir.setdefault(real, set()).add(name)
    index = {d: tuple(sorted(packages)) for d, packages in by_dir.items()}
    _pc_include_index_memo[key] = index
    return index


# ---------------------------------------------------------------------------
# .pc metadata (pure python)
# ---------------------------------------------------------------------------


def _first_lib_stem(libs_value: str) -> str | None:
    """Return the stem of the first ``-l`` flag in a ``Libs:`` value.

    Handles attached and detached forms. Unexpanded ``${var}`` references are
    skipped: this module reads raw ``.pc`` lines without variable expansion,
    and a literal ``${...}`` is not a stem.
    """
    tokens = libs_value.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        stem: str | None = None
        if token == "-l" and index + 1 < len(tokens):
            stem = tokens[index + 1]
            index += 2
        elif token.startswith("-l") and len(token) > 2:
            stem = token[2:]
            index += 1
        else:
            index += 1
        if stem and not stem.startswith("${"):
            return stem
    return None


def _pc_metadata(pkg: str) -> dict:
    """Return {name, libs_first_stem, requires} parsed from *pkg*'s ``.pc``
    file, or {} on any failure (no .pc found, unreadable, malformed).

    Pure python: walks pkg-config's search order with
    ``apptools_pkgconfig._locate_pc_file`` and parses with ``_read_pc_file`` /
    ``_PC_KEYWORD_RE`` instead of spawning a subprocess.
    """
    try:
        path = _ap._locate_pc_file(pkg)
        if path is None:
            return {}
        meta: dict = {}
        for line in _ap._read_pc_file(path):
            match = _ap._PC_KEYWORD_RE.match(line)
            if not match:
                continue
            keyword = match.group(1)
            value = match.group(2).strip()
            if keyword == "Name" and "name" not in meta:
                meta["name"] = value
            elif keyword == "Libs" and "libs_first_stem" not in meta:
                stem = _first_lib_stem(value)
                if stem:
                    meta["libs_first_stem"] = stem
            elif keyword == "Requires" and "requires" not in meta:
                meta["requires"] = _ap._pc_required_packages(value)
        return meta
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Tie-break
# ---------------------------------------------------------------------------


def _header_first_component(header: str) -> str:
    """First path component of an include spelling: ``fam/x.h`` -> ``fam``,
    ``vector`` -> ``vector``."""
    return header.split("/", 1)[0]


def tie_break(candidates, header, header_path) -> str | None:
    """Pick one package from *candidates* for *header*, or None.

    Rules in order, stopping at the first that leaves exactly one candidate:

    1. the ``.pc`` ``Name:`` field or the package stem equals the header
       spelling's first path component (``fam/x.h`` -> ``fam``);
    2. the package's first ``-l`` stem equals that component;
    3. the package is not named in any sibling candidate's ``Requires``
       (umbrella packages win over their parts).

    None means the candidates are genuinely indistinguishable -> the caller
    reports ``REASON_AMBIGUOUS`` with all candidates. *header_path* is part of
    the signature for later stages (absolute-spelling fallback); rules 1-2 use
    the include spelling.
    """
    remaining = list(candidates)
    if len(remaining) <= 1:
        return remaining[0] if remaining else None

    component = _header_first_component(header)

    def metadata_for(pkg):
        return _pc_metadata(pkg)

    # Rule 1: .pc Name: or package stem equals the header's first component.
    matched = [
        pkg for pkg in remaining if component and (metadata_for(pkg).get("name") == component or pkg == component)
    ]
    if len(matched) == 1:
        return matched[0]
    if matched:
        remaining = matched

    # Rule 2: first -l stem equals the component.
    matched = [pkg for pkg in remaining if component and metadata_for(pkg).get("libs_first_stem") == component]
    if len(matched) == 1:
        return matched[0]
    if matched:
        remaining = matched

    # Rule 3: drop candidates named in a sibling's Requires (umbrella wins).
    required: set[str] = set()
    for pkg in remaining:
        required.update(metadata_for(pkg).get("requires", ()))
    matched = [pkg for pkg in remaining if pkg not in required]
    if len(matched) == 1:
        return matched[0]
    return None


# ---------------------------------------------------------------------------
# Standard-header fast path
# ---------------------------------------------------------------------------

# C89-C23 (including the C95/C11/C23 additions) and C++98-C++23 standard
# headers, plus the common POSIX names. Fast path only -- Stage 4 completes
# the guard with the compiler built-in include-dir probe.
_STD_HEADERS = frozenset(
    {
        # C89
        "assert.h",
        "ctype.h",
        "errno.h",
        "float.h",
        "limits.h",
        "locale.h",
        "math.h",
        "setjmp.h",
        "signal.h",
        "stdarg.h",
        "stddef.h",
        "stdio.h",
        "stdlib.h",
        "string.h",
        "time.h",
        # C95
        "iso646.h",
        "wchar.h",
        "wctype.h",
        # C99
        "complex.h",
        "fenv.h",
        "inttypes.h",
        "stdbool.h",
        "stdint.h",
        "tgmath.h",
        # C11
        "stdalign.h",
        "stdatomic.h",
        "stdnoreturn.h",
        "threads.h",
        # C23
        "stdbit.h",
        "stdckdint.h",
        # C++98
        "cassert",
        "cctype",
        "cerrno",
        "cfloat",
        "climits",
        "clocale",
        "cmath",
        "complex",
        "csetjmp",
        "csignal",
        "cstdarg",
        "cstddef",
        "cstdio",
        "cstdlib",
        "cstring",
        "ctime",
        "cwchar",
        "cwctype",
        "deque",
        "fstream",
        "functional",
        "iomanip",
        "ios",
        "iosfwd",
        "iostream",
        "istream",
        "iterator",
        "limits",
        "list",
        "locale",
        "map",
        "memory",
        "new",
        "numeric",
        "ostream",
        "queue",
        "set",
        "sstream",
        "stack",
        "stdexcept",
        "streambuf",
        "string",
        "typeinfo",
        "utility",
        "valarray",
        "vector",
        # C++11
        "array",
        "atomic",
        "bitset",
        "chrono",
        "codecvt",
        "condition_variable",
        "cfenv",
        "cinttypes",
        "cstdalign",
        "cstdbool",
        "cstdint",
        "ctgmath",
        "cuchar",
        "forward_list",
        "future",
        "initializer_list",
        "mutex",
        "ratio",
        "regex",
        "scoped_allocator",
        "system_error",
        "thread",
        "tuple",
        "typeindex",
        "unordered_map",
        "unordered_set",
        # C++20
        "barrier",
        "bit",
        "charconv",
        "compare",
        "concepts",
        "coroutine",
        "numbers",
        "ranges",
        "source_location",
        "span",
        "stop_token",
        "syncstream",
        "version",
        # C++23
        "expected",
        "flat_map",
        "flat_set",
        "generator",
        "mdspan",
        "print",
        "spanstream",
        "stacktrace",
        "stdfloat",
        # POSIX / common libc
        "dirent.h",
        "dlfcn.h",
        "endian.h",
        "fcntl.h",
        "fnmatch.h",
        "getopt.h",
        "glob.h",
        "grp.h",
        "iconv.h",
        "ifaddrs.h",
        "langinfo.h",
        "libgen.h",
        "libintl.h",
        "malloc.h",
        "monetary.h",
        "mqueue.h",
        "nl_types.h",
        "paths.h",
        "poll.h",
        "pthread.h",
        "pwd.h",
        "regex.h",
        "sched.h",
        "search.h",
        "semaphore.h",
        "spawn.h",
        "strings.h",
        "syslog.h",
        "tar.h",
        "termios.h",
        "ulimit.h",
        "unistd.h",
        "utime.h",
        "utmpx.h",
        "wordexp.h",
    }
)

# libc/kernel-internal prefixes: anything under these trees is not a
# third-party package header. Deliberately kept to the clearly libc/kernel
# prefixes (no drm/, ext2fs/).
_STD_PREFIXES = (
    "sys/",
    "netinet/",
    "arpa/",
    "linux/",
    "asm/",
    "asm-generic/",
    "bits/",
    "gnu/",
    "rpc/",
    "net/",
    "scsi/",
    "xen/",
)


def is_standard_header(name: str) -> bool:
    """True for C/C++/POSIX standard headers and libc/kernel-internal trees."""
    return name in _STD_HEADERS or name.startswith(_STD_PREFIXES)


# ---------------------------------------------------------------------------
# Header location
# ---------------------------------------------------------------------------


def locate_header(name: str, search_dirs) -> str | None:
    """First existing ``search_dir/name`` under *search_dirs*, realpath'd.

    Search-dir order is the caller's (Stage 5 builds it as TU-declared
    ``-isystem``/``-I`` -> state ``-isystem``/``-I`` -> ``CPATH``-family ->
    compiler built-ins); the first hit wins.
    """
    for directory in search_dirs:
        candidate = os.path.join(directory, name)
        # NOT cached: probe of a derived absolute candidate; a process-lifetime
        # isfile memo could miss freshly created fixture trees.
        if os.path.isfile(candidate):
            # NOT cached: realpath of an already-absolute derived candidate
            # (documented skip case #3).
            return os.path.realpath(candidate)
    return None


# Stage 1 spelling, kept working for its existing callers.
_locate = locate_header


def classify_location(header_path: str | None, builtin_dirs) -> str | None:
    """``REASON_COMPILER`` when *header_path* lives under a compiler built-in
    include dir that is not one of :data:`_DEFAULT_INCLUDE_DIRS`; else None.

    This is the location-based half of the compiler-shipped guard (F6):
    ``bits/...``, ``c++/N/...``, ``immintrin.h``, ``omp.h`` and friends live
    under the compiler's private include trees, while a bare ``/usr/include``
    (or ``/usr/local/include``) built-in dir is ordinary libc/kernel material
    a distro package may own and must stay resolvable. Both sides are
    realpath-compared; the prefix check uses a trailing separator so a dir
    like ``/usr/include-extra`` never matches ``/usr/include``.
    """
    if not header_path:
        return None
    # NOT cached: realpath of an already-absolute per-TU header path
    # (documented skip case #3).
    real = os.path.realpath(header_path)
    # NOT cached: realpath of a module-constant default-dir string.
    skip = {os.path.realpath(d) for d in _DEFAULT_INCLUDE_DIRS}
    for directory in builtin_dirs:
        if not directory:
            continue
        # NOT cached: realpath of an already-absolute built-in probe dir.
        real_dir = os.path.realpath(directory)
        if real_dir in skip:
            continue
        if real == real_dir or real.startswith(real_dir + os.sep):
            return REASON_COMPILER
    return None


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


def resolve_via_pc_index(header: str, header_path: str | None, index: Mapping[str, tuple[str, ...]]) -> InferenceResult:
    """Infer the owner from the pc include-dir index.

    Walks the parents of *header_path* upward until a directory is in *index*
    (stopping at the filesystem root). One candidate -> that package; several
    -> :func:`tie_break`; a tie-break miss -> ``REASON_AMBIGUOUS`` with all
    candidates listed in the reason. No directory ever matches ->
    ``REASON_NO_OWNER``.
    """
    if not header_path:
        return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)
    current = os.path.dirname(header_path)
    while True:
        if current in index:
            candidates = index[current]
            if len(candidates) == 1:
                return InferenceResult(header, header_path, candidates[0], "pc-index", candidates, "")
            winner = tie_break(candidates, header, header_path)
            if winner is not None:
                return InferenceResult(header, header_path, winner, "pc-index", candidates, "")
            return InferenceResult(
                header,
                header_path,
                None,
                None,
                candidates,
                f"{REASON_AMBIGUOUS}: {', '.join(candidates)}",
            )
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)


def resolve_via_distro(header: str, header_path: str | None) -> InferenceResult:
    """Distro-DB resolver (Stage 3). Stub: always unresolved; the distro
    subprocess boundaries are left untouched."""
    return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)


def resolve_via_man(header: str, header_path: str | None) -> InferenceResult:
    """man-page resolver (Stage 4). Stub: always unresolved; the man
    subprocess boundary is left untouched."""
    return InferenceResult(header, header_path, None, None, (), "man resolver not implemented")


# ---------------------------------------------------------------------------
# Quiet existence probe (F5)
# ---------------------------------------------------------------------------

# Keyed on (pkg, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR).
_exists_memo: dict[tuple[str, str, str], bool] = {}


def pkg_config_exists_quiet(pkg: str) -> bool:
    """``pkg-config --exists`` with no warnings and no strict-mode interaction.

    Unlike ``apptools_pkgconfig._cached_pkg_config_exists`` this never warns
    on stderr and never consults ``--pkg-config-errors=error``: a guess that
    misses is a diagnostic decision, not a build error. Memoised on
    (pkg, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR); never raises.
    """
    key = (
        pkg,
        os.environ.get("PKG_CONFIG_PATH", ""),
        os.environ.get("PKG_CONFIG_LIBDIR", ""),
    )
    if key in _exists_memo:
        return _exists_memo[key]
    value = _run_pkg_config_exists(pkg)
    _exists_memo[key] = value
    return value


# ---------------------------------------------------------------------------
# infer
# ---------------------------------------------------------------------------


def _is_ambiguity(reason: str) -> bool:
    """True when *reason* is an ambiguity verdict (possibly with detail)."""
    return reason == REASON_AMBIGUOUS or reason.startswith(REASON_AMBIGUOUS + ": ")


def _run_resolver_chain(
    header: str, header_path: str, config: InferenceConfig
) -> tuple[str | None, str | None, tuple[str, ...], str]:
    """Run the pc-index -> distro -> man chain, each step wrapped.

    Returns the winning verdict as (package, resolver, candidates, reason).
    An exception inside a resolver counts as unresolved for that resolver and
    the chain continues (F8). An ambiguity verdict stops the chain: the
    diagnostic is the answer and a later stub would only overwrite it.
    """
    package: str | None = None
    resolver: str | None = None
    candidates: tuple[str, ...] = ()
    reason = REASON_NO_OWNER

    try:
        if "pc" in config.resolvers:
            index = build_pc_include_index(config.compiler_builtin_dirs)
            result = resolve_via_pc_index(header, header_path, index)
            package, resolver, candidates, reason = (
                result.package,
                result.resolver,
                result.candidates,
                result.reason,
            )
    except Exception:
        package, resolver, candidates, reason = None, None, (), REASON_NO_OWNER
    if package is not None or _is_ambiguity(reason):
        return (package, resolver, candidates, reason)

    try:
        result = resolve_via_distro(header, header_path)
        package, resolver, candidates, reason = (
            result.package,
            result.resolver,
            result.candidates,
            result.reason,
        )
    except Exception:
        package, resolver, candidates, reason = None, None, (), REASON_NO_OWNER
    if package is not None or _is_ambiguity(reason):
        return (package, resolver, candidates, reason)

    try:
        result = resolve_via_man(header, header_path)
        package, resolver, candidates, reason = (
            result.package,
            result.resolver,
            result.candidates,
            result.reason,
        )
    except Exception:
        package, resolver, candidates, reason = None, None, (), REASON_NO_OWNER
    return (package, resolver, candidates, reason)


def _infer_one(header: str, config: InferenceConfig, memo: dict) -> InferenceResult:
    """Resolver chain for one header spelling. Never raises (wrapped by
    :func:`infer`), but kept exception-clean internally as well."""
    # 1. User map wins over everything, including standard headers.
    mapped = config.header_map.get(header)
    if mapped is not None:
        return InferenceResult(header, None, mapped, "map", (), "")

    # 2. Standard-header fast path.
    if is_standard_header(header):
        return InferenceResult(header, None, None, None, (), REASON_STD)

    # 3. Ignore globs, matched against the include spelling.
    for pattern in config.ignore_globs:
        if fnmatch.fnmatch(header, pattern):
            return InferenceResult(header, None, None, None, (), REASON_IGNORED)

    # 4. Locate the header.
    header_path = locate_header(header, config.search_dirs)
    if header_path is None:
        return InferenceResult(header, None, None, None, (), REASON_NOT_FOUND)

    # 5. Compiler-shipped: under a compiler built-in include dir other than
    #    the default dirs -> compiler/libc territory, not pkg-config material
    #    (F6 part 1; the location half of the std-header guard).
    compiler_reason = classify_location(header_path, config.compiler_builtin_dirs)
    if compiler_reason is not None:
        return InferenceResult(header, header_path, None, None, (), compiler_reason)

    # 6. Covered by an explicit include dir -> the user's own -I wins.
    for include_dir in config.explicit_include_dirs:
        # NOT cached: explicit include dirs are already realpaths from the
        # caller (documented skip case #3).
        real_dir = os.path.realpath(include_dir)
        if header_path == real_dir or header_path.startswith(real_dir + os.sep):
            return InferenceResult(header, header_path, None, None, (), REASON_COVERED)

    # 7. Resolver chain, memoised per (header_path, PKG_CONFIG_PATH,
    #    config.resolvers). Post-demotions depend on the config, so the memo
    #    holds the pre-post verdict only.
    chain_key = (header_path, os.environ.get("PKG_CONFIG_PATH", ""), config.resolvers)
    if chain_key in memo:
        package, resolver, candidates, reason = memo[chain_key]
    else:
        package, resolver, candidates, reason = _run_resolver_chain(header, header_path, config)
        memo[chain_key] = (package, resolver, candidates, reason)

    # 8. Post-demotions: an inferred package the user already covers
    #    explicitly reports REASON_COVERED, keeping the package attribution.
    if package is not None:
        if package in config.explicit_packages:
            return InferenceResult(header, header_path, package, resolver, candidates, REASON_COVERED)
        stem = _pc_metadata(package).get("libs_first_stem")
        if stem and stem in config.explicit_lib_stems:
            return InferenceResult(header, header_path, package, resolver, candidates, REASON_COVERED)

    return InferenceResult(header, header_path, package, resolver, candidates, reason)


def infer(headers: Sequence[str], config: InferenceConfig, *, memo: dict) -> list[InferenceResult]:
    """Run the resolver chain for each include spelling, input order preserved.

    One result per *distinct* header (later duplicates of a spelling are
    dropped). Never raises: the whole per-header body is wrapped, and a
    header that somehow raises still yields an unresolved result.

    Covered contract: a header located under an explicit include dir is
    demoted before the resolver chain and reports ``REASON_COVERED`` with
    ``package=None``; a chain verdict demoted because the user already
    covers the package explicitly (``explicit_packages``, or the package's
    first ``-l`` stem in ``explicit_lib_stems``) keeps its
    ``package``/``resolver``/``candidates`` attribution and reports
    ``REASON_COVERED`` as the reason.
    """
    results: list[InferenceResult] = []
    seen: set[str] = set()
    for header in headers:
        if header in seen:
            continue
        seen.add(header)
        try:
            results.append(_infer_one(header, config, memo))
        except Exception:
            results.append(InferenceResult(header, None, None, None, (), REASON_NO_OWNER))
    return results


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_cache() -> None:
    """Clear this module's memos (pc include index, quiet-exists probe).

    Does NOT clear caller-supplied :func:`infer` ``memo`` dicts -- those are
    owned by their callers. Fanned out from
    ``apptools_pkgconfig.clear_cache``.
    """
    _pc_include_index_memo.clear()
    _exists_memo.clear()
