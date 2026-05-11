"""Structured representation of compile-flag state.

A Flags instance holds the four flag categories as token tuples, plus
the compiler identity. It centralizes the operations the codebase has
historically scattered across apptools, build_backend, and magicflags:
tokenization, -D/-U stripping, hash-relevance filtering, and include-
path inspection.

Flags is INSTANTIATED ONCE per build (at parseargs end) and stored on
args.flags. Existing args.CPPFLAGS / args.CPPFLAGS_tokens etc. are
kept for backward compat. New code should prefer args.flags.

Flags is frozen and uses tuple slots so it is hashable, equality-safe,
and immune to in-place mutation by consumers. Mutation-style helpers
(e.g. append_include) return a NEW Flags via dataclasses.replace.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field


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

    @classmethod
    def from_args(cls, args) -> Flags:
        """Build a Flags from a parsed args object.

        Requires args.{CPPFLAGS,CFLAGS,CXXFLAGS,LDFLAGS}_tokens to have
        been populated (parseargs does this; testhelper.create_args
        mirrors it). Raises AttributeError otherwise -- callers must go
        through parseargs / create_args, not construct args ad hoc.

        The compiler_identity is computed against the gitroot anchor so
        in-workspace wrapper scripts (coverage / sccache / distcc shims)
        canonicalise to ``<GITROOT>/...`` instead of leaking the per-
        checkout absolute path into every downstream cache key.
        """
        from compiletools.apptools import compiler_identity
        from compiletools.git_utils import find_git_root

        cxx_command = getattr(args, "CXX", "") or ""
        anchor_root = find_git_root() or ""
        return cls(
            cpp=tuple(args.CPPFLAGS_tokens),
            c=tuple(args.CFLAGS_tokens),
            cxx=tuple(args.CXXFLAGS_tokens),
            ld=tuple(args.LDFLAGS_tokens),
            compiler_identity=compiler_identity(cxx_command, anchor_root=anchor_root),
        )

    def hash_relevant(self, slot: str) -> list[str]:
        """Return tokens for the given slot with -D/-U and diagnostic-only
        flags removed. Used by cache-key hashing.

        slot: one of "cpp", "c", "cxx", "ld".
        """
        from compiletools.apptools import filter_hash_irrelevant_tokens, strip_d_u_tokens

        stripped = strip_d_u_tokens(getattr(self, slot))
        return filter_hash_irrelevant_tokens(stripped)

    def existing_include_paths(self, slot: str) -> set[str]:
        """Return the set of -I paths (attached or detached) in the given
        slot's tokens."""
        from compiletools.apptools import extract_include_paths_from_tokens

        return extract_include_paths_from_tokens(getattr(self, slot))

    def append_include(self, path: str, slots: Iterable[str] = ("cpp", "c", "cxx")) -> Flags:
        """Return a new Flags with ``-I path`` (detached form) appended to
        each named slot, but only for slots where path isn't already
        present as an -I entry. Slots that already contain the path are
        left unchanged.
        """
        from compiletools.apptools import dedup_include_paths_to_append

        updates: dict[str, tuple[str, ...]] = {}
        for slot in slots:
            tokens = getattr(self, slot)
            added = dedup_include_paths_to_append(tokens, (path,))
            if added:
                updates[slot] = tokens + tuple(added)
        if not updates:
            return self
        return dataclasses.replace(self, **updates)
