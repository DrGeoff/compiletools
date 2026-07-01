"""Unit tests for BuildContext.

Compiler-free and network-free: BuildContext is a plain state/cache holder,
so these exercise its documented contracts directly.
"""

from __future__ import annotations

import copy
import types

from compiletools.build_context import BuildContext


def test_deepcopy_returns_self() -> None:
    """copy.deepcopy(ctx) must return the SAME context, not a clone.

    A BuildContext holds per-build caches whose values are not deep-copyable
    (stringzilla.Str, a BuildTimer owning a threading.Lock). Some code paths
    deep-copy an args namespace that transitively references the live context
    via args._context (e.g. fetch._augmented_headerdeps). Sharing the one
    context by reference is what keeps that deepcopy cheap and non-crashing.
    """
    ctx = BuildContext()
    copied = copy.deepcopy(ctx)
    assert copied is ctx, "BuildContext.__deepcopy__ must return self (identity contract)"


def test_deepcopy_of_namespace_shares_context_but_copies_siblings() -> None:
    """Deep-copying an args-like namespace keeps the SAME context by reference
    while genuinely deep-copying sibling mutable attributes.

    This is the exact shape fetch._augmented_headerdeps relies on: it
    ``copy.deepcopy``s args (which carries ``_context``) to append throwaway
    ``-I`` flags, and passes the *real* context through explicitly. The context
    must survive as the shared live object; unrelated mutable state must not.
    """
    ctx = BuildContext()
    ns = types.SimpleNamespace(_context=ctx, siblings=["a", "b"])

    copied = copy.deepcopy(ns)

    # The context is shared by reference (return-self contract).
    assert copied._context is ctx
    # A sibling mutable attribute is genuinely deep-copied, not aliased.
    assert copied.siblings == ["a", "b"]
    assert copied.siblings is not ns.siblings
    copied.siblings.append("c")
    assert ns.siblings == ["a", "b"]
