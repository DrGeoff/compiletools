import os

import compiletools.apptools


def _cpu_count():
    # Python 3.13+: os.process_cpu_count() honours sched_getaffinity on Linux
    # and falls back to os.cpu_count() elsewhere — exactly the semantics we want
    # for build-tool parallelism inside cgroups, taskset, slurm, etc.
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        count = process_cpu_count()
        if count is None:
            raise RuntimeError("os.process_cpu_count() could not determine CPU count")
        return count

    # Pre-3.13 Linux: do it ourselves. PyPy historically lacked sched_getaffinity.
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        try:
            return len(sched_getaffinity(0))
        except OSError:
            pass

    count = os.cpu_count()
    if count is None:
        raise RuntimeError("os.cpu_count() could not determine CPU count")
    return count


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
