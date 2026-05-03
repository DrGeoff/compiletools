import os
import sys

import compiletools.apptools


def _cpus_linux():
    # PyPy does not expose os.sched_getaffinity; fall back to os.cpu_count().
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        return len(sched_getaffinity(0))
    return os.cpu_count() or 4


_cpus_android = _cpus_linux  # Termux is Linux; sched_getaffinity works on CPython


def _cpus_darwin():
    # macOS lacks os.sched_getaffinity; nproc isn't installed by default.
    import subprocess

    try:
        res = subprocess.run(
            ["sysctl", "-n", "hw.ncpu"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        )
        return int(res.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError):
        return os.cpu_count() or 4


_CPU_DISPATCH = {
    "linux": _cpus_linux,
    "android": _cpus_android,
    "darwin": _cpus_darwin,
}


def _cpu_count():
    fn = _CPU_DISPATCH.get(sys.platform)
    if fn is None:
        return os.cpu_count() or 4
    try:
        return fn()
    except Exception:
        return os.cpu_count() or 4


def add_arguments(cap):
    if compiletools.apptools._parser_has_option(cap, "--jobs"):
        return
    cap.add(
        "-j",
        "--jobs",
        "--CAKE_PARALLEL",
        "--parallel",
        dest="parallel",
        type=int,
        default=_cpu_count(),
        help="Sets the number of CPUs to use in parallel for a build.",
    )


def main(argv=None):
    cap = compiletools.apptools.create_parser(
        "Determine optimal number of parallel jobs", argv=argv, include_config=False
    )
    add_arguments(cap)
    args = cap.parse_args(args=argv)
    if args.verbose >= 2:
        compiletools.apptools.verbose_print_args(args)
    print(args.parallel)

    return 0
