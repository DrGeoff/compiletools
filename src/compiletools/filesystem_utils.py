"""Filesystem detection and compatibility utilities.

This module provides filesystem type detection and policy decisions for:
1. File locking strategies (makefile.py)
2. Memory mapping safety (file_analyzer.py)
3. Filesystem-specific performance tuning
"""

import contextlib
import errno
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from functools import lru_cache

from stringzilla import Str

import compiletools.apptools_canonicalize
import compiletools.publish_copy

# ---------------------------------------------------------------------------
# cas-exedir sidecar family (single source of truth)
# ---------------------------------------------------------------------------
#
# Every cas-exedir artefact (``<basename>_<linkkey>.{exe,a,so}``) may carry
# up to three sidecars, deposited by ``cas_publish`` / ``build_backend`` and
# consumed by ``trim_cache`` / ``cache_report``:
#
# * ``.manifest`` -- source-identity JSON (``cas_publish.publish``). Depends
#   only on the artefact itself, never on how a test using it was invoked --
#   an IDENTITY sidecar, named ``<primary>.manifest`` with no extra key.
# * ``.result`` / ``.xml`` -- TEST sidecars: the content-keyed success marker
#   and the content-addressed JUnit report. Both are keyed not just on the
#   exe's bytes (the link key already captures that) but on how the test was
#   INVOKED -- ``TESTPREFIX`` (``valgrind`` vs none) changes what "success"
#   or "the XML" means for byte-identical exes, so the primary exe path alone
#   is not a safe cache key for these two. They are named
#   ``<primary>.<test_sidecar_key>.result`` / ``.xml`` where
#   ``test_sidecar_key`` is :func:`compute_test_sidecar_key`.
#
# ``match_exe_sidecar`` is the single match-and-strip entry point every
# producer/consumer must share: given a sidecar LEAF name, it returns
# ``(primary_leaf_name, suffix)`` -- the bare exe/lib leaf name with both the
# suffix and (for TEST sidecars) the key segment stripped, so trim's
# "does the primary still exist" check and the ``_is_cas_exe_leaf_name``
# shape gate keep working unchanged. A TEST-sidecar leaf with no (or a
# non-hex, non-16-char) key segment is treated as a legacy pre-migration
# sidecar and stripped bare-suffix-only, matching the pre-A12 behaviour --
# it is neither corrupted nor spuriously reaped merely because a newer
# argv-keyed sibling now also exists beside the same primary.
EXE_MANIFEST_SUFFIX: str = ".manifest"
EXE_RESULT_SUFFIX: str = ".result"
EXE_XML_SUFFIX: str = ".xml"
_EXE_IDENTITY_SIDECAR_SUFFIXES: tuple[str, ...] = (EXE_MANIFEST_SUFFIX,)
_EXE_TEST_SIDECAR_SUFFIXES: tuple[str, ...] = (EXE_RESULT_SUFFIX, EXE_XML_SUFFIX)
_EXE_SIDECAR_SUFFIXES: tuple[str, ...] = _EXE_IDENTITY_SIDECAR_SUFFIXES + _EXE_TEST_SIDECAR_SUFFIXES

# Re-exported from the import leaf that owns the secure copy. The copy runs
# as a subprocess twice per test on a warm build, so it must not drag this
# module's stringzilla / apptools_canonicalize / subprocess imports onto that
# path -- see compiletools/publish_copy.py. In-process callers
# (``trace_backend``) keep importing it from here.
_PUBLISH_TMP_SUFFIX: str = compiletools.publish_copy.PUBLISH_TMP_SUFFIX
QUIET_MISSING_SRC_FLAG = compiletools.publish_copy.QUIET_MISSING_SRC_FLAG
secure_publish_copy = compiletools.publish_copy.secure_publish_copy

# sha256-hexdigest-prefix length for the test-sidecar key segment. Short by
# design -- unlike the link key (which needs enough entropy that a collision
# would silently miscompile, see build_backend._compute_artefact_key_hash),
# a test-sidecar-key collision only degrades to "two different TESTPREFIX
# invocations share a stale skip/report" -- annoying but self-correcting on
# the next run once the marker is stale for either. 16 hex chars (64 bits)
# is ample.
_TEST_SIDECAR_KEY_HEXLEN = 16
_TEST_SIDECAR_KEY_RE = re.compile(rf"^[0-9a-f]{{{_TEST_SIDECAR_KEY_HEXLEN}}}$")


def compute_test_sidecar_key(argv_tokens: Sequence[str], framework_id: str | None, anchor_root: str) -> str:
    """Semantic (path-blind) key for a test invocation's ``.result``/``.xml``
    sidecars.

    Hashes the CANONICAL ``TESTPREFIX`` tokens plus the detected
    test-framework id -- never the literal argv, which also contains
    workspace-specific ``exe_path``/XML-output paths that would defeat
    cross-workspace CAS sharing if hashed verbatim.

    :func:`compiletools.apptools_canonicalize.canonicalize_for_cache_key`
    rewrites only the recognised path-flag families (``-I``, ``-isystem``,
    ``-B``, ``-L``, ``-Wl``, prefix maps, ...); any other path-bearing
    token (e.g. ``--log-file=<abs path>``) passes through verbatim and so
    forks the key per workspace. That is the safe direction: it costs
    extra re-runs, never a wrong skip.
    """
    canonical = compiletools.apptools_canonicalize.canonicalize_for_cache_key(list(argv_tokens), anchor_root)
    payload = "\x1f".join((framework_id or "", *canonical))
    return hashlib.sha256(payload.encode()).hexdigest()[:_TEST_SIDECAR_KEY_HEXLEN]


def build_test_sidecar_path(cas_primary_path: str, key: str, suffix: str) -> str:
    """Build a TEST-sidecar path: ``<cas_primary_path>.<key><suffix>``.

    *suffix* must be one of :data:`_EXE_TEST_SIDECAR_SUFFIXES`.
    """
    if suffix not in _EXE_TEST_SIDECAR_SUFFIXES:
        raise ValueError(f"build_test_sidecar_path: {suffix!r} is not a TEST sidecar suffix")
    return f"{cas_primary_path}.{key}{suffix}"


def match_exe_sidecar(leaf_name: str) -> tuple[str, str] | None:
    """Match-and-strip a cas-exedir sidecar LEAF name.

    Returns ``(primary_leaf_name, suffix)`` where ``primary_leaf_name`` is
    the bare exe/lib leaf (``<basename>_<linkkey>.{exe,a,so}``) the sidecar
    belongs to -- with both the sidecar suffix and, for a TEST sidecar
    carrying a well-formed key segment, that key segment stripped. Returns
    ``None`` for anything not ending in a member of
    :data:`_EXE_SIDECAR_SUFFIXES`.

    THE single strip implementation shared by every producer/consumer
    (``build_backend``, ``trim_cache``'s orphan reaper and per-artefact
    eviction) -- see the module-level sidecar-family docstring above for the
    key-vs-legacy disambiguation rule.
    """
    for suffix in _EXE_IDENTITY_SIDECAR_SUFFIXES:
        if leaf_name.endswith(suffix):
            return leaf_name[: -len(suffix)], suffix
    for suffix in _EXE_TEST_SIDECAR_SUFFIXES:
        if leaf_name.endswith(suffix):
            body = leaf_name[: -len(suffix)]
            dot = body.rfind(".")
            if dot != -1 and _TEST_SIDECAR_KEY_RE.match(body[dot + 1 :]):
                return body[:dot], suffix
            return body, suffix  # legacy / unkeyed -- stripped bare-suffix-only
    return None


def _umask_default_file_mode() -> int:
    """Return the file mode a normal ``open(path, 'w')`` would create now.

    ``tempfile.mkstemp`` deliberately bypasses umask and always creates
    0o600 as a security feature; callers that want the conventional
    umask-respecting mode have to recompute it. Reading umask requires a
    round-trip set-then-restore — there is no read-only API.
    """
    current_umask = os.umask(0)
    os.umask(current_umask)
    return 0o666 & ~current_umask


def _resolve_target_mode_and_gid(target_path: str, preserve_permissions: bool) -> tuple[int, int | None]:
    """Return ``(mode, gid)`` for an atomic temp file destined to replace ``target_path``.

    When ``preserve_permissions`` and the target exists, copies its mode
    and group. Otherwise (and on any stat error) falls back to the
    umask-derived mode with no group change — needed because
    ``tempfile.mkstemp`` would otherwise leave the file at 0o600 forever
    once ``os.replace`` propagates it to the target.
    """
    if preserve_permissions and os.path.exists(target_path):
        try:
            stat_info = os.stat(target_path)
        except FileNotFoundError:
            pass  # Race: target deleted between exists() and stat(); treat as first-create.
        else:
            return stat_info.st_mode & 0o777, stat_info.st_gid
    return _umask_default_file_mode(), None


def atomic_replace(
    dst: str,
    populate: Callable[[str], None],
    *,
    tmp_prefix: str | None = None,
    tmp_suffix: str | None = None,
) -> None:
    """Atomically install at ``dst`` whatever ``populate`` puts at the
    temp path it is handed.

    The temp file is created in ``dst``'s directory (so the final
    ``os.replace`` is always within-fs, never ``EXDEV``), its placeholder
    is unlinked, and ``populate(tmp_path)`` is called to claim that
    path — typically via ``os.link``, ``os.symlink``, or by writing
    bytes. The result is then ``os.replace``'d onto ``dst``, atomic
    against peer readers and tolerant of a read-only existing target
    (``os.replace`` only needs write permission on the parent dir).

    Concurrent peers each ``mkstemp`` a unique tmp (process-local) and
    each ``os.replace`` independently; the last replace wins. Both
    final-states are valid provided ``populate`` produces byte-equivalent
    output for the same caller intent (the contract callers must uphold).

    ``tmp_prefix`` / ``tmp_suffix`` override the temp filename pattern
    for callers whose orphan-cleanup tooling scans for specific names
    (e.g. ``cas_publish`` looks for ``*.publish.tmp``).
    """
    dst_dir = os.path.dirname(dst) or "."
    if tmp_prefix is None:
        tmp_prefix = f".tmp.{os.path.basename(dst)}."
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dst_dir, prefix=tmp_prefix, suffix=tmp_suffix or "")
    os.close(tmp_fd)
    os.unlink(tmp_path)
    try:
        populate(tmp_path)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def atomic_copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` atomically; hardlink fast path.

    Tries ``os.link`` first (O(1) inode share on the same filesystem)
    and falls back to ``shutil.copy2`` on ``EXDEV``. Inode sharing on
    the fast path means a subsequent ``os.path.samefile(src, dst)``
    returns True, so idempotent callers can skip re-publishing on no-op
    reruns.

    .. warning::
       On the hardlink fast path, ``src`` and ``dst`` share an inode.
       Callers must not *in-place* modify either side — an in-place
       write would corrupt the other. All current callers write via
       temp+rename (atomic_replace, atomic_compile, etc.), which always
       produces a fresh inode for the write target, so the sharing is
       safe. New callers that ``open(dst, 'r+')`` or ``truncate(dst)``
       must use a different helper.
    """

    def populate(tmp_path: str) -> None:
        try:
            os.link(src, tmp_path)
        except AttributeError:
            # Platform lacks hardlink support entirely (e.g. Termux/Android
            # bionic doesn't expose os.link). Fall back to copy as for EXDEV.
            shutil.copy2(src, tmp_path)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            shutil.copy2(src, tmp_path)

    atomic_replace(dst, populate)


def atomic_copy_bytes(src: str, dst: str) -> None:
    """Byte-copy ``src`` to ``dst`` atomically — never a hardlink.

    Production-dead: every production copy goes through
    :func:`secure_publish_copy`. Retained only as the vulnerable
    comparison baseline that keeps that function's security claim
    honest — this variant reopens the destination pathname, so a
    pre-planted symlink defeats it. Deleting it breaks
    ``test_filesystem_utils.py::TestSecurePublishCopy::test_symlink_implantation_defeats_the_naive_atomic_copy_bytes_helper``,
    which is the point: the claim is measured against a live
    counterexample rather than asserted in prose. The ``.publish.tmp``
    suffix keeps interrupted copies reclaimable by
    ``trim_cache.reclaim_orphan_temps``.
    """

    def populate(tmp_path: str) -> None:
        shutil.copyfile(src, tmp_path)

    atomic_replace(
        dst,
        populate,
        tmp_prefix=os.path.basename(dst) + ".",
        tmp_suffix=_PUBLISH_TMP_SUFFIX,
    )


_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def unescape_mount_field(field: str) -> str:
    """Decode the octal escapes the kernel writes into /proc/mounts fields.

    proc(5) mangles exactly four characters in the device and mountpoint
    fields -- space (``\\040``), tab (``\\011``), newline (``\\012``) and
    backslash (``\\134``) -- so a mountpoint containing any of them arrives
    here escaped and must be decoded before it can be compared against a
    realpath.

    One left-to-right pass, never a sequence of ``str.replace`` calls.
    ``\\134`` is the escape for a literal backslash, so a mountpoint whose
    real name is ``/mnt/\\040`` is written ``/mnt/\\134040``; replacing
    ``\\040`` before ``\\134`` (or after it) decodes the tail of that
    escape a second time and yields ``/mnt/ ``. The single pass consumes
    all four characters of ``\\134`` and resumes past them, so the
    remaining ``040`` stays literal text.
    """
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mount_lines(lines) -> list[tuple[str, str]]:
    """Return ``(mountpoint, fstype)`` for each well-formed /proc/mounts line,
    with the mountpoint unescaped. Short lines are skipped."""
    mounts = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            mounts.append((unescape_mount_field(parts[1]), parts[2]))
    return mounts


def match_mountpoint(path: str, mounts) -> str | None:
    """Filesystem type of the most specific mountpoint containing *path*.

    *path* must already be a realpath. Longest mountpoint wins, and the
    match is a component-boundary prefix test (``trimmed + "/"``), not a
    bare ``startswith`` -- ``/data2`` is not under ``/data``. Plain string
    comparison: hosts can carry 400+ mount entries, so per-entry Path
    construction is too slow for this loop.
    """
    for mountpoint, fstype in sorted(mounts, key=lambda x: len(x[0]), reverse=True):
        trimmed = mountpoint.rstrip("/")
        if path == trimmed or path.startswith(trimmed + "/"):
            return fstype
    return None


@lru_cache(maxsize=128)
def get_filesystem_type(path: str) -> str:
    """Detect filesystem type for given path.

    Returns: filesystem type string (e.g., 'ext4', 'gpfs', 'nfs', 'cifs')
             or 'unknown' if cannot be determined

    Caches results by resolved path for efficiency.
    """
    try:
        # Linux: Parse /proc/mounts
        path = os.path.realpath(path)
        with open("/proc/mounts") as f:
            mounts = parse_mount_lines(f)

        fstype = match_mountpoint(path, mounts)
        if fstype is not None:
            return fstype

    except (FileNotFoundError, PermissionError, OSError):
        # /proc/mounts not available, try fallback
        pass

    # Fallback: try stat command (for non-Linux Unix)
    try:
        result = subprocess.run(["stat", "-f", "-c", "%T", path], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        pass

    return "unknown"


def get_lock_strategy(fstype: str) -> str:
    """Determine file locking strategy for filesystem type (for makefile.py).

    Returns:
        'fcntl' - Use fcntl.lockf() (GPFS: cross-node, kernel-managed)
        'lockdir' - Use mkdir-based locking (NFS/Lustre)
        'cifs' - Use exclusive file creation (CIFS/SMB specific)
        'flock' - Use POSIX flock (standard local filesystems)
    """
    fstype_lower = fstype.lower()

    # GPFS: fcntl.lockf() works cross-node (unlike flock which is node-local)
    if "gpfs" in fstype_lower:
        return "fcntl"

    # NFS/Lustre: mkdir-based locking
    if any(fs in fstype_lower for fs in ["lustre", "nfs"]):
        return "lockdir"

    # CIFS/SMB requires exclusive file creation
    if any(fs in fstype_lower for fs in ["cifs", "smb"]):
        return "cifs"

    # Standard POSIX flock
    return "flock"


# Filesystems whose per-file ``stat()`` is a high-latency metadata round-trip
# (cluster / network filesystems) and whose metadata servers service concurrent
# requests well. On these, fanning the trim scan's stat calls across threads is
# a large win — ``stat()`` releases the GIL, so threads overlap the network
# latency. Local-disk filesystems (ext4/xfs/btrfs/tmpfs/zfs/overlay) get no
# benefit (cheap stat served from the page cache) and pay thread-pool overhead,
# so they — and any *unknown* filesystem, which is almost always local disk —
# stay single-threaded.
_PARALLEL_SCAN_FILESYSTEMS: tuple[str, ...] = ("gpfs", "lustre", "nfs", "cifs", "smb", "panfs", "beegfs")


def should_parallelize_scan(fstype: str) -> bool:
    """Whether a metadata-bound directory scan (e.g. ct-trim-cache) should
    fan its ``stat()`` calls out across threads on this filesystem.

    True only for high-latency network/cluster filesystems (GPFS, Lustre,
    NFS, CIFS/SMB, PanFS, BeeGFS), where parallel ``stat()`` overlaps the
    metadata round-trip. False for local-disk and *unknown* filesystems —
    the latter are almost always local disk, so staying serial preserves the
    historical single-threaded behavior with no thread overhead.

    This is only the *gate*: the actual worker count is the caller's
    ``--parallel`` / ``-j`` value (``jobs.py``), which already honours CPU
    affinity, cgroups, and slurm allocations. This function never decides a
    thread count.
    """
    fstype_lower = fstype.lower()
    return any(fs in fstype_lower for fs in _PARALLEL_SCAN_FILESYSTEMS)


def supports_mmap_safely(fstype: str) -> bool:
    """Determine if filesystem supports mmap reliably (for file_analyzer.py).

    Returns:
        True if mmap is known to be safe on this filesystem
        False if mmap has known issues
    """
    fstype_lower = fstype.lower()

    # Known problematic filesystems
    unsafe_filesystems = ["gpfs", "cifs", "smb", "smbfs", "afs"]
    if any(fs in fstype_lower for fs in unsafe_filesystems):
        return False

    # NFS v4 usually works, but has had issues historically. FUSE varies by
    # implementation. Unknown or local filesystems are assumed safe.
    return True


def get_lockdir_sleep_interval(fstype: str) -> float:
    """Get recommended sleep interval for lockdir polling (for makefile.py).

    Returns:
        Sleep interval in seconds for lock acquisition retries
    """
    fstype_lower = fstype.lower()

    if "lustre" in fstype_lower:
        return 0.01  # Lustre is fast parallel filesystem
    if "nfs" in fstype_lower:
        return 0.1  # NFS has network latency
    # Other filesystems (GPFS uses fcntl, not lockdir polling)
    return 0.05  # Default middle ground


def atomic_write(target_path, content, binary=False, preserve_permissions=True):
    """Atomically write content to file via temp file + os.replace().

    Prevents SIGBUS for concurrent readers with memory-mapped files.
    On POSIX, os.replace() is atomic and existing mmaps continue to reference
    the old inode until they close/remap.

    Works correctly on all filesystem types (NFS, GPFS, Lustre, CIFS, local)
    as long as temp and target are on the same filesystem (ensured by creating
    temp in target's directory).

    Args:
        target_path: Final destination path
        content: Content to write (str or bytes)
        binary: If True, write as binary; if False, encode as UTF-8
        preserve_permissions: If True and target exists, preserve its mode/group

    Raises:
        OSError: If write or replace fails
    """
    import tempfile

    target_dir = os.path.dirname(target_path) or "."
    target_name = os.path.basename(target_path)

    # Ensure target directory exists
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    target_mode, target_gid = _resolve_target_mode_and_gid(target_path, preserve_permissions)

    # Create temp file in same directory (ensures same filesystem)
    fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=f".tmp.{target_name}.", suffix=f".{os.getpid()}")

    try:
        # Write content
        if binary:
            if isinstance(content, str):
                content = content.encode("utf-8")
            os.write(fd, content)
        else:
            if isinstance(content, bytes):
                os.write(fd, content)
            else:
                os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None

        os.chmod(temp_path, target_mode)
        if target_gid is not None:
            try:
                os.chown(temp_path, -1, target_gid)
            except PermissionError:
                pass  # Can't change group, not fatal (same as locking.py)

        # Atomic replace
        os.replace(temp_path, target_path)

    except Exception:
        # Clean up on error
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        raise


def atomic_write_if_changed(
    target_path: str,
    content: str | bytes,
    *,
    binary: bool = False,
    preserve_permissions: bool = True,
    encoding: str = "utf-8",
) -> bool:
    """Atomically write *content* to *target_path* iff it differs from on-disk.

    Returns True when a write happened, False when the target was byte-identical
    and was left untouched (preserving its inode and mtime — important for
    consumers like clangd / `find -newer` that treat any mtime change as cache
    invalidation).

    A missing target is treated as "different" and triggers the write.
    Read failures (corrupt file, permission glitch) fall through to the
    unconditional write so a transient error never leaves a stale file.
    """
    new_bytes = content.encode(encoding) if (not binary and isinstance(content, str)) else content
    try:
        with open(target_path, "rb") as f:
            if f.read() == new_bytes:
                return False
    except FileNotFoundError:
        pass
    except OSError:
        pass  # fall through to unconditional write
    atomic_write(target_path, content, binary=binary, preserve_permissions=preserve_permissions)
    return True


def safe_read_text_file(filepath, encoding="utf-8", force_no_mmap=False, respect_locks=False, lock_args=None):
    """Read text file safely, closing file descriptors properly.

    Always uses regular file I/O and closes file descriptors immediately.
    The OS page cache provides good performance for recently accessed files.

    Note: We use regular I/O instead of mmap because:
    1. We're reading entire files (not streaming), so mmap has no lazy-load benefit
    2. Keeping mmap fds open causes resource exhaustion in large builds
    3. OS page cache already provides excellent performance for re-reads
    4. Avoids SIGBUS issues on NFS/GPFS during concurrent writes

    Args:
        filepath: Path to file to read
        encoding: Text encoding
        force_no_mmap: Ignored (kept for API compatibility)
        respect_locks: If True, wait for write locks before reading
        lock_args: Lock configuration (required if respect_locks=True)

    Returns:
        sz.Str object with file contents in memory (no open file descriptor)

    Raises:
        OSError: If file cannot be read
        FileNotFoundError: If file doesn't exist
    """
    # Optional lock barrier - wait for any active writers
    if respect_locks and lock_args:
        from compiletools.locking import FileLock

        with FileLock(filepath, lock_args):
            pass  # Lock released immediately, now safe to read

    # Regular file I/O - safe on all filesystems, closes fd via context manager
    # OS page cache handles performance optimization
    with open(filepath, encoding=encoding, errors="replace") as f:
        return Str(f.read())


def atomic_output_file(target_path, mode="w", encoding="utf-8", preserve_permissions=True, force_mode=None):
    """Context manager for atomic file writes via temp file + os.replace().

    Returns a file object that writes to a temp file. On successful exit,
    atomically replaces target with temp file. On error, removes temp file.

    Prevents SIGBUS for concurrent readers with memory-mapped files.

    Usage:
        with atomic_output_file('/path/to/file.txt') as f:
            f.write(content)

    Args:
        target_path: Final destination path
        mode: File mode ('w', 'wb', etc.)
        encoding: Text encoding (only used for text mode)
        preserve_permissions: If True and target exists, preserve its mode/group
        force_mode: Exact permission bits for the result, overriding both the
            umask and any preserved mode. Pass 0o666 for files in a shared
            CAS pool that every peer must be able to read — the
            first-creator's umask must not lock peers out (same rationale as
            locking.py's explicit fchmod). Group preservation still applies.

    Yields:
        File object for writing

    Raises:
        OSError: If write or replace fails
    """
    import tempfile
    from contextlib import contextmanager

    @contextmanager
    def _atomic_context():
        target_dir = os.path.dirname(target_path) or "."
        target_name = os.path.basename(target_path)

        # Ensure target directory exists
        if not os.path.exists(target_dir):
            os.makedirs(target_dir, exist_ok=True)

        target_mode, target_gid = _resolve_target_mode_and_gid(target_path, preserve_permissions)
        if force_mode is not None:
            target_mode = force_mode

        # Create temp file in same directory
        fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=f".tmp.{target_name}.", suffix=f".{os.getpid()}")

        f = None
        try:
            # Convert fd to file object with requested mode
            if "b" in mode:
                f = os.fdopen(fd, mode)
            else:
                f = os.fdopen(fd, mode, encoding=encoding)

            yield f

            f.close()
            f = None

            os.chmod(temp_path, target_mode)
            if target_gid is not None:
                try:
                    os.chown(temp_path, -1, target_gid)
                except PermissionError:
                    pass

            # Atomic replace
            os.replace(temp_path, target_path)

        except Exception:
            # Clean up on error
            if f is not None and not f.closed:
                try:
                    f.close()
                except OSError:
                    pass
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    return _atomic_context()
