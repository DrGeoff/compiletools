import compiletools.apptools
import compiletools.utils
from compiletools.build_backend import (
    available_backends,
    backend_tool_command,
    ensure_backends_registered,
    get_backend_class,
    is_backend_available,
)


def add_arguments(parser):
    styles = ["pretty", "flat", "filelist"]
    parser.add_argument("--style", choices=styles, default="pretty", help="Output formatting style")

    compiletools.utils.add_boolean_argument(
        parser,
        "all",
        dest="show_all",
        default=False,
        help="List all backends, including those whose build tool is not installed",
    )


def list_backends(args=None):
    ensure_backends_registered()

    style = getattr(args, "style", None) or "pretty"
    show_all = getattr(args, "show_all", False)

    names = available_backends()
    if not show_all:
        names = [n for n in names if is_backend_available(n)]

    if style == "pretty":
        header = f"{'Backend':<10} {'Build file':<18} {'Tool':<18} {'Available'}"
        lines = [header, "-" * len(header)]
        for name in names:
            cls = get_backend_class(name)
            tool = backend_tool_command(name) or "(builtin)"
            available = "yes" if (not show_all or is_backend_available(name)) else "no"
            lines.append(f"{name:<10} {cls.build_filename():<18} {tool:<18} {available}")
        return "\n".join(lines) + "\n"
    elif style == "flat":
        return " ".join(names) + (" " if names else "")
    else:  # filelist
        return "\n".join(names) + ("\n" if names else "")


def main(argv=None):
    cap = compiletools.apptools.create_parser("List available build backends", argv=argv, include_config=False)
    add_arguments(cap)
    args = cap.parse_args(args=argv)
    print(list_backends(args=args), end="")
    return 0
