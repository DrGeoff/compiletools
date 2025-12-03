=======================
cleanup-remote-locks.sh
=======================

--------------------------------------------------------------
Legacy helper for pruning stale lockdir directories remotely
--------------------------------------------------------------

:Author: drgeoffathome@gmail.com
:Date:   2025-11-26
:Version: 6.1.5
:Manual section: 1
:Manual group: developers

STATUS
======
**Deprecated.** ``ct-cleanup-locks`` (Python) is the supported replacement and
should be used for all new automation. This shell script remains in the tree so
older deployments can be referenced, but it will be removed in a future release.

SYNOPSIS
========
cleanup-remote-locks.sh SHARED_OBJECT_DIRECTORY

DESCRIPTION
===========
The script iterates over ``*.lockdir`` directories beneath the supplied shared
object cache, inspects their ``pid`` files (format ``hostname:pid``), and
decides whether the owning process is still running.

For locks created on the current host, ``kill -0`` is used to check liveness.
For locks that originated from a remote machine the script opens an SSH session
and repeats the ``kill -0`` probe remotely. If a process cannot be reached the
lock is treated as stale and removed (unless ``DRY_RUN`` is enabled).

Because this logic predates the modern ``ct-cleanup-locks`` command it does not
read ``ct.conf`` or obey configurable timeout rules. Prefer the Python tool
whenever possible.

OPTIONS
=======
``SHARED_OBJECT_DIRECTORY``
    Root of the shared cache (e.g., ``/shared/build/obj``). The script walks
    this tree searching for ``*.lockdir`` directories.

ENVIRONMENT
===========
``DRY_RUN=1``
    When set, the script only reports which locks would be removed without
    deleting anything.

``SSH`` configuration
    Remote checks rely on password-less SSH to the host recorded in the lock.
    Ensure keys or agent forwarding are configured or the probe will be marked
    ``UNKNOWN``.

EXAMPLES
========

Preview stale locks without deleting them::

    DRY_RUN=1 cleanup-remote-locks.sh /shared/build/cache

Aggressively clean a shared NFS cache::

    cleanup-remote-locks.sh /mnt/nfs/ct-shared

SEE ALSO
========
``ct-cleanup-locks`` (1), ``ct-lock-helper`` (1)
