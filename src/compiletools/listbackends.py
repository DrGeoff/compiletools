import compiletools.apptools
import compiletools.utils
from compiletools.build_backend import (
    _REGISTRY,
    available_backends,
    backend_tool_command,
    is_backend_available,
)


def _ensure_backends_registered():
    import compiletools.bazel_backend
    import compiletools.cmake_backend
    import compiletools.makefile_backend
    import compiletools.ninja_backend
    import compiletools.shake_backend
    import compiletools.tup_backend  # noqa: F401 -- last import, triggers all registrations


def add_arguments(parser):
    styles = ["pretty", "flat", "filelist"]
    parser.add("--style", choices=styles, default="pretty", help="Output formatting style")

    compiletools.utils.add_boolean_argument(
        parser,
        "all",
        dest="show_all",
        default=False,
        help="List all backends, including those whose build tool is not installed",
    )


def list_backends(args=None):
    _ensure_backends_registered()

    style = "pretty"
    show_all = False
    if args:
        style = args.style or "pretty"
        show_all = args.show_all

    names = available_backends()
    if not show_all:
        names = [n for n in names if is_backend_available(n)]

    if style == "pretty":
        lines = []
        lines.append(f"{'Backend':<10} {'Build file':<18} {'Tool':<18} {'Available'}")
        lines.append("-" * 59)
        for name in names:
            cls = _REGISTRY[name]
            tool = backend_tool_command(name) or "(builtin)"
            available = "yes" if is_backend_available(name) else "no"
            lines.append(f"{name:<10} {cls.build_filename():<18} {tool:<18} {available}")
        return "\n".join(lines) + "\n"
    elif style == "flat":
        return " ".join(names) + (" " if names else "")
    else:  # filelist
        return "\n".join(names) + ("\n" if names else "")


def main(argv=None):
    cap = compiletools.apptools.create_parser(
        "List available build backends", argv=argv, include_config=False
    )
    add_arguments(cap)
    args = cap.parse_args(args=argv)
    print(list_backends(args=args), end="")
    return 0
