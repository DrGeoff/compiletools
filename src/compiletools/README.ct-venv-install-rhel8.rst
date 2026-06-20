=====================
ct-venv-install-rhel8
=====================

------------------------------------------------------------------------
One-command compiletools dev bootstrap for RHEL 8 / CentOS 8 / Rocky 8
------------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-07
:Version: 10.1.10
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-venv-install-rhel8 [--skip-pkg] [--skip-venv] [--dry-run] [-y|--yes]
                      [--gcc=PATH] [--python=SPEC] [-h|--help]

DESCRIPTION
===========
``ct-venv-install-rhel8`` bootstraps a complete compiletools development
environment on the RHEL 8 family (RHEL 8, CentOS 8, Rocky 8, AlmaLinux 8,
Oracle Linux 8). A plain ``uv pip install -e ".[tui,dev]"`` does not work
out-of-the-box on RHEL 8 for two independent reasons; this script handles
both automatically:

1. **The system Python is too old, and AppStream tops out below 3.13.**
   RHEL 8 ships ``python3`` as CPython 3.6 (below the >=3.10 floor) and
   the newest interpreter in AppStream is older than 3.13. Rather than
   ride a stale interpreter, the script asks ``uv venv --python 3.13`` to
   download and cache a managed CPython 3.13 build on demand. Override
   with ``--python=3.14`` (or any other uv version spec / binary / path)
   if you want a different interpreter.

2. **The system GCC cannot compile stringzilla's AVX-512 intrinsics, AND
   the bundled default variant needs gcc 14+.** RHEL 8's default ``gcc``
   (8.x, with the older AppStream branch shipping GCC 10) is below the
   GCC 12 floor that stringzilla 4.6+ requires for its AVX-512 paths,
   and below the GCC 14 floor that the default ``ct-cake`` variant
   ``gcc.cxx26.debug`` requires for substantially-complete C++26. The
   script discovers a usable modern GCC in this order: ``--gcc=PATH``
   override, then the system ``gcc`` if it is already 12+, then the
   most recent ``gcc-toolset-{14,13,12}`` enable script under ``/opt/rh/``.
   If none is present, ``gcc-toolset-14`` is added to the ``dnf install``
   set (satisfies both floors with one toolchain). Stringzilla is then
   compiled with ``CC`` pointed at the modern GCC and the four AVX-512
   ``CFLAGS`` prescribed by the ``INSTALL`` recipe
   (``-mavx512f -mavx512bw -mavx512vl -mavx512vbmi``). The flags are a
   *compile-time* requirement so the compiler will emit code for those
   intrinsics; stringzilla still does runtime CPU dispatch, so the
   resulting wheel works on hosts without AVX-512.

   If only a 12 or 13 toolset is already installed, the script accepts
   it (skips the ``dnf install``) and emits a post-install warning: the
   default variant won't work, but ``ct-cake --variant=gcc,cxx20,debug``
   (or any lower C++ standard) will.

Unlike the Termux recipe, the ``[dev]`` extra *is* used here (extended
to ``[tui,dev]`` to pull in textual for the timing TUI): ruff has a
prebuilt manylinux x86_64 wheel on PyPI, so ``uv`` does not fall back to
the cargo build that OOMs aarch64 Android.

PREREQUISITES
=============
``uv`` must already be on ``$PATH``. The script does not install it --
provision it via your environment's preferred mechanism (vendor binary,
distro package, environment module, or a manual
``python3 -m pip install --user uv``) before running. The script aborts
in preflight if ``uv`` is not found.

The script is otherwise idempotent: re-running on a complete install
hits only the verify block.

PHASES
======
1. **Preflight.** Refuses to run anywhere other than the RHEL 8 family
   (parses ``/etc/os-release`` for ``ID=rhel|centos|rocky|almalinux|ol``
   and ``VERSION_ID=8.*``). Refuses unless the script is sitting in a
   compiletools checkout (``pyproject.toml`` two levels up). Refuses
   unless ``uv`` is already on ``$PATH``.

2. **dnf pkgs (only the missing ones).** Each prerequisite is added to
   the ``dnf install`` set only if it is not already satisfied:

   * ``git`` -- skipped if already on ``$PATH``.
   * ``gcc-toolset-14`` -- skipped if ``--gcc=PATH`` is given, if the
     system ``gcc`` is already 12+, or if any of
     ``/opt/rh/gcc-toolset-{14,13,12}/enable`` exists. 14 (not 13) is
     the install target because the bundled default variant
     ``gcc.cxx26.debug`` needs gcc 14+.

   If both are present, the ``dnf install`` step is skipped entirely --
   no ``sudo`` prompt. Otherwise ``sudo dnf install -y`` runs only with
   the missing pkgs; already-installed pkgs in that list are no-ops.

   Python is intentionally not installed here -- ``uv`` will fetch a
   managed interpreter for the venv. ``nodejs`` is intentionally not
   installed -- the ``pyright`` PyPI wheel ships its own
   node-bootstrap, which downloads a Node runtime into the venv on
   first invocation; a system ``nodejs`` would only shadow it.

3. **Modern GCC.** Resolves the GCC the stringzilla compile will use, in
   priority order: ``--gcc=PATH`` override, system ``gcc`` if already 12+,
   ``/opt/rh/gcc-toolset-{14,13,12}/enable``. The toolset ``enable``
   script is sourced into the installer's own shell so the chosen GCC's
   ``ld`` and ``binutils`` are picked up too. **The toolset is not
   persisted to the user's shell rc** -- it is only needed for the
   stringzilla build, not for compiletools' day-to-day operation.

4. **Venv.** Creates ``$REPO_ROOT/.venv`` with ``uv venv --python
   $PYTHON_SPEC $REPO_ROOT/.venv`` (default spec: ``3.13``) if it does
   not exist; reuses it otherwise. With a bare version spec, ``uv``
   downloads and caches a managed CPython build on first use, so this
   step works on RHEL 8 hosts whose AppStream Python is older than 3.13.
   Sources the activate script.

5. **stringzilla.** Skipped if already importable. Otherwise installs
   ``stringzilla>=4.6.0`` with ``CC`` and ``CFLAGS`` set to the modern
   GCC and the AVX-512 enable flags. Pre-existing ``CC``/``CFLAGS`` are
   saved and restored around the install.

6. **compiletools + dev/tui tooling.** ``uv pip install -e
   $REPO_ROOT[tui,dev]`` -- editable install plus ``bump-my-version``,
   ``pytest``, ``pytest-xdist``, ``pytest-cov``, ``pyright``,
   ``prek``, ``ruff``, and ``textual`` (the latter from the
   ``[tui]`` extra, used by the timing TUI).

7. **Verify.** Imports stringzilla, compiletools, and textual; runs
   ``--version`` on ruff/prek/pytest/pyright. Any failure aborts
   with a non-zero exit code.

OPTIONS
=======
``--skip-pkg``
    Skip the ``dnf install`` step. Use when the prerequisites have
    already been installed (e.g. by an admin) or are otherwise satisfied
    on the host.

``--skip-venv``
    Skip ``uv venv`` creation. The script will error out if
    ``$REPO_ROOT/.venv/bin/python`` does not exist.

``--dry-run``
    Print every command that would be executed, but execute none of them.
    Useful for inspecting the planned actions before running for real.

``-y``, ``--yes``
    Do not prompt for confirmation. Currently no interactive prompts
    exist in the RHEL 8 path; reserved for future preflight checks.

``--gcc=PATH``
    Use the GCC at ``PATH`` instead of auto-detecting one. The path must
    be executable and report a major version of 12 or newer. Useful when
    the modern GCC is provided outside ``gcc-toolset`` (vendor install,
    custom build, third-party package, etc.).

``--python=SPEC``
    Python spec passed through to ``uv venv --python`` (default:
    ``3.13``). Accepts a bare version (``3.13``, ``3.14``), a binary
    name (``python3.13``), or an absolute path. Bare versions cause
    ``uv`` to fetch a managed CPython build on demand. Must resolve to
    an interpreter ``>=3.10``.

``-h``, ``--help``
    Print this help text and exit.

ENVIRONMENT
===========
``CC``
    Set inside the script for the duration of the stringzilla install
    only; saved and restored around that window so the rest of the
    install (and the parent shell, if exported) is unaffected.

``CFLAGS``
    Same handling as ``CC``: set to the four AVX-512 enable flags only
    while stringzilla is being compiled.

EXAMPLES
========

Full bootstrap from a fresh checkout::

    git clone https://github.com/DrGeoff/compiletools
    cd compiletools
    scripts/ct-venv-install-rhel8

Re-run after pulling new commits (everything is idempotent)::

    scripts/ct-venv-install-rhel8 --skip-pkg

Inspect the plan without executing::

    scripts/ct-venv-install-rhel8 --dry-run

Use a modern GCC provided outside ``gcc-toolset`` (point ``--gcc`` at
the binary directly)::

    scripts/ct-venv-install-rhel8 --gcc=/opt/gcc-13/bin/gcc

Use Python 3.14 (uv-managed) instead of the default 3.13::

    scripts/ct-venv-install-rhel8 --python=3.14

EXIT STATUS
===========
``0``
    Bootstrap (or dry run) completed successfully and every verification
    check passed.

``1``
    Preflight failed (not on RHEL 8 family, not in a compiletools
    checkout, no usable modern GCC, or one or more verification checks
    failed).

``2``
    Unknown command-line option.

SEE ALSO
========
The ``stringzilla on RHEL 8 / older GCC`` subsection of the project
``INSTALL`` file documents the underlying recipe step-by-step for users
who would rather run it manually.

The companion ``ct-venv-install-termux`` script handles the analogous
bootstrap for Termux (Android, aarch64), where the obstacles are the
opposite shape: too-new clang plus an OOM-prone cargo build for ruff.
