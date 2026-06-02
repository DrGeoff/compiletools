"""Whole-``BuildGraph`` golden + determinism regression net.

This module is the safety net for the upcoming refactor that decomposes
``BuildBackend.build_graph()`` into phase helpers (see Plan 01). Any change
to the *set*, *order*, or *shape* of the rules ``build_graph()`` emits will
trip one of the two tests here:

* :func:`test_build_graph_is_deterministic` -- builds the graph twice from
  the same inputs and asserts full-fidelity equality. This catches any
  source of nondeterminism (set iteration order, dict ordering, time- or
  pid-seeded tokens) regardless of checkout location. It compares the
  *raw* (workspace-absolute) serialization, so it is maximally strict.

* :func:`test_build_graph_matches_golden` -- asserts a *normalized*
  serialization equals a committed golden. The normalization strips every
  machine- / checkout- / toolchain-dependent token (the pytest tmpdir, the
  package + gitroot path prefixes, the compiler basename, the variant
  directory component, and the content-addressable hashes that fold in
  ``compiler_identity``) down to stable ``<PLACEHOLDER>`` sentinels. What
  survives is the structural skeleton: rule types, the emission/ordering of
  rules, and the *shape* of each output path and command. That skeleton is
  exactly what the helper-extraction refactor must preserve.

Fixture choice
--------------
``examples-end-to-end/calculator/`` (``main.cpp`` -> ``calculator.{h,cpp}``
-> ``add.{H,C}``), **copied into the pytest tmpdir** before building. The
multi-source graph exercises three **compile** rules (header-driven implied
-source discovery: ``calculator.h`` -> ``calculator.cpp``, ``add.H`` ->
``add.C``), the **link** rule, the **symlink** CAS-exe publish, the
per-bucket / variant-root **mkdir** rules, and the **phony** ``build`` /
``all`` aggregates -- a genuinely non-trivial graph built in ~a second with
no real compilation (graph construction only; no ``execute()``).

Why copy into the tmpdir rather than build the example in place? This dev
environment is a *worktree* whose editable install points ``compiletools``
at a sibling checkout (``master/``) while the gitroot resolves to the
worktree (``arch-cleanup/``). Building the example from the package dir
makes ``find_git_root()`` and the example's own location disagree, leaking a
second absolute checkout path into the graph that no single anchor can
canonicalize. Copying every source into the tmpdir collapses both into the
``<TMP>`` anchor, mirroring the established ``write_failing_gtest_fixture``
pattern and keeping the golden checkout-independent.

The ``--variant=gcc.debug`` pin keeps the variant directory component out
of the toolchain-default-``-std`` drift (e.g. ``gcc.cxx26.debug``); the
golden normalizes the variant segment regardless so a clang-default machine
still matches.
"""

from __future__ import annotations

import os
import re
import shutil

import pytest

import compiletools.apptools as apptools
import compiletools.examples_registry as examples_registry
import compiletools.testhelper as uth
from compiletools.build_graph import BuildGraph
from compiletools.makefile_backend import MakefileBackend

# Golden lives next to this test as a committed artefact. Regenerate with
# ``CT_UPDATE_GOLDEN=1 pytest ...::test_build_graph_matches_golden``.
_GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_build_graph_golden.txt")

# The example whose graph we snapshot. ``main.cpp`` is the root TU; the
# remaining files are pulled in by implied-source discovery + header deps.
_EXAMPLE = "calculator"
_EXAMPLE_FILES = ("main.cpp", "calculator.cpp", "calculator.h", "add.C", "add.H")
_EXAMPLE_ROOT_TU = "main.cpp"


def _build_graph(tmp_path) -> tuple[MakefileBackend, BuildGraph]:
    """Construct a real ``(backend, graph)`` from the calculator example.

    Copies the example sources into *tmp_path* (so every path the graph
    references anchors under the single ``<TMP>`` root -- see module
    docstring), then drives the full apptools -> headerdeps -> magicflags ->
    Hunter -> backend pipeline via :func:`testhelper.build_real_backend`.
    ``--variant=gcc.debug`` pins the variant so the directory component is
    stable across runs; no compiler is invoked -- only the graph is built.
    """
    tmp_path = str(tmp_path)
    os.makedirs(tmp_path, exist_ok=True)
    for name in _EXAMPLE_FILES:
        shutil.copy(examples_registry.example_file(f"{_EXAMPLE}/{name}"), os.path.join(tmp_path, name))
    root_tu = os.path.join(tmp_path, _EXAMPLE_ROOT_TU)
    return uth.build_real_backend(
        MakefileBackend,
        tmp_path,
        [root_tu],
        extra_argv=["--variant=gcc.debug"],
    )


def _serialize_raw(backend: MakefileBackend, graph: BuildGraph) -> str:
    """Stable, output-sorted serialization of a BuildGraph.

    Each rule is rendered as a tuple
    ``(rule_type, output, sorted(inputs), command, cwd)`` with paths
    canonicalized to be checkout-independent (the pytest tmpdir, the package
    examples root, and the gitroot are each rewritten to a sentinel). Rules
    are sorted by output for a deterministic ordering.

    This is the *raw* form: it preserves the canonicalized absolute path
    tails and the content hashes, so it is suitable for the determinism
    assertion (two builds from identical inputs must produce byte-identical
    output) but NOT for a committed golden (hashes fold in compiler_identity
    and so vary across machines).
    """
    anchors = _anchor_map(backend, str(_tmp_root(graph)))

    def canon_path(p: str) -> str:
        return _canon_path(p, anchors)

    def canon_token(tok: str) -> str:
        # Reuse the production token canonicalizer for path-bearing flags
        # (-I, -isystem, -Wl,..., -ffile-prefix-map=, ...) against the
        # gitroot, then sweep the remaining anchors over every token so the
        # tmpdir / package prefixes are normalized in plain path arguments
        # too (e.g. the bare ``-c <src>`` operand, ``-o <obj>``).
        (canon,) = apptools.canonicalize_for_cache_key([tok], backend._anchor_root)
        return _canon_path(canon, anchors)

    lines: list[str] = []
    for rule in sorted(graph.rules, key=lambda r: r.output):
        output = canon_path(rule.output)
        inputs = sorted(canon_path(i) for i in rule.inputs)
        if rule.command is None:
            command = None
        else:
            command = [canon_token(t) for t in rule.command]
        cwd = canon_path(rule.cwd) if rule.cwd else None
        lines.append(repr((rule.rule_type, output, inputs, command, cwd)))
    return "\n".join(lines) + "\n"


def _serialize_golden(backend: MakefileBackend, graph: BuildGraph) -> str:
    """Checkout- AND machine-independent serialization for the committed golden.

    Starts from :func:`_serialize_raw`, then normalizes the remaining
    volatile tokens that legitimately differ across machines / toolchains
    but say nothing about the graph's *structure*:

    * content-addressable hashes (obj / exe / lib / pch), which fold in
      ``compiler_identity`` (realpath|size|mtime_ns) -> ``<HASH>``;
    * the variant directory component (``gcc.debug`` / ``clang.debug`` /
      ``gcc.cxx26.debug`` depending on the resolved compiler) -> ``<VARIANT>``;
    * the compiler / linker basename (``g++`` / ``clang++`` / ``gcc``) and
      the cwd-relative source spelling the backend may emit -> ``<CXX>`` etc.
    """
    raw = _serialize_raw(backend, graph)
    return _normalize_volatile(raw)


def _tmp_root(graph: BuildGraph) -> str:
    """Recover the pytest tmpdir from the graph's mkdir outputs.

    ``build_real_backend`` roots ``obj`` / ``bin`` / ``cas-*`` under the
    tmpdir; the ``bin`` mkdir output is exactly ``<tmp>/bin``. Deriving it
    from the graph keeps the serializer self-contained (no need to thread
    tmp_path through every call site)."""
    for rule in graph.rules:
        if rule.rule_type == "mkdir" and rule.output.endswith("/bin"):
            return os.path.dirname(rule.output)
    # Fallback: longest common prefix of all outputs.
    return os.path.commonpath([r.output for r in graph.rules if r.output.startswith("/")])


def _anchor_map(backend: MakefileBackend, tmp_root: str) -> list[tuple[str, str]]:
    """Ordered (prefix, sentinel) anchors, longest-prefix-first.

    The example sources resolve through the editable-install package root
    (which may be a *different* checkout than the gitroot worktree), so we
    must canonicalize against the package examples dir AND the gitroot AND
    the tmpdir. Order matters: more specific / longer prefixes first so a
    nested anchor wins over a parent.
    """
    pkg_root = examples_registry._PACKAGE_ROOT
    anchors = [
        (tmp_root, "<TMP>"),
        (pkg_root, "<PKG>"),
    ]
    if backend._anchor_root:
        anchors.append((backend._anchor_root, "<GITROOT>"))
    # Longest prefix first.
    anchors.sort(key=lambda pair: len(pair[0]), reverse=True)
    return anchors


def _canon_path(p: str, anchors: list[tuple[str, str]]) -> str:
    """Rewrite the first matching anchor prefix in ``p`` to its sentinel."""
    for prefix, sentinel in anchors:
        if not prefix:
            continue
        prefix = prefix.rstrip("/")
        if p == prefix:
            return sentinel
        if p.startswith(prefix + "/"):
            return sentinel + p[len(prefix) :]
    return p


# Variant directory component: appears as a path segment, e.g.
# ``<TMP>/obj/gcc.debug/93/...``, ``<TMP>/cas-pchdir/clang.cxx26.debug/...``,
# or as a bare trailing segment ``<TMP>/obj/gcc.debug`` (the variant-root
# mkdir). Match a dotted token whose first segment is a known toolchain
# family, bounded by ``/`` on the left and ``/`` or the segment end on the
# right (the closing path-quote in the repr).
_VARIANT_RE = re.compile(r"/(?:gcc|clang)(?:\.[A-Za-z0-9_+]+)+(?=/|')")

# Content-addressable hash runs: 12+ hex chars. Covers the obj triple-hash
# (``_<h12>_<h14>_<h16>``), the exe/lib link key, the pch cmd hash, and any
# compiler-identity-derived token. Bounded by non-hex-or-underscore so the
# underscore-joined obj triple is each normalized in turn (a plain ``\b``
# would not fire between ``_`` and a hex digit).
_HASH_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{12,}(?![0-9a-fA-F])")

# CAS bucket shard: a 2-hex path segment immediately after the variant
# component (e.g. ``/cas-exedir/<VARIANT>/db/`` or ``/obj/<VARIANT>/93/``).
# The exe/lib bucket derives from the link key (folds compiler_identity) and
# so varies across machines; normalize it. Run after _VARIANT_RE so the
# ``<VARIANT>`` sentinel is already in place to anchor the match.
_BUCKET_RE = re.compile(r"(?<=<VARIANT>/)[0-9a-f]{2}(?=/|')")

# Compiler / linker basename as argv[0] (and as a quoted token). Normalize
# the concrete tool name so a clang machine matches a gcc-authored golden.
# The trailing names end in ``+`` (g++/clang++), which is not a word char,
# so the right boundary is an explicit closing quote rather than ``\b``.
_COMPILER_RE = re.compile(r"(?<=')(?:g\+\+|gcc|clang\+\+|clang|c\+\+|cc)(?=')")

# -std=... reflects the resolved toolchain default; normalize the value.
_STD_RE = re.compile(r"-std=[A-Za-z0-9+]+")


def _normalize_volatile(text: str) -> str:
    """Replace machine- / toolchain-dependent tokens with stable sentinels.

    Order matters:

    * ``_VARIANT_RE`` first (it contains a toolchain family name) so
      ``gcc.debug`` -> ``<VARIANT>`` is not half-eaten by the compiler sweep;
    * ``_BUCKET_RE`` next -- it anchors on the just-inserted ``<VARIANT>``;
    * ``_HASH_RE`` next, before the compiler sweep, so a hash that happens
      to spell ``cc``/``gcc`` substrings is consumed as a hash;
    * ``_COMPILER_RE`` last.
    """
    text = _VARIANT_RE.sub("/<VARIANT>", text)
    text = _BUCKET_RE.sub("<B>", text)
    text = _HASH_RE.sub("<HASH>", text)
    text = _STD_RE.sub("-std=<STD>", text)
    text = _COMPILER_RE.sub("<CXX>", text)
    return text


@uth.requires_functional_compiler
def test_build_graph_is_deterministic(tmp_path):
    """Two builds from identical inputs must produce byte-identical graphs.

    This is the whole-graph equality the test suite previously lacked --
    individual ``test_build_backend`` cases assert *properties* of the
    graph, never that the entire emission is reproducible. Compares the raw
    (un-normalized) serialization so even a hash-level nondeterminism is
    caught.

    Both builds target the *same* tmpdir: the CAS link-key folds in the
    canonical bindir (CLAUDE.md: "full canonical bindir"), so building into
    two different tmpdirs legitimately yields different exe/lib hashes --
    that is correct behaviour, not nondeterminism. "Same inputs" therefore
    means same workspace root.
    """
    backend1, graph1 = _build_graph(tmp_path)
    backend2, graph2 = _build_graph(tmp_path)

    s1 = _serialize_raw(backend1, graph1)
    s2 = _serialize_raw(backend2, graph2)

    assert s1 == s2, (
        "build_graph() is not deterministic: two builds from identical "
        "inputs produced different (canonicalized) graphs.\n"
        f"--- build 1 ---\n{s1}\n--- build 2 ---\n{s2}"
    )
    # Sanity: the fixture must yield a non-trivial graph or the snapshot is
    # worthless as a regression net.
    types = {r.rule_type for r in graph1.rules}
    assert {"compile", "link", "symlink", "phony", "mkdir"} <= types, (
        f"calculator fixture produced a degenerate graph (types={sorted(types)}); "
        "the golden would not protect compile/link/symlink structure"
    )


@uth.requires_functional_compiler
def test_build_graph_matches_golden(tmp_path):
    """Normalized graph serialization must equal the committed golden.

    Regenerate after an *intended* change to ``build_graph()``::

        CT_UPDATE_GOLDEN=1 python -m pytest \\
            src/compiletools/test_build_graph_golden.py::test_build_graph_matches_golden

    and review the diff -- an unexpected change here means the rule set or
    ordering moved, which the helper-extraction refactor must not do.
    """
    backend, graph = _build_graph(tmp_path)
    actual = _serialize_golden(backend, graph)

    if os.environ.get("CT_UPDATE_GOLDEN") == "1":
        with open(_GOLDEN_PATH, "w") as fh:
            fh.write(actual)
        pytest.skip(f"golden regenerated at {_GOLDEN_PATH}")

    assert os.path.exists(_GOLDEN_PATH), (
        f"golden missing at {_GOLDEN_PATH}; regenerate with "
        f"CT_UPDATE_GOLDEN=1 python -m pytest {os.path.basename(__file__)}"
    )
    with open(_GOLDEN_PATH) as fh:
        expected = fh.read()

    assert actual == expected, (
        "BuildGraph golden mismatch -- the set/order/shape of rules emitted "
        "by build_graph() changed. If intentional, regenerate with "
        "CT_UPDATE_GOLDEN=1 and review the diff.\n"
        f"--- expected (golden) ---\n{expected}\n--- actual ---\n{actual}"
    )
