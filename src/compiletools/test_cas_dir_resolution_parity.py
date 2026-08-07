"""Differential drift guard over the two cas-dir resolution paths.

compiletools resolves a ``--cas-*dir`` twice, by two disjoint code paths:

* **build** -- ``gather_inputs`` (``build_inputs._anchored_cas_dir``) feeds the
  raw value to the pure ``stage_resolve_names``, and ``populate_args`` writes
  the result back onto the namespace. This is what ct-cake uses.
* **diagnostics** -- ``apptools_argparse.resolve_cas_directory_arguments``,
  called by ct-cache-report / ct-trim-cache / ct-cleanup-locks, which parse with
  ``cap.parse_args`` and never reach the gather->compute pipeline.

They name the same on-disk pools: one writes them, the other scans and trims
them. A divergence is silent -- the tool reports an empty cache, or trims a
directory nothing wrote -- so this test drives both paths over the same matrix
and asserts they agree character for character.

Sharing is now structural (``build_state.cas_dir_name``,
``build_state.canonical_variant_name``, and
``apptools_argparse.anchor_cas_dir_to_gitroot`` are each the single
implementation, called from both sides), and this test is what keeps it that
way. Measured against the pre-sharing code, all three tests fail: 24 of the 64
canonical-variant cells and 64 of 64 reordered-variant cells diverge.
"""

from __future__ import annotations

import argparse
import pathlib

import compiletools.git_utils
import compiletools.wrappedos
from compiletools.apptools_argparse import resolve_cas_directory_arguments
from compiletools.build_context import BuildContext
from compiletools.build_inputs import gather_inputs
from compiletools.build_state import stage_resolve_names
from compiletools.trim_cache import cell_pool_root

# Every shape the two paths handle differently at some layer: the unsupplied
# sentinel (default derivation), explicit-empty (disabled -- must NOT become
# the gitroot), absolute, bare-relative and dot-relative (the anchoring gate),
# redundant separators (normalization), the root path (where a naive
# ``+ "/" +`` builds an implementation-defined ``//``), and a value already
# carrying the suffix (idempotence).
RAW_VALUES = (
    "unsupplied",
    "",
    "/abs/pool",
    "relpool",
    "./pool",
    "/cas//obj",
    "/cas/./obj",
    "/",
)
KINDS = (("cas_objdir", "obj"), ("cas_pchdir", "pch"), ("cas_pcmdir", "pcm"), ("cas_exedir", "exe"))


def _repo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A gitroot and a subdir of it. Both cwds are needed: the anchoring gate
    only fires when the invocation cwd differs from the gitroot."""
    repo = tmp_path / "repo"
    sub = repo / "sub"
    sub.mkdir(parents=True)
    git_dir = repo / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return repo, sub


def _clear_caches():
    """Both paths call ``find_git_root`` and the cached ``wrappedos`` stats,
    and the matrix walks two cwds -- a cached answer from the previous cell
    would resolve the next one against the wrong root."""
    compiletools.wrappedos.clear_cache()
    compiletools.git_utils.clear_cache()


def _diagnostics_path(variant, attr, raw):
    args = argparse.Namespace(verbose=0, variant=variant, **{attr: raw})
    resolve_cas_directory_arguments(args)
    return getattr(args, attr), args.variant


def _build_path(variant, attr, raw):
    args = argparse.Namespace(verbose=0, variant=variant, **{attr: raw})
    names = stage_resolve_names(gather_inputs(args, BuildContext()))
    return getattr(names, attr), names.variant


def _mismatches(cwd, variant, monkeypatch):
    monkeypatch.chdir(cwd)
    out = []
    for attr, _kind in KINDS:
        for raw in RAW_VALUES:
            _clear_caches()
            diagnostics = _diagnostics_path(variant, attr, raw)
            _clear_caches()
            build = _build_path(variant, attr, raw)
            if diagnostics != build:
                out.append(f"{attr} raw={raw!r} cwd={cwd.name}: diagnostics={diagnostics!r} build={build!r}")
    return out


def test_both_paths_name_the_same_cas_dir(tmp_path, monkeypatch):
    """The whole matrix, from the gitroot and from a subdir of it."""
    repo, sub = _repo(tmp_path)
    mismatches = _mismatches(repo, "gcc.debug", monkeypatch) + _mismatches(sub, "gcc.debug", monkeypatch)
    assert not mismatches, "the diagnostic tools and a build disagree on the pool path:\n" + "\n".join(mismatches)


def test_both_paths_canonicalize_a_reordered_variant_the_same_way(tmp_path, monkeypatch):
    """The variant IS the last path component, so canonicalization has to be
    shared too. A build canonicalizes ``debug.gcc`` to ``gcc.debug`` (via
    ``stage_resolve_names``); a diagnostic tool that kept the literal spelling
    would scan ``cas-objdir/debug.gcc``, which no build ever writes."""
    repo, sub = _repo(tmp_path)
    mismatches = _mismatches(repo, "debug.gcc", monkeypatch) + _mismatches(sub, "debug.gcc", monkeypatch)
    assert not mismatches, "the diagnostic tools and a build disagree on the pool path:\n" + "\n".join(mismatches)


def test_resolver_leaves_the_canonical_variant_on_args_for_cell_pool_root(tmp_path, monkeypatch):
    """Why the resolver assigns ``args.variant`` rather than canonicalizing
    locally: ct-cleanup-locks and ct-cache-report strip the suffix back off
    with ``cell_pool_root(args.cas_objdir, args.variant)``, so the two have to
    be the same string. With the literal spelling left in place the strip
    misses and the pool root keeps a variant component."""
    repo, _sub = _repo(tmp_path)
    monkeypatch.chdir(repo)
    _clear_caches()
    args = argparse.Namespace(verbose=0, variant="debug.gcc", cas_objdir=str(repo / "pool"))
    resolve_cas_directory_arguments(args)

    assert args.variant == "gcc.debug"
    assert args.cas_objdir == str(repo / "pool" / "gcc.debug")
    assert cell_pool_root(args.cas_objdir, args.variant) == str(repo / "pool")
