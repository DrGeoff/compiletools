"""The one secure byte-copy for test-report artefacts, as an import leaf.

THE single implementation of the deposit/materialise copy's security
properties. ``filesystem_utils`` re-exports :func:`secure_publish_copy` so
in-process callers keep their existing import site.

**This module is deliberately a leaf: stdlib only, no ``compiletools``
imports, ever.** It is executed as a subprocess once per test-report copy
(twice per test on a fresh-workspace/warm-CAS build: one deposit, one
materialise), so its import graph is on the critical path of exactly the
builds the CAS exists to make fast. Living in ``filesystem_utils`` cost a
measured +18.4 ms per spawn over the inline snippet it replaced -- ~740 ms
of CPU on a 40-test build, ~5% wall clock -- and only 6.7 ms of that was
the security primitives themselves. The rest was ``stringzilla``,
``apptools_canonicalize``, ``subprocess`` and the rest of
``filesystem_utils``, none of which a byte copy needs.

Enforced two ways, because a docstring does not stop an import: the
``publish_copy-stays-a-leaf`` contract in ``.importlinter``, and
``test_publish_copy.py::test_module_imports_no_heavy_dependencies``, which
spawns a fresh interpreter and fails if the resolved module graph grows a
``compiletools`` or third-party entry. Adding an import here is a
performance regression on every warm CI build, so treat both as
load-bearing.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import sys

_SECURE_PUBLISH_MAX_ATTEMPTS = 100

# Temps carry this suffix so ``trim_cache.reclaim_orphan_temps`` reclaims a
# copy interrupted by SIGKILL. ``filesystem_utils`` re-exports it as
# ``_PUBLISH_TMP_SUFFIX``; that module's ``atomic_copy_bytes`` shares the
# suffix for the same reason.
PUBLISH_TMP_SUFFIX: str = ".publish.tmp"

QUIET_MISSING_SRC_FLAG = "--quiet-missing-src"


def copy_command(python: str, src: str, dst: str, *, quiet_missing_src: bool = False) -> list[str]:
    """Argv that performs one secure copy in a subprocess.

    THE single place the out-of-process invocation form is decided, shared by
    the deposit recipe (``build_graph.render_shell_recipe``) and the
    materialise recipe (``BuildBackend._build_copy_publish_rule``).

    Runs this module **by path** rather than as ``python -m
    compiletools.publish_copy``. Being a stdlib-only leaf is what makes that
    legal, and it is worth doing: ``-m`` pays both ``compiletools/__init__``
    (which imports ``version`` -> ``subprocess``) and ``runpy``'s import
    machinery, neither of which a byte copy needs. Measured per spawn against
    the inline snippet this replaced -- inline 29.6 ms, ``-m`` 41.4 ms,
    by-path 35.7 ms -- so by-path recovers roughly half the remaining
    overhead, on a path that runs once per test on warm cache-hit builds.

    Embedding an absolute module path keeps recipes byte-stable for a given
    install, the same property ``sys.executable`` (already embedded beside
    it) has. ``__file__`` is absolute on every supported Python; the ``-m``
    fallback covers an exotic loader that does not set it.
    """
    module_path = globals().get("__file__")
    argv = [python, module_path] if module_path else [python, "-m", "compiletools.publish_copy"]
    if quiet_missing_src:
        argv.append(QUIET_MISSING_SRC_FLAG)
    argv += [src, dst]
    return argv


def secure_publish_copy(src: str, dst: str, *, warn_missing_src: bool = True) -> None:
    """Byte-copy *src* to *dst*, resistant to symlink implantation by a peer
    sharing *dst*'s directory (a CAS pool).

    This is the ONE byte-copy path for test-report artefacts in BOTH
    directions: the workspace -> CAS deposit (a framework's JUnit XML into
    its content-addressed ``<cas_exe>.xml`` home) and the CAS -> workspace
    materialisation that puts it back under ``--test-xml-dir`` on a
    cache-hit build. Three callers route through it -- the shell-rendered
    deposit recipe (``render_shell_recipe`` in ``build_graph.py``), Shake's
    in-process deposit (``trace_backend._record_test_outcome``), and the
    materialise recipe (``BuildBackend._build_copy_publish_rule``), the
    two shell-rendered ones via ``python -m compiletools.publish_copy``
    -- so there is exactly one implementation of the copy's security
    properties, not several of differing strength.

    *warn_missing_src* suppresses the missing-*src* warning below. The
    materialise direction sets it False: a missing src there means the
    deposit already warned about the same misdetected test earlier in the
    same build, and repeating it on every later cache-hit build is noise.

    Two properties close the holes that a naive "hash of src" temp name or
    an unlink-then-reopen helper (``atomic_replace``) leave open:

    1. **Temp name is runtime-random, not a hash of anything guessable.**
       ``atomic_replace``-style helpers derive the temp name from ``dst``
       alone or from a hash of ``src`` -- both are computable by any peer
       who knows the workspace layout, so a peer can pre-create that exact
       path as a symlink before the real writer ever runs (implantation).
       Two peer workspaces publishing the *same* ``src`` path to the *same*
       shared ``dst`` also collide on the same hash-derived temp, so one
       peer's ``mv -f``/``os.rename`` loses the source and gets ``ENOENT``
       (a passing test reported failed). A ``secrets.token_hex`` name with
       retry-on-collision defeats both: unguessable defeats implantation,
       runtime-random-per-call defeats cross-peer collision.
    2. **The temp is populated through the SAME retained file descriptor
       used to create it, never by reopening the pathname.** ``os.open``
       with ``O_CREAT | O_EXCL | O_NOFOLLOW`` atomically creates-or-fails
       and refuses to follow a symlink placed at that exact path in the
       (vanishingly small, now-unguessable) race window; writing through
       that same fd means there is no second ``open()`` call for a peer to
       race against by replacing the path with a symlink in between
       (the gap ``atomic_replace`` + ``atomic_copy_bytes`` has: they close
       and unlink the O_EXCL temp, then ``shutil.copyfile`` reopens the
       bare pathname).

    The final ``os.replace`` is atomic against concurrent readers of
    ``dst`` and, like ``mv -f``, follows-and-replaces rather than writing
    through a symlink at ``dst`` itself (POSIX ``rename()`` never
    dereferences its target). The ``.publish.tmp`` suffix keeps orphans
    (a build killed mid-copy) reclaimable by
    ``trim_cache.reclaim_orphan_temps``, which matches on that suffix
    alone -- unaffected by the temp basename now being random instead of
    hash-derived.

    **A missing *src* is a warning, not an error.** This is only ever
    called on rc==0 (the test process exited successfully), so a missing
    *src* means framework detection misclassified the test -- it links
    something matched by ``//#PKG-CONFIG`` package-name substring or an
    unevaluated ``#include``, but its actual ``main()`` never writes the
    report the detected framework promised. The test genuinely passed;
    only the optional report is missing. Failing the build here would
    turn a passing test into a rendered infrastructure error (an absent
    file is an ``OSError``, caught by ``cake.py``'s ``_FATAL_ERROR_RENDERERS``
    and reported as "No such file or directory", masking the real,
    green, test-failures aggregate). So this returns quietly after a
    stderr warning naming the test and the missing path, leaving *dst*
    untouched -- the CAS is never touched by a report that was never
    produced. Callers still touch the ``success_marker`` afterwards --
    the marker asserts "the test passed," not "the report landed."
    """
    if not os.path.exists(src):
        if warn_missing_src:
            print(
                f"Warning: expected test report {src!r} not found after a passing test run "
                f"(rc==0) -- framework detection likely misclassified this test. Skipping "
                f"deposit to {dst!r}; the test result itself is unaffected.",
                file=sys.stderr,
            )
        return
    dst_dir = os.path.dirname(dst) or "."
    base = os.path.basename(dst)
    tmp_fd = None
    tmp_path = None
    for _ in range(_SECURE_PUBLISH_MAX_ATTEMPTS):
        candidate = os.path.join(dst_dir, f"{base}.{secrets.token_hex(16)}{PUBLISH_TMP_SUFFIX}")
        try:
            tmp_fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            continue
        tmp_path = candidate
        break
    if tmp_fd is None or tmp_path is None:
        raise OSError(f"secure_publish_copy: could not create a unique temp under {dst_dir!r}")

    try:
        with os.fdopen(tmp_fd, "wb") as tmp_f, open(src, "rb") as src_f:
            shutil.copyfileobj(src_f, tmp_f)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def main(argv: list[str] | None = None) -> int:
    """CLI entry point:
    ``python -m compiletools.publish_copy [--quiet-missing-src] <src> <dst>``.

    Thin argv wrapper around :func:`secure_publish_copy` so shell-rendered
    build recipes (make/ninja) can invoke the same secure copy Shake's
    in-process path uses, keeping the recipe text static (no per-build
    computed temp name) while the temp itself stays runtime-random.

    ``--quiet-missing-src`` is parsed by hand rather than through argparse:
    this runs twice per test on a warm build, so the recipe pays this
    module's import cost on the critical path, and a
    two-positional-plus-one-flag surface does not justify pulling argparse
    in. ``PINNED_CLI_TOOLS`` in ``test_entry_point_surface`` allowlists the
    same shape for ``cas_publish`` / ``ct_lock_helper``.
    """
    args = sys.argv[1:] if argv is None else argv
    warn_missing_src = True
    if args and args[0] == QUIET_MISSING_SRC_FLAG:
        warn_missing_src = False
        args = args[1:]
    if len(args) != 2:
        print(
            f"usage: python -m compiletools.publish_copy [{QUIET_MISSING_SRC_FLAG}] <src> <dst>",
            file=sys.stderr,
        )
        return 2
    secure_publish_copy(args[0], args[1], warn_missing_src=warn_missing_src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
