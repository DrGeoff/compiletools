"""Lock-wrapper command emission for the Make and Ninja build backends.

These free functions translate a bare compile/link command into a
lock-wrapped shell command suitable for embedding in a native build file.
They are the makefile-level counterpart to ``locking.atomic_compile`` /
``locking.atomic_link`` (the in-Python path used by Shake).

This module is a deliberately thin lower layer: it imports only stdlib plus
genuinely-leaf compiletools modules so that ``build_backend`` can re-export
these names without creating an import cycle. ``build_backend`` binds them
back into its own namespace, preserving object identity for both call sites
inside ``BuildBackend`` and ``unittest.mock.patch`` targets.
"""

from __future__ import annotations

import functools
import shlex
import shutil

import compiletools.filesystem_utils


@functools.lru_cache(maxsize=1)
def _native_flock_available() -> bool:
    """Check if native flock binary (util-linux) is available."""
    return shutil.which("flock") is not None


def _build_lock_env_prefix(strategy: str, args, filesystem_type: str) -> str:
    """Build the CT_LOCK_* environment variable prefix for ct-lock-helper.

    Args:
        strategy: Lock strategy (lockdir, fcntl, cifs, flock)
        args: Namespace with sleep_interval_lockdir, sleep_interval_cifs,
              sleep_interval_flock_fallback, lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Space-terminated env var prefix string, or empty string if no vars needed.
    """
    env_vars = []

    if strategy == "lockdir":
        if args.sleep_interval_lockdir is not None:
            sleep_interval = args.sleep_interval_lockdir
        else:
            sleep_interval = compiletools.filesystem_utils.get_lockdir_sleep_interval(filesystem_type)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL={sleep_interval}")
    elif strategy == "fcntl":
        pass  # fcntl.lockf() blocks in kernel, no sleep interval needed
    elif strategy == "cifs":
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_CIFS={args.sleep_interval_cifs}")
    else:  # flock (fallback when native flock unavailable)
        env_vars.append(f"CT_LOCK_SLEEP_INTERVAL_FLOCK={args.sleep_interval_flock_fallback}")

    env_vars.append(f"CT_LOCK_WARN_INTERVAL={args.lock_warn_interval}")
    env_vars.append(f"CT_LOCK_TIMEOUT={args.lock_cross_host_timeout}")

    return " ".join(env_vars) + " " if env_vars else ""


def wrap_compile_with_lock(compile_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a compile command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per compilation.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Shared by Make and Ninja backends. When args.file_locking is False,
    returns the command with ``-o target`` appended unchanged.

    Args:
        compile_cmd: Compile command without -o flag (e.g., "gcc -c file.c")
        target: Target file (e.g., "$@" for Make, or an actual path for Ninja)
        args: Namespace with file_locking, sleep_interval_lockdir,
              sleep_interval_cifs, sleep_interval_flock_fallback,
              lock_warn_interval, lock_cross_host_timeout
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return compile_cmd + " -o " + target

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup).
    # Two invariants must hold under concurrent peer makes on an object CAS:
    #   1. Lock on a SIDECAR ``<target>.lock`` file, NOT on ``<target>``. flock
    #      opens its lock argument with O_RDWR|O_CREAT, so locking the target
    #      directly would create an empty ``<target>`` with mtime=now BEFORE
    #      the inner compile runs. A peer make's mtime check then treats the
    #      target as up-to-date and skips the compile recipe entirely, going
    #      straight to link — producing ``undefined reference to 'main'``
    #      errors. Locking a sidecar leaves ``<target>`` untouched until the
    #      mv lands, so peer makes see ``<target>`` only when it is complete.
    #   2. Compile to a temp file then atomically rename — protects link rules
    #      that read .o files WITHOUT any lock. Without temp+rename a peer
    #      linker could mmap-read a half-written .o.
    # DO NOT 'optimize' back to ``flock <target> gcc -o <target>``: that form
    # violates BOTH invariants. See locking.atomic_compile() for the rationale
    # the helper-mode path below relies on.
    if strategy == "flock" and _native_flock_available():
        target_q = shlex.quote(target)
        lock_q = shlex.quote(f"{target}.lock")
        temp_q = shlex.quote(f"{target}.compiletools.tmp")
        # $$ escapes to $ at Make-recipe expansion so the shell sees $? / $ec.
        inner = f"{compile_cmd} -o {temp_q} && mv -f {temp_q} {target_q}; ec=$$?; rm -f {temp_q}; exit $$ec"
        return f"flock {lock_q} sh -c {shlex.quote(inner)}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper compile --target={target} --strategy={strategy} -- {compile_cmd}"


def wrap_link_with_lock(link_cmd: str, target: str, args, filesystem_type: str) -> str:
    """Wrap a link/ar command with file locking.

    For flock strategy, uses native ``flock`` binary (util-linux) to avoid
    the overhead of spawning a Python ct-lock-helper process per link.
    Other strategies (lockdir, fcntl, cifs) continue to use ct-lock-helper.

    Unlike wrap_compile_with_lock, the command is passed through unchanged
    (including any -o flag) since atomic_link does not manipulate output paths.

    Args:
        link_cmd: Complete link command string (e.g., "g++ -o bin/foo obj/foo.o")
        target: Target file for locking (e.g., "$@" for Make, or an actual path)
        args: Namespace with file_locking, sleep_interval_lockdir, etc.
        filesystem_type: Result of filesystem_utils.get_filesystem_type()

    Returns:
        Complete command string, lock-wrapped if file_locking is enabled.
    """
    if not args.file_locking:
        return link_cmd

    strategy = compiletools.filesystem_utils.get_lock_strategy(filesystem_type)

    # Fast path: use native flock binary for flock strategy (avoids Python startup).
    # Lock on ``<target>.lock`` sidecar, NOT on ``<target>``: ``flock`` opens
    # its lock argument with O_CREAT, which would create an empty ``<target>``
    # with mtime=now and trick a peer make process into treating the target
    # as up-to-date (mtime newer than its prerequisites). See
    # wrap_compile_with_lock for the full rationale.
    if strategy == "flock" and _native_flock_available():
        lock_q = shlex.quote(f"{target}.lock")
        return f"flock {lock_q} {link_cmd}"

    env_prefix = _build_lock_env_prefix(strategy, args, filesystem_type)
    return f"{env_prefix}ct-lock-helper link --target={target} --strategy={strategy} -- {link_cmd}"


def check_lock_helper_available() -> bool:
    """Check if ct-lock-helper is on PATH. Returns True if found."""
    return shutil.which("ct-lock-helper") is not None


def report_lock_helper_missing() -> None:
    """Raise RuntimeError when ct-lock-helper is not found on PATH."""
    raise RuntimeError(
        "ct-lock-helper not found in PATH\n"
        "\n"
        "The --file-locking flag requires ct-lock-helper to be installed.\n"
        "\n"
        "Solutions:\n"
        "  1. Install compiletools: pip install compiletools\n"
        "  2. Install from source: pip install -e .\n"
        "  3. Add ct-lock-helper to your PATH\n"
        "\n"
        "Or disable file locking with: --no-file-locking"
    )
