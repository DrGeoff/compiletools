# vim: set filetype=python:
from __future__ import print_function
import sys
import configargparse
import ct.wrappedos
import ct.utils
from ct.hunter import Hunter


class Rule:

    """ A rule is a target, prerequisites and optionally a recipe
        https://www.gnu.org/software/make/manual/html_node/Rule-Introduction.html#Rule-Introduction
        Example: myrule = Rule( target='mytarget'
                              , prerequisites='file1.hpp file2.hpp'
                              , recipe='g++ -c mytarget.cpp -o mytarget.o'
                              )
        Note: it had to be a class rather than a dict so that we could hash it.
    """

    def __init__(self, target, prerequisites, recipe=None, phony=False):
        self.target = target
        self.prerequisites = prerequisites
        self.recipe = recipe
        self.phony = phony

    def __repr__(self):
        return "%s(%r)" % (self.__class__, self.__dict__)

    def __str__(self):
        return "%r" % (self.__dict__)

    def __eq__(self, other):
        return self.target == other.target

    def __hash__(self):
        return hash(self.target)

    def write(self, makefile):
        """ Write the given rule into the given Makefile."""
        if self.phony:
            makefile.write(" ".join([".PHONY:", self.target, "\n"]))

        makefile.write(self.target + ": " + self.prerequisites + "\n")
        try:
            makefile.write("\t" + self.recipe + "\n")
        except TypeError:
            pass
        makefile.write("\n")


class MakefileCreator:

    """ Create a Makefile based on the filename, --static and --dynamic command line options """

    def __init__(self, parser, variant, argv=None):
        self.namer = ct.utils.Namer(parser, variant, argv)
        ct.utils.add_target_arguments(parser)
        ct.utils.add_link_arguments(parser)

        # Keep track of what build artifacts are created for easier cleanup
        self.objects = set()
        self.object_directories = set()

        # The rules need to be written to disk in a specific order
        self.rules = ct.utils.OrderedSet()

        self.args = None
        # self.args will exist after this call
        ct.utils.setattr_args(self,argv)

        self.hunter = Hunter(argv)

    def create(self):
        # Find the realpaths of the given filenames (to avoid this being
        # duplicated many times)
        realpath_sources = [ct.wrappedos.realpath(filename)
                            for filename in self.args.filename]
        all_exes_dirs = [
            self.namer.executable_dir(source) for source in realpath_sources]
        all_exes = [self.namer.executable_pathname(
            source) for source in realpath_sources]

        # By using a set, duplicate rules will be eliminated.
        rule_all = Rule(
            target="all",
            prerequisites=" ".join(["mkdir_output"] + all_exes),
            phony=True)
        self.rules.add(rule_all)

        self.rules |= self._create_makefile_rules_for_sources(realpath_sources)

        rule_mkdir_output = Rule(
            target="mkdir_output",
            prerequisites="",
            recipe=" ".join(
                ["mkdir -p"] +
                all_exes_dirs +
                list(
                    self.object_directories)),
            phony=True)
        self.rules.add(rule_mkdir_output)

        # Use a "./" in front of files/directories to be removed to avoid any
        # nasy surprises by files beginning with "-"
        safe_rm_objdir = [
            "find",
            "./" +
            self.args.objdir,
            "-type d -empty -delete"]
        safe_rm_bindir = [
            "find",
            "./" +
            self.args.bindir,
            "-type d -empty -delete"]
        rule_clean = Rule(
            target="clean",
            prerequisites="",
            recipe=" ".join(
                ["rm -f"] +
                all_exes +
                list(
                    self.objects) +
                [";"] +
                safe_rm_objdir +
                [";"] +
                safe_rm_bindir),
            phony=True)
        self.rules.add(rule_clean)

        rule_realclean = Rule(target="realclean", prerequisites="", recipe=" ".join(
            ["rm -rf", self.args.bindir, "; rm -rf", self.args.objdir]), phony=True)
        self.rules.add(rule_realclean)

        self.write()

    def _create_compile_rule_for_source(self, source):
        """ For a given source file return the compile rule required for the Makefile """
        deplist = self.hunter.header_dependencies(source)
        prerequisites = [source] + sorted([str(dep) for dep in deplist])

        self.object_directories.add(self.namer.object_dir(source))
        obj_name = self.namer.object_pathname(source)
        self.objects.add(obj_name)
        if ct.wrappedos.isc(source):
            magic_c_flags = self.hunter.magic()[source].get('CFLAGS', [])
            return Rule(target=obj_name,
                        prerequisites=" ".join(prerequisites),
                        recipe=" ".join([self.args.CC,
                                         self.args.CFLAGS] + list(magic_c_flags) + ["-c",
                                                                                    "-o",
                                                                                    obj_name,
                                                                                    source]))
        else:
            magic_cxx_flags = self.hunter.magic()[source].get('CXXFLAGS', [])
            return Rule(target=obj_name,
                        prerequisites=" ".join(prerequisites),
                        recipe=" ".join([self.args.CXX,
                                         self.args.CXXFLAGS] + list(magic_cxx_flags) + ["-c",
                                                                                        "-o",
                                                                                        obj_name,
                                                                                        source]))

    def _create_link_rule(self, source_filename, complete_sources):
        """ For a given source file (so usually the file with the main) and the
            set of complete sources (i.e., all the other source files + the original)
            return the link rule required for the Makefile
        """

        exe_name = self.namer.executable_pathname(
            ct.wrappedos.realpath(source_filename))
        object_names = " ".join(
            self.namer.object_pathname(source) for source in complete_sources)

        all_magic_ldflags = set()
        for source in complete_sources:
            magic_flags = self.hunter.magic()[source]
            all_magic_ldflags |= magic_flags.get('LDFLAGS', set())
            all_magic_ldflags |= magic_flags.get(
                'LINKFLAGS',
                set())  # For backward compatibility with cake

        return Rule(target=exe_name,
                    prerequisites=object_names,
                    recipe=" ".join([self.args.LD,
                                     self.args.LDFLAGS] + ["-o",
                                                           exe_name,
                                                           object_names] + list(all_magic_ldflags)))

    def _create_makefile_rules_for_sources(self, sources):
        """ For all the given source files return the set of rules required for the Makefile """


        # The set of rules needed to turn the source file into an executable
        # (or library as appropriate)
        rules_for_source = ct.utils.OrderedSet()
       
        # Output all the link rules
        for source in sources:
            complete_sources = self.hunter.required_source_files(source)
            if self.args.verbose >= 6:
                print(
                    "Complete list of implied source files for " +
                    source +
                    ": " +
                    " ".join(
                        cs for cs in complete_sources))
            rules_for_source.add(self._create_link_rule(source, complete_sources))

        # Output all the compile rules
        for source in sources:
            # Reset the cycle detection because we are starting a new source file
            cycle_detection = set()
            complete_sources = self.hunter.required_source_files(source)
            for item in complete_sources:
                if item not in cycle_detection:
                    cycle_detection.add(item)
                    rules_for_source.add(
                        self._create_compile_rule_for_source(item))
                else:                    
                    print("ct-create-makefile detected cycle on source " + item)

        return rules_for_source

    def write(self, makefile_name='Makefile'):
        """ Take a list of rules and write the rules to a Makefile """
        with open(makefile_name, mode='w') as mfile:
            mfile.write("# Makefile generated by ct-create-makefile\n")
            for rule in self.rules:
                rule.write(mfile)


def main(argv=None):
    if argv is None:
        argv = sys.argv
    else:
        print("Must be in a test.  Somebody has supplied argv.")
        print(argv)
    variant = ct.utils.extract_variant_from_argv(argv)
    cap = configargparse.getArgumentParser()
    makefile_creator = MakefileCreator(cap, variant, argv)

    myargs = cap.parse_known_args(argv[1:])
    ct.utils.verbose_print_args(myargs[0])

    makefile_creator.create()
    return 0