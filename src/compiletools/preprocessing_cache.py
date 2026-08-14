"""Unified preprocessing cache for compiletools.

This module provides a centralized cache for preprocessing results that can be
shared across SimplePreprocessor, DirectMagicFlags, and CppHeaderDeps.

The cache uses two strategies:
1. Macro-invariant files (no conditionals): cached by content_hash only
2. Macro-variant files (has conditionals): cached by (content_hash, macro_cache_key)

This optimizes the common case where files have #define but no #if/#ifdef.

IMPORTANT: MacroState.get_hash() uses stringzilla's deterministic hash function
for O(n) performance without sorting. XOR combination ensures order independence.
The hash is deterministic across Python runs, enabling future disk caching support.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Optional

import stringzilla as sz

# Type aliases for macro dictionaries and cache keys
MacroDict = dict[sz.Str, sz.Str]
MacroCacheKey = frozenset[tuple[sz.Str, sz.Str]]
# Parameter lists of function-like macros, keyed by the same bare macro name
# used in MacroDict. A name absent here is object-like.
FunctionParamsDict = dict[sz.Str, tuple[sz.Str, ...]]
# Read-only views of the same shapes, for values shared across cache
# consumers (FileEffects fields are MappingProxyType at runtime).
MacroMapping = Mapping[sz.Str, sz.Str]
FunctionParamsMapping = Mapping[sz.Str, tuple[sz.Str, ...]]
# Cache-key namespace for the parameter-list contribution. '#' cannot appear in
# a C identifier, so a synthesized entry can never collide with a real macro's
# (name, value) pair. A NUL prefix would also be collision-free but renders as
# an empty sz.Str in every -v dump of a cache key.
_FUNCTION_PARAMS_KEY_PREFIX = "#params:"

# Include-path env vars the compiler's preprocessor reads (gcc: CPATH acts
# like -I, the per-language *_INCLUDE_PATH vars like -isystem). Folded into
# the compile-side build-context hash so a value change invalidates cached
# objects — ccache hashes these same four vars for the same reason. Shared
# with headerdeps, which widens the DirectHeaderDeps walk over the same
# list. Deliberately NOT the link-side list (_LINK_ENVIRONMENT_VARS in
# backend_command_args.py) — that one must stay byte-stable independently.
INCLUDE_PATH_ENV_VARS = (
    "CPATH",
    "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH",
    "OBJC_INCLUDE_PATH",
)


def include_path_environment_snapshot() -> dict[str, str]:
    """Snapshot of the CPATH-family env vars at call time.

    Same convention as backend_command_args._link_environment_snapshot:
    unset vars contribute the empty string so 'absent' and 'set to ""'
    hash identically — one less attack surface for cache-poisoning via
    env trickery.
    """
    return {var: os.environ.get(var, "") for var in INCLUDE_PATH_ENV_VARS}


@dataclass(frozen=True)
class FileEffects:
    """What processing one file did to the macro state, as one value.

    Every warm cache tier must reproduce the same three steps a cold run
    performed — remove the file's #undef targets, apply its #defines, replay
    its condition occurrences against the deferred-warning stores. Carrying
    the slots separately made each tier re-implement that sequence (and one
    tier, DirectHeaderDeps, forgot the replay entirely for a release);
    :meth:`apply` is the single implementation.

    Instances are deeply immutable, not merely frozen: one FileEffects is
    shared by identity with every ProcessingResult a warm tier serves, so
    ``__post_init__`` snapshots the mapping fields behind read-only views.

    Attributes:
        content_hash: Hash of the producing file's content, used to resolve
            the file path for condition-occurrence replay.
        file_defines: Macros this file actively #define's (final values).
        file_undefs: Names this file actively #undef's. The sets MAY
            overlap: get_or_compute_preprocessing deliberately carries a
            name that is both undef'd and redefined in both sets so a warm
            caller whose input already holds a stale value reconstructs to
            the file's final one — which is exactly why apply()'s
            undefs-before-defines order is load-bearing. (magicflags'
            producer additionally makes ITS two sets disjoint per the
            positional verdict; that is a property of that producer, not
            something apply() relies on.)
        file_function_params: Parameter lists of the function-like macros
            among file_defines. Without them a replay restores a macro's
            body but not its arity.
        condition_occurrences: ``(kind, directive_type, condition, line_num)``
            tuples for every condition this file's processing resolved or
            found unreached, plus every macro expansion that reached a fixed
            point under the iteration cap (see
            :func:`_replay_condition_occurrences`). The four kinds are
            ``resolved`` / ``unreached`` for conditions and ``expansion`` /
            ``has_operand_expansion`` for the two expansion helpers; an
            expansion tuple names its directive in ``directive_type`` /
            ``line_num`` (or ``"<none>"`` / ``-1`` when it has none) and
            carries the expansion's own input text in ``condition``.
    """

    content_hash: str
    file_defines: MacroMapping = field(default_factory=dict)
    file_undefs: frozenset = field(default_factory=frozenset)
    file_function_params: FunctionParamsMapping = field(default_factory=dict)
    condition_occurrences: tuple = ()

    # frozen=True would auto-generate a __hash__ that raises at runtime
    # (mappingproxy fields are unhashable); declare unhashability instead.
    __hash__ = None  # pyright: ignore[reportAssignmentType]

    def __post_init__(self):
        # frozen=True only stops attribute REBINDING; a shared entry's dict
        # fields would still be mutable in place, and one FileEffects is
        # shared by identity with every ProcessingResult served from a warm
        # tier. Deep-freeze: snapshot-copy (severing aliasing with whatever
        # dict the producer built) behind a read-only view, and coerce the
        # other two fields — and the param-list values, which with_updates
        # stores by reference into every derived MacroState — so a list/set
        # argument cannot smuggle mutability in either.
        object.__setattr__(self, "file_defines", MappingProxyType(dict(self.file_defines)))
        object.__setattr__(
            self,
            "file_function_params",
            MappingProxyType({k: tuple(v) for k, v in self.file_function_params.items()}),
        )
        object.__setattr__(self, "file_undefs", frozenset(self.file_undefs))
        object.__setattr__(self, "condition_occurrences", tuple(self.condition_occurrences))

    def apply(self, macro_state: "MacroState", context) -> "MacroState":
        """Apply this file's recorded effects to `macro_state`; returns the new state.

        Undefs before defines: a name can be both undef'd and redefined
        within the same file (#undef X / #define X 2), so the sets can share
        a key at the boundary where the producer recorded them. Removing
        first then reapplying the file's final value reproduces the file's
        positional outcome; defines-first would let without_keys strip the
        just-reapplied value back out.

        Also replays the producer's condition occurrences so a warm hit
        retracts pending "cannot evaluate" records exactly like the cold run
        whose result it stands in for.
        """
        result = macro_state
        if self.file_undefs:
            result = result.without_keys(self.file_undefs)
        if self.file_defines:
            result = result.with_updates(self.file_defines, self.file_function_params)
        _replay_condition_occurrences(self.condition_occurrences, self.content_hash, context)
        return result


class _FrozenEntries(tuple):
    """Marker type for a tuple this module fully froze itself.

    The pass-through check in _frozen_directive_entries keys on this exact
    type, so only tuples whose every entry went through the snapshot-copy
    below can skip it — a hand-built tuple of proxies (or one mixing proxies
    with plain dicts) is re-frozen rather than trusted.
    """

    __slots__ = ()


def _frozen_directive_entries(entries) -> tuple[Mapping, ...]:
    """Snapshot directive dicts behind read-only views, as a tuple.

    Each entry is snapshot-copied first, so the result also stops aliasing
    the producer's dicts (the cached FileAnalysisResult's own
    includes/magic_flags/defines lists). The list-valued keys a define entry
    carries (``lines``, ``params``) are coerced to tuples: a shallow copy
    would leave those lists aliased to the producer's cached dicts, so one
    consumer's in-place append would corrupt the entry for every other
    consumer of the same cache tier.

    An already-frozen sequence passes through by identity. The warm-hit path
    rebuilds a ProcessingResult around the cached one's own containers, so
    re-copying every entry there would put the snapshot cost on every hit.
    """
    if type(entries) is _FrozenEntries:
        return entries
    return _FrozenEntries(
        MappingProxyType({k: tuple(v) if isinstance(v, list) else v for k, v in entry.items()}) for entry in entries
    )


@dataclass(frozen=True)
class ProcessingResult:
    """Result of preprocessing a file with conditional compilation.

    The four ``active_*`` containers are deep-frozen for the same reason
    FileEffects is: one result is served by identity to every consumer of a
    warm cache entry, so ``__post_init__`` coerces them to tuples of
    read-only views with the list values inside each entry (``lines``,
    ``params``) coerced to tuples (see :func:`_frozen_directive_entries`).
    Their annotations stay at the read-only supertype so a producer can
    pass the lists it built. ``updated_macros`` is exempt: it is a fresh
    MacroState the warm-hit path rebuilds per caller, never shared.

    Attributes:
        active_lines: Line numbers that are active after preprocessing (0-based)
        active_includes: Active #include directives with metadata
        active_magic_flags: Active magic flags with metadata
        active_defines: Active #define directives with metadata
        updated_macros: Macro state after processing (input + defines - undefs)
        effects: What processing this file did to the macro state — the
            defines/undefs/function-params/condition-occurrences bundle a
            warm cache tier replays via :meth:`FileEffects.apply`. The
            four legacy slot names remain readable as delegating properties.
    """

    active_lines: Sequence[int]
    active_includes: Sequence[Mapping]
    active_magic_flags: Sequence[Mapping]
    active_defines: Sequence[Mapping]
    updated_macros: "MacroState"  # Forward reference
    effects: FileEffects

    # Same declared unhashability as FileEffects: the mappingproxy entries
    # inside the active_* tuples would make the auto-generated hash raise.
    __hash__ = None  # pyright: ignore[reportAssignmentType]

    def __post_init__(self):
        # frozen=True only stops attribute REBINDING; the list fields of a
        # cached result would still be mutable in place, and one result is
        # handed by identity to every caller a warm tier serves. Deep-freeze
        # so no consumer can corrupt the entry for all the others.
        object.__setattr__(self, "active_lines", tuple(self.active_lines))
        object.__setattr__(self, "active_includes", _frozen_directive_entries(self.active_includes))
        object.__setattr__(self, "active_magic_flags", _frozen_directive_entries(self.active_magic_flags))
        object.__setattr__(self, "active_defines", _frozen_directive_entries(self.active_defines))

    @property
    def file_defines(self) -> MacroMapping:
        return self.effects.file_defines

    @property
    def file_undefs(self) -> frozenset:
        return self.effects.file_undefs

    @property
    def file_function_params(self) -> FunctionParamsMapping:
        return self.effects.file_function_params

    @property
    def condition_occurrences(self) -> tuple:
        return self.effects.condition_occurrences


@dataclass
class MacroState:
    """Structured macro state and build context for preprocessing.

    Separates static (core) from dynamic (variable) macros, reducing cache key
    computation cost by ~80% by avoiding repeated hashing of unchanging macros.

    Also carries build context fields (compiler_path, cppflags, cflags, cxxflags)
    — these are not macros, but they are build-invariant context.  compiler_path
    and cppflags are needed by the preprocessor to evaluate __has_* functions
    (e.g., __has_include(<iostream>)).  All four fields live here rather than
    being threaded as separate parameters because MacroState already flows
    through the entire preprocessing pipeline.  Like core macros, they are set
    once at construction and propagated automatically by with_updates()/
    without_keys().

    Build context fields are included in the full hash (include_core=True) used
    for object file naming, so that different compilers, include paths, or
    optimization flags produce different object names.  They are NOT included in
    preprocessing cache keys (include_core=False) because the compiler and flags
    are constant per-process.

    Not a dict: callers that want to read macros must do so through the explicit
    attributes (``.core``, ``.variable``) or ``all_macros()`` for the merged view.
    The narrow API surface keeps the immutability story tight and avoids tempting
    callers to mutate or hash macros without going through ``get_cache_key()`` /
    ``get_hash()``.

    Attributes:
        core: Static macros (compiler built-ins + cmdline -D flags). ~388 macros.
              These never change during a build, so we exclude them from cache keys.
        variable: Dynamic macros accumulated from #define directives in files.
                  These grow as files are processed and determine cache behavior.
        compiler_path: Compiler executable (e.g., 'gcc') for __has_* evaluation.
                       Not a macro — build context carried for convenience.
        cppflags: Raw preprocessor flags (e.g., '-I/usr/include').  The -I paths
                  are needed so __has_include can search the right directories.
                  Not macros — only the -D portions are extracted into core.
        cflags: Raw C compiler flags (e.g., '-O2 -fPIC').  Hashed for object
                naming so different optimization levels produce different objects.
        cxxflags: Raw C++ compiler flags (e.g., '-std=c++17').  Hashed for object
                  naming so different C++ standards produce different objects.
        cmdline_origin: Names in `core` that came from cmdline -D flags (vs
                        compiler built-ins). When non-empty, callers can pass a
                        scope_filter to get_hash(include_core=True) so only the
                        cmdline -D macros actually referenced by the TU are
                        included in the hash. Default empty: no filtering.
        cppflags_tokens: Optional structured token list for cppflags with -D/-U
                         already stripped. When provided, get_hash() hashes the
                         tokens instead of the raw cppflags string (so the -D
                         tokens that scope_filter is meant to filter don't sneak
                         back in via the build-context hash). None = today's
                         raw-string hashing.
        cflags_tokens: Same idea for cflags.
        cxxflags_tokens: Same idea for cxxflags.
        compiler_identity: Stable identity string for the compiler binary
                           (realpath|size|mtime_ns) produced by
                           ``apptools.compiler_identity``. Folded into the
                           build-context portion of the include_core hash so
                           an in-place toolchain swap that does not change
                           the user-visible ``compiler_path`` (e.g. ``g++``)
                           still invalidates stale objects. Default ``""``
                           preserves backward compat for tests that don't
                           set it.  Symmetric with the PCH cache key in
                           ``build_backend._pch_command_hash``.
    """

    core: MacroDict  # Static: compiler + cmdline macros
    variable: MacroDict  # Dynamic: file #defines
    function_params: FunctionParamsDict  # Parameter lists of the function-like variable macros
    compiler_path: str  # Build context: compiler executable for __has_* queries
    cppflags: str  # Build context: raw flags (-I paths etc.) for __has_* queries
    cflags: str  # Build context: C compiler flags for object naming
    cxxflags: str  # Build context: C++ compiler flags for object naming
    cmdline_origin: frozenset  # Names in core that came from cmdline -D flags
    cppflags_tokens: Optional[list]  # Structured cppflags tokens (-D/-U stripped)
    cflags_tokens: Optional[list]  # Structured cflags tokens (-D/-U stripped)
    cxxflags_tokens: Optional[list]  # Structured cxxflags tokens (-D/-U stripped)
    compiler_identity: str  # Build context: compiler binary identity (realpath|size|mtime_ns)
    anchor_root: str  # Build context: gitroot used to canonicalize -I paths in the hash
    _cache_key: Optional[MacroCacheKey]  # Cached frozenset for cache keys
    _hash: Optional[str]  # Cached hex digest for convergence detection (variable only)
    _hash_full: Optional[str]  # Cached hex digest including core + variable + build context
    _build_context_hash: Optional[int]  # Cached sz.hash() of the canonicalised build_context block

    def __init__(
        self,
        core: MacroDict,
        variable: Optional[MacroDict] = None,
        compiler_path: str = "",
        cppflags: str = "",
        cflags: str = "",
        cxxflags: str = "",
        cmdline_origin: frozenset = frozenset(),
        cppflags_tokens: Optional[list] = None,
        cflags_tokens: Optional[list] = None,
        cxxflags_tokens: Optional[list] = None,
        compiler_identity: str = "",
        function_params: Optional[FunctionParamsDict] = None,
        *,
        anchor_root: str,  # required: gitroot for canonicalisation; pass "" only in tests
    ):
        """Initialize macro state.

        Args:
            core: Static macros (compiler built-ins + cmdline flags)
            variable: Dynamic macros (file defines). Defaults to empty dict.
            compiler_path: Compiler executable for evaluating __has_* functions
            cppflags: Additional preprocessor flags forwarded to __has_* queries
            cflags: C compiler flags (e.g., '-O2') for object naming hash
            cxxflags: C++ compiler flags (e.g., '-std=c++17') for object naming hash
            cmdline_origin: Names in `core` that came from cmdline -D flags
                (the rest of `core` is compiler built-ins). Default empty.
            cppflags_tokens: Optional tokenized cppflags with -D/-U stripped.
                None falls back to hashing the raw cppflags string.
            cflags_tokens: Optional tokenized cflags with -D/-U stripped.
            cxxflags_tokens: Optional tokenized cxxflags with -D/-U stripped.
            compiler_identity: Stable identity for the compiler binary
                (realpath|size|mtime_ns) from ``apptools.compiler_identity``.
                Folded into the include_core hash so an in-place toolchain
                swap invalidates objects. Default ``""`` when not applicable.
            function_params: Parameter lists of the function-like macros among
                `variable`, keyed by the same bare name. Needed because a
                function-like macro's expansion depends on its parameter list,
                and `variable` alone carries only the body. Default empty
                contributes nothing to any cache key or hash, so a file with
                no function-like macros keys exactly as before.
            anchor_root: Gitroot prefix used by ``canonicalize_path_for_cache_key``
                to make flag-token hashes workspace-independent. Pass ``""``
                only in tests or when the gitroot cannot be resolved (graceful
                no-op). Required: omitting it silently re-introduces the
                gitroot-leak bug the canonicaliser exists to prevent.
        """
        self.core = core
        self.variable = variable if variable is not None else {}
        self.function_params = function_params if function_params is not None else {}
        self.compiler_path = compiler_path
        self.cppflags = cppflags
        self.cflags = cflags
        self.cxxflags = cxxflags
        self.cmdline_origin = cmdline_origin
        self.cppflags_tokens = cppflags_tokens
        self.cflags_tokens = cflags_tokens
        self.cxxflags_tokens = cxxflags_tokens
        self.compiler_identity = compiler_identity
        self.anchor_root = anchor_root
        self._cache_key = None  # Lazy-computed cache key
        self._hash = None  # Lazy-computed hash (variable only)
        self._hash_full = None  # Lazy-computed hash (core + variable + build context)
        self._build_context_hash = None  # Lazy-computed sz.hash() of build_context block

    def all_macros(self) -> MacroDict:
        """Get merged view of all macros (core + variable).

        Returns:
            Dictionary containing all macros. Variable macros override core if conflicts.
        """
        result = self.core.copy()
        result.update(self.variable)
        return result

    def with_updates(
        self, new_macros: MacroMapping, new_function_params: Optional[FunctionParamsMapping] = None
    ) -> "MacroState":
        """Create new MacroState with additional macros merged into variable.

        Args:
            new_macros: Macros to merge (typically from file #defines)
            new_function_params: Parameter lists for whichever of `new_macros`
                are function-like. A name in `new_macros` but absent here is
                object-like, so any parameter list it carried before is
                dropped — a redefinition can turn a function-like macro
                object-like.

        Returns:
            New MacroState with same core but updated variable macros.
            Returns self if new_macros is empty or contains no effective changes.
        """
        # Short-circuit: if no new macros, return self to preserve cached state
        if not new_macros:
            return self

        # Filter out no-op updates to ensure immutability efficiency
        # Only apply updates that actually change the value or add a new key
        actual_updates = {k: v for k, v in new_macros.items() if k not in self.variable or self.variable[k] != v}

        updated_params = {k: v for k, v in self.function_params.items() if k not in new_macros}
        if new_function_params:
            updated_params.update({k: v for k, v in new_function_params.items() if k in new_macros})
        params_changed = updated_params != self.function_params

        # A redefinition can leave the body identical and change only the
        # parameter list, so an empty actual_updates is not by itself a no-op.
        if not actual_updates and not params_changed:
            return self

        updated_variable = self.variable.copy()
        updated_variable.update(actual_updates)
        new_state = MacroState(
            self.core,
            updated_variable,
            compiler_path=self.compiler_path,
            cppflags=self.cppflags,
            cflags=self.cflags,
            cxxflags=self.cxxflags,
            cmdline_origin=self.cmdline_origin,
            cppflags_tokens=self.cppflags_tokens,
            cflags_tokens=self.cflags_tokens,
            cxxflags_tokens=self.cxxflags_tokens,
            compiler_identity=self.compiler_identity,
            function_params=updated_params,
            anchor_root=self.anchor_root,
        )

        # Optimization: incrementally compute cache key when possible
        # Only for pure additions (no key overwrites) since frozenset union
        # doesn't replace - it adds. Macro definitions are typically additive
        # (include guards, feature flags), so pure additions are the common case.
        # A changed parameter-list contribution rules the shortcut out: those
        # entries are namespaced, so a union cannot replace a stale one either.
        if self._cache_key is not None and not params_changed:
            overwrites = any(k in self.variable for k in actual_updates)
            if not overwrites:
                # Pure addition - O(k) frozenset union instead of O(n) rebuild
                new_state._cache_key = self._cache_key | frozenset(actual_updates.items())

        return new_state

    def without_keys(self, keys) -> "MacroState":
        """Create new MacroState with specified keys removed from variable.

        A removed macro loses its parameter list with its body: #undef takes
        the whole definition, so no orphan entry may survive to be consulted
        by a later redefinition.
        """
        removed = {k for k in keys if k in self.variable}
        if not removed:
            return self
        updated_variable = {k: v for k, v in self.variable.items() if k not in removed}
        updated_params = {k: v for k, v in self.function_params.items() if k not in removed}
        return MacroState(
            self.core,
            updated_variable,
            compiler_path=self.compiler_path,
            cppflags=self.cppflags,
            cflags=self.cflags,
            cxxflags=self.cxxflags,
            cmdline_origin=self.cmdline_origin,
            cppflags_tokens=self.cppflags_tokens,
            cflags_tokens=self.cflags_tokens,
            cxxflags_tokens=self.cxxflags_tokens,
            compiler_identity=self.compiler_identity,
            function_params=updated_params,
            anchor_root=self.anchor_root,
        )

    def get_cached_key_if_available(self) -> Optional[MacroCacheKey]:
        """Get cache key if already computed, None otherwise.

        Use this to avoid recomputing the cache key when it might already be available.
        Useful in hot paths where you want to check before computing.

        Returns:
            Cached frozenset if available, None if not yet computed
        """
        return self._cache_key

    def get_cache_key(self) -> MacroCacheKey:
        """Get or compute cache key for this MacroState.

        Returns cached key if available, otherwise computes and caches it.
        """
        if not self.variable:
            return _EMPTY_FROZENSET

        if self._cache_key is None:
            self._cache_key = frozenset(self.variable.items()) | self._function_params_key_items()

        return self._cache_key

    def _function_params_key_items(self, names: Optional[frozenset[sz.Str]] = None) -> MacroCacheKey:
        """Cache-key contribution of the function-like macros' parameter lists.

        Two definitions can share a body and differ only in their parameter
        names (``#define F(a) a`` vs ``#define F(b) a``), so the parameter list
        has to key alongside the body. The entries are namespaced under a
        synthetic name that keeps the (name, value) shape every key consumer
        expects while being unable to collide with a real macro.

        Returns the empty frozenset when there are no function-like macros, so
        a state without them keys byte-for-byte as it did before the feature.
        """
        if not self.function_params:
            return _EMPTY_FROZENSET
        items = self.function_params.items()
        if names is not None:
            items = [(n, self.function_params[n]) for n in names if n in self.function_params]
        return frozenset(
            (sz.Str(_FUNCTION_PARAMS_KEY_PREFIX + str(name)), sz.Str(",".join(str(p) for p in params)))
            for name, params in items
        )

    def get_relevant_key(self, relevant_macros: frozenset[sz.Str]) -> MacroCacheKey:
        """Get cache key filtered to only macros that affect the target file.

        For variant caching, only macros referenced in conditionals (#ifdef, #if, etc.)
        can affect preprocessing. Other macros in the state are irrelevant for this file
        and should not create unique cache keys.

        Args:
            relevant_macros: Set of macro names from file_result.conditional_macros

        Returns:
            Frozenset of (name, value) pairs for only the relevant variable macros
        """
        if not relevant_macros:
            return _EMPTY_FROZENSET

        # Build filtered key - only include variable macros that matter
        relevant_items = tuple((m, self.variable[m]) for m in relevant_macros if m in self.variable)
        relevant_params = self._function_params_key_items(relevant_macros)
        if not relevant_items:
            return relevant_params
        return frozenset(relevant_items) | relevant_params

    def get_hash(
        self,
        include_core: bool = False,
        scope_filter: Optional[frozenset] = None,
    ) -> str:
        """Get or compute stable hash of this MacroState for convergence detection.

        Args:
            include_core: If True, include core macros + build context in hash.
                If False (default), only hash variable macros (preprocessing cache).
            scope_filter: Optional set of cmdline -D macro names to include from
                `core`. Names in `core` but NOT in `cmdline_origin` (compiler
                builtins) are always hashed; names in BOTH `core` and
                `cmdline_origin` are hashed only if they appear in scope_filter.
                Variable macros are always hashed (unaffected by scope_filter).
                Ignored when include_core is False. None preserves today's
                behavior (no filtering — all of core is hashed).

        Returns a hex string of stable 64-bit hash using stringzilla's deterministic hash.
        Hash is deterministic across Python runs (suitable for disk caching).
        Uses cached hash to avoid recomputation on repeated calls (only for the
        unfiltered include_core path; filtered hashes are not cached because
        each TU may pass a different scope_filter).

        INVARIANT: equal cache keys produce equal hashes (1-to-1 mapping)
        Performance: O(n) with no sorting - XOR is commutative so order doesn't matter
        """
        # Variable-only path (preprocessing cache) — unaffected by scope_filter.
        if not include_core:
            if self._hash is not None:
                return self._hash
            combined = 0
            for name, value in self.get_cache_key():
                combined ^= sz.hash(bytes(name))
                combined ^= sz.hash(bytes(value))
            self._hash = format(combined, "016x")
            return self._hash

        # Full hash path. Cache only the unfiltered call.
        if scope_filter is None and self._hash_full is not None:
            return self._hash_full

        combined = self._compute_full_hash_combined(scope_filter)
        result = format(combined, "016x")
        if scope_filter is None:
            self._hash_full = result
        return result

    def _compute_full_hash_combined(self, scope_filter: Optional[frozenset]) -> int:
        """Compute the XOR-combined 64-bit hash for the full (include_core) path.

        Filters cmdline-origin core macros against scope_filter when provided,
        and hashes structured flag tokens instead of raw flag strings when
        tokens are available.
        """
        combined = 0
        # Core + variable macros. Match original behavior: dedup (name, value)
        # pairs via frozenset so that duplicates between core and variable
        # don't cancel under XOR.
        if scope_filter is None or not self.cmdline_origin:
            items_to_hash = frozenset(list(self.core.items()) + list(self.variable.items()))
        else:
            filtered_core = [(n, v) for n, v in self.core.items() if n not in self.cmdline_origin or n in scope_filter]
            items_to_hash = frozenset(filtered_core + list(self.variable.items()))
        items_to_hash = items_to_hash | self._function_params_key_items()
        for name, value in items_to_hash:
            combined ^= sz.hash(bytes(name))
            combined ^= sz.hash(bytes(value))

        combined ^= self._get_build_context_hash()
        return combined

    def _get_build_context_hash(self) -> int:
        """Hash of the canonicalised build_context block (compiler path,
        identity, flag tokens, CPATH-family env snapshot). All inputs are
        MacroState invariants or run-stable environment — cache so the
        scope-filtered hash path doesn't re-canonicalise on every per-TU
        call.
        """
        cached = self._build_context_hash
        if cached is not None:
            return cached

        # Deferred import: apptools transitively pulls in many modules and
        # preprocessing_cache is imported very early at startup.
        from compiletools.apptools import (
            canonicalize_for_cache_key,
            canonicalize_path_for_cache_key,
            filter_hash_irrelevant_tokens,
            tokenize_compile_flags,
        )
        from compiletools.utils import deduplicate_compiler_flags

        # tokenize_compile_flags accepts raw strings OR pre-tokenized lists
        # (idempotent on the latter — it strips -D/-U which upstream callers
        # already stripped), so passing whichever the caller populated works.
        cpp_in = self.cppflags if self.cppflags_tokens is None else self.cppflags_tokens
        c_in = self.cflags if self.cflags_tokens is None else self.cflags_tokens
        cxx_in = self.cxxflags if self.cxxflags_tokens is None else self.cxxflags_tokens
        cppflags_tokens, cflags_tokens, cxxflags_tokens = tokenize_compile_flags(cpp_in, c_in, cxx_in)

        def _canon(toks):
            return canonicalize_for_cache_key(filter_hash_irrelevant_tokens(toks), self.anchor_root)

        # The cpp slot is hashed as dedup(cpp + cxx) — the same merge
        # build_state.stage_unify applies — so key equality tracks argv
        # equality: raw CPPFLAGS never reaches a compile line on its own
        # (the C++ argv is CXX + cxxflags; the C argv is CC + cflags).
        # Argv equality is sufficient for safety: configs this key
        # conflates compile identically, and any header-RESOLUTION drift
        # between them (DirectHeaderDeps searches CPPFLAGS-derived paths
        # only) is captured independently by dep_hash in the object key.
        # Hashing the slot verbatim forked the key space whenever a token
        # was promoted between cpp and cxx without changing any argv.
        # cflags/cxxflags stay exact: each IS an argv, and both order and
        # c-vs-cxx placement are argv properties (a collision here would
        # be a silent miscompile — cas-objdir has no verification at
        # link).
        cpp_union_cxx = deduplicate_compiler_flags(list(cppflags_tokens) + list(cxxflags_tokens))
        cppflags_part = "CPPFLAGS_TOKENS=" + "\x00".join(_canon(cpp_union_cxx))
        cflags_part = "CFLAGS_TOKENS=" + "\x00".join(_canon(cflags_tokens))
        cxxflags_part = "CXXFLAGS_TOKENS=" + "\x00".join(_canon(cxxflags_tokens))

        canonical_cc = canonicalize_path_for_cache_key(self.compiler_path, self.anchor_root)

        # The CPATH-family vars change which headers the compiler (and
        # CppHeaderDeps' child cpp) resolves without appearing in any flag
        # string, so a value change must invalidate cached objects. Read at
        # hash time, not construction: the env is stable within a run (same
        # assumption as the link-side snapshot).
        def _canon_env_value(value):
            # Entries are canonicalized as given, not normpath'd first —
            # matching the flag-token canonicalizer, which is a textual
            # string-prefix op. Empty entries pass through untouched: gcc
            # treats them as "the current directory", not a path.
            if not value:
                return value
            return os.pathsep.join(
                canonicalize_path_for_cache_key(entry, self.anchor_root) if entry else entry
                for entry in value.split(os.pathsep)
            )

        include_env = include_path_environment_snapshot()
        include_env_part = "INCLUDE_ENV=" + "\x00".join(
            f"{n}={_canon_env_value(v)}" for n, v in sorted(include_env.items())
        )
        build_context = (
            f"CC={canonical_cc}\x00"
            f"COMPILER_IDENTITY={self.compiler_identity}\x00"
            f"{cppflags_part}\x00{cflags_part}\x00{cxxflags_part}\x00"
            f"{include_env_part}"
        )
        result = int(sz.hash(bytes(build_context, "utf-8")))
        self._build_context_hash = result
        return result


# Simple cache: if variable dict is empty, return cached empty frozenset
_EMPTY_FROZENSET: MacroCacheKey = frozenset()


def decode_macro_cache_key(key: MacroCacheKey) -> tuple[MacroDict, FunctionParamsDict]:
    """Split a cache key back into its variable macros and parameter lists.

    The inverse of ``MacroState.get_cache_key``: the synthetic
    ``#params:<NAME>`` entries ``MacroState._function_params_key_items`` writes
    are routed to the parameter dict instead of being read as object-like
    macros. A caller that rebuilds a MacroState from a key must use this —
    a flat ``dict(key)`` strands every function-like macro's arity and puts a
    name no C file can spell into the variable dict.

    Feeding the result back to ``MacroState`` re-encodes the identical key, so
    a key that has been round-tripped is unchanged and caches keyed on it stay
    warm.

    An entry whose value is empty decodes to an empty parameter tuple — a
    zero-arity function-like macro (``#define F() 1``), which is distinct from
    an object-like macro of the same name.
    """
    variable: MacroDict = {}
    function_params: FunctionParamsDict = {}
    for name, value in key:
        name_str = str(name)
        if name_str.startswith(_FUNCTION_PARAMS_KEY_PREFIX):
            bare = sz.Str(name_str[len(_FUNCTION_PARAMS_KEY_PREFIX) :])
            value_str = str(value)
            params = tuple(sz.Str(p) for p in value_str.split(",")) if value_str else ()
            function_params[bare] = params
        else:
            variable[name] = value
    return variable, function_params


def is_permanently_invariant(file_result) -> bool:
    """Determine if a file is permanently invariant (no conditionals).

    Files with no conditional compilation directives are always invariant
    regardless of macro state. They can be processed once and never need
    reprocessing during convergence iterations.

    Args:
        file_result: FileAnalysisResult with conditional_macros field

    Returns:
        True if file has no conditionals at all
    """
    return not file_result.conditional_macros


def is_macro_invariant(file_result, input_macros: "MacroState") -> bool:
    """Determine if a file's active lines are independent of current macro state.

    A file is effectively invariant if none of its conditional macros are currently defined
    in the VARIABLE macros. We only check variable macros because core macros (compiler
    built-ins + cmdline) are identical for all files in a build.

    Examples of effectively invariant files:
    - Headers with #ifdef __GNUC__ when __GNUC__ is in core (always invariant for that file)
    - Files with platform checks that don't match current build
    - Headers with only #define, #include, #pragma (no conditionals at all)

    Args:
        file_result: FileAnalysisResult with conditional_macros field
        input_macros: MacroState with current macro state

    Returns:
        True if none of the file's conditional macros are defined in variable macros
    """
    # If file has no conditionals at all, it's always invariant
    if is_permanently_invariant(file_result):
        return True

    # Only check variable macros - core macros are the same for all files
    return not any(m in input_macros.variable for m in file_result.conditional_macros)


# Dual cache strategy:
# 1. Invariant cache: content_hash -> ProcessingResult (for files without conditionals)
# 2. Variant cache: (content_hash, macro_frozenset) -> ProcessingResult (for files with conditionals)
#
# NOTE: We use manual caching instead of @lru_cache because:
# 1. Function arguments (FileAnalysisResult, Dict) are not hashable
# 2. Cache key must be extracted from file_result and macros
# 3. We need full objects to compute results, not just hashes
# 4. Provides enhanced debugging (dump_cache_keys with file path resolution)
def _replay_condition_occurrences(condition_occurrences, content_hash, context) -> None:
    """Replay a cached compute's condition and expansion events on a cache HIT.

    A live SimplePreprocessor run never happens on a cache hit, so
    ``_note_condition_resolved``/``_note_condition_unreached`` (and their
    expansion counterpart ``_note_expansion_settled``) cannot fire on their
    own -- without this, a pending "cannot evaluate" or "recursive macro
    definition cycle" record left by an earlier pass over this same file
    survives forever once a later pass's result comes from the cache instead
    of a live rerun, even though the cached entry is itself proof the
    condition resolves (or was never reached, or expands under the cap) at
    this exact macro state.

    Reached only through :meth:`FileEffects.apply`, which every warm tier
    over a computed ProcessingResult uses: the two caches here, and
    DirectHeaderDeps' include-list cache (which fronts this one and can
    serve the only settled-state evaluation a convergence sees).
    """
    if not condition_occurrences:
        return
    # Mirror the live path's deferral gate (_note_condition_resolved returns
    # before touching either store when no convergence is listening): a
    # depth-0 warm hit — Hunter's headerdeps walks between TU parses — must
    # not write the resolved store, or the key survives until the NEXT
    # convergence's flush and silences a "cannot evaluate" report that
    # convergence still owes. The stores are per-convergence state; only a
    # replay inside one may touch them.
    if getattr(context, "preprocessor_convergence_depth", 0) <= 0:
        return
    pending = getattr(context, "pending_preprocessor_warnings", None)
    resolved = getattr(context, "resolved_preprocessor_conditions", None)
    pending_expansions = getattr(context, "pending_preprocessor_expansion_warnings", None)
    resolved_expansions = getattr(context, "resolved_preprocessor_expansions", None)
    if pending is None and resolved is None and pending_expansions is None and resolved_expansions is None:
        return

    from compiletools.global_hash_registry import get_filepath_by_hash
    from compiletools.simple_preprocessor import EXPANSION_OCCURRENCE_KINDS

    # Same fallback a live run uses (process_structured's `filepath or
    # "<unknown>"`): pending records for an unresolvable hash are keyed
    # under "<unknown>", so the replay must address them the same way.
    filepath = get_filepath_by_hash(content_hash, context) or "<unknown>"

    for kind, directive_type, condition, line_num in condition_occurrences:
        # An expansion occurrence carries the expansion's input text in the
        # condition slot and its helper in the kind slot, and addresses the
        # expansion stores -- whose key shape differs so that a clean
        # expansion of a #if's text cannot retract that #if's own pending
        # "cannot evaluate" record, which is a different claim entirely.
        if kind in EXPANSION_OCCURRENCE_KINDS:
            expansion_key = (filepath, directive_type, line_num, kind, condition)
            if resolved_expansions is not None:
                resolved_expansions.add(expansion_key)
            if pending_expansions is not None:
                pending_expansions.pop(expansion_key, None)
            continue
        key = (filepath, directive_type, condition, line_num)
        if kind == "resolved" and resolved is not None:
            resolved.add(key)
        if pending is not None:
            pending.pop(key, None)


def get_or_compute_preprocessing(
    file_result,
    input_macros: "MacroState",
    verbose: int = 0,
    *,
    context,
) -> ProcessingResult:
    """Get preprocessing result from cache or compute if not cached.

    Uses dual cache strategy:
    - Macro-invariant files: cached by content_hash only
    - Macro-variant files: cached by (content_hash, macro_cache_key)

    IMPORTANT: Caller must propagate macro state across files:
        result1 = get_or_compute_preprocessing(file1, initial_macros, verbose)
        result2 = get_or_compute_preprocessing(file2, result1.updated_macros, verbose)

    Args:
        file_result: FileAnalysisResult with file content and metadata
        input_macros: MacroState with current macro state for this file
            (compiler_path and cppflags are read from MacroState for __has_* evaluation)
        verbose: Verbosity level for debugging

    Returns:
        ProcessingResult with active lines, includes, magic flags, defines, and updated MacroState
    """
    from compiletools.simple_preprocessor import SimplePreprocessor

    inv_cache = context.invariant_preprocessing_cache
    var_cache = context.variant_preprocessing_cache
    stats = context.preprocessing_stats

    stats["total_calls"] += 1

    content_hash = file_result.content_hash
    invariant = is_macro_invariant(file_result, input_macros)

    # One hit/miss sequence for both tiers, parameterised by cache dict and
    # key; the invariant/variant statistics counters stay separate.
    if invariant:
        # Macro-invariant: cache key is content_hash only
        cache, cache_key, tier = inv_cache, content_hash, "invariant"
    else:
        # Macro-variant: cache key is (content_hash, file_specific_macro_key)
        # Use file-specific key: only macros that affect this file's conditionals
        macro_key = input_macros.get_relevant_key(file_result.conditional_macros)
        cache, cache_key, tier = var_cache, (content_hash, macro_key), "variant"

    cached = cache.get(cache_key)
    if cached is not None:
        stats["hits"] += 1
        stats[f"{tier}_hits"] += 1
        # Reconstruct updated_macros from caller's input + the cached effects
        # to prevent stale macro pollution from the first caller's context.
        # apply() owns the undefs-before-defines ordering and the
        # condition-occurrence replay.
        return ProcessingResult(
            active_lines=cached.active_lines,
            active_includes=cached.active_includes,
            active_magic_flags=cached.active_magic_flags,
            active_defines=cached.active_defines,
            updated_macros=cached.effects.apply(input_macros, context),
            effects=cached.effects,
        )

    stats["misses"] += 1
    stats[f"{tier}_misses"] += 1

    # Compute result - pass all macros to preprocessor
    all_macros = input_macros.all_macros()
    preprocessor = SimplePreprocessor(
        all_macros,
        verbose=verbose,
        compiler_path=input_macros.compiler_path,
        cppflags=input_macros.cppflags,
        function_params=input_macros.function_params,
    )
    active_lines = preprocessor.process_structured(file_result, context)
    active_line_set = set(active_lines)

    # Extract active includes
    active_includes = [inc for inc in file_result.includes if inc["line_num"] in active_line_set]

    # Resolve computed includes from directives
    for directive in file_result.directives:
        if directive.directive_type == "include" and directive.line_num in active_line_set and directive.condition:
            resolved = preprocessor.resolve_computed_include(directive.condition)
            if resolved:
                active_includes.append(
                    {
                        "line_num": directive.line_num,
                        "filename": sz.Str(resolved),
                        "is_system": False,
                        "is_commented": False,
                    }
                )

    # Extract active magic flags
    active_magic_flags = [magic for magic in file_result.magic_flags if magic["line_num"] in active_line_set]

    # Extract active defines
    active_defines = [define for define in file_result.defines if define["line_num"] in active_line_set]

    # Build updated MacroState from preprocessor results
    # Core stays the same, variable gets new defines from this file
    # Only add to variable if not in core
    new_variable_macros = {k: v for k, v in preprocessor.macros.items() if k not in input_macros.core}

    # Active undef targets: macro names from #undef directives on active lines.
    # Input-independent: safe to cache for both invariant and variant entries.
    # without_keys() handles the intersection with the caller's variable macros.
    # Computed BEFORE file_defines: the delta filter below must consult it.
    file_undefs = frozenset(
        d.macro_name
        for d in file_result.directives
        if d.directive_type == "undef" and d.macro_name and d.line_num in active_line_set
    )

    # Names this file actively #define's on active lines. Input-independent
    # for the entry's key space (activity is fixed by the cache key), so
    # every one of them belongs in file_defines: a cold run over this file
    # defines them no matter what the caller's input held.
    actively_defined = {d["name"] for d in active_defines}

    # Store file-specific defines for cache reconstruction
    # file_defines contains every macro this file actively defines, plus any
    # macro whose value changed relative to input. Filtering to the delta
    # against THIS call's input is wrong on both halves: a redefinition whose
    # final value happens to equal the producer's own input is no delta HERE
    # but is one for a warm caller with a different (or absent) input — the
    # entry is shared, so omitting the define makes the reconstruction depend
    # on which caller populated the cache first. The delta conditions are
    # kept for macros that reach new_variable_macros without an active
    # #define line, and file_undefs names are carried so the undefs-then-
    # defines reconstruction can reapply the file's final value.
    # Note: Include guards are already excluded by SimplePreprocessor._handle_define_structured()
    file_defines: MacroDict = {}
    for k, v in new_variable_macros.items():
        if k in actively_defined or k in file_undefs or k not in input_macros.variable or input_macros.variable[k] != v:
            file_defines[k] = v

    # Parameter lists travel with the bodies they belong to, through both the
    # updated state and the cache-reconstruction path.
    new_function_params: FunctionParamsDict = {
        k: v for k, v in preprocessor.function_params.items() if k in new_variable_macros
    }
    file_function_params: FunctionParamsDict = {k: v for k, v in new_function_params.items() if k in file_defines}

    # Build updated state: new_variable_macros already reflects the correct
    # post-preprocessing state (input macros + file defines - file undefs)
    updated_macro_state = MacroState(
        input_macros.core,
        new_variable_macros,
        compiler_path=input_macros.compiler_path,
        cppflags=input_macros.cppflags,
        cflags=input_macros.cflags,
        cxxflags=input_macros.cxxflags,
        cmdline_origin=input_macros.cmdline_origin,
        cppflags_tokens=input_macros.cppflags_tokens,
        cflags_tokens=input_macros.cflags_tokens,
        cxxflags_tokens=input_macros.cxxflags_tokens,
        compiler_identity=input_macros.compiler_identity,
        function_params=new_function_params,
        anchor_root=input_macros.anchor_root,
    )

    # Create result
    result = ProcessingResult(
        active_lines=active_lines,
        active_includes=active_includes,
        active_magic_flags=active_magic_flags,
        active_defines=active_defines,
        updated_macros=updated_macro_state,
        effects=FileEffects(
            content_hash=content_hash,
            file_defines=file_defines,
            file_undefs=file_undefs,
            file_function_params=file_function_params,
            condition_occurrences=tuple(preprocessor.condition_occurrences),
        ),
    )

    # Store in the tier selected above
    cache[cache_key] = result

    return result


def get_cache_stats(context) -> dict:
    """Return cache statistics for debugging and monitoring.

    Returns:
        Dictionary with cache metrics:
        - entries: Total number of cached results
        - invariant_entries: Number of macro-invariant cache entries
        - variant_entries: Number of macro-variant cache entries
        - hits: Number of cache hits
        - invariant_hits: Number of invariant cache hits
        - variant_hits: Number of variant cache hits
        - misses: Number of cache misses
        - invariant_misses: Number of invariant cache misses
        - variant_misses: Number of variant cache misses
        - total_calls: Total calls to get_or_compute_preprocessing
        - hit_rate: Percentage of cache hits (0-100)
        - memory_bytes: Approximate memory usage
        - memory_mb: Memory usage in MB
    """
    inv_c = context.invariant_preprocessing_cache
    var_c = context.variant_preprocessing_cache
    st = context.preprocessing_stats

    # sys.getsizeof always raises TypeError on PyPy unless a default is supplied;
    # memory_bytes is documented as approximate, so 0 is fine as the fallback.
    total_size = 0
    for result in inv_c.values():
        total_size += sys.getsizeof(result.active_lines, 0)
        total_size += sys.getsizeof(result.active_includes, 0)
        total_size += sys.getsizeof(result.active_magic_flags, 0)
        total_size += sys.getsizeof(result.active_defines, 0)
        total_size += sys.getsizeof(result.updated_macros, 0)

    for result in var_c.values():
        total_size += sys.getsizeof(result.active_lines, 0)
        total_size += sys.getsizeof(result.active_includes, 0)
        total_size += sys.getsizeof(result.active_magic_flags, 0)
        total_size += sys.getsizeof(result.active_defines, 0)
        total_size += sys.getsizeof(result.updated_macros, 0)

    hit_rate = 0.0
    if st["total_calls"] > 0:
        hit_rate = (st["hits"] / st["total_calls"]) * 100

    return {
        "entries": len(inv_c) + len(var_c),
        "invariant_entries": len(inv_c),
        "variant_entries": len(var_c),
        "hits": st["hits"],
        "invariant_hits": st["invariant_hits"],
        "variant_hits": st["variant_hits"],
        "misses": st["misses"],
        "invariant_misses": st["invariant_misses"],
        "variant_misses": st["variant_misses"],
        "total_calls": st["total_calls"],
        "hit_rate": hit_rate,
        "memory_bytes": total_size,
        "memory_mb": total_size / (1024 * 1024),
    }


def clear_cache(context):
    """Clear the preprocessing cache and reset statistics on the given context.

    In production code, creating a fresh BuildContext is preferred over clearing.
    This function exists for tests that need to reset mid-test.
    """
    context.invariant_preprocessing_cache.clear()
    context.variant_preprocessing_cache.clear()
    for key in context.preprocessing_stats:
        context.preprocessing_stats[key] = 0


def clear_variant_cache(context):
    """Clear only the macro-variant preprocessing cache.

    Used during two-pass header discovery to ensure Pass 2 gets fresh results
    with converged macros. The invariant cache is preserved since those files
    have no conditionals and their results are truly macro-independent.
    """
    context.variant_preprocessing_cache.clear()


def print_preprocessing_stats(context):
    """Print preprocessing cache and SimplePreprocessor statistics."""
    stats = get_cache_stats(context)

    print("\n=== Preprocessing Cache Statistics ===")
    print(f"Total preprocessing calls: {stats['total_calls']}")
    print(f"Cache hits: {stats['hits']}")
    print(f"Cache misses: {stats['misses']}")
    print(f"Cache hit rate: {stats['hit_rate']:.1f}%")
    print("\nCache entries:")
    print(f"  Invariant entries: {stats['invariant_entries']}")
    print(f"  Variant entries: {stats['variant_entries']}")
    print(f"  Total entries: {stats['entries']}")
    print("\nHit breakdown:")
    print(f"  Invariant hits: {stats['invariant_hits']}")
    print(f"  Variant hits: {stats['variant_hits']}")
    print("\nMiss breakdown:")
    print(f"  Invariant misses: {stats['invariant_misses']}")
    print(f"  Variant misses: {stats['variant_misses']}")

    # Print SimplePreprocessor call statistics
    from compiletools.simple_preprocessor import print_preprocessor_stats

    print_preprocessor_stats()
