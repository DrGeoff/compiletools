import argparse
import contextlib
import logging
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
from collections.abc import Generator

import stringzilla as sz

import compiletools.apptools_argparse
import compiletools.apptools_compiler

# Re-exported from the leaf apptools_pkgconfig module so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and test/patch targets keep working with identical object
# identity. All are pure re-exports consumed only by external modules /
# tests (``_setup_pkg_config_overrides`` and ``_add_flags_from_pkg_config``
# lost their internal callers when the legacy pipeline was deleted), so they
# carry the redundant ``name as name`` alias to mark them as intentional
# re-exports for the F401 linter. ``_PKG_CONFIG_OVERRIDE_LOCK`` is the single
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
# ``_fix_variable_handling_method`` (called by ``parseargs``) has a live
# internal caller that stays in apptools, so it is a plain import referenced
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

# ``_fix_variable_handling_method`` is referenced by *bare name* from code
# that stays in apptools (``parseargs``), hence no ``as`` alias. See the
# re-export rationale comment above the first apptools_argparse import.
from compiletools.apptools_argparse import (
    _fix_variable_handling_method,
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
    resolve_cas_directory_arguments as resolve_cas_directory_arguments,
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
from compiletools.apptools_canonicalize import (
    _PREFIX_MAP_FLAG_PREFIXES as _PREFIX_MAP_FLAG_PREFIXES,
)

# Re-exported from the leaf apptools_canonicalize module so existing
# ``apptools.<name>`` call sites, ``from compiletools.apptools import ...``
# importers, and test/patch targets keep working with identical object
# identity. ``_PREFIX_MAP_FLAG_PREFIXES`` has a live internal caller in this
# module (``_has_prefix_map_flag`` reads it) — plain import. The rest are
# pure re-exports (only consumed by external modules / docstrings), so they
# carry the redundant ``name as name`` alias to mark them as intentional
# re-exports for the F401 linter.
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
# identity. All are pure re-exports consumed only by external modules /
# docstrings (``compiler_kind``'s production caller is
# ``build_inputs.gather_inputs``, which imports the leaf directly), so they
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
    compiler_kind as compiler_kind,
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
    _add_flags_from_pkg_config as _add_flags_from_pkg_config,
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
    _setup_pkg_config_overrides as _setup_pkg_config_overrides,
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
from compiletools.apptools_pkgconfig import (
    tokenize_pkg_config_specs as tokenize_pkg_config_specs,
)

# Re-exported from the leaf apptools_validate module so existing
# ``apptools.<name>`` call sites (``parseargs`` reads these as module
# globals), ``from compiletools.apptools import ...``
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
from compiletools.flag_ops import (
    dedup_include_paths_to_append as dedup_include_paths_to_append,
)

# Re-exported from the leaf flag_ops module so existing
# ``apptools.<name>`` call sites and test/patch targets keep working with
# identical object identity. ``extract_include_paths_from_tokens`` is a
# pure re-export (no internal apptools caller), hence the explicit
# redundant alias to mark it as an intentional re-export for linters.
from compiletools.flag_ops import (
    extract_d_macros,
    filter_hash_irrelevant_tokens,
    strip_d_u_tokens,
    system_include_paths_from_tokens,
)
from compiletools.flag_ops import (
    extract_include_paths_from_tokens as extract_include_paths_from_tokens,
)
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


def _collect_explicit_target_files(args):
    """All explicit target filenames: positional, --static, --dynamic, --tests."""
    targets = []
    for attr in ("filename", "static", "dynamic", "tests"):
        value = getattr(args, attr, None)
        if value:
            targets.extend(value)
    return targets


def _target_conf_filenames(variant, argv):
    """Conf filenames the current variant resolution consults, in flat_paths
    order (ct.conf, then each axis conf, then the composite conf)."""
    resolution = compiletools.configutils.resolve_variant(variant=variant, argv=argv)
    filenames = ["ct.conf"]
    filenames.extend(f"{axis.name}.conf" for axis in resolution.axes)
    if resolution.canonical_name and "." in resolution.canonical_name:
        filenames.append(f"{resolution.canonical_name}.conf")
    return tuple(dict.fromkeys(filenames))


def _target_value_flags_from_parser(cap):
    """Every option string whose action collects target files (dest in
    tests/static/dynamic), so remedy-command argv filtering recognizes all
    synonyms (--begintests, --static-library, --dynamic-library) without a
    hand-maintained list that can drift from the parser."""
    flags = []
    for action in cap._actions:
        if action.dest in ("tests", "static", "dynamic"):
            flags.extend(action.option_strings)
    return tuple(dict.fromkeys(flags))


def _registered_conf_keys_and_canonicalizers(cap):
    """Normalized (dash->underscore) config keys the parser accepts, plus a
    per-key value canonicalizer derived from each action's type. Flag and
    boolean actions (store_true/store_false and the nargs='?' to_bool form)
    canonicalize through utils.to_bool -- the same coercion
    add_flag_argument/add_boolean_argument register -- so boolean synonyms
    (``true`` vs ``1``) compare equal across subproject confs."""
    registered_keys = set()
    canonicalizers = {}
    for action in cap._actions:
        canonicalizer = None
        # Private argparse classes, stable across CPython releases; the
        # public alternative (inspecting action.const) misclassifies
        # store_const actions with boolean consts.
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            canonicalizer = compiletools.utils.to_bool
        elif callable(action.type):
            canonicalizer = action.type
        for key in cap.get_possible_config_keys(action):
            if key.startswith("-"):
                continue
            normalized = key.replace("-", "_")
            registered_keys.add(normalized)
            if canonicalizer is not None:
                canonicalizers[normalized] = canonicalizer
    return registered_keys, canonicalizers


# Defensive bound shared by the target-anchored config fixpoint here and the
# --auto discovery re-anchor loop in findtargets. Termination is guaranteed by
# strict growth of cap._default_config_files (fresh-path realpath dedup), so
# the cap is purely a runaway backstop; it must exceed any plausible legal
# ancestor-chain depth (a 10-deep nested-subproject layout is legal; 100 is
# not plausible). Exhaustion is a hard error -- a silent stop would ship a
# half-anchored config.
_MAX_TARGET_CONF_ROUNDS = 100


def _fixpoint_not_converged_error(what, detail):
    """Shared "fixpoint did not converge" RuntimeError for both bounded
    loops, so the two messages cannot drift."""
    return RuntimeError(f"{what} did not converge after {_MAX_TARGET_CONF_ROUNDS} rounds; {detail}")


def _apply_target_conf_layers(cap, argv, args, verbose, auto=False, reparse=True):
    """Load explicit targets' subproject config layers to a fixpoint.

    Same-tier contradiction (cwd layer vs target layer, target vs target) is
    rendered to stderr with its remedy commands and exits via SystemExit(1) --
    the argparse convention, so every ct-* entry point gets traceback-free
    rendering without per-tool handlers. At verbose >= 2 the underlying
    ConfContradictionError propagates instead so the traceback is available.
    Returns *args* unchanged when the walk surfaces nothing new -- the common
    case costs only the ancestor walk.

    *auto* is True on the ``--auto`` re-anchor path (targets came from
    discovery, not argv); the remedy commands then take the ``cd`` + fresh
    discovery form rather than argv target filtering.

    *reparse* False widens ``cap._default_config_files`` (after validation)
    but performs only a single widening round, returning *args* unchanged --
    for callers that re-run the full ``parseargs`` themselves and would
    discard the namespace. Their follow-up ``parseargs`` re-enters this
    function, whose fixpoint then continues where the single round stopped.

    Fixpoint: a target injected BY a freshly loaded layer (a subproject
    ct.conf's own ``tests = foo.cpp`` -- ``tests``/``static``/``dynamic``
    are config-file-settable keys) appears on the re-parsed namespace, so
    the next round walks its ancestor layers too. Terminates because
    ``cap._default_config_files`` grows strictly each round and is bounded
    by the conf files on the targets' ancestor chains; contradiction
    validation runs over the layers accumulated across ALL rounds, so a
    round-2 layer conflicting with a round-1 layer errors identically to
    the single-round case. The accumulation persists on the parser
    (``cap._ct_loaded_target_layers``) across ``parseargs`` invocations:
    the ``--auto`` driver re-enters via a fresh ``parseargs`` per
    discovery round, and a layer loaded in an earlier driver round is
    already in ``cap._default_config_files`` (so the freshness filter
    skips it) yet still shapes the build -- without the persisted list a
    later round's layer could contradict it silently.

    Outside a git repository ``find_git_root()`` falls back to the cwd, so
    ``cwd == gitroot`` and the cwd layer never participates in same-tier
    validation -- the cwd conf is then the fallback project tier, which a
    target layer legitimately overrides via re-parse ordering.
    """
    if getattr(cap, "_default_config_files", None) is None:
        return args
    context = args._context

    # cwd layer participates in same-tier comparison only when cwd is not
    # the gitroot: gitroot confs are the project tier, which subproject
    # layers legitimately override. Computed once: later rounds only ever
    # add target layers, never cwd-layer entries.
    cwd = compiletools.wrappedos.realpath(os.getcwd())
    gitroot = compiletools.wrappedos.realpath(compiletools.git_utils.find_git_root())
    cwd_layer_paths = []
    if cwd != gitroot:
        for path in cap._default_config_files:
            real = compiletools.wrappedos.realpath(path)
            if os.path.dirname(real) == cwd or os.path.dirname(real) == os.path.join(cwd, "ct.conf.d"):
                cwd_layer_paths.append(real)
    cwd_layer_dir = cwd if cwd_layer_paths else None

    if not hasattr(cap, "_ct_loaded_target_layers"):
        cap._ct_loaded_target_layers = []
    loaded_layers = cap._ct_loaded_target_layers
    for _round in range(_MAX_TARGET_CONF_ROUNDS):
        targets = _collect_explicit_target_files(args)
        if not targets:
            return args

        default_config_files = cap._default_config_files
        conf_filenames = _target_conf_filenames(args.variant, argv)
        git_bounded = getattr(args, "git_root", True)
        layers = compiletools.configutils.walk_target_conf_layers(
            targets, conf_filenames, verbose=verbose, git_bounded=git_bounded
        )

        loaded = {compiletools.wrappedos.realpath(p) for p in default_config_files}
        new_layers = []
        for layer in layers:
            fresh = tuple(p for p in layer.conf_paths if compiletools.wrappedos.realpath(p) not in loaded)
            if fresh:
                new_layers.append(
                    compiletools.configutils.TargetConfLayer(
                        subproject_dir=layer.subproject_dir,
                        conf_paths=fresh,
                        anchor_targets=layer.anchor_targets,
                        git_bounded=layer.git_bounded,
                        above_home=layer.above_home,
                    )
                )
        if not new_layers:
            return args
        # Validate the candidate set BEFORE accumulating, so a caught
        # contradiction leaves no rejected layers on the parser.
        candidate_layers = loaded_layers + new_layers
        remedy_commands = compiletools.configutils.build_separate_build_commands(
            os.path.basename(sys.argv[0]) if sys.argv else "ct-cake",
            list(argv),
            candidate_layers,
            targets,
            cwd_layer_dir=cwd_layer_dir,
            auto=auto,
            target_value_flags=_target_value_flags_from_parser(cap),
        )
        registered_keys, value_canonicalizers = _registered_conf_keys_and_canonicalizers(cap)
        try:
            compiletools.configutils.validate_no_conf_contradictions(
                candidate_layers,
                cwd_layer_paths,
                args.variant,
                remedy_commands,
                registered_keys=registered_keys,
                value_canonicalizers=value_canonicalizers,
            )
        except compiletools.configutils.ConfContradictionError as err:
            if verbose >= 2:
                raise
            print(str(err), file=sys.stderr)
            raise SystemExit(1) from None
        loaded_layers.extend(new_layers)
        # Fresh, validated layers only: emitting before validation would
        # re-warn on a retried parse after a caught contradiction, and
        # re-emitting for already-loaded layers would repeat the warning
        # every fixpoint round.
        compiletools.configutils.emit_unbounded_walk_notices(new_layers, verbose)

        new_paths = [p for layer in new_layers for p in layer.conf_paths]
        _check_legacy_cas_config_keys(new_paths)
        _check_legacy_variant_config_keys(new_paths)
        _note_case_mismatched_conf_keys(cap, new_paths, verbose)
        if verbose >= 1:
            # A vestigial subproject ct.conf that was inert before target
            # anchoring now changes flags; naming the anchoring target and
            # the loaded files makes a mysteriously changed build
            # self-diagnosing, and the invocation-wide scope answers "why
            # did that conf affect THIS translation unit".
            for layer in new_layers:
                anchors = " ".join(layer.anchor_targets)
                scalars = sorted(
                    raw_key
                    for normalized, (raw_key, _, _) in compiletools.configutils._effective_layer_values(
                        layer.conf_paths
                    ).items()
                    if not normalized.startswith(("append_", "prepend_"))
                )
                scalar_note = f" (scalar keys: {' '.join(scalars)})" if scalars else ""
                print(
                    f"ct: note: target {anchors} anchored config layer {layer.subproject_dir}: "
                    f"loaded {' '.join(layer.conf_paths)}; these settings apply to the whole invocation"
                    f"{scalar_note}",
                    file=sys.stderr,
                )
            if verbose >= 2 and cwd_layer_paths and _round == 0:
                print(
                    f"ct: note: cwd config layer {cwd} participates in same-tier contradiction "
                    f"validation against the target-anchored layers",
                    file=sys.stderr,
                )
        cap._default_config_files = list(default_config_files) + new_paths
        if not reparse:
            return args
        args = cap.parse_args(args=argv)
        _stash_private_attrs(args, cap, context, argv)
    raise _fixpoint_not_converged_error(
        "target-anchored config discovery",
        f"last layers: {' '.join(layer.subproject_dir for layer in loaded_layers)}",
    )


def _note_case_mismatched_conf_keys(cap, conf_paths, verbose):
    """At ``verbose >= 1``, note conf keys that miss a registered key only by
    case (or dash/underscore spelling).

    Conf keys are case-sensitive and ``create_parser`` sets
    ``ignore_unknown_config_file_keys``, so ``append-cppflags`` is silently
    dropped where ``append-CPPFLAGS`` would apply -- a no-op invisible at the
    point of damage. Genuinely unknown keys stay silent (they may belong to
    another ct-* tool sharing the conf file); only near-misses are noted.
    """
    if verbose < 1:
        return

    def _fold(key):
        return key.lower().replace("_", "-")

    registered = {}
    for action in cap._actions:
        for key in cap.get_possible_config_keys(action):
            if not key.startswith("-"):
                registered.setdefault(_fold(key), key)
    for path in conf_paths:
        try:
            items = compiletools.configutils._parse_conf_file_cached(path)
        except OSError:
            continue
        for key in items:
            canonical = registered.get(_fold(key))
            if canonical is not None and canonical != key:
                print(
                    f"ct: note: conf key {key!r} in {path} is not registered and was "
                    f"ignored (conf keys are case-sensitive); did you mean {canonical!r}?",
                    file=sys.stderr,
                )


def reanchor_config_for_discovered_targets(args):
    """Re-anchor config discovery after ``--auto`` target discovery.

    ``findtargets.process`` assigns discovered targets onto an
    already-parsed namespace, so their subproject conf layers were invisible
    to the parse-time anchoring in ``parseargs``. Re-run the same walk; when
    it surfaces new layers, re-run the full ``parseargs`` with the widened
    config set. This is a pure config re-anchor: the returned namespace
    holds only argv/conf-level target lists, NOT the discovered targets --
    the caller (``findtargets.discover_targets_and_reanchor``) re-discovers
    under the new config, so re-applying stale targets here would both
    duplicate (``FindTargets.process`` appends) and bypass any freshly
    loaded discovery-affecting keys (``exemarkers``/``testmarkers``/
    ``disable-tests``).

    Returns the fresh namespace, or ``None`` when nothing new was found
    (fixpoint -- the caller keeps its namespace, discovered targets intact).

    A namespace built without going through ``parseargs`` (e.g. a test
    double's hand-built ``SimpleNamespace``) lacks the stashed
    ``_parser``/``_argv``/``_context`` attributes; there is nothing to
    re-anchor against in that case, so this is a no-op.
    """
    cap = getattr(args, "_parser", None)
    argv = getattr(args, "_argv", None)
    if cap is None or argv is None:
        return None

    before = list(getattr(cap, "_default_config_files", []) or [])
    # reparse=False: the internal re-parse would be discarded anyway --
    # parseargs below re-runs the whole pipeline over the widened config set.
    _apply_target_conf_layers(cap, argv, args, args.verbose, auto=True, reparse=False)
    after = list(getattr(cap, "_default_config_files", []) or [])
    if after == before:
        return None

    # The first parseargs latched context.pkg_config_overrides_applied, so
    # the re-run below would early-return and drop any prepend-/append-
    # PKG-CONFIG-PATH contributed by the freshly loaded target layers.
    # Restore resets the latch (and un-mutates PKG_CONFIG_PATH) so the
    # re-apply sees the pre-override environment plus the widened conf set.
    args._context.restore_pkg_config_path()
    return parseargs(cap, argv, context=args._context)


_FLAG_SOURCE_TO_SLOT = {"CPPFLAGS": "cpp", "CFLAGS": "c", "CXXFLAGS": "cxx", "LDFLAGS": "ld"}


def _state_slot_tokens(args, flag_sources):
    """(legacy slot name, token tuple) pairs read from the stashed BuildState.

    The flag helpers below keep their historical ``flag_sources`` vocabulary
    (legacy slot names) but read the authoritative token tuples from
    ``args._build_state`` instead of re-tokenizing the legacy raw strings.
    Namespaces that never went through populate_args get the named
    ``get_build_state`` error pointing at ``testhelper.finalize_flag_state``.
    """
    import compiletools.build_apply

    flags = compiletools.build_apply.get_build_state(args).flags
    return [(name, getattr(flags, _FLAG_SOURCE_TO_SLOT[name])) for name in flag_sources]


def extract_system_include_paths(args, flag_sources=None, verbose=0):
    """Extract -I and -isystem include paths from the build-state flags.

    Args:
        args: Namespace carrying a stashed BuildState (post parseargs /
            testhelper.finalize_flag_state)
        flag_sources: List of legacy slot names to extract from
            (default: ['CPPFLAGS', 'CXXFLAGS'])
        verbose: Verbosity level for debugging

    Returns:
        List of unique include paths in order
    """
    if flag_sources is None:
        flag_sources = ["CPPFLAGS", "CXXFLAGS"]

    # Walk each slot separately -- concatenating the slots would let a
    # malformed dangling -I at the end of one slot swallow the first
    # token of the next. Dedup across slots at the end.
    include_paths = []
    for _, slot_tokens in _state_slot_tokens(args, flag_sources):
        include_paths.extend(system_include_paths_from_tokens(slot_tokens))
    include_paths = list(dict.fromkeys(include_paths))

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
    """Extract -D macro definitions from the build-state flags.

    Args:
        args: Namespace carrying a stashed BuildState (post parseargs /
            testhelper.finalize_flag_state)
        flag_sources: List of legacy slot names to extract from
            (default: ['CPPFLAGS', 'CFLAGS', 'CXXFLAGS'])
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

    # extract_d_macros recognizes attached and detached -D forms; it must
    # stay form-consistent with cmdline_d_macro_names or a macro would be
    # absent from one macro universe but present in the other, defeating
    # the cache-key scoping.
    for flag_name, slot_tokens in _state_slot_tokens(args, flag_sources):
        for macro_name, macro_value in extract_d_macros(slot_tokens).items():
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
        args: Namespace carrying a stashed BuildState (post parseargs /
            testhelper.finalize_flag_state)
        flag_sources: List of legacy slot names to extract from
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
    for flag_name, slot_tokens in _state_slot_tokens(args, flag_sources):
        for macro_name in extract_d_macros(slot_tokens):
            names.add(macro_name)
            if verbose >= 9:
                print(f"cmdline_d_macro_names: added {macro_name} from {flag_name}")

    return frozenset(sz.Str(name) for name in names)


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


def _do_xxpend_list(args, name, destname=None):
    """List-typed sibling of ``build_state.stage_xxpend`` for attrs whose
    canonical form is a Python list (e.g. ct-cake's hook-script lists),
    not a flag string. The base attr is read from
    ``args.<destname or name.replace('-','_')>``, and the prepend/append
    sources from ``args.{prepend,append}_<destname or name.replace('-','_')>``.

    Mirrors the stage's dedup-and-place rule (prepend leftmost,
    append rightmost, skip duplicates already present in the list
    accumulated so far — prepend extras merge into the base before the
    append pass, so append entries dedup against base *plus* prepend
    contributions, but never against other entries of their own group)
    so consumers of ``--prepend-PKG-CONFIG`` / ``--append-PKG-CONFIG``
    get the same composition semantics that compiler-flag slots have.

    Dedup compares elements verbatim, so a caller whose conf surface can
    spell one logical entry more than one way normalises the three attrs
    before calling (see the pkg-config call site).
    """
    dest = (destname or name).lower().replace("-", "_")
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


def _note_shadowed_bare_hook_values(args, name, dest):
    """At ``verbose >= 1``, emit a stderr note for each conf-file value of
    the bare hook key *name* that did not survive into ``args.<dest>``.

    The bare key is documented last-writer-wins, and ``<name> = []``
    suppression is a documented feature — so this is a note, not a
    warning, and it is silent by default. It exists because the failure
    mode the semantics enable (a hook silently never running) is
    otherwise invisible at the point of damage. Fires for any losing
    value — a later line in the same conf file as well as a
    higher-priority layer/env winner, hence the "later or
    higher-priority" wording. Uses the conf-file provenance side channel
    (``_ComposingArgumentParser.get_conf_file_provenance``); env-var and
    CLI winners don't appear there, so the note cannot name the winner.
    Provenance values predate ``_strip_quotes``, so both sides of the
    membership test are normalised through ``_safely_unquote_string`` —
    without that, a quoted conf value that survived (post-strip) would be
    misreported as discarded.

    Must run AFTER ``_do_xxpend_list`` for *name* so values that lost the
    bare-key contest but re-entered via ``append-``/``prepend-`` are not
    misreported as discarded. Emits at most once per (args, key):
    ``substitutions()`` re-runs tier-one after external fetches widen
    INCLUDE, and the note would otherwise repeat.
    """
    if getattr(args, "verbose", 0) < 1:
        return
    emitted = getattr(args, "_hook_shadow_notes_emitted", None)
    if emitted is None:
        emitted = set()
        args._hook_shadow_notes_emitted = emitted
    if name in emitted:
        return
    emitted.add(name)
    parser = getattr(args, "_parser", None)
    if parser is None or not hasattr(parser, "get_conf_file_provenance"):
        return
    try:
        provenance = parser.get_conf_file_provenance()
    except Exception:
        # Same verbose >= 1 gate as the note itself (checked at function
        # entry): a silently-failed lookup is indistinguishable from
        # "nothing was shadowed", which is the ambiguity this note exists
        # to remove.
        print(
            f"ct: note: hook-shadow provenance lookup failed for {name!r}; skipping note",
            file=sys.stderr,
        )
        return
    final = {_safely_unquote_string(v) for v in (getattr(args, dest, []) or [])}
    for value, source_file, lineno, _literal in provenance.get(name, []):
        if _safely_unquote_string(value) not in final:
            print(
                f"ct: note: {name} = {value} (from {source_file}:{lineno}) was discarded "
                f"by a later or higher-priority {name} setting (bare keys are "
                f"last-writer-wins; use append-{name.upper()} to accumulate instead)",
                file=sys.stderr,
            )


def _strip_quotes(args):
    """Remove shell quotes from arguments while preserving content quotes.

    Uses proper shell parsing to understand when quotes are shell quoting
    vs. part of the actual content. Also strips extraneous whitespace.

    Private attributes (leading underscore) are internal stashes, not
    user-supplied argument values, and are skipped: iterating-and-
    assigning a dict stash inserts keys mid-iteration (RuntimeError), a
    tuple stash rejects item assignment (TypeError), and _argv must stay
    verbatim for the variant re-resolve.
    """
    for name in vars(args):
        if name.startswith("_"):
            continue
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
        # A single shlex token means the outer quotes were shell quotes;
        # multiple tokens mean the quotes delimit content, so the value is
        # returned unchanged.
        tokens = split_command_cached(value)
        if len(tokens) == 1:
            unquoted = tokens[0]

            # Nested quoting (e.g. conf value '"-DFOO"' quoted once by the
            # conf layer and once by the user) leaves a quote pair per layer;
            # recurse until no enclosing pair remains.
            if (unquoted.startswith('"') and unquoted.endswith('"')) or (
                unquoted.startswith("'") and unquoted.endswith("'")
            ):
                return _safely_unquote_string(unquoted)
            return unquoted
        return value
    except ValueError:
        # Malformed quoting that shlex rejects: strip only a matching
        # enclosing quote pair rather than failing the whole parse.
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            return value[1:-1].strip()
        return value.strip("\"'").strip()


def _flatten_variables(args):
    """Flatten list-valued flag variables into the single space-separated
    string the rest of the code base expects. These variables are registered
    with ``nargs="+"``, so argparse stores a list of tokens; downstream
    consumers read one string.

    Uses ``shlex.join`` (not ``' '.join``) so that list elements containing
    shell-special characters (embedded spaces, double-quotes, etc.) survive the
    subsequent ``split_command_cached`` tokenization in ``gather_inputs``.
    When the user passes ``--CPPFLAGS '-DFOO=bar baz'`` on the CLI, the shell
    consumes the outer quotes and argparse stores ``'-DFOO=bar baz'`` as a
    single list element; ``' '.join`` would produce ``'-DFOO=bar baz -Wall'``
    (unsplit on the space), and shlex-splitting would then misparse it as
    three tokens. ``compute_build_state`` applies the same ``shlex.join`` rule
    when deriving the slot strings from token tuples.
    """
    for varname in ("CPPFLAGS", "CFLAGS", "CXXFLAGS", "INCLUDE"):
        if isinstance(getattr(args, varname, None), list):
            setattr(args, varname, shlex.join(getattr(args, varname)))


def resubstitute(args) -> None:
    """Re-run the pure build-state core on *args* — the only sanctioned
    re-run path (cake's second-stage target discovery, the //#GIT=
    external fetch, compilation_database's --auto refresh).

    Post-swap this is re-gather + recompute, not a legacy
    ``substitutions()`` replay. None of ``parseargs``'s pre-gather
    namespace steps (quiet latch, variant-resolution stash, CPP/LD
    exe-name substitution, hook-script lists, preprocess/magic aliasing,
    test-xml-dir anchoring) need replaying here: none of them feed
    ``BuildInputs`` — verified directly against ``gather_inputs``/
    ``BuildInputs`` — so their absence changes nothing gather reads.

    The four raw flag slots (CPPFLAGS/CFLAGS/CXXFLAGS/LDFLAGS) need no
    handling here at all: ``populate_args`` never writes them (the
    derived surface lives only on the stashed BuildState), so the live
    attrs still hold the pre-parse raw values and ``gather_inputs``
    rebuilds from the same base by construction. (Re-gathering from
    derived strings would reproduce the same token SET but a different
    token ORDER — a genuine first pass lands ``stage_include_paths``'
    ``-I`` pairs ahead of the accumulated CXXFLAGS tail because CPPFLAGS
    is still short pre-unify — and since flag-slot hashing is
    argv-shaped, that reorder would fork the object CAS key between
    --auto and --no-auto builds of the same sources.) ``args.INCLUDE``
    is the one genuine between-pass input.

    The re-run is therefore a fixed point BY CONSTRUCTION, not by a
    seed-restore-and-diff guard: the ``RuntimeError`` drift check the
    legacy path enforced here is retired. ``cache_naming_view`` is logged
    at ``verbose >= 2`` as an informational before/after diff — since
    gather is a pure function of the (possibly caller-mutated) namespace,
    any observed change reflects the caller's own edit, never corruption.
    """
    from compiletools.build_apply import apply_effects, get_build_state, populate_args
    from compiletools.build_inputs import gather_inputs
    from compiletools.build_state import cache_naming_view, compute_build_state

    context = args._context
    prior_view = cache_naming_view(get_build_state(args))

    inputs = gather_inputs(args, context)
    state = compute_build_state(inputs)
    apply_effects(state, context)
    populate_args(args, state)

    if args.verbose >= 2:
        new_view = cache_naming_view(state)
        if new_view != prior_view:
            print(
                "resubstitute: cache-naming view changed across the re-run "
                f"(informational only, not an error):\n  before: {prior_view}\n  after:  {new_view}"
            )


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


def _stash_private_attrs(args, cap, context, argv):
    """Attach the private post-parse attributes to a freshly parsed namespace.

    Every namespace ``parseargs`` finishes needs all three:

    * ``_parser`` — read by ``verboseprintconfig`` (``print_values()`` at
      verbose >= 3) and passed into ``_setup_pkg_config_overrides_locked``
      for conf-file provenance attribution at verbose >= 4.
    * ``_context`` — read unconditionally by the pkg-config override
      setup in parseargs' pre-gather block and by ``resubstitute``;
      missing it is an AttributeError.
    * ``_argv`` — routes the pre-gather ``resolve_variant`` (the ``-vv``
      provenance trace) through the explicit_config branch when
      ``--config=path`` was supplied, and lets a CLI
      ``--variant-canonical-order`` reach the re-canonicalization
      (absent, the flag is silently ignored and the canonical dotted
      variant — hence CAS dir suffixes — can shift).

    MUST be called after every ``cap.parse_args`` that produces the
    namespace ``parseargs`` returns. The append-mode reparse in
    ``_fix_variable_handling_method`` returns a fresh namespace, so the
    stashes have to be applied twice; keeping them in one helper stops the
    two sites drifting apart (the stash set grew one attribute at a time
    across three commits, and each grower updated only the first site —
    that drift is exactly the bug that broke append mode).
    """
    args._parser = cap
    args._context = context
    args._argv = argv


def parseargs(cap, argv, verbose=None, *, context):
    """argv must be the logical equivalent of sys.argv[1:]

    Runs the pure build-state core: parse -> pre-gather namespace steps ->
    ``gather_inputs`` (the impure boundary) -> ``compute_build_state``
    (pure) -> ``apply_effects`` / ``populate_args`` -> compiler checks.
    The legacy ``substitutions()`` pipeline is no longer called here; it
    remains as the reference pipeline for the differential suite and the
    transition-period ``resubstitute`` re-run path.

    Args:
        context: BuildContext for per-build state. Stored as args._context;
            owns the PKG_CONFIG_PATH restore sentinel and the pkg-config
            query memo.
    """
    # Deferred imports: build_inputs/build_state/build_apply import from
    # apptools (sentinels, helpers), so top-level imports here would cycle.
    from compiletools.build_apply import apply_effects, populate_args
    from compiletools.build_inputs import gather_inputs
    from compiletools.build_state import compute_build_state

    # Console entry points pass argv=None meaning "use sys.argv". Normalize
    # here so everything downstream (target-anchored conf discovery, the
    # _argv stash the --auto re-anchor reads) sees a real list -- argparse
    # only resolves None internally, which left _argv=None and disabled the
    # re-anchor on the real CLI.
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    # command-line values override environment variables which override config file values which override defaults.
    args = cap.parse_args(args=argv)
    _stash_private_attrs(args, cap, context, argv)

    if "verbose" not in vars(args):
        raise ValueError(
            "verbose was not found in args. Fix is to call apptools.add_common_arguments "
            "or apptools.add_base_arguments before calling parseargs"
        )

    # Propagate --allow-fake-git into the git_utils module-level setting
    # BEFORE any downstream find_git_root() call -- gather_inputs calls
    # find_git_root for gitroot/cas anchoring/include widening, and the
    # target-conf walk below calls it even earlier. set_allow_fake_git
    # clears the @functools.cache when the value actually changes, so
    # earlier strict-mode lookups don't poison subsequent permissive ones.
    compiletools.git_utils.set_allow_fake_git(getattr(args, "allow_fake_git", False))

    if verbose is None:
        verbose = args.verbose

    # Target-anchored config discovery: explicit targets outside the cwd
    # subproject pull in their nearest-ancestor ct.conf / ct.conf.d layer.
    # Raises ConfContradictionError when same-tier layers disagree.
    args = _apply_target_conf_layers(cap, argv, args, verbose)

    # configargparse only applies the "override" method to environment-sourced
    # variables — it has no native "append" method for env vars — so when the
    # user asks for "append", _fix_variable_handling_method partially undoes
    # the override and reparses.
    if args.variable_handling_method == "append":
        args = _fix_variable_handling_method(cap, argv, verbose)
        _stash_private_attrs(args, cap, context, argv)
    _flatten_variables(args)
    _strip_quotes(args)

    if verbose > 8:
        print(f"Parsing commandline arguments has occured. Before build-state core args={args}")

    # Set CXX default if not specified and a functional compiler is available
    if hasattr(args, "CXX") and args.CXX is None:
        functional_compiler = get_functional_cxx_compiler()
        if functional_compiler:
            args.CXX = functional_compiler
            if verbose >= 6:
                print(f"Set CXX to detected functional compiler: {functional_compiler}")
        else:
            raise RuntimeError("No functional C++ compiler detected. Please set CXX explicitly.")

    # ---- Pre-gather namespace steps (see the swap inventory) -------------
    # Each mutates args state that is out of BuildState's cell-naming scope
    # (or is an impure diagnostic); gather_inputs reads the result.

    # Apply --quiet to --verbose exactly once per namespace and latch it:
    # gather honours _quiet_applied, so an unlatched decrement would
    # double-subtract on every re-gather. args.verbose is what every
    # downstream consumer reads; populate_args does not carry verbose.
    if not getattr(args, "_quiet_applied", False):
        args.verbose -= args.quiet
        args._quiet_applied = True

    # Variant-resolution provenance for the -vv trace. The stash's only
    # production consumer is the print itself; resolve_variant splits and
    # canonicalizes its own input, and the canonical dotted variant the
    # build uses comes from stage_resolve_names via populate_args.
    args._variant_resolution = compiletools.configutils.resolve_variant(
        variant=args.variant, argv=getattr(args, "_argv", None)
    )
    if args.verbose >= 2 and args._variant_resolution is not None:
        print(compiletools.configutils.format_variant_resolution(args._variant_resolution))

    # CPP/LD *executable names*. BuildState deliberately does not model
    # them (build_inputs docstring); the flag-slot fallback halves live in
    # stage_defaults.
    if hasattr(args, "CPP"):
        args.CPP = unsupplied_replacement(args.CPP, args.CXX, args.verbose, "CPP")
    if hasattr(args, "LD"):
        args.LD = unsupplied_replacement(args.LD, args.CXX, args.verbose, "LD")

    # ct-cake's hook lists: bare prebuild-script / postbuild-script conf
    # keys are last-writer-wins; append-/prepend- accumulates. hasattr-
    # guarded because only ct-cake registers these arguments.
    if hasattr(args, "prebuild_scripts"):
        _do_xxpend_list(args, "prebuild-script", destname="prebuild-scripts")
        _note_shadowed_bare_hook_values(args, "prebuild-script", "prebuild_scripts")
    if hasattr(args, "postbuild_scripts"):
        _do_xxpend_list(args, "postbuild-script", destname="postbuild-scripts")
        _note_shadowed_bare_hook_values(args, "postbuild-script", "postbuild_scripts")

    # Cake used preprocess to mean both magic flag preprocess and headerdeps preprocess
    if hasattr(args, "preprocess") and args.preprocess:
        args.magic = "cpp"
        args.headerdeps = "cpp"

    # Anchor --test-xml-dir to gitroot so the value survives a `cd` into a
    # subdirectory between parseargs and the build. Out of BuildState's
    # scope (names no CAS cell); re-run-safe via the isabs gate.
    test_xml_dir = getattr(args, "test_xml_dir", None)
    if test_xml_dir and not os.path.isabs(test_xml_dir):
        git_root = compiletools.git_utils.find_git_root()
        if git_root:
            args.test_xml_dir = os.path.join(git_root, test_xml_dir)
        else:
            args.test_xml_dir = os.path.abspath(test_xml_dir)

    # ---- The functional build-state core ---------------------------------
    # gather_inputs owns every ambient read (env, filesystem, git,
    # pkg-config subprocesses); compute_build_state is a pure function of
    # the result; apply_effects executes the effects (PKG_CONFIG_PATH
    # SetEnv, wild-B symlink dir); populate_args writes the legacy args
    # surface (raw slot strings, *_tokens, args.flags, variant/bindir/
    # cas-*dir names, the drift snapshot).
    inputs = gather_inputs(args, context)
    state = compute_build_state(inputs)
    apply_effects(state, context)
    populate_args(args, state)

    # With args.variant canonicalised and the raw compile flags final,
    # validate that the resolved compiler is actually usable for what the
    # variant requested. Three checks:
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
    #
    # The raw args.{CPPFLAGS,...} attrs keep their pre-gather values --
    # populate_args stashes the derived state on args._build_state and
    # never overwrites the slots, so a later resubstitute re-gathers
    # from the same base by construction.
    _check_resolved_compiler_available(args)
    _check_wild_linker_usable(args)
    _check_compiler_supports_requested_standard(args)

    if verbose > 8:
        print("parseargs has completed.  Returning args")
    return args


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
