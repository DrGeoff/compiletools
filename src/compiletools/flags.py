"""Structured representation of compile-flag state.

A Flags instance holds the four flag categories as token tuples, plus
the compiler identity. It centralizes the operations the codebase has
historically scattered across apptools, build_backend, and magicflags:
tokenization, -D/-U stripping, hash-relevance filtering, and include-
path inspection.

Flags is INSTANTIATED ONCE per build, inside
``build_state.compute_build_state``; consumers reach it as
``get_build_state(args).flags`` — the flag surface is state-only, so
there is no ``args.flags`` attr.

Flags is frozen and uses tuple slots so it is hashable, equality-safe,
and immune to in-place mutation by consumers. Mutation-style helpers
(e.g. append_include) return a NEW Flags via dataclasses.replace.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field

from compiletools.flag_ops import (
    dedup_include_paths_to_append,
    extract_include_paths_from_tokens,
    filter_hash_irrelevant_tokens,
    strip_d_u_tokens,
)


@dataclass(frozen=True)
class Flags:
    """Structured compile-flag state (immutable).

    Token tuples are immutable; mutation-style helpers return a new
    Flags instance. Equality compares all five fields element-wise and
    the dataclass is hashable, so Flags can be used as a dict key or
    set member.
    """

    cpp: tuple[str, ...] = field(default_factory=tuple)
    c: tuple[str, ...] = field(default_factory=tuple)
    cxx: tuple[str, ...] = field(default_factory=tuple)
    ld: tuple[str, ...] = field(default_factory=tuple)
    compiler_identity: str = ""

    def hash_relevant(self, slot: str) -> list[str]:
        """Return tokens for the given slot with -D/-U and diagnostic-only
        flags removed. Used by cache-key hashing.

        slot: one of "cpp", "c", "cxx", "ld".
        """
        stripped = strip_d_u_tokens(getattr(self, slot))
        return filter_hash_irrelevant_tokens(stripped)

    def existing_include_paths(self, slot: str) -> set[str]:
        """Return the set of -I paths (attached or detached) in the given
        slot's tokens."""
        return extract_include_paths_from_tokens(getattr(self, slot))

    def append_include(self, path: str, slots: Iterable[str] = ("cpp", "c", "cxx")) -> Flags:
        """Return a new Flags with ``-I path`` (detached form) appended to
        each named slot, but only for slots where path isn't already
        present as an -I entry. Slots that already contain the path are
        left unchanged.
        """
        updates: dict[str, tuple[str, ...]] = {}
        for slot in slots:
            tokens = getattr(self, slot)
            added = dedup_include_paths_to_append(tokens, (path,))
            if added:
                updates[slot] = tokens + tuple(added)
        if not updates:
            return self
        return dataclasses.replace(self, **updates)
