import os
import sys

import compiletools.apptools
import compiletools.findtargets
import compiletools.git_utils
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.utils
import compiletools.wrappedos


class FlatStyle(compiletools.git_utils.NameAdjuster):
    def __call__(self, sourcefiles):
        for source in sourcefiles:
            print(self.adjust(source))


class IndentStyle(compiletools.git_utils.NameAdjuster):
    def __call__(self, sourcefiles):
        for source in sourcefiles:
            print("\t", self.adjust(source))


class HeaderPassFilter:
    def __call__(self, files):
        return {fn for fn in files if compiletools.utils.is_header(fn)}


class SourcePassFilter:
    def __call__(self, files):
        return {fn for fn in files if compiletools.utils.is_source(fn)}


class AllPassFilter:
    def __call__(self, files):
        return files


_STYLE_REGISTRY = {
    "flat": FlatStyle,
    "indent": IndentStyle,
}

_FILTER_REGISTRY = {
    "header": HeaderPassFilter,
    "source": SourcePassFilter,
    "all": AllPassFilter,
}


def check_filename(filename):
    if not compiletools.wrappedos.isfile(filename):
        sys.stderr.write(
            f"The supplied filename ({filename}) isn't a file. "
            "Did you spell it correctly?"
            "Another possible reason is that you didn't supply a filename"
            " and that configargparse has picked an unused positional argument"
            " from the config file.\n"
        )
        exit(1)


class Filelist:
    def __init__(self, args, hunter, style=None):
        self.args = args
        self._hunter = hunter

        if style is None:
            style = self.args.style
        styleclass = _STYLE_REGISTRY[style.lower()]
        self.styleobject = styleclass(args)

    def _not_auto_excluded(self, filepaths):
        """The subset of *filepaths* ``--auto-exclude`` does not drop.

        Read per call rather than in ``__init__``: the re-anchor driver can
        grow ``auto_exclude`` with a subproject conf's values between
        discovery and this walk.
        """
        patterns = tuple(getattr(self.args, "auto_exclude", None) or ())
        if not patterns:
            return set(filepaths)
        anchor_root = compiletools.git_utils.find_git_root()
        kept = set()
        for filepath in filepaths:
            realpath = compiletools.wrappedos.realpath(filepath)
            if compiletools.findtargets.is_auto_excluded(realpath, patterns, anchor_root):
                if self.args.verbose >= 3:
                    print("Excluded from the file list by --auto-exclude: " + realpath)
                continue
            kept.add(filepath)
        return kept

    @staticmethod
    def add_arguments(cap):
        if compiletools.apptools._parser_has_option(cap, "--extrafile"):
            return
        compiletools.apptools.add_target_arguments(cap)
        cap.add_argument("--extrafile", help="Extra files to directly add to the filelist", nargs="*")
        cap.add_argument(
            "--extradir",
            help="Extra directories to add all files from to the filelist",
            nargs="*",
        )
        cap.add_argument(
            "--extrafilelist",
            help="Read the given files to find a list of extra files to add to the filelist",
            nargs="*",
        )

        # Output style and filter choices come from the explicit registries above.
        cap.add_argument("--style", choices=list(_STYLE_REGISTRY), default="flat", help="Output formatting style")

        cap.add_argument(
            "--filter",
            choices=list(_FILTER_REGISTRY),
            default="all",
            help="What type of files are allowed in the output",
        )

        compiletools.utils.add_flag_argument(cap, "merge", default=True, help="Merge all outputs into a single list")
        compiletools.hunter.add_arguments(cap)
        # Discovery half only: findtargets' own --style formats its target
        # report and its choices are incompatible with the ones above.
        compiletools.findtargets.add_discovery_arguments(cap)

    def process(self):
        filterclass = _FILTER_REGISTRY[self.args.filter.lower()]
        filterobject = filterclass()
        extras = set()

        # Add all the command line specified extras
        if self.args.extrafile:
            extras.update(self.args.extrafile)
        if self.args.extrafilelist:
            for fname in self.args.extrafilelist:
                with open(fname) as ff:
                    extras.update([line.strip() for line in ff])

        # Directory sweeps: the caller named the directory, not the files in
        # it, so --auto-exclude applies to what the sweep turns up (files
        # named one by one above are the explicit-target case it never
        # filters).
        swept = set()
        if self.args.extradir:
            for ed in self.args.extradir:
                swept.update(
                    [
                        os.path.join(ed, ff)
                        for ff in os.listdir(ed)
                        if compiletools.wrappedos.isfile(os.path.join(ed, ff))
                    ]
                )

        # Add all the files in the same directory as test files
        if self.args.tests:
            for testfile in self.args.tests:
                testdir = compiletools.wrappedos.dirname(compiletools.wrappedos.realpath(testfile))
                swept |= {
                    os.path.join(testdir, fileintestdir)
                    for fileintestdir in os.listdir(testdir)
                    if compiletools.wrappedos.isfile(os.path.join(testdir, fileintestdir))
                }
        extras |= self._not_auto_excluded(swept)

        mergedfiles = []
        if self.args.merge:
            filteredfiles = filterobject({compiletools.wrappedos.realpath(fname) for fname in extras})
            mergedfiles.extend(filteredfiles)
        else:
            for fname in extras:
                realpath = compiletools.wrappedos.realpath(fname)
                print(self.styleobject.adjust(realpath))

        followable = []
        lists = [
            self.args.filename,
            self.args.static,
            self.args.dynamic,
            self.args.tests,
        ]
        for ll in lists:
            if ll:
                followable.extend(ll)
        followable = compiletools.utils.ordered_unique(followable)
        for filename in followable:
            check_filename(filename)
            realpath = compiletools.wrappedos.realpath(filename)
            files = self._hunter.required_files(realpath)
            filteredfiles = filterobject(files)

            if self.args.merge:
                mergedfiles.extend(filteredfiles)
            else:
                try:
                    # Remove realpath from the list so that the style object
                    # doesn't have to worry about it.
                    filteredfiles = [f for f in filteredfiles if f != realpath]
                except KeyError:
                    pass
                print(self.styleobject.adjust(realpath))
                self.styleobject(sorted(filteredfiles))

        if self.args.merge:
            mergedfiles = compiletools.utils.ordered_unique(mergedfiles)
            self.styleobject(sorted(mergedfiles))


def main(argv=None):
    cap = compiletools.apptools.create_parser("Generate file lists for packaging", argv=argv)
    Filelist.add_arguments(cap)
    from compiletools.build_context import BuildContext

    context = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=context)

    # Same gate as cake.process(): with no explicit targets, discover them
    # through the shared driver so the file list covers exactly what
    # ct-cake --auto would build, under the same re-anchored config.
    if args.auto and not any([args.filename, args.static, args.dynamic, args.tests]):
        args = compiletools.findtargets.discover_targets_and_reanchor(args, context)
        # Re-gather and recompute over the widened namespace so the
        # dependency walk below uses the discovered targets' flags.
        compiletools.apptools.resubstitute(args)

    headerdeps = compiletools.headerdeps.create(args, context=context)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=context)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=context)
    filelist = Filelist(args, hunter)
    filelist.process()

    # For testing purposes, clear out the memcaches for the times when main is called more than once.
    compiletools.wrappedos.clear_cache()
    compiletools.utils.clear_cache()
    compiletools.git_utils.clear_cache()
    headerdeps.clear_cache()
    magicparser.clear_cache()
    hunter.clear_cache()

    return 0
