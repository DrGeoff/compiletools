import argparse
import functools
import importlib.util
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import warnings
from collections.abc import Sequence

# Only used for the verbose print.
import configargparse
import stringzilla as sz

import compiletools.configutils
import compiletools.git_utils
import compiletools.utils
import compiletools.wrappedos
from compiletools.flags import Flags
from compiletools.utils import split_command_cached
from compiletools.version import __version__

_rich_rst_available = importlib.util.find_spec("rich_rst") is not None

if _rich_rst_available and sys.version_info >= (3, 9):

    class DocumentationAction(argparse.BooleanOptionalAction):
        def __init__(self, option_strings, dest):
            super().__init__(
                option_strings=option_strings,
                dest=dest,
                default=None,
                required=False,
                help="Show the documentation/manual page",
            )

        def __call__(self, parser, namespace, values, option_string=None):
            if option_string in self.option_strings and not option_string.startswith("--no-"):
                import inspect

                import rich
                from rich_rst import RestructuredText

                this_dir = os.path.dirname(compiletools.wrappedos.realpath(inspect.getsourcefile(lambda: 0)))
                doc_filename = os.path.join(this_dir, f"README.{parser.prog}.rst")
                try:
                    with open(doc_filename) as docfile:
                        text = docfile.read()
                        rich.print(RestructuredText(text))
                except FileNotFoundError:
                    rich.print("No man/doc available")

                sys.exit(0)


def parser_has_option(cap, option_string):
    """Check whether *cap* already has an action for *option_string*.

    Used by every backend's ``add_arguments(cap)`` to remain idempotent.
    """
    return any(option_string in a.option_strings for a in cap._actions)


# Backwards-compat alias for callers that imported the underscored form.
_parser_has_option = parser_has_option


def add_base_arguments(cap, argv=None, variant=None):
    """All compiletools applications MUST call this function.

    Note that it is usually called indirectly from add_common_arguments.

    Safe to call more than once on the same parser — duplicate calls are
    silently ignored.
    """
    if _parser_has_option(cap, "--variant"):
        return

    # Even though the variant is actually sucked out of the command line by
    # parsing the sys.argv directly, we put it into the configargparse to get
    # the help.
    if variant is None:
        variant = compiletools.configutils.extract_variant(argv=argv)

    cap.add(
        "--variant",
        help="Specifies which variant of the config should be used. Use the config name without the .conf",
        default=variant,
    )
    cap.add(
        "-v",
        "--verbose",
        help="Output verbosity. Add more v's to make it more verbose",
        action="count",
        default=0,
    )
    cap.add(
        "-q",
        "--quiet",
        help="Decrement verbosity. Useful in apps where the default verbosity > 0.",
        action="count",
        default=0,
    )
    cap.add("--version", action="version", version=__version__)
    cap.add("-?", action="help", help="Help")

    if _rich_rst_available and sys.version_info >= (3, 9):
        cap.add("--man", "--doc", action=DocumentationAction)


def _add_xxpend_argument(cap, name, destname=None, extrahelp=None):
    """Add a prepend flags argument and an append flags argument to the config arg parser"""
    if destname is None:
        destname = name

    if extrahelp is None:
        extrahelp = ""

    xxlist = ("prepend", "append")
    for xx in xxlist:
        cap.add(
            "".join(["--", xx, "-", name.upper()]),
            dest="_".join([xx, destname.lower().replace("-", "_")]),
            action="append",
            default=[],
            help=" ".join(
                [
                    xx.title(),
                    "the given text to the",
                    name.upper(),
                    "already set. Useful for adding search paths etc.",
                    extrahelp,
                ]
            ),
        )


def _add_xxpend_arguments(cap, xxpendableargs):
    """Add prepend-BLAH and append-BLAH for the common flags"""
    for arg in xxpendableargs:
        _add_xxpend_argument(cap, arg)


def add_common_arguments(cap, argv=None, variant=None):
    """Insert common arguments into the configargparse object.

    Safe to call more than once on the same parser — duplicate calls are
    silently ignored.
    """
    if _parser_has_option(cap, "--variable-handling-method"):
        return
    add_base_arguments(cap, argv=argv, variant=variant)
    cap.add(
        "--variable-handling-method",
        dest="variable_handling_method",
        help="Does specifying --<someflag> (say CXXFLAGS) mean override existing flags "
        "or append to the existing? Choices are override or append.",
        default="override",
    )
    cap.add(
        "--ID",
        help="Compiler identification string.  The same string as CMake uses.",
        default=None,
    )
    cap.add("--CPP", help="C preprocessor (override)", default="unsupplied_implies_use_CXX")
    cap.add("--CC", help="C compiler (override)", default="gcc")
    # Default will be set later using functional compiler detection
    cap.add("--CXX", help="C++ compiler (override)", default=None)
    cap.add(
        "--CPPFLAGS",
        nargs="+",
        help="C preprocessor flags (override)",
        default="unsupplied_implies_use_CXXFLAGS",
    )
    cap.add("--CXXFLAGS", nargs="+", help="C++ compiler flags (override)", default="-fPIC -g -Wall")
    cap.add("--CFLAGS", nargs="+", help="C compiler flags (override)", default="-fPIC -g -Wall")
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="git-root",
        dest="git_root",
        default=True,
        help="Determine the git root then add it to the include paths.",
    )
    cap.add(
        "--INCLUDE",
        "--include",
        dest="INCLUDE",
        nargs="+",
        default="",
        help="Extra path(s) to add to the list of include paths (override)",
    )
    cap.add(
        "--pkg-config",
        dest="pkg_config",
        help="Query pkg-config to obtain libs and flags for these packages.",
        action="append",
        default=[],
    )
    _add_xxpend_argument(
        cap,
        "pkg-config-path",
        extrahelp="Directories are applied to the PKG_CONFIG_PATH environment variable.",
    )
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="separate-flags-CPP-CXX",
        dest="separate_flags_CPP_CXX",
        default=False,
        help="Keep CPPFLAGS and CXXFLAGS separate instead of unified.",
    )
    compiletools.git_utils.NameAdjuster.add_arguments(cap)
    _add_xxpend_arguments(cap, xxpendableargs=("include", "cppflags", "cflags", "cxxflags"))
    add_locking_arguments(cap)


def add_locking_arguments(cap):
    """Add file locking configuration arguments.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--file-locking"):
        return
    compiletools.utils.add_boolean_argument(
        parser=cap,
        name="file-locking",
        dest="file_locking",
        default=True,
        help="Enable file locking for concurrent multi-user/multi-host builds",
    )
    cap.add(
        "--lock-cross-host-timeout",
        type=int,
        default=600,
        help="Timeout in seconds for cross-host locks before escalating warnings (default: 600 = 10 min)",
    )
    cap.add(
        "--lock-warn-interval",
        type=int,
        default=60,
        help="Interval in seconds between lock wait warnings (default: 60)",
    )
    cap.add(
        "--sleep-interval-lockdir",
        type=float,
        default=None,
        help="Sleep interval for lockdir polling (NFS/Lustre) (default: auto-detect based on filesystem)",
    )
    cap.add(
        "--sleep-interval-cifs",
        type=float,
        default=0.2,
        help="Sleep interval for CIFS lock polling (default: 0.2)",
    )
    cap.add(
        "--sleep-interval-flock-fallback",
        type=float,
        default=0.1,
        help="Sleep interval for flock fallback polling (default: 0.1)",
    )


def add_link_arguments(cap):
    """Insert the link arguments into the parser.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--LD"):
        return
    cap.add("--LD", help="Linker (override)", default="unsupplied_implies_use_CXX")
    cap.add(
        "--LDFLAGS",
        "--LINKFLAGS",
        help="Linker flags (override)",
        default="unsupplied_implies_use_CXXFLAGS",
    )
    _add_xxpend_argument(cap, "ldflags")
    _add_xxpend_argument(
        cap,
        "linkflags",
        destname="ldflags",
        extrahelp="Synonym for setting LDFLAGS.",
    )


def add_output_directory_arguments(cap, variant):
    if _parser_has_option(cap, "--bindir"):
        return
    cap.add(
        "--bindir",
        help="Output directory for executables",
        default="".join(["bin/", variant]),
    )
    git_root = compiletools.git_utils.find_git_root()
    if git_root:
        default_cas_objdir = os.path.join(git_root, "cas-objdir", variant)
    else:
        default_cas_objdir = "".join(["bin/", variant, "/obj"])
    cap.add(
        "--cas-objdir",
        help="Output directory for object files (content-addressable store)",
        default=default_cas_objdir,
    )
    if git_root:
        default_cas_pchdir = os.path.join(git_root, "cas-pchdir", variant)
    else:
        default_cas_pchdir = os.path.join("bin", variant, "pch")
    cap.add(
        "--cas-pchdir",
        help="Output directory for precompiled header cache (content-addressable store)",
        default=default_cas_pchdir,
    )


def add_target_arguments(cap):
    """Insert the arguments that control what targets get created.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--dynamic"):
        return
    cap.add("filename", nargs="*", help="File(s) to compile to an executable(s)")
    cap.add(
        "--dynamic",
        "--dynamic-library",
        nargs="*",
        help="File(s) to compile to a dynamic library",
    )
    cap.add(
        "--static",
        "--static-library",
        nargs="*",
        help="File(s) to compile to a static library",
    )
    cap.add("--tests", nargs="*", help="File(s) to compile to a test and then execute")


def add_target_arguments_ex(cap):
    """Add the target arguments and the extra arguments that augment
    the target arguments.

    Safe to call more than once on the same parser.
    """
    if _parser_has_option(cap, "--TESTPREFIX"):
        return
    add_target_arguments(cap)
    cap.add(
        "--TESTPREFIX",
        help='Runs tests with the given prefix, eg. "valgrind --quiet --error-exitcode=1"',
    )
    cap.add(
        "--project-version",
        dest="projectversion",
        help="Set the CAKE_PROJECT_VERSION macro to this value",
    )
    cap.add(
        "--project-version-cmd",
        dest="projectversioncmd",
        help="Execute this command to determine the CAKE_PROJECT_VERSION macro",
    )


def unsupplied_replacement(variable, default_variable, verbose, variable_str):
    """If a given variable has the letters "unsupplied" in it
    then return the given default variable.
    """
    replacement = variable
    if "unsupplied" in variable:
        replacement = default_variable
        if verbose >= 6:
            print(" ".join([variable_str, "was unsupplied. Changed to use ", default_variable]))
    return replacement


def _substitute_CXX_for_missing(args):
    """If C PreProcessor variables (and the same for the LD*) are not set
    but CXX ones are set then just use the CXX equivalents
    """
    if args.verbose > 8:
        print("Using CXX variables as defaults for missing C, CPP, LD variables")
    args.CPP = unsupplied_replacement(args.CPP, args.CXX, args.verbose, "CPP")
    args.CPPFLAGS = unsupplied_replacement(args.CPPFLAGS, args.CXXFLAGS, args.verbose, "CPPFLAGS")
    try:
        args.LD = unsupplied_replacement(args.LD, args.CXX, args.verbose, "LD")
    except AttributeError:
        pass
    try:
        args.LDFLAGS = unsupplied_replacement(args.LDFLAGS, args.CXXFLAGS, args.verbose, "LDFLAGS")
    except AttributeError:
        pass


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
            args.INCLUDE = " ".join(args.INCLUDE.split() + list(git_roots))
            if args.verbose > 6:
                print(f"Extended includes to have the gitroots {git_roots}")
        else:
            raise ValueError(
                "args.git_root is True but no git roots found. :( .  If this is expected then specify --no-git-root."
            )


def extract_include_paths_from_tokens(tokens) -> set[str]:
    """Return the set of -I paths (attached or detached form) in tokens.

    Recognises ``-I/p``, ``-I /p`` (two-token detached form), and
    ``-Idir`` only -- not ``-isystem`` or ``-L`` (those are different
    flag families). Used by include-path dedup helpers in apptools and
    flags.py.
    """
    paths: set[str] = set()
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-I" and i + 1 < n:
            paths.add(tokens[i + 1])
            i += 2
        elif tok.startswith("-I") and len(tok) > 2:
            paths.add(tok[2:])
            i += 1
        else:
            i += 1
    return paths


def dedup_include_paths_to_append(existing_tokens, new_paths) -> list[str]:
    """Return tokens to append (in detached ``-I path`` form) to add
    ``new_paths`` to ``existing_tokens`` without duplicating any path
    already present as a -I entry.
    """
    seen = extract_include_paths_from_tokens(existing_tokens)
    out: list[str] = []
    for path in new_paths:
        if path in seen:
            continue
        out.extend(("-I", path))
        seen.add(path)
    return out


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
                    print("Warning: No functional C++ compiler detected. Skipping compiler macros.")

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


def strip_d_u_tokens(tokens: Sequence[str]) -> list[str]:
    """Strip ``-D`` and ``-U`` entries (in both attached and detached
    forms) from a pre-tokenized flag sequence.

    This is the strip-only half of :func:`tokenize_compile_flags`,
    extracted so that callers that already hold a pre-tokenized list
    or tuple (e.g. ``magicflags._parse``, ``_pch_command_hash``,
    ``Flags.hash_relevant``) don't have to pay the tokenization cost
    a second time.

    Both attached form (``-DFOO``, ``-DFOO=bar``, ``-UFOO``) and
    detached form (``-D FOO``, ``-D FOO=bar``, ``-U FOO``) are
    stripped. Detached form drops both the flag token and the
    following value token. A dangling ``-D`` / ``-U`` at the end of
    the list drops just the flag token. All other flags (``-I``,
    ``-O``, ``-std``, ``-W``, ``-f``...) pass through unchanged.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-D" or tok == "-U":
            # Detached form: skip flag and the next token (value).
            # Dangling flag at end of list: skip just the flag.
            i += 2
            continue
        if tok.startswith("-D") or tok.startswith("-U"):
            # Attached form: skip this single token.
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


# Flag-prefix classification: tokens whose presence/value never affects
# the compiled object bytes. Excluded from cache-key hashing so that
# changing a warning level doesn't trigger a rebuild.
#
# These cover the GCC/Clang diagnostic and verbosity ecosystem:
# - -W*: warnings (pure diagnostic; -Werror is the one exception, see below)
# - -fdiagnostics-*, -fmessage-length=, -fno-show-column,
#   -fno-diagnostics-show-option, -fcaret-diagnostics,
#   -fno-color-diagnostics, -fcolor-diagnostics: message formatting
# - -pipe: tells compiler to use pipes for I/O between stages
# - -v / --verbose: prints the compile invocation
# - --help / -###: introspection-only
# Prefix-matched diagnostic flag families: any token starting with one
# of these strings is hash-irrelevant. -W and -fdiagnostics- are open-
# ended families (-Wall, -Wextra, -Wno-foo, -fdiagnostics-color, ...),
# so prefix matching is correct.
_HASH_IRRELEVANT_PREFIXES: tuple[str, ...] = (
    "-W",  # warnings (see _HASH_RELEVANT_W_FLAGS exception below)
    "-fdiagnostics-",
    "-fmessage-length=",
    "-fno-show-column",
    "-fno-diagnostics-show-option",
    "-fcaret-diagnostics",
    "-fno-color-diagnostics",
    "-fcolor-diagnostics",
)

# Exact-matched diagnostic flags: single-token flags that should NOT
# match prefix-style. e.g. ``-v`` must not silently swallow a hypothetical
# future ``-vN``-style flag, and ``-pipe`` must not match
# ``-pipefoo``. These are checked with ``tok ==`` rather than
# ``tok.startswith()`` so the match is precise.
_HASH_IRRELEVANT_EXACT: frozenset[str] = frozenset(
    {
        "-pipe",
        "-v",
        "--verbose",
        "--help",
        "-###",
    }
)

# Exception: -Werror promotes warnings to errors, which CAN affect the
# build outcome (compile fails vs succeeds). Treat -Werror and
# -Werror=<warning> as hash-relevant.
_HASH_RELEVANT_W_FLAGS: tuple[str, ...] = (
    "-Werror",
    "-Werror=",
)


def filter_hash_irrelevant_tokens(tokens: Sequence[str]) -> list[str]:
    """Remove tokens that don't affect compiled output from a flag sequence.

    Used by cache-key hashing to elide diagnostic-only flag changes.
    Accepts either a list or tuple. ``-W*`` warnings are dropped
    EXCEPT ``-Werror`` and ``-Werror=...`` (which can change compile
    outcome). Returns a NEW list; input is not mutated.
    """
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # -Werror exception: hash-relevant. ``-Werror`` itself, and the
        # ``-Werror=<warning>`` parametrized form, both promote warnings
        # to errors and thus can change build outcome.
        if any(tok == we or tok.startswith(we) for we in _HASH_RELEVANT_W_FLAGS):
            out.append(tok)
            i += 1
            continue
        # Exact-matched diagnostic flags: drop without prefix-eating risk.
        if tok in _HASH_IRRELEVANT_EXACT:
            i += 1
            continue
        # Prefix-matched diagnostic flag families: drop. None of these
        # take a separate value token in current GCC/Clang
        # (``-fmessage-length=`` is the attached form), so a single-
        # token skip suffices.
        if any(tok.startswith(prefix) for prefix in _HASH_IRRELEVANT_PREFIXES):
            i += 1
            continue
        out.append(tok)
        i += 1
    return out


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


@functools.lru_cache(maxsize=64)
def compiler_identity(cxx: str) -> str:
    """Return a stable identity string for a compiler binary.

    Used as part of cache keys (PCH cache key in ``build_backend`` and the
    per-TU object cache key via ``MacroState.compiler_identity``). Two users
    on the same shared filesystem with different ``$PATH``s could otherwise
    collide on the same key while resolving ``args.CXX`` (e.g. bare ``g++``)
    to different binaries (different versions, different stdlibs). GCC's PCH
    stamp catches this at *consume* time -- but the slow fallback compile
    defeats the cache. By including binary realpath + (st_size, st_mtime),
    we make distinct compilers produce distinct cache entries.

    Falls back to the original string when the binary cannot be stat'd
    (e.g. user passed a non-path command like ``ccache g++``). Returns
    ``""`` when ``cxx`` is None / empty so unconfigured ``args.CXX``
    (some unit-test fixtures) doesn't crash the helper.

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
        return f"{compiletools.wrappedos.realpath(resolved)}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return resolved


def clear_cache():
    """Clear any caches for macro extraction and pkg-config."""
    cached_pkg_config.cache_clear()
    _get_functional_cxx_compiler_cached.cache_clear()
    compiler_identity.cache_clear()


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
get_functional_cxx_compiler.cache_clear = _get_functional_cxx_compiler_cached.cache_clear


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
            try:
                if "obj_path" in locals():
                    os.unlink(obj_path)
            except OSError:
                pass

        return success

    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


@functools.cache
def cached_pkg_config(package, option):
    """Cache pkg-config results for package and option (--cflags or --libs)"""
    # First check if the package exists
    exists_result = subprocess.run(["pkg-config", "--exists", package], capture_output=True, check=False)
    if exists_result.returncode != 0:
        # Package doesn't exist, return empty string
        # TODO: Switch from warnings to logging for pkg-config messages
        warnings.warn(f"pkg-config package '{package}' not found", UserWarning, stacklevel=2)
        return ""

    result = subprocess.run(
        ["pkg-config", option, package],
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.rstrip()


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
        system_include_paths.add(os.path.normpath(os.path.join(prefix, "include")))

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
            normalized_path = os.path.normpath(path)
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


def _setup_pkg_config_overrides(context, verbose=0, prepend_paths=None, append_paths=None):
    """Apply project-level and CLI-specified pkg-config path overrides to PKG_CONFIG_PATH.

    Priority order (highest first):

    1. ``--prepend-PKG-CONFIG-PATH`` directories (CLI)
    2. ``<cwd>/ct.conf.d/pkgconfig/`` (project-local)
    3. ``<gitroot>/ct.conf.d/pkgconfig/`` (repo-level)
    4. Existing ``PKG_CONFIG_PATH`` entries
    5. ``--append-PKG-CONFIG-PATH`` directories (CLI)

    Args:
        context: BuildContext instance tracking per-build state.
        verbose: verbosity level for diagnostic output.
        prepend_paths: directories to prepend (from ``--prepend-PKG-CONFIG-PATH``).
        append_paths: directories to append (from ``--append-PKG-CONFIG-PATH``).

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
    """
    with _PKG_CONFIG_OVERRIDE_LOCK:
        _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths)


# Process-local serialization for the env-mutation in _setup_pkg_config_overrides.
# See the docstring of that function for the full contract.
_PKG_CONFIG_OVERRIDE_LOCK = threading.Lock()


def _setup_pkg_config_overrides_locked(context, verbose, prepend_paths, append_paths):
    """Body of _setup_pkg_config_overrides; assumes the module lock is held."""
    if context.pkg_config_overrides_applied:
        return

    gitroot = compiletools.git_utils.find_git_root()

    # Collect candidate pkgconfig directories in priority order
    # (highest priority first).
    candidates = []

    cwd_pkgconfig = os.path.join(os.getcwd(), "ct.conf.d", "pkgconfig")
    if compiletools.wrappedos.isdir(cwd_pkgconfig):
        candidates.append(os.path.normpath(cwd_pkgconfig))

    if gitroot:
        repo_pkgconfig = os.path.join(gitroot, "ct.conf.d", "pkgconfig")
        if compiletools.wrappedos.isdir(repo_pkgconfig):
            repo_pkgconfig = os.path.normpath(repo_pkgconfig)
            if repo_pkgconfig not in candidates:
                candidates.append(repo_pkgconfig)

    existing = os.environ.get("PKG_CONFIG_PATH", "")
    existing_dirs = [os.path.normpath(d) for d in existing.split(os.pathsep)] if existing else []

    # Build the final path with explicit precedence:
    #   prepend_paths (highest) > candidates > middle (existing) > append_paths
    # Each entry appears at most once. An entry that is already in
    # PKG_CONFIG_PATH gets *moved* to the requested position rather than
    # being silently dropped — so --prepend-PKG-CONFIG-PATH=/X actually
    # promotes /X to the front when /X was already present.
    prepend_normd = [os.path.normpath(d) for d in (prepend_paths or [])]
    candidates_normd = [os.path.normpath(d) for d in candidates]
    append_normd = [os.path.normpath(d) for d in (append_paths or [])]
    forced_at_end = set(append_normd)

    middle = [d for d in existing_dirs if d not in forced_at_end]

    seen: set[str] = set()
    final: list[str] = []
    for source, label in (
        (prepend_normd, "Prepended"),
        (candidates_normd, "Prepended"),
        (middle, None),
        (append_normd, "Appended"),
    ):
        for d in source:
            if not d or d in seen:
                continue
            seen.add(d)
            final.append(d)
            if label is not None and verbose >= 4:
                print(f"{label} pkg-config path: {d}")

    new_value = os.pathsep.join(final) if final else None

    # Save original ONLY if we are about to mutate, so restore_pkg_config_path
    # can faithfully undo. Set the flag AFTER the mutation succeeds so a
    # caller hitting an exception above can retry.
    if new_value is not None and new_value != existing:
        context._original_pkg_config_path = existing if "PKG_CONFIG_PATH" in os.environ else True
        os.environ["PKG_CONFIG_PATH"] = new_value

    context.pkg_config_overrides_applied = True


def _add_flags_from_pkg_config(args):
    packages = list(args.pkg_config)
    if not packages:
        return

    # Batch pkg-config calls: query all packages at once instead of one subprocess
    # per package.  Falls back to per-package calls if the batch fails (e.g. a
    # package is missing and we need to identify which one).
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
    """Query pkg-config for all *packages* at once, returning {pkg: output}.

    Fast path: validate all packages with a single ``--exists`` call, then
    query each with *option* (skipping the per-package ``--exists``).
    If the batch ``--exists`` fails, fall back to per-package cached calls
    which handle missing packages individually.
    """
    # Single --exists check for all packages at once
    exists = subprocess.run(
        ["pkg-config", "--exists"] + packages,
        capture_output=True,
        check=False,
    )
    if exists.returncode != 0:
        # At least one package is missing — fall back to per-package
        return {pkg: cached_pkg_config(pkg, option) for pkg in packages}

    # All packages exist — query each without the redundant --exists check.
    out: dict[str, str] = {}
    for pkg in packages:
        r = subprocess.run(
            ["pkg-config", option, pkg],
            stdout=subprocess.PIPE,
            text=True,
        )
        out[pkg] = r.stdout.rstrip()
    return out


def _set_project_version(args):
    """C/C++ source code can rely on the CAKE_PROJECT_VERSION macro being set.
    If the user specified a projectversion then use that.
    Otherwise execute projectversioncmd to determine projectversion.
    In the completely unspecified case, use the zero version.
    """
    # Only try to determine version if not already set
    if not (hasattr(args, "projectversion") and args.projectversion):
        try:
            args.projectversion = (
                subprocess.check_output(args.projectversioncmd.split(), universal_newlines=True).strip("\n").split()[0]
            )
            if args.verbose >= 6:
                print("Used projectversioncmd to set projectversion")
        except (subprocess.CalledProcessError, OSError) as err:
            sys.stderr.write(
                " ".join(
                    [
                        "Could not use projectversioncmd =",
                        args.projectversioncmd,
                        "to set projectversion.\n",
                    ]
                )
            )
            if args.verbose <= 2:
                sys.stderr.write(str(err) + "\n")
                sys.exit(1)
            else:
                raise
        except AttributeError:
            if args.verbose >= 6:
                print(
                    "Could not use projectversioncmd to set projectversion. "
                    "Will use either existing projectversion or the zero version."
                )

    try:
        if not args.projectversion:
            args.projectversion = "-".join([os.path.basename(os.getcwd()), "0.0.0-0"])
            if args.verbose >= 5:
                print("Set projectversion to the zero version")

        # Escape for C string literal (backslashes and double quotes)
        version_escaped = args.projectversion.replace("\\", "\\\\").replace('"', '\\"')

        if "-DCAKE_PROJECT_VERSION" not in args.CPPFLAGS:
            args.CPPFLAGS += " -DCAKE_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
        if "-DCAKE_PROJECT_VERSION" not in args.CFLAGS:
            args.CFLAGS += " -DCAKE_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')
        if "-DCAKE_PROJECT_VERSION" not in args.CXXFLAGS:
            args.CXXFLAGS += " -DCAKE_PROJECT_VERSION=" + shlex.quote(f'"{version_escaped}"')

        if args.verbose >= 6:
            print("*FLAG variables have been modified with the project version:")
            print("\tCPPFLAGS=" + args.CPPFLAGS)
            print("\tCFLAGS=" + args.CFLAGS)
            print("\tCXXFLAGS=" + args.CXXFLAGS)
    except AttributeError:
        if args.verbose >= 3:
            print("No projectversion specified for the args.")


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


def _unify_cpp_cxx_flags(args):
    """Combine CPPFLAGS and CXXFLAGS into a single deduplicated value.

    Skipped when --separate-flags-CPP-CXX is set.
    """
    if getattr(args, "separate_flags_CPP_CXX", False):
        return
    unified = " ".join(compiletools.utils.combine_and_deduplicate_compiler_flags(args.CPPFLAGS, args.CXXFLAGS))
    args.CPPFLAGS = unified
    args.CXXFLAGS = unified


def _deduplicate_all_flags(args):
    """Deduplicate all compiler and linker flags after all processing is complete"""
    flaglist = ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS")
    for flag_name in flaglist:
        if hasattr(args, flag_name):
            flag_value = getattr(args, flag_name)
            if flag_value:
                # Split the flag string into individual flags and deduplicate
                deduplicated_flags = compiletools.utils.combine_and_deduplicate_compiler_flags(flag_value)
                # Convert back to space-separated string
                setattr(args, flag_name, " ".join(deduplicated_flags))


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
    change to slip in with minimal code changes, we flatten out the list into a single string."""
    for varname in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "INCLUDE"):
        if isinstance(getattr(args, varname, None), list):
            setattr(args, varname, " ".join(getattr(args, varname)))


def _commonsubstitutions(args):
    """If certain arguments have not been specified but others have
    then there are some obvious substitutions to make
    """
    args.verbose -= args.quiet

    if args.verbose > 8:
        print("Performing common substitutions")

    # Fix the variant for any variant aliases
    # Taking the easy way out and just reparsing
    args.variant = compiletools.configutils.extract_variant()
    if args.verbose > 6:
        print(f"Determined variant to be {args.variant}")

    _tier_one_modifications(args)
    _extend_includes_using_git_root(args)
    _add_include_paths_to_flags(args)
    _setup_pkg_config_overrides(
        args._context,
        args.verbose,
        prepend_paths=getattr(args, "prepend_pkg_config_path", None),
        append_paths=getattr(args, "append_pkg_config_path", None),
    )
    _add_flags_from_pkg_config(args)
    _set_project_version(args)
    _unify_cpp_cxx_flags(args)

    try:
        # If the user didn't explicitly supply a bindir then modify the bindir to use the variant name
        args.bindir = unsupplied_replacement(args.bindir, os.path.join("bin", args.variant), args.verbose, "bindir")
    except AttributeError:
        pass

    try:
        # Same idea as the bindir modification -- use cas-objdir at git root if available
        git_root = compiletools.git_utils.find_git_root()
        if git_root:
            default_cas_objdir = os.path.join(git_root, "cas-objdir", args.variant)
        else:
            default_cas_objdir = os.path.join(args.bindir, "obj")
        args.cas_objdir = unsupplied_replacement(args.cas_objdir, default_cas_objdir, args.verbose, "cas-objdir")
    except AttributeError:
        pass

    try:
        git_root = compiletools.git_utils.find_git_root()
        if git_root:
            default_cas_pchdir = os.path.join(git_root, "cas-pchdir", args.variant)
        else:
            default_cas_pchdir = os.path.join(args.bindir, "pch")
        args.cas_pchdir = unsupplied_replacement(args.cas_pchdir, default_cas_pchdir, args.verbose, "cas-pchdir")
    except AttributeError:
        pass


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


def _fix_variable_handling_method(cap, argv, verbose):
    # TODO: FIXME: Correct fix is to have a PR into configargparse
    verbose_print = verbose > 8
    fix_keys = ["CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS", "INCLUDE"]
    for key in fix_keys:
        value = os.getenv(key)
        if value:
            appendkey = f"APPEND_{key}"
            if verbose_print:
                print(f"Changing {key=} into {appendkey} with {value=}")
            os.environ[appendkey] = value
            os.environ.pop(key)

    if verbose_print:
        print(f"{os.environ=}")
        print("_fix_variable_handling_method is forcing reparsing of cap.parse_args")
    return cap.parse_args(args=argv)


_LEGACY_CAS_KEY_RE = re.compile(r"^\s*(objdir|pchdir)\s*=", re.MULTILINE)


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
            with open(path) as fh:
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


def create_parser(description, argv=None, include_config=True, include_write_config=False):
    """Create a standardized parser with consistent compiletools behavior.

    Parameters:
            description (str): Human-readable parser description shown in --help.
            argv (list[str] | None): The command-line argv (excluding argv[0]) to use
                    when extracting the variant/config file set. If None, the current
                    process args are used by helper utilities where applicable.
            include_config (bool):
                    - True (default): Build a full config-aware parser using
                        compiletools.configutils to:
                            * extract the active variant (respecting argv), and
                            * compute default_config_files for that variant.
                        The returned parser supports -c/--config and loads defaults from
                        those config files (env vars still apply via configargparse).
                    - False: Create a simple parser and only add the base/common
                        arguments via add_base_arguments(); no variant/config file
                        plumbing is wired up.
            include_write_config (bool): If True, expose -w/--write-out-config-file
                    on the returned parser (only meaningful when include_config=True).

    Returns:
            configargparse.ArgumentParser: A configured parser ready for use with
            parseargs().

    Notes:
            - The config-aware branch sets formatter_class to
                ArgumentDefaultsHelpFormatter, provides --config, and ignores unknown
                keys in config files to keep tools resilient across versions.
            - Call add_common_arguments()/add_link_arguments()/etc. on the parser as
                needed by each tool after creation when include_config=False.
    """
    if include_config:
        variant = compiletools.configutils.extract_variant(argv=argv)
        config_files = compiletools.configutils.config_files_from_variant(variant=variant, argv=argv)
        _check_legacy_cas_config_keys(config_files)
        kwargs = {
            "description": description,
            "formatter_class": configargparse.ArgumentDefaultsHelpFormatter,
            "auto_env_var_prefix": "",
            "default_config_files": config_files,
            "args_for_setting_config_path": ["-c", "--config"],
            "ignore_unknown_config_file_keys": True,
            "conflict_handler": "resolve",
        }
        if include_write_config:
            kwargs["args_for_writing_out_config_file"] = ["-w", "--write-out-config-file"]
        return configargparse.ArgumentParser(**kwargs)
    else:
        cap = configargparse.ArgumentParser(description=description, conflict_handler="resolve")
        add_base_arguments(cap, argv=argv)
        return cap


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

    if "verbose" not in vars(args):
        raise ValueError(
            "verbose was not found in args. Fix is to call apptools.add_common_arguments "
            "or apptools.add_base_arguments before calling parseargs"
        )

    if verbose is None:
        verbose = args.verbose

    # TODO: if arg.variable_handling_method == "append" then fix up the environment
    # Note that configargparse uses the "override" method, so we need to partially undo that.
    # TODO: Write up a PR for configargparse to do override
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


def verbose_print_args(args):
    # Print the args in two columns Attr: Value
    print("\n\nFinal aggregated variables for build:")
    maxattrlen = 0
    for attr in args.__dict__:
        if len(attr) > maxattrlen:
            maxattrlen = len(attr)
    fmt = "".join(["{0:", str(maxattrlen + 1), "}: {1}"])
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
        strvalue = str(value)
        valuelen = len(strvalue)
        if rightcolbegin + valuelen < maxcols:
            print(fmt.format(attr, strvalue))
        else:
            # values are too long to fit.  Split them on spaces
            valuesplit = strvalue.split(" ", valuelen % rightcolsize)
            print(fmt.format(attr, valuesplit[0]))
            for kk in range(1, len(valuesplit)):
                print(fmt.format("", valuesplit[kk]))
