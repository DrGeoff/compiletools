import glob
import os

import compiletools.apptools
import compiletools.configutils
import compiletools.git_utils
import compiletools.utils


def add_arguments(parser):
    compiletools.utils.add_boolean_argument(
        parser,
        "configname",
        default=False,
        help="Print the .conf at the end of the variant",
    )

    compiletools.utils.add_boolean_argument(
        parser,
        "repoonly",
        default=False,
        help="Restrict the results to the local repository config files",
    )

    compiletools.utils.add_boolean_argument(
        parser,
        "shorten",
        default=True,
        help="Shorten from the full path to the config filenames to only the variant name",
    )

    parser.add_argument("--style", choices=list(_STYLE_REGISTRY), default="pretty", help="Output formatting style")


class PrettyStyle:
    def __init__(self):
        self.output = ""

    def append_text(self, text):
        if text is None:
            text = ""
        self.output += text + "\n"

    def append_variants(self, variants):
        if not variants:
            self.output += "    None found\n"
        else:
            for vv in sorted(variants):
                self.output += "    " + vv + "\n"


class FlatStyle:
    def __init__(self):
        self.output = ""

    def append_text(self, text):
        del text

    def append_variants(self, variants):
        for vv in sorted(variants):
            self.output += vv + " "


class FilelistStyle:
    def __init__(self):
        self.output = ""

    def append_text(self, text):
        del text

    def append_variants(self, variants):
        for vv in sorted(variants):
            self.output += vv + "\n"


_STYLE_REGISTRY = {
    "pretty": PrettyStyle,
    "flat": FlatStyle,
    "filelist": FilelistStyle,
}


def find_possible_variants(
    user_config_dir=None, system_config_dir=None, exedir=None, args=None, verbose=0, gitroot=None
):
    stylename = getattr(args, "style", None) or "pretty"
    style = _STYLE_REGISTRY[stylename]()
    shorten = getattr(args, "shorten", True)
    repoonly = getattr(args, "repoonly", False)
    configname = getattr(args, "configname", False)

    style.append_text("Variants compose via axis conf files (e.g. --variant=gcc,debug,asan).")
    canonical_order, order_source = compiletools.configutils.get_canonical_order(
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        verbose=verbose,
        gitroot=gitroot,
    )
    style.append_text(f"Canonical token order ({order_source}):")
    style.append_text("  " + ", ".join(canonical_order))
    style.append_text("From highest to lowest priority configuration directories, the available axis confs are:")

    search_directories = compiletools.configutils.default_config_directories(
        user_config_dir=user_config_dir,
        system_config_dir=system_config_dir,
        exedir=exedir,
        repoonly=repoonly,
        verbose=verbose,
        gitroot=gitroot,
        current_dir=os.getcwd(),
    )

    for cfg_dir in search_directories:
        style.append_text(cfg_dir)
        found = []
        for cfg_path in glob.glob(os.path.join(cfg_dir, "*.conf")):
            if not shorten:
                found.append(cfg_path)
                continue
            entry = compiletools.configutils.removedotconf(os.path.basename(cfg_path))
            if configname:
                entry += ".conf"
            if repoonly:
                entry = compiletools.git_utils.strip_git_root(os.path.join(cfg_dir, entry))
            found.append(entry)
        style.append_variants(found)

    return style.output


def main(argv=None):
    cap = compiletools.apptools.create_parser("List available build variants", argv=argv, include_config=False)
    add_arguments(cap)
    args = cap.parse_args(args=argv)
    print(find_possible_variants(args=args, verbose=args.verbose))
    return 0
