"""``ct-git-sha-report`` CLI: print git blob SHA1s for the working tree.

The git-hashing library functions live in :mod:`compiletools.git_sha`; this
module is only the command-line entry point over them, so core library code
(e.g. ``global_hash_registry``) can depend on the hashing helpers without
importing a CLI entry point.
"""

from compiletools.git_sha import get_complete_working_directory_hashes, get_current_blob_hashes


def main(argv=None):
    """Main entry point for ct-git-sha-report command."""
    import compiletools.apptools
    from compiletools.build_context import BuildContext

    description = (
        "Print git blob SHA1s for the current working tree. By default lists "
        "tracked files only; pass --all (or --untracked) to also include "
        "untracked files via a synthetic hash-object pass."
    )
    cap = compiletools.apptools.create_parser(description, argv=argv, include_config=False)
    cap.add_argument(
        "--all",
        "--untracked",
        dest="include_untracked",
        action="store_true",
        default=False,
        help="Include untracked files in the report.",
    )
    args = cap.parse_args(args=argv)
    args.verbose -= args.quiet

    context = BuildContext()

    if args.include_untracked:
        print("# Complete working directory fingerprint (tracked + untracked files)")
        blob_map = get_complete_working_directory_hashes(context)
    else:
        print("# Tracked files only (use --all or --untracked to include untracked files)")
        blob_map = get_current_blob_hashes(context)

    for path, sha in sorted(blob_map.items()):
        print(f"{sha}  {path}")
