"""pkg-config helpers (leaf module).

Extracted from :mod:`compiletools.apptools` as a behavior-preserving facade
split. This module is an import-time leaf: at module scope it imports only stdlib
plus :mod:`compiletools.wrappedos` and :mod:`compiletools.utils` (themselves
leaves). It MUST NOT import ``compiletools.apptools`` -- doing so would
reintroduce the very cycle this split removes.

:mod:`compiletools.git_utils` is imported *inside*
:func:`_setup_pkg_config_overrides_locked` (deferred), not at module scope,
because ``git_utils`` itself does a top-level ``import compiletools.apptools``
(used only lazily). A module-scope import here would form the cycle
apptools -> apptools_pkgconfig -> git_utils -> apptools and crash at
``apptools`` initialisation. The deferred import runs only after every module
is fully initialised.

It groups the functions that invoke ``pkg-config`` and that manage the
process-wide ``PKG_CONFIG_PATH`` override state:

* :func:`cached_pkg_config` -- ``@functools.cache``-memoised single-package
  ``pkg-config --cflags`` / ``--libs`` probe.
* :func:`tokenize_pkg_config_specs` -- normalize conf, CLI, and magic-marker
  values into individual package specs without splitting version constraints.
* :func:`filter_pkg_config_cflags` -- rewrite ``-I`` to ``-isystem`` and drop
  default system include paths.
* :func:`_batch_pkg_config` -- batched multi-package query with per-package
  fallback through :func:`cached_pkg_config`.
* :func:`_add_flags_from_pkg_config` -- fold pkg-config cflags/libs into
  ``args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}``.
* :func:`_setup_pkg_config_overrides` /
  :func:`_setup_pkg_config_overrides_locked` -- apply project + CLI
  ``PKG_CONFIG_PATH`` overrides under ``_PKG_CONFIG_OVERRIDE_LOCK``.
* :func:`_pkg_config_provenance_label` -- best-effort origin attribution for
  emitted ``Prepended/Appended pkg-config path: ...`` diagnostic lines.
* :func:`_audit_pkg_config_output` -- the two undefined-``${variable}``
  detectors (:func:`_undefined_pc_variables` over the ``Requires`` closure,
  :func:`_bare_detached_flags` over the query output), run on every
  successful flag query and routed through the same warn/error policy.

The ``args_parser`` provenance side-channel
(``_ComposingArgumentParser.get_conf_file_provenance()``) is reached purely
through the *parameter* passed in by the caller, never imported -- so this
module stays decoupled from the parser machinery still living in
``apptools.py``.

``apptools.py`` re-exports every name here by binding so its existing
``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
importers, and test/patch targets keep working with identical object
identity. ``apptools.clear_cache`` fans out to
:func:`clear_cache` here to clear both the result memo and the package-spec
existence/diagnostic memo.
"""

import functools
import os
import re
import shlex
import subprocess
import sys
import threading
import warnings
from typing import Literal

import compiletools.wrappedos
from compiletools.utils import split_command_cached

_PKG_CONFIG_COMPARISON_RE = re.compile(r"^(?:==|>=|<=|!=|=|<|>)(?P<operand>.*)$")
_PKG_CONFIG_TRAILING_COMPARISON_RE = re.compile(r"^.+?(?:==|>=|<=|!=|=|<|>)$")
_pkg_config_errors: Literal["warn", "error"] = "warn"


def tokenize_pkg_config_specs(values: list[str]) -> list[str]:
    """Split pkg-config values into package specs, preserving constraints.

    Configargparse's ``action='append'`` representation can contain a whole
    whitespace-separated conf value in one list element (``["a b c"]``),
    while repeated CLI options normally arrive as separate elements.  Magic
    ``//#PKG-CONFIG=`` values have the former shape as well. Normalize each
    element and comma-delimited fragment independently, then concatenate the
    specs: neither boundary may supply a missing version operand to its
    predecessor. Join a package name to an adjacent comparison and version
    within each fragment so the per-package fallback never mistakes ``>=`` or
    ``1.2`` for package names.

    Attached forms such as ``zlib>=1.2`` are already one token and remain so.
    Half-spaced forms remain one diagnostic unit but are rejected before a
    probe: pkgconf can accept ``zlib >=1.2`` after silently consuming the
    first version character into the operator, while ``zlib>= 1.2`` becomes
    two invented package names. A trailing comparison without a version also
    remains attached so it produces one malformed diagnostic.
    """
    specs: list[str] = []
    for value in values:
        for fragment in value.split(","):
            try:
                tokens = shlex.split(fragment)
            except ValueError:
                tokens = fragment.split()

            i = 0
            while i < len(tokens):
                package = tokens[i]
                spec_length = 1
                if _PKG_CONFIG_TRAILING_COMPARISON_RE.fullmatch(package) and i + 1 < len(tokens):
                    spec_length = 2
                elif i + 1 < len(tokens):
                    comparison = _PKG_CONFIG_COMPARISON_RE.fullmatch(tokens[i + 1])
                    if comparison is not None:
                        spec_length = 2
                        if not comparison.group("operand") and i + 2 < len(tokens):
                            spec_length = 3

                specs.append(" ".join(tokens[i : i + spec_length]))
                i += spec_length

    return specs


def clear_cache():
    """Clear the pkg-config cache moved out of :mod:`compiletools.apptools`.

    ``apptools.clear_cache`` fans out here so the result memo
    (``cached_pkg_config``), its package-spec existence memo, and the
    once-per-package undefined-variable report are all cleared. The
    ``pc_path`` memo is deliberately kept: it caches pkg-config's compiled-in
    default search list, which no build changes.

    Deliberately leaves the failure policy alone. ``--pkg-config-errors``
    is set once by ``parseargs`` and is an enforcement policy, not a cache;
    resetting it here disarmed strict mode for the rest of the process on
    any of the shipped cache-clearing fan-outs (``Hunter.clear_cache`` ->
    ``MagicFlagsBase.clear_cache`` -> ``apptools.clear_cache`` -> here).
    Callers wanting the policy back at its default say so explicitly.
    """
    cached_pkg_config.cache_clear()
    _cached_pkg_config_exists.cache_clear()
    _report_undefined_pc_variables.cache_clear()


def get_pkg_config_errors() -> Literal["warn", "error"]:
    """Return the process-local failure policy for pkg-config consumers."""
    return _pkg_config_errors


def set_pkg_config_errors(errors: Literal["warn", "error"]) -> None:
    """Set the process-local failure policy for pkg-config consumers."""
    if errors not in ("warn", "error"):
        raise ValueError(f"unsupported pkg-config error mode: {errors!r}")
    global _pkg_config_errors
    if errors != _pkg_config_errors:
        cached_pkg_config.cache_clear()
        _cached_pkg_config_exists.cache_clear()
        _report_undefined_pc_variables.cache_clear()
    _pkg_config_errors = errors


def _pkg_config_constraint_package(spec: str) -> tuple[str | None, bool]:
    """Return ``(bare_package, malformed)`` for one tokenized spec."""
    tokens = spec.split()
    if not tokens or tokens[0][0] in "<>=!":
        return None, True

    if _PKG_CONFIG_TRAILING_COMPARISON_RE.fullmatch(tokens[0]):
        return None, True

    if len(tokens) < 2:
        return None, False

    comparison = _PKG_CONFIG_COMPARISON_RE.fullmatch(tokens[1])
    if comparison is None:
        return None, False
    if comparison.group("operand") or len(tokens) < 3:
        return None, True
    return tokens[0], False


def _pkg_config_stderr(result: subprocess.CompletedProcess[str]) -> str:
    stderr = result.stderr
    if isinstance(stderr, bytes):
        return stderr.decode(errors="replace").strip()
    return stderr.strip() if isinstance(stderr, str) else ""


class PkgConfigError(RuntimeError):
    """A pkg-config failure promoted by ``--pkg-config-errors=error``."""


def render_pkg_config_error(error: PkgConfigError, extra: str = "") -> str:
    """Return the shared, traceback-free pkg-config strict-mode diagnostic."""
    lines = [str(error)]
    if extra:
        lines.append(extra)
    lines.append("Install the package, correct the package declaration, or use --pkg-config-errors=warn.")
    return "\n".join(lines)


def _warn_pkg_config(message: str, detail: str = "") -> None:
    if detail:
        message = f"{message}: {detail}"
    if _pkg_config_errors == "error":
        raise PkgConfigError(message)
    warnings.warn(message, UserWarning, stacklevel=4)


@functools.cache
def _cached_pkg_config_exists(package: str) -> bool:
    """Check one package spec once and emit a stable failure category."""
    bare_package, malformed = _pkg_config_constraint_package(package)
    if malformed:
        _warn_pkg_config(
            f"pkg-config malformed package specification {package!r}",
            "comparison operators require a package and version separated by spaces; "
            "otherwise pkg-config may invent package names or silently corrupt the version requirement",
        )
        return False

    exists_result = subprocess.run(
        ["pkg-config", "--print-errors", "--exists", package],
        capture_output=True,
        check=False,
        text=True,
    )
    if exists_result.returncode == 0:
        return True

    detail = _pkg_config_stderr(exists_result)
    if bare_package is None:
        _warn_pkg_config(f"pkg-config package {package!r} not found", detail)
        return False

    bare_result = subprocess.run(
        ["pkg-config", "--print-errors", "--exists", bare_package],
        capture_output=True,
        check=False,
        text=True,
    )
    if bare_result.returncode == 0:
        _warn_pkg_config(f"pkg-config version requirement {package!r} not satisfied", detail)
    else:
        _warn_pkg_config(
            f"pkg-config package {bare_package!r} not found while evaluating {package!r}",
            detail or _pkg_config_stderr(bare_result),
        )
    return False


# pkg-config supplies these itself, so a ``.pc`` that references one without
# assigning it is correct. Measured on pkgconf 1.4.2: pcfiledir,
# pc_sysrootdir and pc_top_builddir resolve to a value for a file defining
# none of them; the remaining three resolve to empty but are global
# properties of the installation, never the per-file typo this hunts.
_PC_BUILTIN_VARIABLES = frozenset(
    {
        "pcfiledir",
        "pc_sysrootdir",
        "pc_top_builddir",
        "pc_path",
        "pc_system_includedirs",
        "pc_system_libdirs",
    }
)

_PC_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.+-]*)\s*=")
_PC_REFERENCE_RE = re.compile(r"\$\{([^}]*)\}")
_PC_KEYWORD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*)\s*:\s*(.*)$")

# Flags whose argument may be detached. A bare one at the end of the output,
# or immediately before another flag, is what an ``-I${undefined}`` collapses
# to once the expansion eats the whole path.
_PC_ARGUMENT_TAKING_FLAGS = frozenset(
    {"-I", "-L", "-l", "-D", "-U", "-F", "-isystem", "-iquote", "-idirafter", "-include", "-framework"}
)


def _bare_package_name(spec: str) -> str:
    """Return the package name from a tokenized spec, dropping any version
    constraint in either the spaced (``zlib >= 1.2``) or attached
    (``zlib>=1.2``) form."""
    head = spec.split()[0] if spec.split() else spec
    for index, character in enumerate(head):
        if character in "<>=!":
            return head[:index]
    return head


@functools.cache
def _pkg_config_default_search_dirs() -> tuple[str, ...]:
    """Return pkg-config's own default ``.pc`` search list.

    One subprocess per process, and only when ``PKG_CONFIG_LIBDIR`` is unset
    (that variable replaces the default list outright).

    ANY failure degrades to an empty list rather than propagating. This is
    the only probe the undefined-variable scanner adds to the pkg-config
    call pattern, and it must not be able to change the outcome of the query
    it decorates -- a caller (or a test) that stubs ``subprocess.run`` to
    model only ``--exists`` and ``--cflags`` would otherwise see a build fail
    on a diagnostic. An empty list means the scanner locates no ``.pc`` file
    and stays silent, which is the correct degradation.
    """
    try:
        result = subprocess.run(
            ["pkg-config", "--variable", "pc_path", "pkg-config"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            return ()
        return tuple(d for d in result.stdout.strip().split(os.pathsep) if d)
    except Exception:
        return ()


def _pkg_config_search_dirs() -> tuple[str, ...]:
    """Return the live ``.pc`` search list, honouring PKG_CONFIG_PATH and
    PKG_CONFIG_LIBDIR. Read from the environment on every call because both
    change between builds (and between tests)."""
    dirs = [d for d in os.environ.get("PKG_CONFIG_PATH", "").split(os.pathsep) if d]
    libdir = os.environ.get("PKG_CONFIG_LIBDIR")
    if libdir is not None:
        dirs.extend(d for d in libdir.split(os.pathsep) if d)
    else:
        dirs.extend(_pkg_config_default_search_dirs())
    return tuple(dirs)


def _locate_pc_file(package: str) -> str | None:
    """Return the ``.pc`` file pkg-config would read for *package*, or None.

    A pure-python walk of the same search order rather than a
    ``--variable=pcfiledir`` subprocess per package: measured agreement with
    pkgconf's own answer on 20/20 sampled system packages, at ~4us against
    ~1.3ms. Returning None costs only the diagnostic.
    """
    for directory in _pkg_config_search_dirs():
        candidate = os.path.join(directory, f"{package}.pc")
        if os.path.isfile(candidate):
            return candidate
    return None


def _pc_required_packages(value: str) -> list[str]:
    """Return the bare package names in a ``Requires``/``Requires.private``
    value, dropping comparison operators and version operands."""
    packages = []
    tokens = value.replace(",", " ").split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token[0] in "<>=!":
            # A spaced comparison consumes its operator and its operand.
            index += 2 if _PKG_CONFIG_COMPARISON_RE.fullmatch(token) and not token.rstrip("<>=!") else 1
            continue
        name = _bare_package_name(token)
        if name:
            packages.append(name)
        # An attached constraint (``foo>=1.2``) carries its own operand;
        # a trailing bare operator takes the following token as the operand.
        if name != token:
            index += 1
            continue
        index += 1
        if index < len(tokens) and tokens[index][0] in "<>=!":
            operator = tokens[index]
            index += 2 if not operator.rstrip("<>=!") else 1
    return packages


def _read_pc_file(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def _undefined_pc_variables(path: str) -> list[str]:
    """Return the names of ``${var}`` references *path* never assigns.

    The scan is order-sensitive: only an assignment on a strictly earlier
    line satisfies a reference, and an assignment takes effect at the end of
    its own line so ``foo=${foo}/x`` is checked against the prior ``foo``.
    Both implementations were measured to agree that a definition placed
    below its use is undefined -- pkgconf expands ``-I${late}/b`` to
    ``-I/b`` at exit 0, freedesktop 0.29.2 reports ``Variable 'late' not
    defined`` and exits 1 -- so an order-independent scan would deliberately
    suppress a true positive.

    Commented-out lines are skipped: three system icu ``.pc`` files carry
    ``${pkgdatadir}`` behind a ``#``, and counting those put the measured
    false-positive rate at 3/236 instead of 0/236.

    Deliberately NOT treating ``PKG_CONFIG_<PKG>_<VAR>`` as a definition:
    measured on pkgconf 1.4.2, that override is ignored outright (it does not
    even override a variable the file *does* define), so honouring it here
    would suppress a true positive on the shipped implementation.
    """
    defined = set(_PC_BUILTIN_VARIABLES)
    undefined: list[str] = []
    for line in _read_pc_file(path):
        if line.lstrip().startswith("#"):
            continue
        for name in _PC_REFERENCE_RE.findall(line):
            if name and name not in defined and name not in undefined:
                undefined.append(name)
        assignment = _PC_ASSIGNMENT_RE.match(line)
        if assignment:
            defined.add(assignment.group(1))
    return undefined


def _pc_requires_closure(package: str) -> list[tuple[str, str]]:
    """Return ``(package, pc_path)`` for *package* and every package reachable
    through ``Requires`` / ``Requires.private``, in breadth-first order.

    The consumer's flags carry its dependencies' flags, so a dependency's
    typo truncates the consumer's compile line -- and the consumer is the only
    name the user typed. Cycles are terminated by the seen set; the shipped
    ``cycle-alpha``/``cycle-beta`` example pair is exactly that shape.
    """
    found: list[tuple[str, str]] = []
    seen = {package}
    queue = [package]
    while queue:
        current = queue.pop(0)
        path = _locate_pc_file(current)
        if path is None:
            continue
        found.append((current, path))
        for line in _read_pc_file(path):
            if line.lstrip().startswith("#"):
                continue
            keyword = _PC_KEYWORD_RE.match(line)
            if keyword is None or keyword.group(1) not in ("Requires", "Requires.private"):
                continue
            for dependency in _pc_required_packages(keyword.group(2)):
                if dependency not in seen:
                    seen.add(dependency)
                    queue.append(dependency)
    return found


@functools.cache
def _report_undefined_pc_variables(package: str) -> None:
    """Warn once per package about ``${var}`` references nothing defines.

    pkgconf expands an undefined variable to the empty string and exits 0 --
    measured identical on 1.4.2 and 2.3.0, under every switch that looks like
    it should report (``--validate``, ``--print-errors``,
    ``--errors-to-stdout``, ``--simulate``, ``--log-file``,
    ``PKG_CONFIG_DEBUG_SPEW``). freedesktop pkg-config 0.29.2 reports it and
    exits 1, so the build breaks loudly there and silently here. Scanning the
    ``.pc`` text is the only detection available on the shipped
    implementation.
    """
    for owner, path in _pc_requires_closure(_bare_package_name(package)):
        for variable in _undefined_pc_variables(path):
            via = "" if owner == _bare_package_name(package) else f" (required by {package!r})"
            _warn_pkg_config(
                f"pkg-config package {owner!r}{via} references undefined variable ${{{variable}}} in {path}",
                "pkgconf expands it to the empty string and still exits 0, so the flag it appears in "
                "reaches the compiler silently truncated",
            )


def _bare_detached_flags(output: str) -> list[str]:
    """Return argument-taking flags left bare in *output*.

    The visible symptom of the same defect at the far end: once
    ``-I${undefined}`` has eaten the whole path, the remaining ``-I`` eats the
    next argv token instead, and a bare ``-L`` before ``-lfoo`` makes the
    linker read the library name as a search *directory*.

    Only a flag that is last, or immediately followed by another flag, counts.
    A detached-but-satisfied pair is legitimate and common -- narrowing to
    this shape took the measured false-positive count on 236 system packages
    from one (``libbsd-overlay``'s ``-isystem /usr/include/bsd``) to zero.
    """
    try:
        tokens = shlex.split(output)
    except ValueError:
        tokens = output.split()
    bare = []
    for index, token in enumerate(tokens):
        if token not in _PC_ARGUMENT_TAKING_FLAGS:
            continue
        if index + 1 == len(tokens) or tokens[index + 1].startswith("-"):
            bare.append(token)
    return bare


def _audit_pkg_config_output(package: str, option: str, output: str) -> None:
    """Run both undefined-variable detectors over one successful query.

    Called from the single funnel every flag query passes through, so the
    batch fast path (which skips the per-package ``--exists``) is covered by
    the same code as the per-package fallback.
    """
    _report_undefined_pc_variables(package)
    for flag in _bare_detached_flags(output):
        _warn_pkg_config(
            f"pkg-config {option} {package!r} returned a detached {flag!r} with no argument",
            "an undefined ${variable} expands to the empty string, so the flag consumes the next "
            "token on the command line instead of its own path",
        )


def _run_pkg_config_query(package: str, option: str) -> str:
    """Run one flag query and route a nonzero returncode through the policy.

    A passing ``--exists`` does not promise the flag query passes: a ``.pc``
    with an unresolvable ``Requires.private``, or a broken variable
    expansion, satisfies the existence probe and fails ``--cflags``. Without
    this check the empty stdout is indistinguishable from a package that
    contributes no flags, so the build compiles or links without what it
    asked for, silently, even under ``--pkg-config-errors=error``.

    stderr is captured to build that diagnostic, so a query that exits 0
    while still writing to stderr has its output forwarded verbatim rather
    than swallowed. It is not routed through the failure policy: pkg-config
    exited 0 and returned usable flags, which is not a failure for
    ``--pkg-config-errors=error`` to promote.
    """
    result = subprocess.run(
        ["pkg-config", option, package],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        _warn_pkg_config(f"pkg-config {option} {package!r} failed", _pkg_config_stderr(result))
        return ""
    stderr = _pkg_config_stderr(result)
    if stderr:
        print(stderr, file=sys.stderr)
    output = result.stdout.rstrip()
    _audit_pkg_config_output(package, option, output)
    return output


@functools.cache
def cached_pkg_config(package, option):
    """Cache pkg-config results for one package spec and output option."""
    if not _cached_pkg_config_exists(package):
        return ""

    return _run_pkg_config_query(package, option)


def filter_pkg_config_cflags(cflags_str, verbose=0):
    """
    Process pkg-config cflags output.
    Converts -I to -isystem, except for default system include paths
    which are dropped to prevent include order issues (e.g. with libc++).
    Uses shlex for robust shell tokenization and quoting.
    """
    if not cflags_str:
        return ""

    # Standard system include paths
    system_include_paths = set(["/usr/include"])
    prefix = os.environ.get("PREFIX")
    if prefix:
        system_include_paths.add(compiletools.wrappedos.normpath(os.path.join(prefix, "include")))

    # Use shlex to correctly handle quoted paths in flags
    try:
        flags = split_command_cached(cflags_str)
    except ValueError:
        # Fallback for malformed strings
        flags = cflags_str.split()

    flag_iter = iter(flags)
    processed_flags = []

    for flag in flag_iter:
        if flag.startswith("-I"):
            path = None
            if flag == "-I":
                # Detached -I
                try:
                    path = next(flag_iter)
                except StopIteration:
                    # Trailing -I at end of string, preserve as-is
                    processed_flags.append(shlex.quote(flag))
                    break
            else:
                # Attached -Ipath
                path = flag[2:]

            # Normalize and check
            normalized_path = compiletools.wrappedos.normpath(path)
            is_system = normalized_path in system_include_paths

            if is_system:
                if verbose >= 6:
                    print(f"Dropping default system include path from pkg-config: {path}")
                continue

            # Reconstruct as -isystem, quoting path for shell safety
            processed_flags.append(f"-isystem {shlex.quote(path)}")
        else:
            # Re-quote other flags to preserve them correctly in the output string
            processed_flags.append(shlex.quote(flag))

    return " ".join(processed_flags)


_PkgConfigOrigin = Literal["prepend", "append", "candidate-cwd", "candidate-gitroot"]


def _pkg_config_provenance_label(
    path,
    origin: _PkgConfigOrigin,
    provenance,
):
    """Return a parenthetical origin label for a PKG_CONFIG_PATH entry, or
    empty string if no useful attribution is available.

    ``origin`` is one of ``'prepend'``, ``'append'``, ``'candidate-cwd'``,
    or ``'candidate-gitroot'``. The candidate-* origins go straight to the
    auto-discovered label without consulting ``provenance``. For
    prepend/append, the matching ``prepend-PKG-CONFIG-PATH`` /
    ``append-PKG-CONFIG-PATH`` provenance entries are searched for a
    realpath-equal value; first match wins. Falls back to ``(from CLI)``
    when no provenance entry matches.
    """
    if origin == "candidate-cwd":
        return "(auto-discovered: cwd)"
    if origin == "candidate-gitroot":
        return "(auto-discovered: gitroot)"
    key = "prepend-PKG-CONFIG-PATH" if origin == "prepend" else "append-PKG-CONFIG-PATH"
    try:
        target_real = compiletools.wrappedos.realpath(path)
    except (OSError, ValueError):
        target_real = path
    for entry in provenance.get(key, []):
        value, source_file, lineno = entry[0], entry[1], entry[2]
        literal = entry[3] if len(entry) >= 4 else value
        try:
            value_real = compiletools.wrappedos.realpath(value)
        except (OSError, ValueError):
            value_real = value
        if value_real == target_real:
            if literal != value:
                return f"(from {source_file}:{lineno}, literal: {literal})"
            return f"(from {source_file}:{lineno})"
    return "(from CLI)"


def _setup_pkg_config_overrides(context, verbose=0, prepend_paths=None, append_paths=None, args_parser=None):
    """Apply project-level and CLI-specified pkg-config path overrides to PKG_CONFIG_PATH.

    Priority order (highest first):

    1. ``prepend-PKG-CONFIG-PATH`` entries, with CLI winning over conf-file
       entries and — within the accumulated conf-file entries — the
       higher-priority axis conf (composed later in the variant) winning
       over the lower-priority one (e.g. project ``ct.conf``).
    2. ``<cwd>/ct.conf.d/pkgconfig/`` (project-local, auto-discovered)
    3. ``<gitroot>/ct.conf.d/pkgconfig/`` (repo-level, auto-discovered)
    4. Existing ``PKG_CONFIG_PATH`` entries
    5. ``append-PKG-CONFIG-PATH`` entries, symmetric to (1): CLI wins over
       conf-file entries, higher-priority axis wins within the conf-file
       group.

    Args:
        context: BuildContext instance tracking per-build state.
        verbose: verbosity level for diagnostic output.
        prepend_paths: directories to prepend (from ``--prepend-PKG-CONFIG-PATH``).
        append_paths: directories to append (from ``--append-PKG-CONFIG-PATH``).
        args_parser: optional ``_ComposingArgumentParser`` whose
            ``get_conf_file_provenance()`` is consulted at ``verbose >= 4``
            to attribute each emitted ``Prepended/Appended pkg-config
            path: ...`` line back to its origin (conf-file:line, CLI, or
            auto-discovered). Best-effort: if absent or empty the
            output degrades to bare paths (today's format).

    Must be called before any pkg-config subprocess invocation
    (i.e., before _add_flags_from_pkg_config and before magicflags
    processing).

    Concurrency contract
    --------------------
    This function mutates the **process-wide** ``os.environ['PKG_CONFIG_PATH']``,
    which is global state. Callers MUST observe the following:

    * Per-process serialization is enforced via a module-level
      ``threading.Lock`` (``_PKG_CONFIG_OVERRIDE_LOCK``). Two threads
      racing into this function will not interleave their reads/writes
      of ``PKG_CONFIG_PATH``.
    * The lock does NOT protect against other code paths in the process
      mutating ``os.environ['PKG_CONFIG_PATH']`` independently.
    * The lock does NOT serialize across processes. Multiple processes
      sharing a single ``BuildContext`` is unsupported.
    * The ``context.pkg_config_overrides_applied`` flag is checked and
      set within the lock to make the apply-once invariant safe under
      concurrent calls on the same context.
    * After mutation, ``context._original_pkg_config_path`` records the
      prior value so ``BuildContext.restore_pkg_config_path()`` can
      undo the mutation. Restore is also single-process / serial.
      Callers should prefer the
      ``BuildContext.pkg_config_path_restored()`` context manager
      (``cake.main`` holds it around the whole invocation) over pairing
      apply/restore calls by hand.
    """
    with _PKG_CONFIG_OVERRIDE_LOCK:
        _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths, args_parser)


# Process-local serialization for the env-mutation in _setup_pkg_config_overrides.
# See the docstring of that function for the full contract.
_PKG_CONFIG_OVERRIDE_LOCK = threading.Lock()


def _merged_pkg_config_path_entries(existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates):
    """Yield ``(dir, label, origin)`` for each entry of the final
    PKG_CONFIG_PATH, in emission order, deduplicated.

    Pure merge with explicit precedence:
    ``prepend_paths (highest) > candidates > middle (existing) > append_paths``.
    Each entry appears at most once. An entry already in PKG_CONFIG_PATH is
    *moved* to the requested position rather than being silently dropped —
    so ``--prepend-PKG-CONFIG-PATH=/X`` actually promotes /X to the front
    when /X was already present.

    ``prepend_paths`` / ``append_paths`` arrive ordered
    ``[low-priority conf, ..., high-priority conf, CLI in parse order]`` —
    the order ``_AccumulatingConfigFileParser`` and the
    ``_ComposingArgumentParser`` CLI re-append produce for every
    ``prepend-*`` / ``append-*`` key. Compiler-flag slots emit that list
    left-to-right and rely on the compiler's "last token wins" rule to
    honor CLI > high-conf > low-conf. ``PKG_CONFIG_PATH`` resolves
    leftmost-first, so both lists are *reversed* here so the same priority
    ordering survives the inversion of the wins rule. Symmetric for
    prepend and append: within each group, the highest-priority source
    ends up leftmost in PATH (winning), the lowest-priority source ends up
    rightmost in its group (only used as a fallback for packages no higher
    source defines).

    ``label``/``origin`` are the provenance-printing hints
    ``_setup_pkg_config_overrides_locked`` consumes at ``verbose >= 4``;
    both are None for middle (pre-existing) entries.
    """
    existing_dirs = [compiletools.wrappedos.normpath(d) for d in existing.split(os.pathsep)] if existing else []
    prepend_normd = [compiletools.wrappedos.normpath(d) for d in reversed(prepend_paths or [])]
    append_normd = [compiletools.wrappedos.normpath(d) for d in reversed(append_paths or [])]
    forced_at_end = set(append_normd)

    middle = [d for d in existing_dirs if d not in forced_at_end]

    seen: set[str] = set()
    emission_passes: list[tuple[list[str], str | None, _PkgConfigOrigin | None]] = [
        (prepend_normd, "Prepended", "prepend"),
        (list(cwd_candidates), "Prepended", "candidate-cwd"),
        (list(gitroot_candidates), "Prepended", "candidate-gitroot"),
        (middle, None, None),
        (append_normd, "Appended", "append"),
    ]
    for source, label, origin in emission_passes:
        for d in source:
            if not d or d in seen:
                continue
            seen.add(d)
            yield d, label, origin


def compute_pkg_config_path(existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates):
    """Pure merge producing the final PKG_CONFIG_PATH value, or None when
    the merge is empty.

    Extraction of the merge loop of ``_setup_pkg_config_overrides_locked``
    (which now calls this and keeps only the candidate discovery, env write
    and provenance printing). ``gather_inputs`` calls it too so the pure
    build-state core receives the value as data instead of reading the
    mutated environment.
    """
    final = [
        d
        for d, _label, _origin in _merged_pkg_config_path_entries(
            existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates
        )
    ]
    return os.pathsep.join(final) if final else None


def emit_pkg_config_path_provenance(
    existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates, verbose, args_parser=None
):
    """Print the ``Prepended/Appended pkg-config path: ...`` provenance
    lines at ``verbose >= 4``. Shared by the locked writer and the
    parseargs path (where gather computes the value and apply_effects
    performs the env write, so neither owns a natural print site)."""
    if verbose < 4:
        return
    provenance = {}
    if args_parser is not None:
        try:
            provenance = args_parser.get_conf_file_provenance()
        except Exception as exc:
            provenance = {}
            print(
                f"warning: pkg-config provenance lookup failed ({type(exc).__name__}: {exc}); "
                f"falling back to bare-path output",
                file=sys.stderr,
            )
    for d, label, origin in _merged_pkg_config_path_entries(
        existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates
    ):
        if label is None or origin is None:
            continue
        attribution = _pkg_config_provenance_label(d, origin, provenance)
        if attribution:
            print(f"{label} pkg-config path: {d} {attribution}")
        else:
            print(f"{label} pkg-config path: {d}")


def _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths, args_parser=None):
    """Body of _setup_pkg_config_overrides; assumes the module lock is held."""
    if context.pkg_config_overrides_applied:
        return

    # Deferred import: ``compiletools.git_utils`` is NOT a leaf -- it does a
    # top-level ``import compiletools.apptools`` (used only lazily inside its
    # own functions). Importing it at module scope here would create the cycle
    # apptools -> apptools_pkgconfig -> git_utils -> apptools and fail at
    # ``apptools`` init time (partially-initialised module). Importing inside
    # the function keeps apptools_pkgconfig a true import-time leaf; by the
    # time this runs every module is fully initialised. Importing the symbol
    # (``from ... import find_git_root``) rather than the submodule avoids
    # rebinding the local ``compiletools`` name, keeping the
    # ``compiletools.wrappedos.*`` references below resolvable by the type
    # checker.
    from compiletools.git_utils import find_git_root

    gitroot = find_git_root()

    cwd_candidates = []
    cwd_pkgconfig = os.path.join(os.getcwd(), "ct.conf.d", "pkgconfig")
    if compiletools.wrappedos.isdir(cwd_pkgconfig):
        cwd_candidates.append(compiletools.wrappedos.normpath(cwd_pkgconfig))

    gitroot_candidates = []
    if gitroot:
        repo_pkgconfig = os.path.join(gitroot, "ct.conf.d", "pkgconfig")
        if compiletools.wrappedos.isdir(repo_pkgconfig):
            repo_pkgconfig = compiletools.wrappedos.normpath(repo_pkgconfig)
            if repo_pkgconfig not in cwd_candidates:
                gitroot_candidates.append(repo_pkgconfig)

    existing = os.environ.get("PKG_CONFIG_PATH", "")

    emit_pkg_config_path_provenance(
        existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates, verbose, args_parser
    )

    new_value = compute_pkg_config_path(existing, prepend_paths, append_paths, cwd_candidates, gitroot_candidates)

    # Save original ONLY if we are about to mutate, so restore_pkg_config_path
    # can faithfully undo. Set the flag AFTER the mutation succeeds so a
    # caller hitting an exception above can retry.
    if new_value is not None and new_value != existing:
        context._original_pkg_config_path = existing if "PKG_CONFIG_PATH" in os.environ else True
        os.environ["PKG_CONFIG_PATH"] = new_value

    context.pkg_config_overrides_applied = True


def _add_flags_from_pkg_config(args):
    """Add flags for the package specs already canonicalized on ``args``.

    Legacy mutate-in-place writer with no production caller: the build
    pipeline queries pkg-config in ``gather_inputs`` and folds the results
    in ``build_state.stage_pkg_config_flags``. Kept (and re-exported from
    ``apptools``) for tests and library embedders that drive it directly;
    expects ``args.pkg_config`` to already be a tokenized list.
    """
    packages = list(args.pkg_config)
    if not packages:
        return

    # Batch pkg-config calls: query all packages at once instead of one subprocess
    # per package.  Falls back to per-package calls if the batch fails (e.g. a
    # package is missing and we need to identify which one).
    # hasattr IS the CAP registration: populate_args never materializes
    # slot attrs, so an unregistered LDFLAGS stays absent and hasattr
    # answers "does this tool link?" on any namespace shape.
    want_libs = hasattr(args, "LDFLAGS")

    batch_cflags = _batch_pkg_config(packages, "--cflags")
    batch_libs = _batch_pkg_config(packages, "--libs") if want_libs else {}

    for pkg in packages:
        raw_cflags = batch_cflags.get(pkg, "")
        cflags = filter_pkg_config_cflags(raw_cflags, args.verbose)

        if cflags:
            args.CPPFLAGS += f" {cflags}"
            args.CFLAGS += f" {cflags}"
            args.CXXFLAGS += f" {cflags}"
            if args.verbose >= 6:
                print(f"pkg-config --cflags {pkg} added FLAGS={cflags}")

        if want_libs:
            libs = batch_libs.get(pkg, "")
            if libs:
                args.LDFLAGS += f" {libs}"
                if args.verbose >= 6:
                    print(f"pkg-config --libs {pkg} added LDFLAGS={libs}")


def _batch_pkg_config(packages: list[str], option: str) -> dict[str, str]:
    """Query pkg-config for all package specs, returning ``{spec: output}``.

    Fast path: validate all packages with a single ``--exists`` call, then
    query each with *option* (skipping the per-package ``--exists``).
    If the batch ``--exists`` fails, fall back to per-package cached calls
    which handle missing packages individually.
    """
    # Malformed specs are diagnosed without invoking pkg-config. Keep them out
    # of the batch so valid co-listed packages can still use the fast path.
    malformed = [pkg for pkg in packages if _pkg_config_constraint_package(pkg)[1]]
    out = {pkg: cached_pkg_config(pkg, option) for pkg in malformed}
    query_packages = [pkg for pkg in packages if pkg not in out]
    if not query_packages:
        return out

    # Single --exists check for all valid package specs at once
    exists = subprocess.run(
        ["pkg-config", "--exists"] + query_packages,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        # At least one package is missing — fall back to per-package
        out.update({pkg: cached_pkg_config(pkg, option) for pkg in query_packages})
        return out

    # All packages exist — query each without the redundant --exists check.
    for pkg in query_packages:
        out[pkg] = _run_pkg_config_query(pkg, option)
    return out
