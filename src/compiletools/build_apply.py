"""Impure apply layer: executes BuildState.effects and populates the
legacy args surface during the consumer-migration period."""

from __future__ import annotations

import os
import shutil

from compiletools.build_state import BuildState, EnsureLinkerSymlinkDir, SetEnv


def apply_effects(state: BuildState, context) -> None:
    """Execute state.effects against the live process (env, filesystem).

    SetEnv mirrors _setup_pkg_config_overrides_locked's save-original
    protocol: context._original_pkg_config_path is set only for
    PKG_CONFIG_PATH and only when the value actually changes, to True
    when the var was previously unset (so restore_pkg_config_path can
    tell "delete it" from "put this string back").

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
                if effect.name == "PKG_CONFIG_PATH":
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


_SLOT_TO_TOKENS = {
    "CPPFLAGS": "cpp",
    "CFLAGS": "c",
    "CXXFLAGS": "cxx",
    "LDFLAGS": "ld",
}


def populate_args(args, state: BuildState) -> None:
    """Write the post-parseargs legacy surface from a BuildState.

    All four slots' raw strings and *_tokens lists are materialized
    unconditionally (matching _finalize_flag_state's "materialise for
    all four, snapshot only the registered ones" split) so downstream
    consumers that read an unregistered slot still see a well-formed
    empty value rather than an AttributeError.
    """
    strings = {
        "CPPFLAGS": state.cppflags,
        "CFLAGS": state.cflags,
        "CXXFLAGS": state.cxxflags,
        "LDFLAGS": state.ldflags,
    }
    for slot, ts_field in _SLOT_TO_TOKENS.items():
        setattr(args, slot, strings[slot])
        setattr(args, f"{slot}_tokens", list(getattr(state.tokens, ts_field)))
    args.flags = state.flags
    args.variant = state.names.variant
    args.bindir = state.names.bindir
    args.cas_objdir = state.names.cas_objdir
    args.cas_pchdir = state.names.cas_pchdir
    args.cas_pcmdir = state.names.cas_pcmdir
    args.cas_exedir = state.names.cas_exedir
    args._flag_string_snapshot = tuple(
        (slot, strings[slot]) for slot in _SLOT_TO_TOKENS if slot in state.registered_slots
    )
