import contextlib
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Generator

import stringzilla as sz

import compiletools.apptools_argparse
import compiletools.apptools_compiler

# Re-exported from the leaf apptools_pkgconfig module so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and test/patch targets keep working with identical object
# identity. ``_setup_pkg_config_overrides`` and ``_add_flags_from_pkg_config``
# both have live internal callers that stay in apptools
# (``_commonsubstitutions``), so they are plain imports. The rest are pure
# re-exports consumed only by external modules / tests, so they carry the
# redundant ``name as name`` alias to mark them as intentional re-exports for
# the F401 linter. ``_PKG_CONFIG_OVERRIDE_LOCK`` is the single
# ``threading.Lock`` instance defined in the leaf module; re-exporting it by
# binding keeps ``apptools._PKG_CONFIG_OVERRIDE_LOCK`` and
# ``apptools_pkgconfig._PKG_CONFIG_OVERRIDE_LOCK`` the SAME object (a copy
# would break mutual exclusion). ``apptools.clear_cache`` fans out to
# ``compiletools.apptools_pkgconfig.clear_cache`` (the module import just
# below) to clear the moved ``cached_pkg_config`` memo.
import compiletools.apptools_pkgconfig
import compiletools.configutils
import compiletools.git_utils
import compiletools.utils
import compiletools.wrappedos

# Re-exported from the apptools_argparse module (the CLI argument-registration
# + configargparse layer) so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and the many ``unittest.mock.patch("compiletools.apptools.<name>")``
# targets keep working with identical object identity.
#
# ``resolve_cas_directory_arguments`` (called by ``_commonsubstitutions``) and
# ``_fix_variable_handling_method`` (called by ``parseargs``) both have live
# internal callers that stay in apptools, so they are plain imports referenced
# by bare name. Every other name is a pure re-export consumed only by entry
# points / other modules / tests, so it carries the redundant ``name as name``
# alias to mark it as an intentional re-export for the F401 linter. (ruff's
# import sorter interleaves these into several ``from`` groups; they are all
# the same logical re-export block.)
#
# apptools_argparse reaches BACK into this module for the ``_UNSUPPLIED_*``
# sentinels and ``unsupplied_replacement`` / ``_ensure_variant_suffix`` via a
# deferred ``import compiletools.apptools`` inside the four functions that need
# them (those symbols stay in the substitution core). That deferred import is
# the accepted cycle-break: this top-level import of apptools_argparse pulls in
# git_utils, which imports the apptools facade — but only uses it at call time,
# so the partially-initialised module is fine (the pre-existing cycle pattern).
from compiletools.apptools_argparse import (
    _CONF_DIR_PLACEHOLDER as _CONF_DIR_PLACEHOLDER,
)
from compiletools.apptools_argparse import (
    _CONF_DIR_SEGMENT_HEADER_PREFIX as _CONF_DIR_SEGMENT_HEADER_PREFIX,
)
from compiletools.apptools_argparse import (
    _CONF_DIR_SEGMENT_HEADER_SUFFIX as _CONF_DIR_SEGMENT_HEADER_SUFFIX,
)
from compiletools.apptools_argparse import (
    _DOLLAR_SENTINEL as _DOLLAR_SENTINEL,
)
from compiletools.apptools_argparse import (
    _AccumulatingConfigFileParser as _AccumulatingConfigFileParser,
)
from compiletools.apptools_argparse import (
    _add_xxpend_argument as _add_xxpend_argument,
)
from compiletools.apptools_argparse import (
    _add_xxpend_arguments as _add_xxpend_arguments,
)
from compiletools.apptools_argparse import (
    _ComposingArgumentParser as _ComposingArgumentParser,
)
from compiletools.apptools_argparse import (
    _expand_conf_dir as _expand_conf_dir,
)
from compiletools.apptools_argparse import (
    _expand_env_and_user as _expand_env_and_user,
)

# The two names below are referenced by *bare name* from code that stays in
# apptools (``resolve_cas_directory_arguments`` from ``_commonsubstitutions``,
# ``_fix_variable_handling_method`` from ``parseargs``), hence no ``as`` alias.
# See the re-export rationale comment above the first apptools_argparse import.
from compiletools.apptools_argparse import (
    _fix_variable_handling_method,
    resolve_cas_directory_arguments,
)
from compiletools.apptools_argparse import (
    _open_conf_file_utf8 as _open_conf_file_utf8,
)
from compiletools.apptools_argparse import (
    _parser_has_option as _parser_has_option,
)
from compiletools.apptools_argparse import (
    _rich_rst_available as _rich_rst_available,
)
from compiletools.apptools_argparse import (
    _user_passed_no_timing as _user_passed_no_timing,
)
from compiletools.apptools_argparse import (
    add_base_arguments as add_base_arguments,
)
from compiletools.apptools_argparse import (
    add_cas_arguments as add_cas_arguments,
)
from compiletools.apptools_argparse import (
    add_cas_directory_arguments as add_cas_directory_arguments,
)
from compiletools.apptools_argparse import (
    add_common_arguments as add_common_arguments,
)
from compiletools.apptools_argparse import (
    add_fetch_arguments as add_fetch_arguments,
)
from compiletools.apptools_argparse import (
    add_link_arguments as add_link_arguments,
)
from compiletools.apptools_argparse import (
    add_locking_arguments as add_locking_arguments,
)
from compiletools.apptools_argparse import (
    add_otel_export_arguments as add_otel_export_arguments,
)
from compiletools.apptools_argparse import (
    add_output_directory_arguments as add_output_directory_arguments,
)
from compiletools.apptools_argparse import (
    add_target_arguments as add_target_arguments,
)
from compiletools.apptools_argparse import (
    add_target_arguments_ex as add_target_arguments_ex,
)
from compiletools.apptools_argparse import (
    create_parser as create_parser,
)
from compiletools.apptools_argparse import (
    parser_has_option as parser_has_option,
)
from compiletools.apptools_argparse import (
    validate_otel_timing_pair as validate_otel_timing_pair,
)
from compiletools.apptools_canonicalize import (
    _GITROOT_SENTINEL as _GITROOT_SENTINEL,
)
from compiletools.apptools_canonicalize import (
    _PATH_BEARING_FLAGS as _PATH_BEARING_FLAGS,
)

# Re-exported from the leaf apptools_canonicalize module so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and test/patch targets keep working with identical object
# identity. ``_PREFIX_MAP_FLAG_PREFIXES`` still has a live internal caller
# that stays in apptools (``_has_prefix_map_flag`` reads it) — plain import.
# ``canonicalize_path_for_cache_key``'s former internal caller
# (``compiler_identity``) moved to ``apptools_compiler``, so it is now a pure
# re-export here. The rest are pure re-exports (only consumed by external
# modules / docstrings), so they carry the redundant ``name as name`` alias
# to mark them as intentional re-exports for the F401 linter.
from compiletools.apptools_canonicalize import (
    _PREFIX_MAP_FLAG_PREFIXES,
)
from compiletools.apptools_canonicalize import (
    _canonicalize_one_path as _canonicalize_one_path,
)
from compiletools.apptools_canonicalize import (
    _canonicalize_one_path_to_target as _canonicalize_one_path_to_target,
)
from compiletools.apptools_canonicalize import (
    _canonicalize_tokens_to_target as _canonicalize_tokens_to_target,
)
from compiletools.apptools_canonicalize import (
    canonicalize_for_cache_key as canonicalize_for_cache_key,
)
from compiletools.apptools_canonicalize import (
    canonicalize_for_command as canonicalize_for_command,
)
from compiletools.apptools_canonicalize import (
    canonicalize_path_for_cache_key as canonicalize_path_for_cache_key,
)
from compiletools.apptools_canonicalize import (
    canonicalize_path_for_command as canonicalize_path_for_command,
)
from compiletools.apptools_canonicalize import (
    canonicalize_paths_for_cache_key as canonicalize_paths_for_cache_key,
)

# Re-exported from the leaf apptools_compiler module so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and test/patch targets keep working with identical object
# identity. ``compiler_kind`` has a live internal caller that stays in
# apptools (``_normalize_wild_linker``); the rest are
# pure re-exports consumed only by external modules / docstrings, so they
# carry the redundant ``name as name`` alias to mark them as intentional
# re-exports for the F401 linter. ``apptools.clear_cache`` fans out to
# ``compiletools.apptools_compiler.clear_cache`` (imported above as a
# module) to clear the moved caches.
from compiletools.apptools_compiler import (
    _compiler_major_version as _compiler_major_version,
)
from compiletools.apptools_compiler import (
    _get_functional_cxx_compiler_cached as _get_functional_cxx_compiler_cached,
)
from compiletools.apptools_compiler import (
    _test_compiler_functionality as _test_compiler_functionality,
)
from compiletools.apptools_compiler import (
    compiler_default_cxx_std as compiler_default_cxx_std,
)
from compiletools.apptools_compiler import (
    compiler_identity as compiler_identity,
)
from compiletools.apptools_compiler import (
    compiler_kind,
)
from compiletools.apptools_compiler import (
    derive_c_compiler_from_cxx as derive_c_compiler_from_cxx,
)
from compiletools.apptools_compiler import (
    find_system_std_module_source as find_system_std_module_source,
)
from compiletools.apptools_compiler import (
    get_functional_cxx_compiler as get_functional_cxx_compiler,
)
from compiletools.apptools_compiler import (
    tool_version as tool_version,
)
from compiletools.apptools_pkgconfig import (
    _PKG_CONFIG_OVERRIDE_LOCK as _PKG_CONFIG_OVERRIDE_LOCK,
)
from compiletools.apptools_pkgconfig import (
    _add_flags_from_pkg_config,
    _setup_pkg_config_overrides,
)
from compiletools.apptools_pkgconfig import (
    _batch_pkg_config as _batch_pkg_config,
)
from compiletools.apptools_pkgconfig import (
    _pkg_config_provenance_label as _pkg_config_provenance_label,
)
from compiletools.apptools_pkgconfig import (
    _PkgConfigOrigin as _PkgConfigOrigin,
)
from compiletools.apptools_pkgconfig import (
    _setup_pkg_config_overrides_locked as _setup_pkg_config_overrides_locked,
)
from compiletools.apptools_pkgconfig import (
    cached_pkg_config as cached_pkg_config,
)
from compiletools.apptools_pkgconfig import (
    filter_pkg_config_cflags as filter_pkg_config_cflags,
)

# Re-exported from the leaf apptools_validate module so existing
# ``apptools.<name>`` call sites (``parseargs`` / ``_commonsubstitutions``
# read these as module globals), ``from compiletools.apptools import ...``
# importers, and test targets keep working with identical object identity.
# All five checks are invoked by bare name from code that stays in apptools,
# so the redundant ``name as name`` alias marks them as intentional re-exports
# for the F401 linter; the three constants/regexes are pure re-exports consumed
# only by tests / docstrings. ``apptools_validate`` reaches the apptools-resident
# sentinels (``_UNSUPPLIED_USE_*``) and helpers (``_variant_has_axis`` /
# ``_effective_link_driver``) via a deferred ``import compiletools.apptools``
# inside the two functions that need them, so no cycle forms here.
from compiletools.apptools_validate import (
    _LEGACY_CAS_KEY_RE as _LEGACY_CAS_KEY_RE,
)
from compiletools.apptools_validate import (
    _LEGACY_VARIANT_KEY_RE as _LEGACY_VARIANT_KEY_RE,
)
from compiletools.apptools_validate import (
    _STD_MIN_COMPILER_VERSION as _STD_MIN_COMPILER_VERSION,
)
from compiletools.apptools_validate import (
    _check_compiler_supports_requested_standard as _check_compiler_supports_requested_standard,
)
from compiletools.apptools_validate import (
    _check_legacy_cas_config_keys as _check_legacy_cas_config_keys,
)
from compiletools.apptools_validate import (
    _check_legacy_variant_config_keys as _check_legacy_variant_config_keys,
)
from compiletools.apptools_validate import (
    _check_resolved_compiler_available as _check_resolved_compiler_available,
)
from compiletools.apptools_validate import (
    _check_wild_linker_usable as _check_wild_linker_usable,
)

# Re-exported from the leaf flag_ops module so existing
# ``apptools.<name>`` call sites and test/patch targets keep working with
# identical object identity. ``extract_include_paths_from_tokens`` is a
# pure re-export (no internal apptools caller), hence the explicit
# redundant alias to mark it as an intentional re-export for linters.
from compiletools.flag_ops import (
    dedup_include_paths_to_append,
    filter_hash_irrelevant_tokens,
    strip_d_u_tokens,
)
from compiletools.flag_ops import (
    extract_include_paths_from_tokens as extract_include_paths_from_tokens,
)
from compiletools.flags import Flags
from compiletools.utils import split_command_cached

# ``DocumentationAction`` is only DEFINED in apptools_argparse when the optional
# ``rich_rst`` extra is installed (and Python >= 3.9). Re-export it by binding
# when present, but do NOT make it a top-level ``from ... import`` -- that would
# turn a missing optional dependency into an apptools ImportError, breaking
# every ct-* tool on systems without the ``rst`` extra. The conditional bind
# preserves the pre-split behaviour where the symbol simply doesn't exist on
# apptools when rich_rst is absent.
if hasattr(compiletools.apptools_argparse, "DocumentationAction"):
    DocumentationAction = compiletools.apptools_argparse.DocumentationAction

# Sentinel default values used by --CPP, --LD, --CPPFLAGS, --LDFLAGS to mean
# "if the user didn't supply this, fall back to CXX / CXXFLAGS". Kept as
# constants so a rename can't silently break _check_resolved_compiler_available
# (which compares against these strings to skip the existence check on slots
# that haven't been substituted yet).
_UNSUPPLIED_USE_CXX = "unsupplied_implies_use_CXX"
_UNSUPPLIED_USE_CXXFLAGS = "unsupplied_implies_use_CXXFLAGS"

# The closed set of "not supplied by the user" sentinels recognised by
# ``unsupplied_replacement``. The cas-*dir flags register the bare
# ``"unsupplied"`` sentinel; CPP/CPPFLAGS/LD/LDFLAGS register the two
# ``unsupplied_implies_*`` forms above. Membership is checked by *exact*
# equality (not substring) so a user-supplied path that merely contains the
# text ``unsupplied`` (e.g. ``--cas-objdir=/data/unsupplied/obj``) is not
# silently discarded and replaced with the computed default.
_UNSUPPLIED_SENTINELS = frozenset({"unsupplied", _UNSUPPLIED_USE_CXX, _UNSUPPLIED_USE_CXXFLAGS})


def unsupplied_replacement(variable, default_variable, verbose, variable_str):
    """If a given variable is one of the recognised "unsupplied" sentinels
    then return the given default variable.

    The check is exact membership in ``_UNSUPPLIED_SENTINELS`` rather than a
    substring test, so a real user-supplied value that merely contains the
    text ``unsupplied`` is preserved instead of being clobbered.
    """
    replacement = variable
    if variable in _UNSUPPLIED_SENTINELS:
        replacement = default_variable
        if verbose >= 6:
            print(" ".join([variable_str, "was unsupplied. Changed to use ", default_variable]))
    return replacement


def _ensure_variant_suffix(path, variant):
    """Return ``path`` with ``/<variant>`` appended as a final segment
    unless it is already there. Idempotent.

    Used to keep the four ``cas-*dir`` layers separated per variant
    even when the user points them at a bare shared-pool path (e.g.
    ``cas-objdir = /mnt/team-cache``). The check is segment-aware:
    ``/pool/release_old`` does NOT count as ending in ``release``.
    Trailing ``/`` is normalised away so the result never contains
    ``//``."""
    if not path or not variant:
        return path
    normalised = path.rstrip(os.sep) or path
    if os.path.basename(normalised) == variant:
        return normalised
    return os.path.join(normalised, variant)


def _substitute_CXX_for_missing(args):
    """If C PreProcessor variables (and the same for the LD*) are not set
    but CXX ones are set then just use the CXX equivalents.

    LD/LDFLAGS are only registered by ``add_link_arguments`` (used by ct-cake
    and the makefile backend); tools that build only the CPP/CXX side of the
    parser (ct-cleanup-locks, ct-trim-cache, ct-list-variants, ...) won't
    have those attrs, so we skip that substitution rather than raise.
    """
    if args.verbose > 8:
        print("Using CXX variables as defaults for missing C, CPP, LD variables")
    args.CPP = unsupplied_replacement(args.CPP, args.CXX, args.verbose, "CPP")
    args.CPPFLAGS = unsupplied_replacement(args.CPPFLAGS, args.CXXFLAGS, args.verbose, "CPPFLAGS")
    if hasattr(args, "LD"):
        args.LD = unsupplied_replacement(args.LD, args.CXX, args.verbose, "LD")
    if hasattr(args, "LDFLAGS"):
        args.LDFLAGS = unsupplied_replacement(args.LDFLAGS, args.CXXFLAGS, args.verbose, "LDFLAGS")


def _extend_includes_using_git_root(args):
    """Unless turned off, the git root will be added
    to the list of include paths
    """
    if args.git_root and (
        hasattr(args, "filename") or hasattr(args, "static") or hasattr(args, "dynamic") or hasattr(args, "tests")
    ):
        if args.verbose > 8:
            print("Extending the include paths to have the git root")

        git_roots = set()
        git_roots.add(compiletools.git_utils.find_git_root())

        # No matter whether args.filename is a single value or a list,
        # filenames will be a list
        filenames = []

        if hasattr(args, "filename") and args.filename:
            filenames.extend(args.filename)

        if hasattr(args, "static") and args.static:
            filenames.extend(args.static)

        if hasattr(args, "dynamic") and args.dynamic:
            filenames.extend(args.dynamic)

        if hasattr(args, "tests") and args.tests:
            filenames.extend(args.tests)

        for filename in filenames:
            git_roots.add(compiletools.git_utils.find_git_root(filename))

        if git_roots:
            # sorted(), not list(): set iteration order depends on
            # PYTHONHASHSEED, which would shift the -I order between processes
            # and invalidate the cas-objdir cxxflags_tokens hash component on
            # no-op rebuilds. See TestExtendIncludesUsingGitRootDeterministic.
            args.INCLUDE = " ".join(args.INCLUDE.split() + sorted(git_roots))
            if args.verbose > 6:
                print(f"Extended includes to have the gitroots {sorted(git_roots)}")
        else:
            raise ValueError(
                "args.git_root is True but no git roots found. :( .  If this is expected then specify --no-git-root."
            )


def _add_include_paths_to_flags(args):
    """Add all the include paths to all three compile flags.

    Token-walk dedup: a path already present as ``-Ip`` or ``-I p`` is
    skipped, but presence as ``-isystem /p``, ``-L /p``, or ``-DFOO=/p``
    is treated as absent (those are different flag families). The raw
    flag string is only *appended* to (never tokenized-and-rejoined),
    so existing shell-quoted content is preserved verbatim.
    """
    new_paths = args.INCLUDE.split()
    if not new_paths:
        return

    for raw_attr in ("CPPFLAGS", "CFLAGS", "CXXFLAGS"):
        existing = split_command_cached(getattr(args, raw_attr))
        added = dedup_include_paths_to_append(existing, new_paths)
        if added:
            setattr(args, raw_attr, getattr(args, raw_attr) + " " + " ".join(added))

    if args.verbose >= 6 and len(args.INCLUDE) > 0:
        print("Extra include paths have been appended to the *FLAG variables:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def extract_system_include_paths(args, flag_sources=None, verbose=0):
    """Extract -I and -isystem include paths from command-line flags.

    Args:
        args: Parsed arguments object with flag attributes (CPPFLAGS, CFLAGS, CXXFLAGS)
        flag_sources: List of flag names to extract from (default: ['CPPFLAGS', 'CXXFLAGS'])
        verbose: Verbosity level for debugging

    Returns:
        List of unique include paths in order
    """
    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CXXFLAGS"]

    include_paths = []

    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, "")
        if not flag_value:
            continue

        # Use existing shlex functionality from split_command_cached
        try:
            tokens = split_command_cached(flag_value)
        except ValueError:
            # Fall back to simple split if shlex fails
            tokens = flag_value.split()

        # Process tokens to find -I and -isystem flags
        i = 0
        while i < len(tokens):
            token = tokens[i]

            if token == "-I" or token == "-isystem":
                # Next token should be the path
                if i + 1 < len(tokens):
                    include_paths.append(tokens[i + 1])
                    i += 2
                else:
                    i += 1
            elif token.startswith("-I"):
                # -Ipath format
                path = token[2:]
                if path:  # Make sure it's not just "-I"
                    include_paths.append(path)
                i += 1
            elif token.startswith("-isystem"):
                # -isystempath format (though this is unusual)
                path = token[8:]
                if path:  # Make sure it's not just "-isystem"
                    include_paths.append(path)
                i += 1
            else:
                i += 1

    # Remove duplicates while preserving order using existing ordered_unique
    include_paths = compiletools.utils.ordered_unique(include_paths)

    if verbose >= 9 and include_paths:
        print(f"Extracted system include paths: {include_paths}")

    return include_paths


def find_system_header(header_name, args, verbose=0):
    """Find a system header in the -I/-isystem include paths.

    Args:
        header_name: Name of header to find (e.g., "stdio.h", "mylib/header.h")
        args: Parsed arguments object with flag attributes
        verbose: Verbosity level for debugging

    Returns:
        Absolute path to header if found, None otherwise
    """
    include_paths = extract_system_include_paths(args, verbose=verbose)

    for include_path in include_paths:
        candidate = os.path.join(include_path, header_name)
        if compiletools.wrappedos.isfile(candidate):
            return compiletools.wrappedos.realpath(candidate)

    if verbose >= 9:
        print(f"System header '{header_name}' not found in include paths: {include_paths}")

    return None


def extract_command_line_macros(args, flag_sources=None, include_compiler_macros=True, verbose=0):
    """Extract -D macro definitions from command line flags.

    Args:
        args: Parsed arguments object with flag attributes (CPPFLAGS, CFLAGS, CXXFLAGS)
        flag_sources: List of flag names to extract from (default: ['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'])
        include_compiler_macros: Whether to include compiler/platform macros
        verbose: Verbosity level (uses args.verbose if 0)

    Returns:
        Dict[str, str]: macro_name -> macro_value mapping
    """
    if verbose == 0 and hasattr(args, "verbose"):
        verbose = args.verbose

    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CFLAGS", "CXXFLAGS"]

    macros = {}

    # Extract -D macros from specified flag sources
    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, None)
        if not flag_value:
            continue

        # Handle both string and list types for flag_value
        if isinstance(flag_value, list):
            flag_string = " ".join(flag_value)
        else:
            flag_string = flag_value

        # Use shlex.split for robust parsing
        try:
            flags = split_command_cached(flag_string)
        except ValueError:
            # Fallback to simple split if shlex fails on malformed input
            flags = flag_string.split()

        # Walk tokens recognizing both attached (-DFOO, -DFOO=val) and
        # detached (-D FOO, -D FOO=val) forms. The detached form was
        # previously silently dropped and that disagreed with the macro
        # universe computed by cmdline_d_macro_names, defeating the
        # cache-key scoping.
        i = 0
        n = len(flags)
        while i < n:
            flag = flags[i]
            macro_def = None
            if flag == "-D":
                # Detached: name (and optional =value) is the next token.
                if i + 1 < n:
                    macro_def = flags[i + 1]
                i += 2
            elif flag.startswith("-D"):
                macro_def = flag[2:]
                i += 1
            else:
                i += 1
                continue

            if not macro_def:
                continue

            if "=" in macro_def:
                macro_name, macro_value = macro_def.split("=", 1)
            else:
                macro_name = macro_def
                macro_value = "1"  # Default value for macros without explicit values

            if macro_name:
                macros[macro_name] = macro_value
                if verbose >= 9:
                    print(f"extract_command_line_macros: added macro {macro_name} = {macro_value} from {flag_name}")

    # Add compiler, platform, and architecture macros if requested
    if include_compiler_macros:
        import compiletools.compiler_macros

        # Use same pattern as parseargs() - check args.CXX first to avoid redundant detection
        compiler = getattr(args, "CXX", None)
        if compiler is None:
            functional_compiler = get_functional_cxx_compiler()
            if functional_compiler:
                compiler = functional_compiler
            else:
                if verbose >= 1:
                    print(
                        "Warning: No functional C++ compiler detected. Skipping compiler macros.",
                        file=sys.stderr,
                    )

        if compiler is not None:
            compiler_macros = compiletools.compiler_macros.get_compiler_macros(compiler, verbose)
            macros.update(compiler_macros)

    return macros


def extract_command_line_macros_sz(args, flag_sources_sz, verbose=0):
    """Extract -D macro definitions from sz.Str command line flags.

    Args:
        args: Object with sz.Str list attributes
        flag_sources_sz: List of sz.Str flag names
        verbose: Verbosity level

    Returns:
        Dict[sz.Str, sz.Str]: macro_name -> macro_value mapping
    """
    import stringzilla as sz

    macros = {}

    for flag_name_sz in flag_sources_sz:
        flag_list = getattr(args, str(flag_name_sz), None)
        if not flag_list:
            continue

        for flag_sz in flag_list:
            if not flag_sz.startswith("-D"):
                continue

            macro_def = flag_sz[2:]
            eq_pos = macro_def.find("=")
            if eq_pos >= 0:
                macro_name = macro_def[:eq_pos]
                macro_value = macro_def[eq_pos + 1 :]
            else:
                macro_name = macro_def
                macro_value = sz.Str("1")

            if macro_name:
                macros[macro_name] = macro_value
                if verbose >= 9:
                    print(
                        f"extract_command_line_macros_sz: added macro {macro_name} = {macro_value} from {flag_name_sz}"
                    )

    return macros


def cmdline_d_macro_names(args, flag_sources=None, verbose=0) -> frozenset[sz.Str]:
    """Set of macro names defined via cmdline -D flags (CPPFLAGS/CFLAGS/CXXFLAGS).

    Excludes compiler builtins. The returned set is the universe of macros
    that the per-TU cache-key scoping will consider for filtering.

    Recognizes both attached form (-DFOO, -DFOO=bar) and detached form
    (-D FOO, -D FOO=bar) of the -D flag. The macro VALUE is irrelevant
    here -- only the name matters for the scope-filter universe.

    Args:
        args: Parsed arguments object (must have CPPFLAGS/CFLAGS/CXXFLAGS attrs)
        flag_sources: List of flag names to extract from
            (default: ['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'])
        verbose: Verbosity level (uses args.verbose if 0)

    Returns:
        frozenset[sz.Str]: Macro names from cmdline -D flags.
    """
    if verbose == 0 and hasattr(args, "verbose"):
        verbose = args.verbose

    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CFLAGS", "CXXFLAGS"]

    names = set()
    for flag_name in flag_sources:
        flag_value = getattr(args, flag_name, None)
        if not flag_value:
            continue

        if isinstance(flag_value, list):
            flag_string = " ".join(flag_value)
        else:
            flag_string = flag_value

        try:
            tokens = split_command_cached(flag_string)
        except ValueError:
            tokens = flag_string.split()

        i = 0
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            macro_def = None
            if tok == "-D":
                # Detached form: name is the next token.
                if i + 1 < n:
                    macro_def = tokens[i + 1]
                i += 2
            elif tok.startswith("-D"):
                macro_def = tok[2:]
                i += 1
            else:
                i += 1
                continue

            if not macro_def:
                continue
            eq_pos = macro_def.find("=")
            macro_name = macro_def[:eq_pos] if eq_pos >= 0 else macro_def
            if macro_name:
                names.add(macro_name)
                if verbose >= 9:
                    print(f"cmdline_d_macro_names: added {macro_name} from {flag_name}")

    return frozenset(sz.Str(name) for name in names)


def _has_prefix_map_flag(raw_flags: str) -> bool:
    """Return True if *raw_flags* contains any top-level
    ``-f{file,debug,macro,canon}-prefix-map=`` flag.

    Tokenizes via ``shlex.split`` and checks ``tok.startswith(prefix)``
    so that a prefix-map substring nested inside another flag's value
    (e.g. ``-DREASON='-ffile-prefix-map=oops='``) does NOT false-
    positive — naive substring search would, and the symptom (silently
    per-user-divergent ``.o`` bytes for a project that thought it had
    cross-user CAS sharing) would be hard to diagnose.

    Empty / None returns False. Unparseable flag strings (e.g.
    unbalanced quotes from a user's CXXFLAGS) return True — the
    conservative call: an opaque string is unsafe to interpret either
    way, so decline auto-injection rather than risk appending a flag
    the user might already have inside their unparseable text.
    """
    if not raw_flags:
        return False
    try:
        tokens = shlex.split(raw_flags)
    except ValueError:
        return True
    return any(tok.startswith(prefix) for tok in tokens for prefix in _PREFIX_MAP_FLAG_PREFIXES)


def _inject_ffile_prefix_map(args) -> None:
    """Append ``-ffile-prefix-map=<gitroot>=<target>`` to args.CXXFLAGS
    and args.CFLAGS where the user has not already specified any
    prefix-map flag.

    The flag rewrites paths the compiler EMITS (DWARF debug info,
    ``__FILE__`` / ``__builtin_FILE``, ``.d`` output) so two users
    compiling the same source at different workspace paths produce
    byte-identical .o bytes -- the user-stated goal of Round 3 for
    the cas-objdir layer.

    Known scope limitation: PCH (.gch) and C++20 BMI (.pcm / .gcm)
    files embed the absolute source path through gcc's internal
    path-table, which is NOT subject to -ffile-prefix-map.
    -fdebug-compilation-dir= would address this for clang but is not
    a recognised gcc flag (rejected by gcc as of 16.1.0). Closing
    the gcc PCH / BMI gap requires either (a) workspace-relative
    source paths in the precompile rule emitter plus per-backend
    CWD discipline, or (b) a PWD=/proc/self/cwd subprocess-env
    trick. Both are deferred follow-ups; cas-pchdir and cas-pcmdir
    cross-user sharing remains per-user until then. See
    docs/superpowers/specs/2026-05-12-round3-workspace-relative-compile-paths-design.md
    "Open Questions" for the design escalation paths.

    Skipped when ``compiletools.git_utils.find_git_root()`` returns an
    empty / falsy value (no anchor to canonicalize against). Per-slot
    independently -- user can override C++ but accept the C default.

    Mutates ``args.CXXFLAGS`` and ``args.CFLAGS`` in place. The caller
    (``_commonsubstitutions``) triggers ``_finalize_flag_state``
    afterward so the ``*_tokens`` lists and ``args.flags`` reflect the
    new strings.

    Idempotent: a second call detects the previously-injected flag via
    :func:`_has_prefix_map_flag` and skips. The CLI flag
    ``--ffile-prefix-map-target`` (default ``.``) controls the RHS.
    """
    git_root = compiletools.git_utils.find_git_root()
    if not git_root:
        return
    target = getattr(args, "ffile_prefix_map_target", ".")
    flag = f"-ffile-prefix-map={git_root}={target}"
    for attr in ("CXXFLAGS", "CFLAGS"):
        existing = getattr(args, attr, "") or ""
        if _has_prefix_map_flag(existing):
            continue
        setattr(args, attr, f"{existing} {flag}".strip() if existing else flag)


def _effective_link_driver(args) -> str | None:
    """Return the compiler driver that actually performs the link.

    The link runs through ``args.LD`` (gcc.conf/clang.conf set ``LD`` to
    g++/clang++), falling back to ``args.CXX`` when LD is unset or still
    the "use CXX" sentinel. The wild linker selector (``-fuse-ld=wild`` /
    ``--ld-path=wild``) is consumed by THIS driver, so it is what
    ``compiler_kind`` must classify.
    """
    ld = getattr(args, "LD", None)
    if ld and ld not in (_UNSUPPLIED_USE_CXX, _UNSUPPLIED_USE_CXXFLAGS):
        return ld
    return getattr(args, "CXX", None)


def _variant_has_axis(args, axis_name: str) -> bool:
    """True if *axis_name* is one of the variant's selected axis tokens.

    Splits ``args.variant`` on the variant separator (``[\\s,.]+``) so it
    works whether the variant string is in user order or canonicalised.
    Hyphenated tokens (``wild-B``, ``gold-nommap``) survive the split
    because ``-`` is not a separator.
    """
    variant = getattr(args, "variant", "") or ""
    return axis_name in compiletools.configutils.split_variant(variant)


def _materialize_wild_b_searchdir() -> str | None:
    """Create (idempotently) a directory holding a symlink ``ld -> wild`` and
    return its path, for the ``wild-B`` linker axis.

    gcc/clang's ``-B<dir>`` adds <dir> to the executable search path; gcc's
    collect2 then invokes ``<dir>/ld``, which we point at the wild binary.
    This is the universal wild invocation that works on ANY gcc version
    (unlike ``-fuse-ld=wild``, which needs gcc >= 16.1).

    Location: ``<gitroot>/.ct-wild-ld/`` when a gitroot exists, so the
    injected ``-B<gitroot>/.ct-wild-ld`` is rewritten by
    ``canonicalize_for_cache_key`` (``<gitroot>/...`` -> ``<GITROOT>``
    sentinel) in the link key and stays workspace-portable across users
    sharing a CAS. Falls back to a shared system temp dir
    (``<tmpdir>/ct-wild-ld``) when no gitroot exists
    (``_inject_ffile_prefix_map`` is a no-op in that case; here
    we still produce a usable -B dir).

    Returns None if the wild binary can't be found (the startup check
    ``_check_wild_linker_usable`` raises a clear error in that case).
    """
    wild_path = shutil.which("wild")
    if not wild_path:
        return None
    git_root = compiletools.git_utils.find_git_root()
    if git_root:
        search_dir = os.path.join(git_root, ".ct-wild-ld")
    else:
        search_dir = os.path.join(tempfile.gettempdir(), "ct-wild-ld")
    os.makedirs(search_dir, exist_ok=True)
    ld_link = os.path.join(search_dir, "ld")
    # Idempotent: (re)create only when missing or pointing elsewhere.
    try:
        current = os.readlink(ld_link)
    except OSError:
        current = None
    if current != wild_path:
        # Atomic replace via a PID-scoped temp name so concurrent ct-cake
        # processes sharing this gitroot don't race on a shared temp path
        # (one's os.symlink would hit EEXIST, or one's pre-unlink would yank
        # another's live temp out from under its os.replace). os.replace is
        # atomic and every racer writes the same target, so last-writer-wins
        # is safe. The pre-unlink only clears a leftover temp from a prior
        # crash of THIS pid.
        tmp_link = f"{ld_link}.{os.getpid()}.tmp"
        try:
            os.unlink(tmp_link)
        except OSError:
            pass
        os.symlink(wild_path, tmp_link)
        os.replace(tmp_link, ld_link)
    return search_dir


def _normalize_wild_linker(args) -> None:
    """Rewrite the wild linker selection to the form the link driver accepts,
    and wire up the ``wild-B`` ``-B``/symlink fallback.

    ``wild.conf`` emits the canonical, user-facing token ``-fuse-ld=wild`` —
    exactly what gcc >= 16.1 wants, but clang rejects it ("invalid linker
    name in argument '-fuse-ld=wild'") unless an ``ld.wild`` symlink is on
    PATH, which wild's installer does NOT create. clang's portable
    invocation is ``--ld-path=wild``, so for a clang link driver we rewrite
    the token (bare name, PATH-resolved, to keep per-user absolute paths out
    of the link key).

    The ``wild-B`` axis (a comment-only conf) selects the universal
    ``-B<dir>`` + ``ld -> wild`` symlink trick that works on ANY gcc; the
    symlink dir is materialised by :func:`_materialize_wild_b_searchdir`
    and stashed on ``args._wild_b_search_dir``. The link-rule builders in
    ``build_backend`` append ``-B<absolute_dir>`` directly to the emitted
    link argv, bypassing LDFLAGS (and therefore ``canonicalize_for_command``
    rewriting) — see comment in the wild-B branch below for the silent
    fall-through hazard this avoids.

    For the clang rewrite, mutates ``args.LDFLAGS`` in place. Called from
    ``_commonsubstitutions`` before ``_finalize_flag_state`` (via
    ``substitutions``), so ``args.LDFLAGS_tokens`` / ``args.flags`` are
    rebuilt from the mutated string and ``check_flag_string_drift`` stays
    satisfied. No-op when wild is not selected. Idempotent: after the clang
    rewrite the ``-fuse-ld=wild`` token is gone, so a re-run (e.g. via
    cake's two-stage parse) makes no further change.
    """
    ldflags = getattr(args, "LDFLAGS", "") or ""

    # `wild` axis: clang needs --ld-path=wild in place of -fuse-ld=wild.
    if "-fuse-ld=wild" in split_command_cached(ldflags):
        if compiler_kind(_effective_link_driver(args)) == "clang":
            tokens = split_command_cached(ldflags)
            rewritten = ["--ld-path=wild" if t == "-fuse-ld=wild" else t for t in tokens]
            args.LDFLAGS = shlex.join(rewritten)

    # `wild-B` axis: materialise the -B search dir. The -B<dir> flag itself
    # is injected per-rule by the link-rule builders in build_backend.py —
    # routing it through LDFLAGS would let canonicalize_for_command rewrite
    # the absolute path to a target-relative form (e.g. "-B./.ct-wild-ld")
    # that only resolves when the build runs from the gitroot, silently
    # falling through to the default linker under subdir invocation while
    # the CAS link key still claims the wild-B variant.
    if _variant_has_axis(args, "wild-B"):
        args._wild_b_search_dir = _materialize_wild_b_searchdir()


def tokenize_compile_flags(
    cppflags,
    cflags,
    cxxflags,
    strip_unhashed: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Tokenize compile-flag strings into structured lists with -D/-U removed.

    Used by MacroState's structured build-context hash. -D and -U entries
    are stripped because cmdline -D macros are hashed separately via the
    per-TU scoping mechanism. Other flags (-I, -O, -std, -W, -f...) pass
    through unchanged.

    Each input may be a string (will be shlex-split, with simple-split
    fallback on ValueError, matching extract_command_line_macros) or a
    pre-tokenized list of strings.

    Both attached form (-DFOO, -DFOO=bar, -UFOO) and detached form
    (-D FOO, -D FOO=bar, -U FOO) of -D/-U are stripped. Detached form
    drops both the flag token and the following value token. All other
    flags (-I, -O, -std, -W, -f...) pass through unchanged.

    When ``strip_unhashed=True``, also remove hash-irrelevant diagnostic
    tokens (warnings, message formatting, ``-pipe``, ``-v``) from each
    list via :func:`filter_hash_irrelevant_tokens`. Default ``False``
    preserves the previous behavior (only -D/-U stripped).

    Returns:
        (cppflags_tokens, cflags_tokens, cxxflags_tokens) -- three lists
        of remaining tokens, in original order.
    """

    def _to_tokens(value):
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if not value:
            return []
        try:
            return split_command_cached(value)
        except ValueError:
            return value.split()

    cpp = strip_d_u_tokens(_to_tokens(cppflags))
    c = strip_d_u_tokens(_to_tokens(cflags))
    cxx = strip_d_u_tokens(_to_tokens(cxxflags))
    if strip_unhashed:
        cpp = filter_hash_irrelevant_tokens(cpp)
        c = filter_hash_irrelevant_tokens(c)
        cxx = filter_hash_irrelevant_tokens(cxx)
    return (cpp, c, cxx)


def clear_cache():
    """Clear any caches for macro extraction and pkg-config.

    The compiler-probe caches now live in
    :mod:`compiletools.apptools_compiler` and the pkg-config cache in
    :mod:`compiletools.apptools_pkgconfig`; we fan out to each module's
    ``clear_cache`` so the exact same set of caches is cleared as before the
    facade split: from ``apptools_compiler``
    (``_get_functional_cxx_compiler_cached``, ``compiler_identity``,
    ``compiler_kind``, ``compiler_default_cxx_std``,
    ``find_system_std_module_source``) and from ``apptools_pkgconfig``
    (``cached_pkg_config``). Net effect is identical to the previous
    monolithic implementation.
    """
    compiletools.apptools_pkgconfig.clear_cache()
    compiletools.apptools_compiler.clear_cache()


_PROJECT_MACRO_DEPRECATION_MESSAGE = (
    "ct-cake: --project-version / --project-name (and their *-cmd variants) "
    "are DEPRECATED. They inject -D macros that defeat object-cache reuse "
    "for any TU whose transitive headers textually mention the macro name. "
    "Use --prebuild-script with a generated implementation file instead — "
    "see examples-end-to-end/appinfo/ and README.ct-cake.rst.\n"
)


def _warn_project_macros_deprecated(args):
    """Emit the deprecation warning once per process for the project-macro flags."""
    if getattr(args, "_project_macro_deprecation_warned", False):
        return
    sys.stderr.write(_PROJECT_MACRO_DEPRECATION_MESSAGE)
    args._project_macro_deprecation_warned = True


def _set_project_version(args):
    """Inject ``-DCT_PROJECT_VERSION="<value>"`` into CPPFLAGS/CFLAGS/CXXFLAGS,
    but only if the user opted in.

    Opt-in is any of:
      * ``--project-version VALUE`` on CLI / ct.conf / env
      * ``--project-version-cmd CMD`` on CLI / ct.conf / env

    If neither is set, do nothing — no macro is injected. This keeps
    cmdline ``-D`` cache-key noise off TUs that don't ask for it (see the
    "Macro Scope Filter" section of README.ct-cake.rst for why a
    cmdline ``-D`` macro is sticky once introduced).

    DEPRECATED — see ``_warn_project_macros_deprecated``. The
    generated-implementation-file pattern (``examples-end-to-end/appinfo``)
    is the supported replacement.
    """
    projectversion = getattr(args, "projectversion", None)
    projectversioncmd = getattr(args, "projectversioncmd", None)

    if not projectversion and projectversioncmd:
        try:
            projectversion = (
                subprocess.check_output(projectversioncmd.split(), universal_newlines=True).strip("\n").split()[0]
            )
            args.projectversion = projectversion
            if args.verbose >= 6:
                print("Used projectversioncmd to set projectversion")
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(
                " ".join(
                    [
                        "Could not use projectversioncmd =",
                        projectversioncmd,
                        "to set projectversion.\n",
                    ]
                )
            )
            if args.verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise

    if not projectversion:
        return

    _warn_project_macros_deprecated(args)

    version_escaped = projectversion.replace("\\", "\\\\").replace('"', '\\"')

    if "-DCT_PROJECT_VERSION" not in args.CPPFLAGS:
        args.CPPFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
    if "-DCT_PROJECT_VERSION" not in args.CFLAGS:
        args.CFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
    if "-DCT_PROJECT_VERSION" not in args.CXXFLAGS:
        args.CXXFLAGS += " -DCT_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')

    if args.verbose >= 6:
        print("*FLAG variables have been modified with the project version:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def _set_project_name(args):
    """Inject ``-DCT_PROJECT_NAME="<value>"`` into CPPFLAGS/CFLAGS/CXXFLAGS,
    but only if the user opted in.

    Opt-in is any of:
      * ``--project-name VALUE`` on CLI / ct.conf / env
      * ``--project-name-cmd CMD`` on CLI / ct.conf / env

    If neither is set, do nothing — no macro is injected. Mirrors
    _set_project_version. See the "Macro Scope Filter" section of
    README.ct-cake.rst for why CT_PROJECT_NAME (like any cmdline ``-D``
    macro) should only be turned on when actually needed: comments in
    transitive headers that mention the macro by name will pull it
    into every includer's per-TU cache key.
    """
    projectname = getattr(args, "projectname", None)
    projectnamecmd = getattr(args, "projectnamecmd", None)

    if not projectname and projectnamecmd:
        try:
            projectname = (
                subprocess.check_output(projectnamecmd.split(), universal_newlines=True).strip("\n").split()[0]
            )
            args.projectname = projectname
            if args.verbose >= 6:
                print("Used projectnamecmd to set projectname")
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(
                " ".join(
                    [
                        "Could not use projectnamecmd =",
                        projectnamecmd,
                        "to set projectname.\n",
                    ]
                )
            )
            if args.verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise

    if not projectname:
        return

    _warn_project_macros_deprecated(args)

    name_escaped = projectname.replace("\\", "\\\\").replace('"', '\\"')

    if "-DCT_PROJECT_NAME" not in args.CPPFLAGS:
        args.CPPFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')
    if "-DCT_PROJECT_NAME" not in args.CFLAGS:
        args.CFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')
    if "-DCT_PROJECT_NAME" not in args.CXXFLAGS:
        args.CXXFLAGS += " -DCT_PROJECT_NAME=" + shlex.quote(f'"{name_escaped}"')

    if args.verbose >= 6:
        print("*FLAG variables have been modified with the project name:")
        print("\tCPPFLAGS=" + args.CPPFLAGS)
        print("\tCFLAGS=" + args.CFLAGS)
        print("\tCXXFLAGS=" + args.CXXFLAGS)


def _do_xxpend(args, name):
    """For example, if name is CPPFLAGS, take the
    args.prependcppflags and prepend them to args.CPPFLAGS.
    Similarly for append.
    """
    xxlist = ("prepend", "append")
    for xx in xxlist:
        xxpendname = "_".join([xx, name.lower()])
        if hasattr(args, xxpendname):
            xxpendattr = getattr(args, xxpendname)
            attr = getattr(args, name)

            if xxpendattr:
                extra = []
                for flag in xxpendattr:
                    if flag not in attr:
                        extra.append(flag)
                        if args.verbose > 8:
                            print(f"{xx} {extra} to {name}")
                if xx == "prepend":
                    attr = " ".join(extra + [attr])
                else:
                    attr = " ".join([attr] + extra)
            setattr(args, name, attr)


def _do_xxpend_list(args, name, dest_name=None):
    """List-typed sibling of ``_do_xxpend`` for attrs whose canonical form
    is a Python list (e.g. ``args.pkg_config``), not a flag string. The
    base attr is read from ``args.<dest_name or name.replace('-','_')>``,
    and the prepend/append sources from
    ``args.{prepend,append}_<dest_name or name.replace('-','_')>``.

    Mirrors ``_do_xxpend``'s dedup-and-place rule (prepend leftmost,
    append rightmost, skip duplicates already present in the base) so
    consumers of ``--prepend-PKG-CONFIG`` / ``--append-PKG-CONFIG`` get
    the same composition semantics that compiler-flag slots have.
    """
    dest = (dest_name or name).lower().replace("-", "_")
    base = list(getattr(args, dest, []) or [])
    for xx in ("prepend", "append"):
        xxpendname = f"{xx}_{dest}"
        xxpendattr = getattr(args, xxpendname, None) or []
        extras = [v for v in xxpendattr if v not in base]
        if not extras:
            continue
        if xx == "prepend":
            base = extras + base
        else:
            base = base + extras
    setattr(args, dest, base)


def _unify_cpp_cxx_flags(args):
    """Combine CPPFLAGS and CXXFLAGS into a single deduplicated value.

    Skipped when --separate-flags-CPP-CXX is set.

    Uses ``shlex.join`` (not ``' '.join``) to reconstruct the raw flag
    string from the deduplicated token list.  ``combine_and_deduplicate``
    calls ``shlex.split`` to tokenise, so the token list may contain
    entries with shell-special characters (e.g. ``-DCT_PROJECT_VERSION="1.2.3"``
    with literal double-quote chars produced by ``_set_project_version``).
    A plain ``' '.join`` writes those tokens back to the raw string without
    any shell quoting, causing a subsequent ``shlex.split`` (in
    ``_finalize_flag_state``) to strip the double-quote characters — so the
    C-string-literal delimiters are lost before the token ever reaches the
    compiler.  ``shlex.join`` re-adds single-quote wrapping for tokens that
    contain shell-active characters, preserving the round-trip invariant.
    """
    if getattr(args, "separate_flags_CPP_CXX", False):
        return
    unified = shlex.join(compiletools.utils.combine_and_deduplicate_compiler_flags(args.CPPFLAGS, args.CXXFLAGS))
    args.CPPFLAGS = unified
    args.CXXFLAGS = unified


def _deduplicate_all_flags(args):
    """Deduplicate all compiler and linker flags after all processing is complete.

    Uses ``shlex.join`` to reconstruct each raw flag string from the
    deduplicated token list, preserving shell-special characters in tokens
    (e.g. double-quote chars in ``-DCT_PROJECT_VERSION="1.2.3"``).  See
    ``_unify_cpp_cxx_flags`` for the full rationale.
    """
    flaglist = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
    for flag_name in flaglist:
        if hasattr(args, flag_name):
            flag_value = getattr(args, flag_name)
            if flag_value:
                # Split the flag string into individual flags and deduplicate
                deduplicated_flags = compiletools.utils.combine_and_deduplicate_compiler_flags(flag_value)
                # Use shlex.join (not ' '.join) so tokens with shell-active
                # characters survive the round-trip through shlex.split.
                setattr(args, flag_name, shlex.join(deduplicated_flags))


def _tier_one_modifications(args):
    """Do some early modifications that can potentially cause
    downstream modifications.
    """
    if args.verbose > 8:
        print("Tier one modification")
        print(f"{args=}")
    _substitute_CXX_for_missing(args)
    flaglist = ("INCLUDE", "CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
    for flag in flaglist:
        _do_xxpend(args, flag)

    # args.pkg_config is a list of package names (not a flag string), so
    # it needs the list-typed merge — _do_xxpend would " ".join() it into
    # a single space-separated string and break downstream consumers.
    _do_xxpend_list(args, "pkg-config")

    # Deduplicate all compiler/linker flags after all processing is complete
    _deduplicate_all_flags(args)

    # Cake used preprocess to mean both magic flag preprocess and headerdeps preprocess
    if hasattr(args, "preprocess") and args.preprocess:
        args.magic = "cpp"
        args.headerdeps = "cpp"


def _strip_quotes(args):
    """Remove shell quotes from arguments while preserving content quotes.

    Uses proper shell parsing to understand when quotes are shell quoting
    vs. part of the actual content. Also strips extraneous whitespace.
    """
    for name in vars(args):
        value = getattr(args, name)
        if value is not None:
            # Can't just use the for loop directly because that would
            # try and process every character in a string
            if compiletools.utils.is_non_string_iterable(value):
                for index, element in enumerate(value):
                    value[index] = _safely_unquote_string(element)
            else:
                try:
                    # Otherwise assume its a string
                    setattr(args, name, _safely_unquote_string(value))
                except (AttributeError, ValueError, TypeError):
                    logging.debug("Could not unquote arg %s (type %s)", name, type(value).__name__)


def _safely_unquote_string(value):
    """Safely remove shell quotes from a string using proper parsing.

    Only removes quotes that are actual shell quotes, not content quotes.
    Falls back to compatibility behavior for edge cases.
    """
    if not isinstance(value, str):
        return value

    # Strip whitespace first
    value = value.strip()

    # If the string doesn't look like it has shell quotes, don't process it
    if not value.startswith(('"', "'")):
        return value

    try:
        # Use shlex to parse the string as shell would
        # If it parses to exactly one token, it was properly quoted
        tokens = split_command_cached(value)
        if len(tokens) == 1:
            # Single token means the quotes were shell quotes
            unquoted = tokens[0]

            # For backwards compatibility, if the result still has quotes at both ends,
            # recursively strip them (mimics old behavior for nested quotes)
            if (unquoted.startswith('"') and unquoted.endswith('"')) or (
                unquoted.startswith("'") and unquoted.endswith("'")
            ):
                return _safely_unquote_string(unquoted)
            return unquoted
        else:
            # Multiple tokens or parsing issues - return original
            return value
    except ValueError:
        # Malformed quoting - fall back to original naive approach for compatibility
        # but only strip matching quote pairs
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1].strip()
        return value.strip("\"'").strip()


def _flatten_variables(args):
    """Most of the code base was written to expect CXXFLAGS are a single string with space separation.
    However, around 20240920 we allowed some variables to be lists of those strings.  To allow this
    change to slip in with minimal code changes, we flatten out the list into a single string.

    Uses ``shlex.join`` (not ``' '.join``) so that list elements containing
    shell-special characters (embedded spaces, double-quotes, etc.) survive the
    subsequent ``shlex.split`` call in ``_finalize_flag_state``.  When the user
    passes ``--CPPFLAGS '-DFOO=bar baz'`` on the CLI, the shell consumes the
    outer quotes and argparse stores ``'-DFOO=bar baz'`` as a single list element;
    ``' '.join`` would produce ``'-DFOO=bar baz -Wall'`` (unsplit on the space),
    and ``shlex.split`` would then misparse it as three tokens.  Cousin fix to
    commit 5cd77781 which patched the same pattern in ``_unify_cpp_cxx_flags``
    and ``_deduplicate_all_flags``.
    """
    for varname in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "INCLUDE"):
        if isinstance(getattr(args, varname, None), list):
            setattr(args, varname, shlex.join(getattr(args, varname)))


def _commonsubstitutions(args):
    """If certain arguments have not been specified but others have
    then there are some obvious substitutions to make
    """
    args.verbose -= args.quiet

    if args.verbose > 8:
        print("Performing common substitutions")

    # Canonicalize the parsed args.variant. The default value for --variant
    # came from extract_variant(argv) at parser construction with composite
    # input already canonicalized, but an explicit --variant=gcc,debug,asan
    # in argv bypasses that — argparse stores the raw comma-separated string.
    # Resolve here so downstream consumers (cas-objdir/<variant>/, bindir,
    # compile_commands.<variant>.json) always see the dotted canonical form.
    args.variant = compiletools.configutils.canonicalize_variant_input(args.variant, argv=getattr(args, "_argv", None))
    # Re-resolve fresh from the post-argparse variant value rather than
    # caching the create_parser-time resolution. Cheap (just file stats),
    # avoids module-level mutable state, and uses the correct variant when
    # --variant was explicitly set on the CLI. Pass argv through so the
    # explicit_config branch fires when --config=path was supplied
    # (args.variant becomes the implied basename, which isn't a real
    # axis — the resolver needs argv to know that the basename is from
    # --config and not a request to look up a missing axis).
    args._variant_resolution = compiletools.configutils.resolve_variant(
        variant=args.variant, argv=getattr(args, "_argv", None)
    )
    if args.verbose > 6:
        print(f"Determined variant to be {args.variant}")
    # Per-axis provenance is short (10-15 lines) and answers "why did I get
    # these flags?" without rebuilding. Emit it at -vv and above; quiet by
    # default so build-system wrappers around ct-cake aren't surprised by
    # extra stdout. ct-config auto-bumps verbosity so it always shows.
    if args.verbose >= 2 and args._variant_resolution is not None:
        print(compiletools.configutils.format_variant_resolution(args._variant_resolution))

    _tier_one_modifications(args)
    _extend_includes_using_git_root(args)
    _add_include_paths_to_flags(args)
    _setup_pkg_config_overrides(
        args._context,
        args.verbose,
        prepend_paths=getattr(args, "prepend_pkg_config_path", None),
        append_paths=getattr(args, "append_pkg_config_path", None),
        args_parser=getattr(args, "_parser", None),
    )
    _add_flags_from_pkg_config(args)
    _set_project_version(args)
    _set_project_name(args)
    _unify_cpp_cxx_flags(args)

    try:
        # If the user didn't explicitly supply a bindir then modify the bindir to use the variant name
        args.bindir = unsupplied_replacement(args.bindir, os.path.join("bin", args.variant), args.verbose, "bindir")
    except AttributeError:
        pass

    resolve_cas_directory_arguments(args)

    # Anchor --test-xml-dir to gitroot so the value survives a `cd` into
    # a subdirectory between parseargs and the build, matching how
    # cas-objdir / cas-pchdir are anchored. This is a path resolution
    # only; the directory is created by the build graph's mkdir rule.
    test_xml_dir = getattr(args, "test_xml_dir", None)
    if test_xml_dir and not os.path.isabs(test_xml_dir):
        git_root = compiletools.git_utils.find_git_root()
        if git_root:
            args.test_xml_dir = os.path.join(git_root, test_xml_dir)
        else:
            args.test_xml_dir = os.path.abspath(test_xml_dir)

    # Round 3: inject -ffile-prefix-map=<gitroot>=<target> into CXXFLAGS /
    # CFLAGS so cas-objdir / cas-pchdir / cas-pcmdir contents are byte-
    # identical across users with different checkout paths. Must run AFTER
    # all earlier flag mutations (project version / name macros, CPP/CXX
    # unification, pkg-config flag merging) so the detection scan sees the
    # final user-state of the slot before deciding whether to inject. The
    # subsequent _finalize_flag_state call (via substitutions(args) in the
    # caller) rebuilds args.flags from the now-injected raw strings.
    _inject_ffile_prefix_map(args)

    # Rewrite the wild linker selection to the form the resolved link driver
    # accepts (clang: -fuse-ld=wild -> --ld-path=wild) and wire the wild-B
    # -B/symlink fallback. Runs here so the subsequent _finalize_flag_state
    # (via substitutions) rebuilds args.LDFLAGS_tokens / args.flags from the
    # mutated string. See _normalize_wild_linker.
    _normalize_wild_linker(args)


# List to store the callback functions for parse args
_substitutioncallbacks = [_commonsubstitutions]


def resetcallbacks():
    """Useful in tests to clear out the substitution callbacks"""
    global _substitutioncallbacks
    _substitutioncallbacks = [_commonsubstitutions]


def registercallback(callback):
    """Use this to register a function to be called back during the
    substitutions call (usually during parseargs).
    The callback function will later be given "args" as its argument.
    """
    _substitutioncallbacks.append(callback)


def substitutions(args, verbose=None):
    if verbose is None:
        verbose = args.verbose

    for func in _substitutioncallbacks:
        if verbose > 8:
            print(f"Performing substitution: {func.__qualname__}")
        func(args)

    if verbose >= 8:
        print("Args after substitutions")
        verboseprintconfig(args)

    # substitutions is the canonical mutation site for raw flag strings.
    # Refresh args.{*}_tokens, args.flags, and the drift snapshot so any
    # caller that re-runs substitutions (e.g. cake's two-stage parse for
    # findtargets) sees a coherent post-mutation state. Idempotent when
    # nothing changed.
    if hasattr(args, "CPPFLAGS") or hasattr(args, "CFLAGS") or hasattr(args, "CXXFLAGS") or hasattr(args, "LDFLAGS"):
        _finalize_flag_state(args)


@contextlib.contextmanager
def graceful_shutdown(handler, *signums) -> Generator[None, None, None]:
    """Install *handler* for *signums*, restoring the previous handlers on exit.

    The canonical place for any ct-* tool (or library helper) to wire up
    interrupt handling. Use it like::

        with apptools.graceful_shutdown(my_handler):
            do_work()

    Why a context manager rather than bare ``signal.signal()`` calls:

    * **Restoration is automatic.** Forgetting the ``signal.signal(sig,
      prev_handler)`` line leaks the entry point's handler into the
      caller for the rest of the process. The lint test in
      ``test_entry_point_surface`` enforces this for ``--help``, but the
      context manager makes the bug structurally impossible.
    * **--help / --version safety.** ``argparse``'s ``--help`` action
      raises ``SystemExit`` *before* anything inside the ``with`` block
      runs, so a user typing ``ct-X --help`` never installs the handler.
      A bare ``signal.signal()`` line above ``parse_args`` would
      contaminate the caller (caught ``ct_lock_helper`` doing exactly
      this).
    * **Thread-aware.** ``signal.signal()`` raises ``ValueError`` off
      the main thread; this helper silently no-ops there, matching the
      pattern in ``locking.atomic_compile``.
    * **Robust to weird signums.** Platform-conditional signals
      (``SIGPIPE`` on Windows, ``SIGCHLD`` reservations under uvloop)
      that fail at install time are silently skipped rather than
      crashing the caller.

    Args:
        handler: A callable matching the ``signal.signal`` contract
            (``handler(signum, frame)``). Use the sentinels
            ``signal.SIG_DFL`` / ``signal.SIG_IGN`` if you want to
            *suppress* a signal during the block rather than handle it.
        *signums: Which signals to take over. Defaults to
            ``(SIGINT, SIGTERM)`` -- the standard "user pressed Ctrl-C
            or the process manager is asking us to stop" pair.

    Yields:
        ``None``. The body of the ``with`` block runs with the new
        handlers active.

    Restored handlers come back even if the body raises. Errors during
    restoration (mismatched handler shapes, signal already gone) are
    suppressed -- the caller's original handler may already be invalid
    if the process is in shutdown, and propagating would mask the body's
    real exception.
    """
    if not signums:
        signums = (signal.SIGINT, signal.SIGTERM)

    # Dedupe while preserving order. Without this, a contrived but legal
    # ``graceful_shutdown(h, SIGINT, SIGINT)`` would record
    # ``saved=[(SIGINT, original), (SIGINT, h)]`` and the restore loop
    # would re-install ``h`` last — leaking the body's handler past the
    # with-block exit. ``dict.fromkeys`` is the standard order-preserving
    # dedupe in 3.7+.
    signums = tuple(dict.fromkeys(signums))

    saved = []  # list of (signum, previous_handler); previous_handler matches signal.Handlers

    # ``signal.signal`` raises ``ValueError`` outside the main thread.
    # Skip the install entirely there -- mirrors ``locking.atomic_compile``
    # and ``trace_backend``'s behaviour.
    if threading.current_thread() is threading.main_thread():
        for sig in signums:
            try:
                saved.append((sig, signal.signal(sig, handler)))
            except (ValueError, OSError):
                # ValueError: signum not in the platform's valid set.
                # OSError: kernel-level rejection (rare, but seen with
                # SIGCHLD under some sandbox runners).
                pass

    try:
        yield
    finally:
        for sig, prev in saved:
            # Restoration is best-effort. ``TypeError`` covers prev being
            # a non-callable sentinel that signal.signal rejects on the
            # restore call (rare, but possible on platforms that gave us
            # back an int constant on the install side); raising here
            # would mask any genuine exception bubbling out of the body.
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, prev)


def parseargs(cap, argv, verbose=None, *, context):
    """argv must be the logical equivalent of sys.argv[1:]

    Args:
        context: BuildContext for per-build state. Stored as args._context
            and used by substitution callbacks (e.g. to set up project-level
            pkg-config overrides).
    """
    # command-line values override environment variables which override config file values which override defaults.
    args = cap.parse_args(args=argv)
    args._parser = cap
    args._context = context
    # Stash the original argv so post-parse code paths (notably the second
    # resolve_variant call in _commonsubstitutions) can route through the
    # explicit_config branch when --config=path was supplied. Without it,
    # the re-resolve would try to look up the implied basename as an axis
    # and raise VariantResolutionError.
    args._argv = argv

    if "verbose" not in vars(args):
        raise ValueError(
            "verbose was not found in args. Fix is to call apptools.add_common_arguments "
            "or apptools.add_base_arguments before calling parseargs"
        )

    # Propagate --allow-fake-git into the git_utils module-level setting
    # BEFORE any downstream find_git_root() call inside substitutions /
    # anchor_root computation. The resolver
    # ``resolve_cas_directory_arguments`` ALSO calls this (so that
    # diagnostic-only tools bypassing parseargs still get the flag
    # honoured), but we propagate it here as well so any other
    # find_git_root() callsite reached before the resolver runs (inside
    # _commonsubstitutions) sees the post-parse value. set_allow_fake_git
    # clears the @functools.cache when the value actually changes, so
    # earlier strict-mode lookups don't poison subsequent permissive ones.
    compiletools.git_utils.set_allow_fake_git(getattr(args, "allow_fake_git", False))

    if verbose is None:
        verbose = args.verbose

    # configargparse only applies the "override" method to environment-sourced
    # variables, so when the user asks for "append" we partially undo that in
    # _fix_variable_handling_method. This would be simpler if configargparse
    # natively supported an "append" variable-handling method for env vars.
    if args.variable_handling_method == "append":
        args = _fix_variable_handling_method(cap, argv, verbose)
    _flatten_variables(args)
    _strip_quotes(args)

    if verbose > 8:
        print(f"Parsing commandline arguments has occured. Before substitutions args={args}")

    # Set CXX default if not specified and a functional compiler is available
    if hasattr(args, "CXX") and args.CXX is None:
        functional_compiler = get_functional_cxx_compiler()
        if functional_compiler:
            args.CXX = functional_compiler
            if verbose >= 6:
                print(f"Set CXX to detected functional compiler: {functional_compiler}")
        else:
            raise RuntimeError("No functional C++ compiler detected. Please set CXX explicitly.")

    if verbose > 8:
        print(f"Parsing functioanl compiler has been set. Before substitutions args={args}")

    substitutions(args, verbose)

    # After substitutions canonicalise args.variant and finalise the raw
    # compile flags, validate that the resolved compiler is actually
    # usable for what the variant requested. Three checks:
    #   1. Binary on PATH? — catches "user picked --variant=gcc.* but
    #      gcc isn't installed" (would otherwise be a generic compile
    #      failure with no pointer at the variant chain).
    #   2. Wild linker usable? — catches a missing `wild` binary or the
    #      `wild` axis paired with gcc < 16 (which can't drive
    #      -fuse-ld=wild), before the link fails opaquely.
    #   3. Compiler version supports the requested -std=c++NN? — catches
    #      "user picked cxx26 on a system with gcc 11" (would otherwise
    #      be an opaque "unrecognized command line option '-std=c++26'"
    #      from the compiler).
    # All three checks emit a clear diagnostic naming the variant and
    # suggesting either a different variant or a toolchain upgrade.
    _check_resolved_compiler_available(args)
    _check_wild_linker_usable(args)
    _check_compiler_supports_requested_standard(args)

    # Populate tokenized flag lists alongside the raw strings. Consumers
    # that only need tokens (e.g. build_backend compile commands,
    # magicflags._parse) can use these directly without re-tokenizing on
    # every call. Tokens are populated AFTER all parseargs mutations
    # (env vars, INCLUDE injection, project version macros, pkg-config,
    # CPP/CXX unification) so they reflect the final raw-string state.
    #
    # WARNING: do not mutate args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}
    # after parseargs returns. args.{*}_tokens and args.flags are
    # populated once here and will silently drift from the raw strings
    # if those strings are modified later. All known mutation sites are
    # in this function (substitutions, _add_include_paths_to_flags,
    # project version macros, pkg-config, CPP/CXX unification) and run
    # BEFORE this point.
    _finalize_flag_state(args)

    if verbose > 8:
        print("parseargs has completed.  Returning args")
    return args


def _finalize_flag_state(args) -> None:
    """Populate args.{*}_tokens, args.flags, and the drift snapshot.

    Internal post-parseargs plumbing: called once at the end of
    parseargs. Tests that bypass parseargs (constructing args via
    cap.parse_args / SimpleNamespace) should go through
    ``testhelper.finalize_flag_state`` rather than touching this
    directly. After this returns, args.{*}_tokens lists and args.flags
    (a Flags dataclass) reflect the slots registered by the caller's
    CAP. Slots not registered (e.g. compilation_database omits LDFLAGS)
    are left absent from the drift snapshot so check_flag_string_drift
    can distinguish "not applicable here" from "mutated after parseargs".
    Any subsequent in-place mutation of a registered raw string will be
    flagged by check_flag_string_drift.
    """
    # Compute the CAP-registered slot set ONCE and make it sticky.
    # The materialise step below synthesizes missing slot attributes for
    # downstream consumers; a second call (e.g. via substitutions() re-run
    # from cake's two-stage parse) would then see those synthesized attrs
    # via hasattr and silently expand the registered set, defeating the
    # "not applicable here" signal. Reading from args preserves the
    # original CAP-registration view across re-runs.
    registered = getattr(args, "_registered_flag_slots", None)
    if registered is None:
        registered = tuple(slot for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS") if hasattr(args, slot))
        args._registered_flag_slots = registered
    # Materialise raw strings and *_tokens for ALL four slots so existing
    # consumers (build_backend, magicflags, ...) don't need to handle the
    # absent-slot case. Only registered slots are snapshotted for drift.
    for slot in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
        raw = getattr(args, slot, "") or ""
        if not hasattr(args, slot):
            setattr(args, slot, raw)
        setattr(args, f"{slot}_tokens", compiletools.utils.split_command_cached(raw))
    args.flags = Flags.from_args(args)
    args._flag_string_snapshot = tuple((slot, getattr(args, slot)) for slot in registered)


def check_flag_string_drift(args) -> None:
    """Verify args.{CPPFLAGS,...} have not been mutated since parseargs end.

    parseargs records a snapshot of the raw flag strings as
    ``args._flag_string_snapshot`` immediately after populating
    ``args.{*}_tokens`` and ``args.flags``. If anything later assigns to
    ``args.CPPFLAGS`` (etc.) the tokens and flags will silently drift
    out of sync with the raw string. This function compares the current
    raw strings to the snapshot and raises ``RuntimeError`` if they
    differ, naming the offending slot.

    Call this from any consumer that wants to assert the invariant
    holds. Tests in particular should call it before reading
    ``args.flags`` if they construct ``args`` via a path that may
    mutate flag strings.
    """
    snapshot = getattr(args, "_flag_string_snapshot", None)
    if snapshot is None:
        return
    for slot, expected in snapshot:
        actual = getattr(args, slot, None)
        if actual != expected:
            raise RuntimeError(
                f"args.{slot} mutated after parseargs end: "
                f"args.flags and args.{slot}_tokens are now stale. "
                f"All mutations to compile-flag raw strings must occur "
                f"BEFORE parseargs returns."
            )


def terminalcolumns():
    """How many columns in the text terminal"""
    try:
        result = subprocess.run(["stty", "size"], capture_output=True, text=True, check=True)
        columns = int(result.stdout.split()[1])
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, IndexError, ValueError):
        columns = 80
    return columns


def verboseprintconfig(args):
    if args.verbose >= 3:
        print(" ".join(["Using variant =", args.variant]))
        args._parser.print_values()

    if args.verbose >= 2:
        verbose_print_args(args)


# Secret-carrying arg attrs: their values are replaced with a placeholder in
# `-vv` output so credentials (auth headers, etc.) don't land in CI logs.
_REDACTED_ARG_ATTRS = frozenset({"otel_headers"})
_REDACTED_PLACEHOLDER = "***REDACTED***"


def verbose_print_args(args):
    # Print the args in two columns Attr: Value
    print("\n\nFinal aggregated variables for build:")
    maxattrlen = max(map(len, args.__dict__), default=0)
    fmt = f"{{0:{maxattrlen + 1}}}: {{1}}"
    rightcolbegin = maxattrlen + 3
    maxcols = terminalcolumns()
    rightcolsize = maxcols - rightcolbegin
    if maxcols <= rightcolbegin:
        print("Verbose print of args aborted due to small terminal size!")
        return

    for attr, value in sorted(args.__dict__.items()):
        if value is None:
            print(fmt.format(attr, ""))
            continue
        if attr in _REDACTED_ARG_ATTRS and value:
            print(fmt.format(attr, _REDACTED_PLACEHOLDER))
            continue
        strvalue = str(value)
        if rightcolbegin + len(strvalue) < maxcols:
            print(fmt.format(attr, strvalue))
        else:
            # Value too long for one line: wrap on spaces to the right column
            # width (long spaceless tokens like paths stay unbroken).
            wrapped = textwrap.wrap(strvalue, width=rightcolsize, break_long_words=False, break_on_hyphens=False)
            print(fmt.format(attr, wrapped[0]))
            for line in wrapped[1:]:
                print(fmt.format("", line))
