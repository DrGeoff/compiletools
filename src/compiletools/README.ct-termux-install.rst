==================
ct-termux-install
==================

----------------------------------------------------------------------
One-command compiletools dev bootstrap for Termux (Android, aarch64)
----------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-03
:Version: 8.2.3
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-termux-install [--skip-pkg] [--skip-venv] [--dry-run] [-y|--yes] [-h|--help]

DESCRIPTION
===========
``ct-termux-install`` bootstraps a complete compiletools development
environment on Termux. A plain ``uv pip install -e ".[dev]"`` does not work on
Termux for two independent reasons; this script handles both automatically:

1. **stringzilla fails to compile under Termux's clang 21+.**
   The compiler emits hard errors for incompatible function-pointer types in
   stringzilla's CPython slot tables and for const-discarding calls to
   ``free``/``realloc``/``munmap``. Older clang treated these as warnings.
   The script exports the four ``-Wno-error=...`` ``CFLAGS`` needed to
   downgrade them back to warnings before installing stringzilla. The
   resulting wheel is functionally identical -- this is a build-flag
   issue, not a runtime issue.

2. **ruff has no prebuilt wheel for android_30_arm64_v8a on PyPI.**
   ``uv`` would otherwise fall back to building the sdist via
   ``maturin`` -> ``cargo rustc``. The release profile uses
   ``-C lto=fat -C codegen-units=16``, which spikes RAM at the LTO link
   stage and OOM-kills the device (Android sends ``SIGKILL`` to the whole
   shell). The script installs the prebuilt aarch64 ruff binary via
   ``pkg install ruff`` and then asks ``uv`` to install every other dev
   dependency *except* ruff. Pre-commit and command-line ``ruff check``
   still work because the binary is on ``$PATH``.

The script is idempotent: re-running on a complete install hits only the
verify block.

PHASES
======
1. **Preflight.** Refuses to run anywhere other than Termux (checks
   ``$TERMUX_VERSION`` and ``/data/data/com.termux``). Refuses unless the
   script is sitting in a compiletools checkout (``pyproject.toml`` two
   levels up). Warns when available memory + free swap drops below 2 GB.

2. **Termux pkgs.** ``pkg install -y python clang nodejs ruff uv git`` --
   idempotent; already-installed packages are no-ops. ``ruff`` is the
   prebuilt aarch64 binary, ``nodejs`` is required by pyright at runtime,
   ``clang`` compiles stringzilla, and ``uv``/``git`` are the install
   tooling itself.

3. **Venv.** Creates ``$REPO_ROOT/.venv`` if it does not exist; reuses it
   otherwise. Sources the activate script. Sets ``UV_LINK_MODE=copy``
   because hardlinks across Termux's bind-mounted home are not always
   supported.

4. **stringzilla.** Skipped if already importable. Otherwise installs
   ``stringzilla>=4.6.0`` with the four-flag ``CFLAGS`` workaround.

5. **compiletools + dev tooling.** Editable install of compiletools, then
   ``bump-my-version pytest pytest-xdist pytest-cov pyright pre-commit
   textual``. The ``[dev]`` extra is deliberately not used because it
   would pull in ruff and trigger the cargo OOM.

6. **Verify.** Imports stringzilla, imports compiletools, and runs
   ``--version`` on ruff/pre-commit/pytest/pyright. Any failure aborts
   with a non-zero exit code.

OPTIONS
=======
``--skip-pkg``
    Skip the ``pkg install`` step. Use when the prerequisites have already
    been installed via ``pkg`` or are otherwise on ``$PATH``.

``--skip-venv``
    Skip ``uv venv`` creation. The script will error out if
    ``$REPO_ROOT/.venv/bin/python`` does not exist.

``--dry-run``
    Print every command that would be executed, but execute none of them.
    Useful for inspecting the planned actions before running for real.

``-y``, ``--yes``
    Do not prompt for confirmation on the low-memory warning. Implied by
    non-interactive shells in a future revision; explicit for now.

``-h``, ``--help``
    Print this help text and exit.

ENVIRONMENT
===========
``TERMUX_VERSION``
    The script's preferred Termux detector. Set automatically by Termux's
    own shell init.

``CFLAGS``
    Set inside the script for the duration of the stringzilla install only;
    cleared immediately afterwards so the rest of the install is not
    affected. Any pre-existing ``CFLAGS`` is overwritten during that
    window.

``UV_LINK_MODE``
    Forced to ``copy``. Hardlinking from uv's cache to the venv fails on
    some Termux storage backends; copy mode adds milliseconds and avoids
    the issue.

EXAMPLES
========

Full bootstrap from a fresh checkout::

    git clone https://github.com/DrGeoff/compiletools
    cd compiletools
    scripts/ct-termux-install

Re-run after pulling new commits (everything is idempotent)::

    scripts/ct-termux-install --skip-pkg

Inspect the plan without executing::

    scripts/ct-termux-install --dry-run

Run unattended (no prompts even on low-memory warning)::

    scripts/ct-termux-install -y

EXIT STATUS
===========
``0``
    Bootstrap (or dry run) completed successfully and every verification
    check passed.

``1``
    Preflight failed (not on Termux, not in a compiletools checkout, low
    memory and user declined, or one or more verification checks failed).

``2``
    Unknown command-line option.

SEE ALSO
========
The ``Termux (Android, aarch64)`` subsection of the project ``INSTALL``
file documents the underlying recipe step-by-step for users who would
rather run it manually.
