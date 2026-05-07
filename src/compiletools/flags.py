"""Structured representation of compile-flag state.

A Flags instance holds the four flag categories as token lists, plus
the compiler identity. It centralizes the operations the codebase has
historically scattered across apptools, build_backend, and magicflags:
tokenization, -D/-U stripping, hash-relevance filtering, and include-
path inspection.

Flags is INSTANTIATED ONCE per build (at parseargs end) and stored on
args.flags. Existing args.CPPFLAGS / args.CPPFLAGS_tokens etc. are
kept for backward compat. New code should prefer args.flags.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

import compiletools.apptools


@dataclass(frozen=False)
class Flags:
    """Structured compile-flag state.

    Token lists are mutable so include-path injection and pkg-config
    expansion can extend them in place. After construction, downstream
    consumers should treat them as read-only -- modifications would
    invalidate args.CPPFLAGS_tokens etc. that are populated alongside.
    """

    cpp: list[str] = field(default_factory=list)
    c: list[str] = field(default_factory=list)
    cxx: list[str] = field(default_factory=list)
    ld: list[str] = field(default_factory=list)
    compiler_identity: str = ""

    @classmethod
    def from_args(cls, args) -> Flags:
        """Build a Flags from a parsed args object.

        Reads args.CPPFLAGS_tokens et al. (populated by parseargs).
        Falls back to splitting args.CPPFLAGS if tokens aren't present
        (test-fixture compatibility).
        """
        from compiletools.utils import split_command_cached

        def _slot(name: str) -> list[str]:
            tokens = getattr(args, f"{name}_tokens", None)
            if tokens is not None:
                return list(tokens)
            raw = getattr(args, name, "")
            return split_command_cached(raw) if raw else []

        cxx_command = getattr(args, "CXX", "") or ""
        return cls(
            cpp=_slot("CPPFLAGS"),
            c=_slot("CFLAGS"),
            cxx=_slot("CXXFLAGS"),
            ld=_slot("LDFLAGS"),
            compiler_identity=compiletools.apptools.compiler_identity(cxx_command),
        )

    def hash_relevant(self, slot: str) -> list[str]:
        """Return tokens for the given slot with -D/-U and diagnostic-only
        flags removed. Used by cache-key hashing.

        slot: one of "cpp", "c", "cxx", "ld".
        """
        tokens = getattr(self, slot)
        stripped = compiletools.apptools.strip_d_u_tokens(tokens)
        return compiletools.apptools.filter_hash_irrelevant_tokens(stripped)

    def existing_include_paths(self, slot: str) -> set[str]:
        """Return the set of -I paths (attached or detached) in the given
        slot's tokens."""
        tokens = getattr(self, slot)
        paths: set[str] = set()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "-I" and i + 1 < len(tokens):
                paths.add(tokens[i + 1])
                i += 2
            elif tok.startswith("-I") and len(tok) > 2:
                paths.add(tok[2:])
                i += 1
            else:
                i += 1
        return paths

    def append_include(self, path: str, slots: Iterable[str] = ("cpp", "c", "cxx")) -> None:
        """Append `-I path` (detached form) to each named slot, but only
        if path isn't already present as an -I entry in that slot."""
        for slot in slots:
            if path not in self.existing_include_paths(slot):
                getattr(self, slot).extend(["-I", path])
