====================
ct-pytest-monitor
====================

----------------------------------------------------------------------
Run pytest with crash-survivable diagnostics (Termux/OOM friendly)
----------------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2026-05-04
:Version: 8.3.0
:Manual section: 1
:Manual group: developers

SYNOPSIS
========
ct-pytest-monitor [--logdir DIR] [--interval SECONDS] [-h|--help] [-- pytest args...]

DESCRIPTION
===========
On constrained devices -- notably Termux on Android, where the kernel
OOM-killer can SIGKILL the entire shell mid-test -- ordinary
``pytest`` runs leave no record of which test triggered the kill.
``ct-pytest-monitor`` wraps ``pytest`` with three durable side
channels so post-mortem inspection can identify both the offending
test and the memory pressure leading up to it.

This is **not** a stress harness or a retry loop. It runs ``pytest``
exactly once; the value-add is that test progress and host memory
state are flushed to disk before each test body runs, so a hard kill
leaves a usable trail.

The three side channels are:

1. **Checkpoint log (authoritative).** A pytest hook in
   ``conftest.py`` writes each test's nodeid to
   ``$CT_PYTEST_CHECKPOINT`` *before* the test body runs, then
   ``fsync``\ s the file. The wrapper sets that env var. After a
   SIGKILL, ``tail -n1 checkpoint.log`` is the test that was running.

2. **Memory sampler.** A background loop appends ``/proc/meminfo``
   plus the top RSS consumers to ``meminfo.log`` every N seconds. Each
   iteration opens-writes-closes so the log is flushed up to the last
   completed sample.

3. **Tee'd pytest output.** Full unbuffered ``pytest -v`` output is
   teed to ``pytest.log``. This may lose the very last line(s) on
   SIGKILL because they can sit in tee's userspace buffer; rely on the
   checkpoint log for the authoritative last-test record.

The script is non-invasive: when ``$CT_PYTEST_CHECKPOINT`` is unset,
the conftest hook is a no-op, so normal ``pytest`` runs are unaffected.

OUTPUT FILES
============
All written under ``$LOG_DIR`` (default
``$TMPDIR/ct-pytest-monitor-YYYYMMDD-HHMMSS``):

``checkpoint.log``
    One nodeid per line, fsync'd before each test runs. The last line
    is the test that was running when the shell died (or the last test
    that finished, on a clean run).

``pytest.log``
    Full ``pytest -v`` output. Use for context around failures.

``meminfo.log``
    Periodic memory snapshots. Each sample begins with an ISO-8601
    timestamp followed by selected ``/proc/meminfo`` keys
    (``MemTotal``/``MemFree``/``MemAvailable``/``SwapTotal``/etc.) and
    the top 10 processes by RSS.

``system.log``
    One-shot snapshot at startup: ``uname -a``, ``TERMUX_VERSION``,
    initial ``/proc/meminfo``, ``free -h``, Python and pytest
    versions, and the pytest argv.

``summary.log``
    Written by the EXIT trap on a clean exit: tail of the checkpoint
    and pytest logs plus final ``/proc/meminfo``. (Not written if the
    shell is SIGKILLed -- traps don't run on signal 9. The other logs
    are still on disk.)

OPTIONS
=======
``--logdir DIR``
    Use ``DIR`` instead of an auto-generated path under
    ``$TMPDIR``. The directory is created if missing.

``--interval SECONDS``
    Memory sampler interval. Default ``2``. Lower values capture
    sharper spikes at the cost of log size and IO.

``--``
    End of ct-pytest-monitor options. Everything after ``--`` is
    forwarded verbatim to ``pytest``. Use this when forwarding flags
    that overlap (e.g. ``-h``).

``-h``, ``--help``
    Print this documentation and exit.

Anything else is forwarded to ``pytest``. Common patterns are shown in
EXAMPLES.

ENVIRONMENT
===========
``CT_PYTEST_CHECKPOINT``
    Set automatically by the wrapper to the path of
    ``checkpoint.log``. The conftest hook reads it and writes one
    line (with ``os.fsync``) per test before the test runs. Unset =
    no-op.

``PYTHONUNBUFFERED``
    Forced to ``1`` so pytest's stdout/stderr reach ``tee`` line by
    line.

``TMPDIR``
    Used to construct the default log directory when ``--logdir`` is
    omitted.

EXAMPLES
========

Run the entire suite::

    scripts/ct-pytest-monitor

Run a single test file with a tighter sampler interval::

    scripts/ct-pytest-monitor --interval 1 src/compiletools/test_headerdeps.py

Forward arbitrary pytest flags after ``--``::

    scripts/ct-pytest-monitor -- -k "not slow" --maxfail=3

Inspect the post-mortem after a Termux OOM kill::

    tail -n1 /tmp/ct-pytest-monitor-*/checkpoint.log    # culprit
    tail -n40 /tmp/ct-pytest-monitor-*/meminfo.log      # memory at death

EXIT STATUS
===========
``0``
    pytest exited 0.

Non-zero
    pytest's own exit code is propagated. On SIGKILL, the parent
    shell never reaches the exit trap, so there is no exit code -- the
    log files on disk are the only record.

NOTES
=====
**Why a separate script?** The conftest hook alone is enough to
identify the offending test, but only the wrapper guarantees the env
var is set, the memory sampler is running, and logs land in a known
location. Running ``pytest`` directly (without this wrapper) is fine
for normal use; the hook is a no-op without ``CT_PYTEST_CHECKPOINT``.

**Why not ``pytest --forked``?** Per-test forking would let the
runner survive a single test's OOM, but Termux's OOM-killer SIGKILLs
the whole session (``oom_score_adj`` is shared up the process tree),
so the runner dies anyway. The checkpoint approach is robust to
session-wide kills; per-test isolation would not help here.

SEE ALSO
========
The ``Termux (Android, aarch64)`` subsection of the project ``INSTALL``
file documents the install-time OOM workaround
(``scripts/ct-termux-install``); ``ct-pytest-monitor`` is the runtime
counterpart for the test suite.
