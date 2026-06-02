"""Startup-validation + legacy-config-key checks (leaf-ish module).

Extracted from :mod:`compiletools.apptools` as a behavior-preserving facade
split. It groups the "fail loud, fail early" guards that run after config
substitution and just before/within :func:`apptools.parseargs`:

* :func:`_check_resolved_compiler_available` -- the resolved CC/CXX/LD binary
  must be on PATH.
* :func:`_check_wild_linker_usable` -- the ``wild`` / ``wild-B`` linker axis
  must be installed and (for ``-fuse-ld=wild`` on gcc) new enough.
* :func:`_check_compiler_supports_requested_standard` -- the detected compiler
  major version must clear the ``-std=`` requested by the variant.
* :func:`_check_legacy_variant_config_keys` -- the obsolete ``variantaliases =``
  key is a hard error.
* :func:`_check_legacy_cas_config_keys` -- the renamed ``objdir`` / ``pchdir``
  keys are a hard error.

Plus the constants/regexes consumed only by these checks:
``_STD_MIN_COMPILER_VERSION``, ``_LEGACY_CAS_KEY_RE``, ``_LEGACY_VARIANT_KEY_RE``.

Import-time leaf discipline: at module scope this imports only stdlib,
the leaf flag helper :func:`compiletools.utils.split_command_cached`, and the
compiler-probe leaf
:func:`compiletools.apptools_compiler._compiler_major_version`. It MUST NOT
import :mod:`compiletools.apptools` at module scope -- ``apptools`` imports
*this* module for re-export, so a module-scope back-import would form a cycle
and crash at ``apptools`` initialisation.

Two symbols these checks need still live in ``apptools.py`` because they have
live internal callers there (``_normalize_wild_linker``) and belong with the
argparse layer slated for a later split:

* the sentinels ``_UNSUPPLIED_USE_CXX`` / ``_UNSUPPLIED_USE_CXXFLAGS``
  (read by :func:`_check_resolved_compiler_available`), and
* the helpers ``_variant_has_axis`` / ``_effective_link_driver``
  (read by :func:`_check_wild_linker_usable`).

They are reached through a *deferred* ``import compiletools.apptools`` inside
the two functions that need them (never at module scope). By the time those
functions run every module is fully initialised, so the deferred import is
free of cycle hazard. Referencing them as ``compiletools.apptools.<name>``
also means a test that monkeypatches the facade attribute is honoured.

``apptools.py`` re-exports every public-to-it name here by binding so its
existing ``apptools.<name>`` call sites, ``from compiletools.apptools import
...`` importers, and test targets keep working with identical object identity.
"""

import re

from compiletools.apptools_compiler import _compiler_major_version
from compiletools.utils import split_command_cached

_LEGACY_CAS_KEY_RE = re.compile(r"^\s*(objdir|pchdir)\s*=", re.MULTILINE)
_LEGACY_VARIANT_KEY_RE = re.compile(r"^\s*variantaliases\s*=", re.MULTILINE)


def _check_legacy_variant_config_keys(config_files) -> None:
    """Fail loud on the obsolete ``variantaliases =`` key.

    The alias mechanism was replaced by config-file inheritance + axis
    composition. configargparse silently ignores unknown keys, so an old
    ``ct.conf`` with ``variantaliases = {'debug':'gcc.debug'}`` would
    quietly stop working and the user would build the wrong variant. Raise
    a pointer at the upgrade guide instead.
    """
    offenders = []
    for path in config_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for match in _LEGACY_VARIANT_KEY_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append((path, line_no))
    if offenders:
        details = "\n".join(f"  {p}:{n}: variantaliases" for p, n in offenders)
        raise RuntimeError(
            "Legacy 'variantaliases =' config key detected. The variant alias "
            "mechanism has been replaced by config inheritance + axis composition:\n"
            f"{details}\n"
            "Replace the alias dict with either (a) an `extends = ...` directive in "
            "the named conf file, or (b) the default variant set to the composed name "
            "(e.g. `variant = gcc.debug` instead of "
            "`variantaliases = {'debug':'gcc.debug'}`). See "
            "README.ct-config.rst section 'Upgrading from variantaliases' for the "
            "migration recipe."
        )


# Static (compiler, min-version) table for language-standard support.
# Source: https://gcc.gnu.org/projects/cxx-status.html
#         https://clang.llvm.org/cxx_status.html
# Values are the major version of the compiler that first implemented
# (substantially complete) support for each standard.  When the user
# requests `-std=c++NN` via an axis or CLI flag we compare the detected
# compiler version against this table; an undershoot is a hard error
# (otherwise the compile fails later with an opaque "unrecognized command
# line option" diagnostic and no pointer at the variant chain).
_STD_MIN_COMPILER_VERSION = {
    # C++ standards
    "c++11": {"gcc": 4, "clang": 3},
    "c++14": {"gcc": 6, "clang": 3},
    "c++17": {"gcc": 7, "clang": 5},
    "c++20": {"gcc": 10, "clang": 10},
    "c++23": {"gcc": 13, "clang": 17},
    "c++26": {"gcc": 14, "clang": 18},
    # C standards (informational)
    "c99": {"gcc": 4, "clang": 3},
    "c11": {"gcc": 4, "clang": 3},
    "c17": {"gcc": 7, "clang": 7},
    "c23": {"gcc": 14, "clang": 18},
}


def _check_resolved_compiler_available(args) -> None:
    """Fail loud when the resolved compiler binary isn't on PATH.

    The functional-compiler auto-detect (parseargs:~2454) only fires when
    ``args.CXX`` is None. A toolchain axis like ``gcc.conf`` sets
    ``CXX=g++`` explicitly, so an explicit ``--variant=gcc.*`` request on
    a system without gcc bypasses the auto-detect AND fails at the first
    compile invocation with a generic "g++: command not found" — no
    pointer at *which* variant requested g++.

    This check runs after substitutions and emits a clear error naming
    both the missing binary and the resolved variant so the user knows
    whether to switch variants or install the toolchain.
    """
    import shutil

    # Deferred import: the ``_UNSUPPLIED_USE_*`` sentinels still live in
    # ``apptools.py`` (bound up with the argparse defaults slated for a later
    # split). Importing ``compiletools.apptools`` at module scope here would
    # form the cycle apptools -> apptools_validate -> apptools and crash at
    # ``apptools`` init. By call time every module is fully initialised.
    import compiletools.apptools as _apptools

    variant = getattr(args, "variant", "<unknown>")
    for slot in ("CC", "CXX", "LD"):
        value = getattr(args, slot, None)
        if not value or value in (_apptools._UNSUPPLIED_USE_CXX, _apptools._UNSUPPLIED_USE_CXXFLAGS):
            # The "unsupplied" sentinel means a later step substitutes a
            # real value (typically CXX itself); skip these.
            continue
        # Tokenize so wrapper invocations like "ccache g++" resolve their
        # first token (the actual executable to invoke). Feeding the full
        # compound string to shutil.which would always return None.
        tokens = split_command_cached(value) if " " in value else (value,)
        exe = tokens[0] if tokens else value
        # shutil.which handles both bare names (PATH lookup) and absolute
        # / workspace-relative paths (existence + executability check).
        if shutil.which(exe) is None:
            raise RuntimeError(
                f"Resolved {slot}={value!r} is not on PATH and is not an executable file.\n"
                f"  variant: {variant}\n"
                f"  This usually means the toolchain axis pinned by your --variant "
                f"isn't installed. Install it, or switch to a different toolchain "
                f"axis (e.g. --variant=clang,...) that resolves to a binary you have.\n"
                f"  Run `ct-config --variant={variant} -vv` to see which conf file "
                f"set {slot}."
            )


def _check_wild_linker_usable(args) -> None:
    """Fail loud when the wild linker is selected but unusable.

    Fires only when wild is the selected linker — either the LD tokens carry
    ``-fuse-ld=wild`` / ``--ld-path=wild`` (the ``wild`` axis; the clang
    rewrite in ``_normalize_wild_linker`` runs before this), or the
    ``wild-B`` axis is selected.

    Two failure modes:
      1. ``wild`` not on PATH -> raise with the install instruction.
      2. ``wild`` axis on gcc < 16 -> raise (gcc that old can't drive
         ``-fuse-ld=wild``; use clang, upgrade gcc, or the ``wild-B`` axis).
         ``wild-B`` has no version gate (working on old gcc is its purpose).
    """
    import shutil

    # Deferred import: ``_variant_has_axis`` / ``_effective_link_driver`` still
    # live in ``apptools.py`` (they have live internal callers there in
    # ``_normalize_wild_linker``). A module-scope ``import compiletools.apptools``
    # would form the cycle apptools -> apptools_validate -> apptools. By call
    # time every module is initialised; referencing them through the facade also
    # honours any test that monkeypatches the ``compiletools.apptools`` attr.
    import compiletools.apptools as _apptools

    ldflags = getattr(args, "LDFLAGS", "") or ""
    ld_tokens = split_command_cached(ldflags)
    wild_axis = "-fuse-ld=wild" in ld_tokens or "--ld-path=wild" in ld_tokens
    wild_b_axis = _apptools._variant_has_axis(args, "wild-B")
    if not (wild_axis or wild_b_axis):
        return

    variant = getattr(args, "variant", "<unknown>")
    if shutil.which("wild") is None:
        raise RuntimeError(
            "Wild linker selected but the 'wild' binary is not on PATH.\n"
            f"  variant: {variant}\n"
            "  Install it with: cargo install --locked wild-linker\n"
            "  (the binary lands in ~/.cargo/bin; ensure that is on PATH),\n"
            "  or switch to a different linker axis (e.g. --variant=...,mold)."
        )

    # bazel's link rule recognises -fuse-ld= / --ld-path= but NOT -B as a
    # linker selector (_token_picks_linker in bazel_backend.py). With
    # wild-B and no recognised selector, bazel adds its default
    # --linkopt=-fuse-ld=gold and silently links with gold while the
    # variant claims wild-B. Fail loud instead.
    if wild_b_axis and getattr(args, "backend", None) == "bazel":
        raise RuntimeError(
            "The wild-B axis is unsupported with --backend=bazel.\n"
            f"  variant: {variant}\n"
            "  bazel's link rule does not treat -B<dir> as a linker selector,\n"
            "  so it would silently fall through to its default linker.\n"
            "  Use --variant=...,wild instead (requires clang or gcc >= 16.1),\n"
            "  or pick a different backend."
        )

    if wild_axis and not wild_b_axis:
        cxx = _apptools._effective_link_driver(args)
        # None when no link driver resolved (no LD and no CXX) or the driver
        # isn't a recognised gcc/clang — the version gate is skipped in that case.
        identity = _compiler_major_version(cxx) if cxx else None
        if identity is not None:
            family, major = identity
            if family == "gcc" and major < 16:
                raise RuntimeError(
                    f"Wild linker (-fuse-ld=wild) requires gcc >= 16.1, but "
                    f"resolved link driver {cxx!r} is gcc {major}.\n"
                    f"  variant: {variant}\n"
                    "  Use clang (--variant=clang,wild), upgrade gcc to >= 16.1, "
                    "or use the -B fallback axis (--variant=...,wild-B), which "
                    "works on any gcc."
                )


def _check_compiler_supports_requested_standard(args) -> None:
    """Fail loud when the resolved compiler's major version is too old for
    the language standard requested by the variant.

    Probes ``<args.CXX> --version`` (and ``args.CC --version`` for C
    code) and compares against _STD_MIN_COMPILER_VERSION. Skips silently
    when the compiler driver isn't a recognised gcc/clang (so msvc /
    cross-toolchains / unrecognised wrappers don't trigger spurious
    failures).
    """
    flags_to_check: list[tuple[str, str]] = []  # [(flag-slot-name, attr)]
    if getattr(args, "CXX", None):
        flags_to_check.append(("CXX", "CXXFLAGS"))
    if getattr(args, "CC", None):
        flags_to_check.append(("CC", "CFLAGS"))

    variant = getattr(args, "variant", "<unknown>")
    import re as _re

    for compiler_slot, flags_slot in flags_to_check:
        compiler = getattr(args, compiler_slot, None)
        flags_str = getattr(args, flags_slot, "")
        if not compiler or not flags_str:
            continue
        m = _re.search(r"-std=(c(?:\+\+)?\w+)", flags_str)
        if not m:
            continue
        std = m.group(1)
        # Normalise rare alt-spellings to the table keys.
        std_norm = {"c++2c": "c++26", "c++2b": "c++23", "c++2a": "c++20", "c++1z": "c++17"}.get(std, std)
        if std_norm not in _STD_MIN_COMPILER_VERSION:
            continue
        identity = _compiler_major_version(compiler)
        if identity is None:
            continue  # unknown driver; skip silently
        family, major = identity
        min_required = _STD_MIN_COMPILER_VERSION[std_norm].get(family)
        if min_required is None or major >= min_required:
            continue
        raise RuntimeError(
            f"Resolved {compiler_slot}={compiler!r} is {family} {major}, "
            f"which does not support -std={std} (requires {family} >= {min_required}).\n"
            f"  variant: {variant}\n"
            f"  Either upgrade your {family} toolchain, or compose a lower "
            f"standard axis (e.g. --variant=..,cxx20 in place of ..,{std_norm.replace('c++', 'cxx')}).\n"
            f"  Run `ct-config --variant={variant} -vv` to see which conf file "
            f"requested -std={std}."
        )


def _check_legacy_cas_config_keys(config_files) -> None:
    """Fail loud on legacy ``objdir``/``pchdir`` keys in resolved config files.

    The CAS rename (shared-objdir → cas-objdir, shared-pchdir → cas-pchdir)
    has no backward-compat alias. configargparse silently ignores unknown
    keys, so an upgrader's existing ``ct.conf`` with ``objdir = /shared/path``
    would otherwise fall back to the per-build default and quietly defeat
    shared-cache deployments. Detect and raise instead.
    """
    offenders = []
    for path in config_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for match in _LEGACY_CAS_KEY_RE.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append((path, line_no, match.group(1)))
    if offenders:
        details = "\n".join(f"  {p}:{n}: {k}" for p, n, k in offenders)
        raise RuntimeError(
            "Legacy CAS config keys detected (renamed to cas-objdir / cas-pchdir):\n"
            f"{details}\n"
            "Edit the offending config file(s) to use 'cas-objdir' and 'cas-pchdir'. "
            "There is no backward-compat alias; leaving the old keys in place would "
            "silently fall back to the per-build default and defeat shared-cache deployments."
        )
