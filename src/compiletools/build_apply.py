"""Impure apply layer: executes BuildState.effects, stashes the
BuildState on args, and writes the resolved name attrs."""

from __future__ import annotations

import os
import shutil

from compiletools.build_state import BuildState, EnsureLinkerSymlinkDir, SetEnv


def configure_pkg_config_errors(args) -> None:
    """Apply the parsed pkg-config failure policy before any probes run.

    A namespace whose parser never registered ``--pkg-config-errors`` carries
    no policy, so it leaves the process-global one as it stands. Substituting
    the ``warn`` default here disarmed strict mode for the rest of the
    process on the second ``parseargs`` of any base-arguments-only tool --
    the same silent-disarm hazard ``apptools_pkgconfig.clear_cache``
    deliberately avoids.
    """
    from compiletools.apptools_pkgconfig import set_pkg_config_errors

    errors = getattr(args, "pkg_config_errors", None)
    if errors is None:
        return
    set_pkg_config_errors(errors)


def apply_effects(state: BuildState, context) -> None:
    """Execute state.effects against the live process (env, filesystem).

    SetEnv mirrors _setup_pkg_config_overrides_locked's save-original
    protocol: context._original_pkg_config_path is set only for
    PKG_CONFIG_PATH and only when the value actually changes, to True
    when the var was previously unset (so restore_pkg_config_path can
    tell "delete it" from "put this string back"). The save is guarded
    on the context's current sentinel being None (its BuildContext
    __init__ default) rather than on attribute presence, and applies
    once per context: a second apply_effects call against the same
    context (cake's --auto / //#GIT= re-run flows both call it again)
    must not overwrite an already-recorded original with an
    intermediate value from the first call.

    EnsureLinkerSymlinkDir ports the filesystem half of
    _materialize_wild_b_searchdir: the effect's target is a bare
    executable name ("wild"), resolved via shutil.which the same way
    the original resolves it before symlinking. If which() can't find
    it, the original returns None before creating anything -- no
    directory, no symlink -- and this branch matches that: the effect
    is skipped entirely.
    """
    for effect in state.effects:
        if isinstance(effect, SetEnv):
            existing = os.environ.get(effect.name)
            if existing != effect.value:
                if effect.name == "PKG_CONFIG_PATH" and context._original_pkg_config_path is None:
                    context._original_pkg_config_path = existing if existing is not None else True
                os.environ[effect.name] = effect.value
        elif isinstance(effect, EnsureLinkerSymlinkDir):
            resolved_target = shutil.which(effect.target)
            if resolved_target is None:
                continue
            os.makedirs(effect.directory, exist_ok=True)
            link = os.path.join(effect.directory, effect.link_name)
            # lexists, not exists: a dangling symlink (target since removed)
            # must still count as "present" so this stays a create-once op,
            # never silently overwriting a link a peer process may be using.
            if not os.path.lexists(link):
                os.symlink(resolved_target, link)


def populate_args(args, state: BuildState) -> None:
    """Stash the BuildState and write the resolved name attrs.

    The flag surface is state-only: consumers read
    ``get_build_state(args).flags`` / ``.cppflags`` etc., and the raw
    ``args.{CPPFLAGS,...}`` attrs keep their pre-gather values — never
    overwritten — so ``gather_inputs`` re-reads the same base on every
    re-run (resubstitute's fixed point needs no record/restore
    machinery, and an unsupplied sentinel survives on the attr with its
    meaning intact).

    The name attrs (variant, bindir, cas-*dirs) ARE written: they are
    idempotent under re-gather (canonical variant re-canonicalizes to
    itself; resolved dirs pass through the sentinel checks unchanged)
    and have live consumers outside the state — diagnostics.py reads
    ``args.bindir``, Namer's permanent fallback reads ``args.bindir`` /
    ``args.cas_objdir`` for resolver-only diagnostic tools.

    The stash is refreshed on EVERY call so consumers always see the
    current pass's state after a re-run.
    """
    args._build_state = state
    args.variant = state.names.variant
    args.bindir = state.names.bindir
    args.cas_objdir = state.names.cas_objdir
    args.cas_pchdir = state.names.cas_pchdir
    args.cas_pcmdir = state.names.cas_pcmdir
    args.cas_exedir = state.names.cas_exedir


def get_build_state(args) -> BuildState:
    """Return the BuildState populate_args stashed on *args*.

    The single flag/name read path: consumers read
    state.names/state.flags through this accessor.
    Raises a named error (not a bare AttributeError) when the namespace
    never went through populate_args -- almost always a test fixture
    that built args by hand; route it through parseargs or
    testhelper.finalize_flag_state.
    """
    state = getattr(args, "_build_state", None)
    if state is None:
        raise RuntimeError(
            "args carries no BuildState: this namespace never went through "
            "populate_args (parseargs/resubstitute). Test fixtures that "
            "construct args by hand must run it through parseargs or "
            "testhelper.finalize_flag_state before handing it to a "
            "BuildState-consuming module."
        )
    return state
