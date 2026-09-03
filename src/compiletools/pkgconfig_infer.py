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
4. ``unresolved_explicit_packages`` -- the header's namespace
   (:func:`header_namespace`) names a package the user declared and
   pkg-config cannot find -> ``REASON_DECLARED_MISSING``. Substituting some
   other package that merely vendors a copy of the header would turn a clean
   "no such file" into a build that links the wrong libraries.
5. :func:`locate_header` -- find the header under ``InferenceConfig.search_dirs``;
   not found anywhere -> ``REASON_NOT_FOUND``.
6. :func:`classify_location` -- the header lives under a compiler built-in
   include dir other than the default dirs -> ``REASON_COMPILER``. This
   completes the static std list (item 2) for compiler-shipped headers:
   ``bits/...``, ``c++/N/...``, ``immintrin.h``, ``omp.h`` and friends.
7. Covered-by-include-dir -- the header lives under an explicit ``-I``/
   ``-isystem`` dir -> ``REASON_COVERED``.
8. Resolver chain: pc-index (``"pc"`` in ``InferenceConfig.resolvers``) ->
   distro (``"distro"``) -> man (``"man"``); each stage runs only when its
   name is listed in ``InferenceConfig.resolvers`` (default
   ``("pc", "distro")``). The pc-index resolver walks the
   header's parent dirs upward against :func:`build_pc_include_index`; a
   single candidate wins, several go through :func:`tie_break`. An ambiguity
   verdict is terminal for the chain: the diagnostic is the answer, and
   later stages would only overwrite it.
9. Distro DB (Stage 3): the distro package owning the header is found via
   ONE batched query per TU (rpm -qf / dpkg-query -S / pacman -Qoq / apk
   info --who-owns, see :func:`owners_for_headers`); a libc/kernel owner
   (``_LIBC_OWNERS``) counts as a standard header, an owner shipping no
   ``.pc`` in the live search dirs reports ``REASON_NO_PC``, several stems
   go through :func:`tie_break`. The backend is detected on PATH
   (:func:`detect_distro_backend`, rpm -> dpkg -> pacman -> apk) and can be
   forced or masked with ``CT_DISTRO_BACKEND`` (rpm|dpkg|pacman|apk|none);
   no backend means the stage runs no subprocess at all and only the static
   std fast path (item 2) classifies standard headers.
10. man (Stage 4): :func:`resolve_via_man` honours the ``"man"`` resolver
   gate but is an intentional stub -- it always returns unresolved with the
   reason ``"man resolver not implemented"`` and never touches its
   :func:`_run_man_lookup` boundary, which exists as a drop-in slot for the
   deferred implementation.
11. Post-demotions: an inferred package the user already names explicitly (as a
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
* :func:`pc_stems_shipped_by` -- module-level dict keyed on
  ``(owner, backend, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR)``.
* :func:`detect_distro_backend` -- module-level dict holding the PATH probe
  result (a real None is cached too); the ``CT_DISTRO_BACKEND`` override is
  honoured even when the memo is populated, so masking never needs to know
  the memo state.
* :func:`infer` -- caller-supplied ``memo`` dict keyed on
  ``(header_path, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR, config.resolvers,
  config.compiler_builtin_dirs)`` (via :func:`_chain_memo_key`; the same
  env/builtin-dir sensitivity as the memos above) holding the pre-post
  resolver-chain verdict (post-demotions depend on the config, so they are
  re-applied per call). ``clear_cache()`` clears only the module-level memos;
  caller-supplied memo dicts are the caller's lifetime.

Subprocess boundaries (the ONLY functions here that invoke subprocess, all
wrapped so they never raise): :func:`_run_pkg_config_index`,
:func:`_run_pkg_config_exists`, :func:`_run_distro_owner_query` (one batched
query for all still-unresolved headers of a TU; apk takes one call per path)
and :func:`_run_distro_list_query` (per owning package). The man boundary
(:func:`_run_man_lookup`) is a drop-in slot for the deferred man resolver:
the Stage 4 stub (:func:`resolve_via_man`) never calls it. Everything else is
pure python; tests stub only the boundaries.

Import-surface note: leaf module. Imports stdlib plus
:mod:`compiletools.apptools_pkgconfig` and NOTHING else from compiletools.
``apptools_pkgconfig.clear_cache`` fans out into :func:`clear_cache` via a
function-local import to keep this module off that one's import-time graph.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
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
REASON_DECLARED_MISSING = "declared pkg-config package is unavailable"


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
    unresolved_explicit_packages: frozenset[str] = frozenset()  # declared, but pkg-config cannot find them
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
    """Raw stdout of ONE batched distro-DB owner query for *paths*.

    Per backend:

    * rpm -- ``rpm -qf --qf '%{NAME}\\n' <paths>``: one line per path, in
      order. A path owned by nothing prints ``file <path> is not owned by
      any package`` on stdout instead of a package name; a nonexistent path
      prints only to stderr (so stdout can carry fewer lines than paths).
    * dpkg -- ``dpkg-query -S <paths>``: unordered ``pkg:arch: path`` (or
      ``pkg: path``) lines; an unowned path is reported on stderr only.
    * pacman -- ``pacman -Qoq <paths>``: one package per path, in order;
      errors go to stderr.
    * apk -- no batched who-owns: one ``apk info --who-owns <path>`` per
      path (bounded by the already-filtered candidate count), outputs joined
      with newlines as ``<path> is owned by <pkg>-<version>``.

    The returncode is deliberately NOT gated: rpm/dpkg/pacman exit non-zero
    when *some* paths are unowned while stdout still carries the owned ones,
    and those partial results are exactly what the batched query exists for
    (parsing decides what is trustworthy; see :func:`owners_for_headers`).
    An unknown backend or any exception (tool vanished, interrupted) -> None.
    """
    try:
        if backend == "rpm":
            cmd = ["rpm", "-qf", "--qf", "%{NAME}\\n", *paths]
        elif backend == "dpkg":
            cmd = ["dpkg-query", "-S", *paths]
        elif backend == "pacman":
            cmd = ["pacman", "-Qoq", *paths]
        elif backend == "apk":
            chunks = []
            for path in paths:
                probe = subprocess.run(
                    ["apk", "info", "--who-owns", path],
                    capture_output=True,
                    check=False,
                    text=True,
                )
                chunks.append(probe.stdout)
            return "\n".join(chunks)
        else:
            return None
        probe = subprocess.run(cmd, capture_output=True, check=False, text=True)
        return probe.stdout
    except Exception:
        return None


def _run_distro_list_query(pkg, backend):
    """Raw stdout of the distro-DB file list for package *pkg*
    (``rpm -ql`` / ``dpkg-query -L`` / ``pacman -Qlq`` / ``apk info -L``).

    A non-zero exit (a package that is not installed lists nothing) or any
    exception -> None; an unknown backend -> None.
    """
    commands = {
        "rpm": ["rpm", "-ql", pkg],
        "dpkg": ["dpkg-query", "-L", pkg],
        "pacman": ["pacman", "-Qlq", pkg],
        "apk": ["apk", "info", "-L", pkg],
    }
    cmd = commands.get(backend)
    if cmd is None:
        return None
    try:
        probe = subprocess.run(cmd, capture_output=True, check=False, text=True)
        if probe.returncode != 0:
            return None
        return probe.stdout
    except Exception:
        return None


def _run_man_lookup(header):
    """Subprocess-boundary slot for the man-page resolver. Intentionally
    unimplemented: the Stage 4 stub resolver (:func:`resolve_via_man`) never
    calls it, so the real implementation is a drop-in."""
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
        "algorithm",
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
        "random",
        "ratio",
        "regex",
        "scoped_allocator",
        "system_error",
        "thread",
        "tuple",
        "type_traits",
        "typeindex",
        "unordered_map",
        "unordered_set",
        # C++14
        "shared_mutex",
        # C++17
        "any",
        "charconv",
        "execution",
        "filesystem",
        "memory_resource",
        "optional",
        "string_view",
        "variant",
        # C++20
        "barrier",
        "bit",
        "compare",
        "concepts",
        "coroutine",
        "format",
        "latch",
        "numbers",
        "ranges",
        "semaphore",
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
    "netpacket/",
    "scsi/",
    "xen/",
)


def is_standard_header(name: str) -> bool:
    """True for C/C++/POSIX standard headers and libc/kernel-internal trees."""
    return name in _STD_HEADERS or name.startswith(_STD_PREFIXES)


# ---------------------------------------------------------------------------
# Declared-package namespace
# ---------------------------------------------------------------------------


# Trailing version/ABI suffix of a package name: optional ``+``/``-``
# separator, then a digit run (possibly dotted), end-anchored. ``_PACKAGE_*``
# so it is unmistakably private to the normalization below.
_PACKAGE_VERSION_SUFFIX_RE = re.compile(r"(?:[+-]?\d+(?:\.\d+)*)$")


def normalize_package_name(name: str) -> str:
    """Normalize *name* for the declared-package namespace comparison.

    Casefold, strip one leading ``lib`` (so ``libFoo``, ``LibFoo`` and ``foo``
    compare equal), then strip a trailing version/ABI suffix -- an
    end-anchored ``[+-]?<digits>(.<digits>)*`` run plus any trailing ``+`` --
    so ``libpng16``/``png``, ``gtk+-3.0``/``gtk``, ``libssl3``/``ssl`` and
    ``glib-2.0``/``glib`` compare equal to their bare library namespaces.
    The suffix strip repeats until stable, so compound forms
    (``sigc++-2.0``, ``gtk3+``) normalize the same as their parts.

    Deliberately conservative: only a *trailing* run is stripped, so digits
    that are part of a word (``sdl2_image``) or names with no trailing digits
    (``zlib``) are untouched, and the comparison this feeds
    (:func:`declared_missing_owner`) still errs towards "these are different
    packages", which leaves inference running.
    """
    lowered = name.strip().casefold()
    if lowered.startswith("lib") and len(lowered) > 3:
        lowered = lowered[3:]
    while True:
        stripped = _PACKAGE_VERSION_SUFFIX_RE.sub("", lowered).rstrip("+")
        if stripped == lowered:
            return lowered
        lowered = stripped


def header_namespace(header: str) -> str:
    """Normalized package namespace an include spelling belongs to.

    ``foo/bar.h`` -> ``foo`` (the directory component packages conventionally
    take their name from -- the same convention :func:`tie_break` rule 1
    already relies on); a component-less ``foo.h`` -> ``foo``.
    """
    first = _header_first_component(header)
    stem = first.rsplit(".", 1)[0] if "." in first else first
    return normalize_package_name(stem)


def declared_missing_owner(header: str, unresolved_packages) -> str | None:
    """The declared-but-unavailable package *header* belongs to, or None.

    A user who wrote ``//#PKG-CONFIG=foo`` has already answered the question
    inference exists to guess at. When ``foo`` fails ``pkg-config --exists``,
    attributing ``foo/bar.h`` to whichever installed package happens to
    vendor a copy of that header replaces a clean "no such file" compile
    error with a build that silently links the wrong libraries -- and folds
    those libraries into the CAS key. Matching is by namespace, so the rule
    covers the whole ``foo/`` tree rather than the one spelling the user
    happened to hit first. Ties are resolved by sorting, so the reported
    package is deterministic.
    """
    if not unresolved_packages:
        return None
    namespace = header_namespace(header)
    if not namespace:
        return None
    for package in sorted(unresolved_packages):
        if namespace == normalize_package_name(package):
            return package
    return None


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
# Distro-DB resolver (Stage 3)
# ---------------------------------------------------------------------------

# Owner package names that are libc/kernel material, not pkg-config packages:
# a header owned by one of these is a standard header for inference purposes
# (F6 part 2, the owner half of the std-header guard -- the location half is
# :func:`classify_location`). Checked before any package-list query, so a
# glibc-devel-owned header never costs a second subprocess.
_LIBC_OWNERS = frozenset(
    {
        "glibc-headers",
        "glibc-devel",
        "kernel-headers",
        "libc6-dev",
        "linux-libc-dev",
        "musl-dev",
        "linux-headers",
        "bionic-headers",
        "linux-api-headers",
        "glibc",
    }
)

# Backend name -> query tool, in detection priority order.
_DISTRO_BACKEND_TOOLS: tuple[tuple[str, str], ...] = (
    ("rpm", "rpm"),
    ("dpkg", "dpkg-query"),
    ("pacman", "pacman"),
    ("apk", "apk"),
)
_DISTRO_BACKENDS = frozenset(name for name, _tool in _DISTRO_BACKEND_TOOLS)

# Memoised PATH probe. A real None result (no backend on PATH) is cached too:
# detection runs before every distro resolution, and a non-distro box must
# not pay four shutil.which probes per call. Cleared by clear_cache(). The
# CT_DISTRO_BACKEND override is checked BEFORE this memo, so tests and
# masking always win without knowing the memo state.
_distro_backend_memo: dict[str, str | None] = {}


def detect_distro_backend() -> str | None:
    """First distro package DB whose query tool is on PATH, or None.

    Priority: rpm, dpkg (via ``dpkg-query``), pacman, apk. Memoised --
    including a real None result -- and cleared by :func:`clear_cache`. The
    ``CT_DISTRO_BACKEND`` environment variable overrides detection (values:
    ``rpm`` | ``dpkg`` | ``pacman`` | ``apk`` | ``none``); an unknown value
    is ignored and detection proceeds. The override is honoured even when a
    detection result is already memoised, so forcing or masking the backend
    in tests never needs to know the memo state.
    """
    override = os.environ.get("CT_DISTRO_BACKEND", "").strip().lower()
    if override == "none":
        return None
    if override in _DISTRO_BACKENDS:
        return override
    if "backend" in _distro_backend_memo:
        return _distro_backend_memo["backend"]
    backend: str | None = None
    try:
        for name, tool in _DISTRO_BACKEND_TOOLS:
            # NOT cached: shutil.which probes PATH, which tests change between
            # calls; the memo above is the cache.
            if shutil.which(tool):
                backend = name
                break
    except Exception:
        backend = None
    _distro_backend_memo["backend"] = backend
    return backend


def _parse_rpm_owner_output(stdout: str, paths: Sequence[str]) -> dict[str, str]:
    """rpm prints one line per path in order; an unowned path prints
    ``file <path> is not owned by any package`` instead of a package name, so
    the 1:1 positional mapping survives mixed owned/unowned sets. A line
    count that does not match ``len(paths)`` (a nonexistent path prints to
    stderr only) is unalignable -> no owners at all rather than a shifted
    guess. A package literally named ``file`` is not mistaken for the
    message: the message is longer and pattern-checked.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != len(paths):
        return {}
    owners: dict[str, str] = {}
    for path, line in zip(paths, lines):
        if line.startswith("file ") and line.endswith(" is not owned by any package"):
            continue
        owners[path] = line
    return owners


def _parse_dpkg_owner_output(stdout: str, paths: Sequence[str]) -> dict[str, str]:
    """dpkg-query -S prints ``pkg:arch: path`` (or ``pkg: path``) lines in no
    defined order; a path owned by several packages prints them
    comma-separated (``a, b:arch: path``) -- the first owner wins. Lines that
    do not parse into that shape (diversions, noise) are skipped, which also
    keeps stderr-echoed noise from minting owners."""
    wanted = set(paths)
    owners: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("diversion by ") or line.startswith("local diversion"):
            continue
        left, separator, path = line.rpartition(": ")
        if not separator:
            continue
        path = path.strip()
        if path not in wanted:
            continue
        owner = left.split(",", 1)[0].split(":", 1)[0].strip()
        if owner and path not in owners:
            owners[path] = owner
    return owners


def _parse_pacman_owner_output(stdout: str, paths: Sequence[str]) -> dict[str, str]:
    """pacman -Qoq prints one package per path, in order; unowned paths are
    reported on stderr only, so a line count below ``len(paths)`` is
    unalignable -> no owners at all rather than a shifted guess."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != len(paths):
        return {}
    return dict(zip(paths, lines))


def _parse_apk_owner_output(stdout: str, paths: Sequence[str]) -> dict[str, str]:
    """apk prints ``<path> is owned by <pkg>-<version>`` (one joined chunk per
    path). The version suffix is stripped by splitting off the last two dash
    components (apk versions are ``<upstream>-r<pkgrel>``), which keeps
    package names that themselves contain dashes intact."""
    marker = " is owned by "
    wanted = set(paths)
    owners: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if marker not in line:
            continue
        path, _, pkgver = line.partition(marker)
        path = path.strip()
        if path not in wanted:
            continue
        owner = pkgver.strip().rsplit("-", 2)[0].strip()
        if owner and path not in owners:
            owners[path] = owner
    return owners


_OWNER_PARSERS = {
    "rpm": _parse_rpm_owner_output,
    "dpkg": _parse_dpkg_owner_output,
    "pacman": _parse_pacman_owner_output,
    "apk": _parse_apk_owner_output,
}


def owners_for_headers(paths: Sequence[str], backend: str) -> dict[str, str]:
    """Map each header path to the distro package owning it.

    ONE batched query for the whole set (apk: one call per path inside the
    single :func:`_run_distro_owner_query` call). Unparseable output, an
    unknown backend, or any failure maps to an empty dict: unresolved, never
    a guess, never an exception. Paths with no owner are simply absent.
    """
    paths = list(paths)
    if not paths or backend not in _OWNER_PARSERS:
        return {}
    stdout = _run_distro_owner_query(paths, backend)
    if stdout is None:
        return {}
    try:
        return _OWNER_PARSERS[backend](stdout, paths)
    except Exception:
        return {}


# Keyed on (owner, backend, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR).
_pc_stems_memo: dict[tuple[str, str, str, str], tuple[str, ...]] = {}


def pc_stems_shipped_by(owner: str, backend: str) -> tuple[str, ...]:
    """Sorted stems of the ``.pc`` files distro package *owner* ships.

    Only ``.pc`` files whose directory is one of the live pkg-config search
    dirs (:func:`apptools_pkgconfig._pkg_config_search_dirs`, realpath
    compared on both sides) count: a .pc installed where pkg-config will
    never look is invisible to this feature. The file itself is NOT probed
    for existence -- the package file list is the evidence; a stale list
    entry degrades to a later ``REASON_MISSING_PC``, not a crash. Memoised
    per (owner, backend, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR); cleared by
    :func:`clear_cache`. Never raises.
    """
    key = (
        owner,
        backend,
        os.environ.get("PKG_CONFIG_PATH", ""),
        os.environ.get("PKG_CONFIG_LIBDIR", ""),
    )
    if key in _pc_stems_memo:
        return _pc_stems_memo[key]
    stems: tuple[str, ...] = ()
    try:
        listing = _run_distro_list_query(owner, backend)
        if listing:
            search_dirs = {
                # NOT cached: search dirs come from PKG_CONFIG_PATH, whose
                # entries are user-supplied and may be relative, so a cached
                # realpath could survive a chdir (same reasoning as
                # apptools_pkgconfig._locate_pc_file).
                os.path.realpath(directory)
                for directory in _ap._pkg_config_search_dirs()
                if directory
            }
            found: set[str] = set()
            for line in listing.splitlines():
                line = line.strip()
                if not line.endswith(".pc"):
                    continue
                # NOT cached: realpath of a list line from a one-off
                # subprocess answer.
                if os.path.realpath(os.path.dirname(line)) in search_dirs:
                    found.add(os.path.basename(line)[: -len(".pc")])
            stems = tuple(sorted(found))
    except Exception:
        stems = ()
    _pc_stems_memo[key] = stems
    return stems


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


def resolve_via_distro(
    header: str,
    header_path: str | None,
    *,
    backend: str | None = None,
    owner: str | None = None,
) -> InferenceResult:
    """Infer the owner from the distro package database.

    *backend* ``None`` -> :func:`detect_distro_backend`; still ``None`` ->
    unresolved ``REASON_NO_OWNER`` naming the missing database, and NO
    subprocess is started. *owner* (precomputed by :func:`infer`'s batched
    owner query) skips the per-header owner query -- an empty string is the
    batch's definitive "no owner" answer, so a batched miss never triggers a
    second, per-header query. Otherwise: no owner -> ``REASON_NO_OWNER``; a
    libc/kernel owner (:data:`_LIBC_OWNERS`) -> ``REASON_STD`` (F6 part 2);
    no ``.pc`` in the live search dirs -> ``REASON_NO_PC`` naming the owner;
    one stem -> that package; several -> :func:`tie_break`; a tie-break miss
    -> ``REASON_AMBIGUOUS`` with all stems. Resolver string
    ``distro:<backend>`` on a hit. Never raises.
    """
    if backend is None:
        backend = detect_distro_backend()
    if backend is None:
        return InferenceResult(
            header,
            header_path,
            None,
            None,
            (),
            f"{REASON_NO_OWNER}: no distro package database found",
        )
    if owner is None and header_path:
        owner = owners_for_headers([header_path], backend).get(header_path)
    if not owner:
        return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)
    if owner in _LIBC_OWNERS:
        return InferenceResult(header, header_path, None, None, (), REASON_STD)
    stems = pc_stems_shipped_by(owner, backend)
    if not stems:
        return InferenceResult(header, header_path, None, None, (), f"{REASON_NO_PC}: {owner}")
    if len(stems) == 1:
        return InferenceResult(header, header_path, stems[0], f"distro:{backend}", stems, "")
    winner = tie_break(stems, header, header_path)
    if winner is not None:
        return InferenceResult(header, header_path, winner, f"distro:{backend}", stems, "")
    return InferenceResult(
        header,
        header_path,
        None,
        None,
        stems,
        f"{REASON_AMBIGUOUS}: {', '.join(stems)}",
    )


def resolve_via_man(header: str, header_path: str | None) -> InferenceResult:
    """man-page resolver (Stage 4). Stub by design: always unresolved with
    the ``"man resolver not implemented"`` reason, and the :func:`_run_man_lookup`
    boundary is never touched, so the later implementation is a drop-in."""
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


def _pc_index_stage(header: str, header_path: str, config: InferenceConfig) -> InferenceResult | None:
    """The pc-index stage of the resolver chain.

    Returns a terminal verdict when the pc index settles the header (single
    candidate, tie-break winner, or a terminal ambiguity); ``None`` when the
    stage is disabled, fails (F8: an exception counts as unresolved for that
    resolver and the chain continues), or leaves the header to a later
    resolver. The index itself is memoised, so calling this per header costs
    no extra subprocess beyond the first.
    """
    if "pc" not in config.resolvers:
        return None
    try:
        index = build_pc_include_index(config.compiler_builtin_dirs)
        result = resolve_via_pc_index(header, header_path, index)
    except Exception:
        return None
    if result.package is not None or _is_ambiguity(result.reason):
        return result
    return None


def _demote(result: InferenceResult, config: InferenceConfig) -> InferenceResult:
    """Post-demotion (step 9): an inferred package the user already covers
    explicitly reports ``REASON_COVERED``, keeping the package/resolver/
    candidates attribution."""
    if result.package is None:
        return result
    if result.package in config.explicit_packages:
        return InferenceResult(
            result.header, result.header_path, result.package, result.resolver, result.candidates, REASON_COVERED
        )
    stem = _pc_metadata(result.package).get("libs_first_stem")
    if stem and stem in config.explicit_lib_stems:
        return InferenceResult(
            result.header, result.header_path, result.package, result.resolver, result.candidates, REASON_COVERED
        )
    return result


def _chain_memo_key(
    header_path: str, config: InferenceConfig
) -> tuple[str, str, str, tuple[str, ...], tuple[str, ...]]:
    """Memo key for the resolver-chain verdict, shared by :func:`_pre_chain`
    (lookup + store of the pc-index stage) and :func:`infer` (store of the
    distro -> man tail).

    Folds the same inputs the module's other memos do (F10): the header's
    realpath, the PKG_CONFIG_PATH/PKG_CONFIG_LIBDIR pair, the enabled
    resolvers, and the compiler built-in dirs the pc-index stage consulted
    (they feed :func:`build_pc_include_index`'s skip list, so they change
    which packages an index can attribute). ``_demote``'s inputs are
    deliberately NOT in the key: the memo holds the pre-demotion verdict and
    post-demotions are re-applied per call.
    """
    return (
        header_path,
        os.environ.get("PKG_CONFIG_PATH", ""),
        os.environ.get("PKG_CONFIG_LIBDIR", ""),
        config.resolvers,
        config.compiler_builtin_dirs,
    )


def _pre_chain(header: str, config: InferenceConfig, memo: dict) -> tuple[InferenceResult | None, str | None]:
    """Everything before the distro stage, for one header spelling.

    Returns ``(settled, header_path)``. *settled* is the final
    :class:`InferenceResult` when the header needs no distro stage: a user
    map hit, standard-header fast path, ignore glob, a declared-but-missing
    package owning the namespace, unlocatable, compiler-shipped, covered by
    an explicit include dir, a memo hit, or a terminal pc-index verdict
    (memo hits and pc verdicts already carry their post-demotion). Otherwise
    ``None`` with the located *header_path* for the batched distro stage.
    """
    # 1. User map wins over everything, including standard headers.
    mapped = config.header_map.get(header)
    if mapped is not None:
        return InferenceResult(header, None, mapped, "map", (), ""), None

    # 2. Standard-header fast path.
    if is_standard_header(header):
        return InferenceResult(header, None, None, None, (), REASON_STD), None

    # 3. Ignore globs, matched against the include spelling.
    for pattern in config.ignore_globs:
        if fnmatch.fnmatch(header, pattern):
            return InferenceResult(header, None, None, None, (), REASON_IGNORED), None

    # 4. The header belongs to a package the user declared and pkg-config
    #    cannot find. Never substitute a different package for it.
    missing_owner = declared_missing_owner(header, config.unresolved_explicit_packages)
    if missing_owner is not None:
        return InferenceResult(header, None, None, None, (), f"{REASON_DECLARED_MISSING}: {missing_owner}"), None

    # 5. Locate the header.
    header_path = locate_header(header, config.search_dirs)
    if header_path is None:
        return InferenceResult(header, None, None, None, (), REASON_NOT_FOUND), None

    # 6. Compiler-shipped: under a compiler built-in include dir other than
    #    the default dirs -> compiler/libc territory, not pkg-config material
    #    (F6 part 1; the location half of the std-header guard).
    compiler_reason = classify_location(header_path, config.compiler_builtin_dirs)
    if compiler_reason is not None:
        return InferenceResult(header, header_path, None, None, (), compiler_reason), None

    # 7. Covered by an explicit include dir -> the user's own -I wins.
    # Spelling-aware, not realpath-prefix: the user's flag serves the
    # translation unit only if it resolves THIS spelling (the compiler
    # probes <dir>/<spelling>). A prefix test alone would let a catch-all
    # -I (e.g. the gitroot include build_inputs adds) blanket-cover every
    # header whose realpath merely happens to live under it, including
    # pkg-config fixtures inside the source tree the -I never reaches.
    for include_dir in config.explicit_include_dirs:
        # NOT cached: explicit include dirs are already realpaths from the
        # caller (documented skip case #3).
        real_dir = os.path.realpath(include_dir)
        spelling_candidate = os.path.join(real_dir, header)
        # NOT wrappedos: spelling_probe is chdir-relative-input + a one-shot
        # diagnostic-time existence check against a possibly-symlinked tree;
        # the realpath comparison needs the on-disk target, not a cached one.
        if os.path.isfile(spelling_candidate) and os.path.realpath(spelling_candidate) == header_path:
            return InferenceResult(header, header_path, None, None, (), REASON_COVERED), None

    # 8. Resolver chain, memoised per :func:`_chain_memo_key` -- the same
    #    (header_path, PKG_CONFIG_PATH, PKG_CONFIG_LIBDIR, resolvers,
    #    compiler_builtin_dirs) tuple the module's other memos fold, so a
    #    verdict computed under one pkg-config env/builtin-dir state never
    #    serves a different one. Post-demotions depend on the config, so the
    #    memo holds the pre-demotion verdict only.
    chain_key = _chain_memo_key(header_path, config)
    if chain_key in memo:
        package, resolver, candidates, reason = memo[chain_key]
        return _demote(InferenceResult(header, header_path, package, resolver, candidates, reason), config), header_path

    pc_result = _pc_index_stage(header, header_path, config)
    if pc_result is not None:
        memo[chain_key] = (pc_result.package, pc_result.resolver, pc_result.candidates, pc_result.reason)
        return _demote(pc_result, config), header_path

    return None, header_path


def _chain_tail(
    header: str,
    header_path: str,
    config: InferenceConfig,
    *,
    backend: str | None,
    owner: str | None,
) -> InferenceResult:
    """Distro -> man stages for one header, each wrapped (F8).

    The first stage to settle the header wins (an ambiguity verdict is
    terminal); when every enabled stage leaves it unresolved, the LAST
    stage's verdict is the diagnostic. *backend*/*owner* come from the
    batched precomputation in :func:`infer`: *owner* ``None`` means "not
    precomputed" (the distro resolver queries for itself), an empty string
    means the batch definitively found no owner and nothing further is
    queried.
    """
    if "distro" in config.resolvers:
        try:
            result = resolve_via_distro(header, header_path, backend=backend, owner=owner)
        except Exception:
            result = InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)
        if result.package is not None or _is_ambiguity(result.reason):
            return result
    if "man" in config.resolvers:
        try:
            return resolve_via_man(header, header_path)
        except Exception:
            return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)
    return InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)


def infer(headers: Sequence[str], config: InferenceConfig, *, memo: dict) -> list[InferenceResult]:
    """Run the resolver chain for each include spelling, input order preserved.

    One result per *distinct* header (later duplicates of a spelling are
    dropped). Never raises: every per-header step is wrapped, and a header
    that somehow raises still yields an unresolved result.

    Batching (plan §0: ONE distro-DB query per TU): a first pass runs the
    pre-filters, memo lookups and the pc-index stage for every distinct
    header; the headers still pending then get ONE :func:`owners_for_headers`
    call for the whole set -- skipped entirely when no distro backend exists
    -- and each pending header is finished through the distro -> man chain
    with its precomputed owner. The memo semantics are unchanged: the final
    chain verdict per header_path is memoised pre-demotion; pre-filter
    verdicts are never memoised.

    Covered contract: a header located under an explicit include dir is
    demoted before the resolver chain and reports ``REASON_COVERED`` with
    ``package=None``; a chain verdict demoted because the user already
    covers the package explicitly (``explicit_packages``, or the package's
    first ``-l`` stem in ``explicit_lib_stems``) keeps its
    ``package``/``resolver``/``candidates`` attribution and reports
    ``REASON_COVERED`` as the reason.
    """
    settled: dict[str, InferenceResult] = {}
    order: list[str] = []
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for header in headers:
        if header in seen:
            continue
        seen.add(header)
        order.append(header)
        try:
            result, header_path = _pre_chain(header, config, memo)
        except Exception:
            settled[header] = InferenceResult(header, None, None, None, (), REASON_NO_OWNER)
            continue
        if result is not None:
            settled[header] = result
        else:
            # _pre_chain returning (None, None) cannot happen (every no-distro
            # path returns a result); guard anyway, never raise.
            if header_path is None:  # pragma: no cover
                settled[header] = InferenceResult(header, None, None, None, (), REASON_NO_OWNER)
            else:
                pending.append((header, header_path))

    # ONE batched owner query for everything the chain left pending; no
    # distro backend -> no subprocess at all.
    owners: dict[str, str] = {}
    backend: str | None = None
    if pending:
        backend = detect_distro_backend()
        if backend is not None:
            owners = owners_for_headers([path for _header, path in pending], backend)

    for header, header_path in pending:
        chain_key = _chain_memo_key(header_path, config)
        # The batch answer is definitive: "" (no owner) must not trigger a
        # second, per-header owner query. With no backend at all there is
        # nothing precomputed -- resolve_via_distro re-detects (memoised) and
        # reports the missing database without starting a subprocess.
        precomputed = owners.get(header_path, "") if backend is not None else None
        try:
            verdict = _chain_tail(header, header_path, config, backend=backend, owner=precomputed)
            memo[chain_key] = (verdict.package, verdict.resolver, verdict.candidates, verdict.reason)
            settled[header] = _demote(verdict, config)
        except Exception:
            settled[header] = InferenceResult(header, header_path, None, None, (), REASON_NO_OWNER)

    return [settled[header] for header in order]


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


def clear_cache() -> None:
    """Clear this module's memos (pc include index, quiet-exists probe,
    distro backend detection, per-owner ``.pc`` stems).

    Does NOT clear caller-supplied :func:`infer` ``memo`` dicts -- those are
    owned by their callers. Fanned out from
    ``apptools_pkgconfig.clear_cache``.
    """
    _pc_include_index_memo.clear()
    _exists_memo.clear()
    _distro_backend_memo.clear()
    _pc_stems_memo.clear()
