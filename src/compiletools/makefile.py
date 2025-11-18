# vim: set filetype=python:
import os
import sys
from io import open

import stringzilla as sz

import compiletools.utils
import compiletools.wrappedos
import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.namer
import compiletools.configutils
import compiletools.filesystem_utils


class Rule:
    """A rule is a target, prerequisites and optionally a recipe
    and optionally any order_only_prerequisites.
    https://www.gnu.org/software/make/manual/html_node/Rule-Introduction.html#Rule-Introduction
    Example: myrule = Rule( target='mytarget'
                          , prerequisites='file1.hpp file2.hpp'
                          , recipe='g++ -c mytarget.cpp -o mytarget.o'
                          )
    Note: it had to be a class rather than a dict so that we could hash it.
    """

    def __init__(
        self,
        target,
        prerequisites,
        order_only_prerequisites=None,
        recipe=None,
        phony=False,
    ):
        self.target = target
        self.prerequisites = prerequisites
        self.order_only_prerequisites = order_only_prerequisites
        self.recipe = recipe
        self.phony = phony

    def __repr__(self):
        return "%s(%r)" % (self.__class__, self.__dict__)

    def __str__(self):
        return "%r" % self.__dict__

    def __eq__(self, other):
        return self.target == other.target

    def __hash__(self):
        return hash(self.target)

    def write(self, makefile):
        """Write the given rule into the given Makefile."""
        if self.phony:
            makefile.write(" ".join([".PHONY:", self.target, "\n"]))

        linetowrite = "".join([self.target, ": ", self.prerequisites])
        if self.order_only_prerequisites:
            linetowrite += "".join([" | ", self.order_only_prerequisites])

        makefile.write(linetowrite + "\n")
        try:
            makefile.write("\t" + self.recipe + "\n")
        except TypeError:
            pass
        makefile.write("\n")


class LinkRuleCreator(object):
    """Base class to provide common infrastructure for the creation of
    specific link rules by the derived classes.
    """

    def __init__(self, args, namer, hunter):
        self.args = args
        self.namer = namer
        self.hunter = hunter

    def _create_link_rule(
        self,
        outputname,
        completesources,
        linker,
        linkerflags=None,
        extraprereqs=None,
        suppressmagicldflags=False,
    ):
        """For a given source file (so usually the file with the main) and the
        set of complete sources (i.e., all the other source files + the original)
        return the link rule required for the Makefile
        """
        if extraprereqs is None:
            extraprereqs = []
        if linkerflags is None:
            linkerflags = ""

        allprerequisites = " ".join(extraprereqs)
        # Get macro state hash and dep hash for each source file (required for new naming scheme)
        object_names = compiletools.utils.ordered_unique([
            self.namer.object_pathname(
                source,
                self.hunter.macro_state_hash(source),
                self.namer.compute_dep_hash(self.hunter.header_dependencies(source))
            )
            for source in completesources
        ])
        allprerequisites += " "
        allprerequisites += " ".join(object_names)

        all_magic_ldflags = []
        if not suppressmagicldflags:
            for source in completesources:
                magic_flags = self.hunter.magicflags(source)
                all_magic_ldflags.extend(magic_flags.get(sz.Str("LDFLAGS"), []))
                # LINKFLAGS is now merged into LDFLAGS by magicflags.py
        recipe = ""
        
        if self.args.verbose >= 1:
            recipe += " ".join(["+@echo ...", outputname, ";"])
        
        link_flags = [linker, "-o", outputname] + list(object_names) + [str(flag) for flag in all_magic_ldflags] + [linkerflags]
        link_cmd = " ".join(link_flags)
        recipe += link_cmd
        return Rule(target=outputname, prerequisites=allprerequisites, recipe=recipe)


class StaticLibraryLinkRuleCreator(LinkRuleCreator):
    def __call__(self, sources, libraryname):
        rule = self._create_link_rule(
            outputname=libraryname,
            completesources=sources,
            linker="ar -src",
            suppressmagicldflags=True,
        )
        return [rule]


class DynamicLibraryLinkRuleCreator(LinkRuleCreator):
    def __call__(self, sources, libraryname):
        rule = self._create_link_rule(
            outputname=libraryname,
            completesources=sources,
            linker=self.args.LD,
            linkerflags=self.args.LDFLAGS + " -shared",
        )
        return [rule]


class ExeLinkRuleCreator(LinkRuleCreator):
    def __call__(self, sources, libraryname):
        extraprereqs = []
        linkerflags = self.args.LDFLAGS

        # If there is also a library being built then automatically
        # include the path to that library to allow easy linking
        if self.args.static or self.args.dynamic:
            linkerflags += " -L"
            linkerflags += self.namer.executable_dir()
        if self.args.static:
            staticlibrarypathname = self.namer.staticlibrary_pathname(compiletools.wrappedos.realpath(self.args.static[0]))
            libname = os.path.join(self.namer.executable_dir(), os.path.basename(staticlibrarypathname))
            extraprereqs.append(libname)
        if self.args.dynamic:
            dynamiclibrarypathname = self.namer.dynamiclibrary_pathname(compiletools.wrappedos.realpath(self.args.dynamic[0]))
            libname = os.path.join(self.namer.executable_dir(), os.path.basename(dynamiclibrarypathname))
            extraprereqs.append(libname)

        linkrules = {}
        for source in sources:
            if self.args.verbose >= 4:
                print(
                    "ExeLinkRuleCreator. Asking hunter for required_source_files for source=",
                    source,
                )
            completesources = self.hunter.required_source_files(source)
            if self.args.verbose >= 6:
                print(
                    "ExeLinkRuleCreator. Complete list of implied source files for "
                    + source
                    + ": "
                    + " ".join(cs for cs in completesources)
                )
            exename = self.namer.executable_pathname(compiletools.wrappedos.realpath(source))
            rule = self._create_link_rule(
                outputname=exename,
                completesources=completesources,
                linker=self.args.LD,
                linkerflags=linkerflags,
                extraprereqs=extraprereqs,
            )
            linkrules[rule.target] = rule

        return list(linkrules.values())


class MakefileCreator:
    """Create a Makefile based on the filename, --static and --dynamic
    command line options.
    """

    def __init__(self, args, hunter):
        self.args = args

        # Keep track of what build artifacts are created for easier cleanup
        self.objects = set()
        self.object_directories = set()

        # By using a set, duplicate rules will be eliminated.
        # However, rules need to be written to disk in a specific order
        # so we use a dict to maintain order and uniqueness
        self.rules = {}

        self.namer = compiletools.namer.Namer(args)
        self.hunter = hunter

        # Check if ct-lock-helper is available when using shared_objects
        if args.shared_objects:
            import shutil
            if not shutil.which('ct-lock-helper'):
                print("ERROR: ct-lock-helper not found in PATH", file=sys.stderr)
                print("", file=sys.stderr)
                print("The --shared-objects flag requires ct-lock-helper to be installed.", file=sys.stderr)
                print("", file=sys.stderr)
                print("Solutions:", file=sys.stderr)
                print("  1. Install compiletools: pip install compiletools", file=sys.stderr)
                print("  2. Install from source: pip install -e .", file=sys.stderr)
                print("  3. Add ct-lock-helper to your PATH", file=sys.stderr)
                print("", file=sys.stderr)
                print("Or disable shared objects with: --no-shared-objects", file=sys.stderr)
                sys.exit(1)

        # Detect filesystem type once and cache it
        self._filesystem_type = self._detect_filesystem_type()

        # Detect OS type once and cache it
        self._os_type = self._detect_os_type()

        # Validate umask compatibility with shared-objects
        if self.args.shared_objects:
            self._validate_umask_for_shared_objects()

    @staticmethod
    def add_arguments(cap):
        compiletools.apptools.add_target_arguments_ex(cap)
        compiletools.apptools.add_link_arguments(cap)
        # Don't add the output directory arguments. Namer will do it.
        # compiletools.utils.add_output_directory_arguments(parser, variant)
        compiletools.namer.Namer.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add(
            "--makefilename",
            default="Makefile",
            help="Output filename for the Makefile",
        )
        cap.add(
            "--build-only-changed",
            help="Only build the binaries depending on the source or header absolute filenames in this space-delimited list.",
        )
        compiletools.utils.add_boolean_argument(
            parser=cap,
            name="shared-objects",
            dest="shared_objects",
            default=False,
            help="Enable shared object cache for multi-user/multi-host builds",
        )
        compiletools.utils.add_flag_argument(
            parser=cap,
            name="serialise-tests",
            dest="serialisetests",
            default=False,
            help="Force the unit tests to run serially rather than in parallel. Defaults to false because it is slower.",
        )

    def _detect_filesystem_type(self):
        """Detect filesystem type once for objdir"""
        fstype = compiletools.filesystem_utils.get_filesystem_type(self.args.objdir)
        if self.args.verbose >= 3:
            print(f"Detected filesystem type: {fstype}")
        return fstype

    def _detect_os_type(self):
        """Detect OS type once for platform-specific code generation"""
        import platform
        system = platform.system().lower()
        if 'linux' in system:
            return 'linux'
        elif 'darwin' in system or 'bsd' in system:
            return 'bsd'
        else:
            # Default to linux for unknown platforms
            return 'linux'

    def _validate_umask_for_shared_objects(self):
        """Log warning if umask may affect multi-user shared-objects mode"""
        current_umask = os.umask(0)
        os.umask(current_umask)  # Restore immediately

        # Check if umask blocks group read/write
        # For single-user scenarios, restrictive umask is fine (user owns all files)
        # For multi-user scenarios, group permissions are needed for cross-user cleanup
        if (current_umask & 0o060) and self.args.verbose >= 1:
            print(
                f"Warning: shared-objects enabled with restrictive umask {oct(current_umask)}\n"
                f"  Single-user mode: Works fine (you can always remove your own locks)\n"
                f"  Multi-user mode: Requires umask 0002 or 0007 for cross-user lock cleanup\n"
                f"  If using multi-user cache, set: umask 0002",
                file=sys.stderr
            )

    def _uptodate(self):
        """Is the Makefile up to date?
        If the argv has changed
        then regenerate on the assumption that the filelist or flags have changed
        else check if the modification time of the Makefile is greater than
             the modification times of all the source and header files.
        """
        # Check if the Makefile exists and grab its modification time if it does exist.
        try:
            makefilemtime = compiletools.wrappedos.getmtime(self.args.makefilename)
        except OSError:
            # If the Makefile doesn't exist then we aren't up to date
            if self.args.verbose > 7:
                print("Regenerating Makefile.")
                print(
                    "Could not determine mtime for {}. Assuming that it doesn't exist.".format(self.args.makefilename)
                )
            return False

        # See how the Makefile was previously generated
        expected = "".join(["# Makefile generated by ", str(self.args)])

        with open(self.args.makefilename, mode="r", encoding="utf-8") as mfile:
            previous = mfile.readline().strip()
            if previous != expected:
                if self.args.verbose > 7:
                    print("Regenerating Makefile.")
                    print('Previous generation line was "{}".'.format(previous))
                    print('Current  generation line  is "{}".'.format(expected))
                return False
            elif self.args.verbose > 9:
                print("Makefile header line is identical.  Testing mod time of all the files now.")

        # Check the mod times of all the implied files against the mod time of the Makefile
        for sf in self._gather_root_sources():
            filelist = self.hunter.required_files(sf)
            for ff in filelist:
                if compiletools.wrappedos.getmtime(ff) > makefilemtime:
                    if self.args.verbose > 7:
                        print("Regenerating Makefile.")
                        print(
                            "mtime {} for {} is newer than mtime for the Makefile".format(compiletools.wrappedos.getmtime(ff), ff)
                        )
                    return False
                elif self.args.verbose > 9:
                    print(
                        "mtime {} for {} is older than mtime for the Makefile. This wont trigger regeneration of the Makefile.".format(
                            compiletools.wrappedos.getmtime(ff), ff
                        )
                    )

        if self.args.verbose > 9:
            print("Makefile is up to date.  Not recreating.")

        return True

    def _wrap_compile_with_lock(self, compile_cmd, target):
        """Wrap compile command with ct-lock-helper for locking.

        Args:
            compile_cmd: Compile command without -o flag (e.g., "gcc -c file.c")
            target: Target file (e.g., "$@")

        Returns:
            Complete command with locking (e.g., "ct-lock-helper compile --target=$@ --strategy=lockdir -- gcc -c file.c")
        """
        if not self.args.shared_objects:
            return compile_cmd + " -o " + target

        strategy = compiletools.filesystem_utils.get_lock_strategy(self._filesystem_type)

        # Build environment variables for lock configuration
        env_vars = []

        if strategy == 'lockdir':
            sleep_interval = self._get_lockdir_sleep_interval()
            env_vars.append(f'CT_LOCK_SLEEP_INTERVAL={sleep_interval}')
        elif strategy == 'cifs':
            env_vars.append(f'CT_LOCK_SLEEP_INTERVAL_CIFS={self.args.sleep_interval_cifs}')
        else:  # flock
            env_vars.append(f'CT_LOCK_SLEEP_INTERVAL_FLOCK={self.args.sleep_interval_flock_fallback}')

        env_vars.append(f'CT_LOCK_WARN_INTERVAL={self.args.lock_warn_interval}')
        env_vars.append(f'CT_LOCK_TIMEOUT={self.args.lock_cross_host_timeout}')

        env_prefix = " ".join(env_vars) + " " if env_vars else ""

        return f'{env_prefix}ct-lock-helper compile --target={target} --strategy={strategy} -- {compile_cmd}'

    def _get_locking_recipe_prefix(self):
        """Generate filesystem-specific locking code prefix (deprecated, use _wrap_compile_with_lock)"""
        # Kept for backward compatibility, but now returns empty
        # New code should use _wrap_compile_with_lock() instead
        return ""

    def _get_lockdir_sleep_interval(self):
        """Get sleep interval for lockdir polling.

        Auto-detects optimal interval based on filesystem type, but allows
        user override via --sleep-interval-lockdir.

        Returns:
            float: Sleep interval (only used by lockdir strategy for NFS/GPFS/Lustre)
        """
        # If user explicitly set the interval, use it
        if self.args.sleep_interval_lockdir is not None:
            return self.args.sleep_interval_lockdir

        # Otherwise auto-detect based on filesystem type
        return compiletools.filesystem_utils.get_lockdir_sleep_interval(self._filesystem_type)

    def _get_locking_recipe_suffix(self):
        """Generate filesystem-specific locking code suffix (deprecated)"""
        # Lock cleanup now handled by ct-lock-helper
        return ""

    def _create_all_rule(self):
        """Create the rule that in depends on all build products"""
        prerequisites = ["build"]
        if self.args.tests:
            prerequisites.append("runtests")

        return Rule(target="all", prerequisites=" ".join(prerequisites), phony=True)

    @staticmethod
    def _create_build_rule(prerequisites):
        """Create the rule that in depends on all build products"""
        return Rule(target="build", prerequisites=" ".join(prerequisites), phony=True)

    def _create_clean_rules(self, alloutputs):
        rules = {}

        # Clean will only remove empty directories
        # Use realclean if you want force directories to be removed.
        rmcopiedexes = " ".join(
            [
                "find",
                self.namer.executable_dir(),
                "-type f -executable -delete 2>/dev/null",
            ]
        )
        rmtargetsandobjects = " ".join(["rm -f"] + list(alloutputs) + list(self.objects))
        rmemptydirs = " ".join(["find", self.namer.object_dir(), "-type d -empty -delete"])
        recipe = ";".join([rmcopiedexes, rmtargetsandobjects, rmemptydirs])

        if self.namer.executable_dir() != self.namer.object_dir():
            recipe += " ".join([";find", self.namer.executable_dir(), "-type d -empty -delete"])

        rule_clean = Rule(target="clean", prerequisites="", recipe=recipe, phony=True)
        rules[rule_clean.target] = rule_clean

        # Now for realclean.  Just take a heavy handed rm -rf approach.
        # Note this will even remove the Makefile generated by ct-cake
        recipe = " ".join(["rm -rf", self.namer.executable_dir()])
        if self.namer.executable_dir() != self.namer.object_dir():
            recipe += "; rm -rf " + self.namer.object_dir()
        rule_realclean = Rule(target="realclean", prerequisites="", recipe=recipe, phony=True)
        rules[rule_realclean.target] = rule_realclean

        return list(rules.values())

    def _create_cp_rule(self, output):
        """Given the original output, copy it to the executable_dir()"""
        if self.namer.executable_dir() == compiletools.wrappedos.dirname(output):
            return None

        return Rule(
            target=os.path.join(self.namer.executable_dir(), os.path.basename(output)),
            prerequisites=output,
            recipe=" ".join(["cp", output, self.namer.executable_dir(), "2>/dev/null ||true"]),
        )

    def _create_test_rules(self, alltestsources):
        testprefix = ""
        if self.args.TESTPREFIX:
            testprefix = self.args.TESTPREFIX

        rules = {}

        # Create the PHONY that will run all the tests
        prerequisites = " ".join([".".join([self.namer.executable_pathname(tt), "result"]) for tt in alltestsources])
        runtestsrule = Rule(target="runtests", prerequisites=prerequisites, phony=True)
        rules[runtestsrule.target] = runtestsrule

        # Create a rule for each individual test
        for tt in alltestsources:
            exename = self.namer.executable_pathname(tt)
            testresult = ".".join([exename, "result"])

            recipe = ""
            if self.args.verbose >= 1:
                recipe += " ".join(["@echo ...", exename, ";"])
            recipe += " ".join(["rm -f", testresult, "&&", testprefix, exename, "&& touch", testresult])
            rule = Rule(target=testresult, prerequisites=exename, recipe=recipe)
            rules[rule.target] = rule
        return list(rules.values())

    @staticmethod
    def _create_tests_not_parallel_rule():
        return Rule(target=".NOTPARALLEL", prerequisites="runtests", phony=True)

    def _gather_root_sources(self):
        """Gather all the source files listed on the command line
        into one uber set
        """
        sources = []
        if self.args.static:
            sources.extend(self.args.static)
        if self.args.dynamic:
            sources.extend(self.args.dynamic)
        if self.args.filename:
            sources.extend(self.args.filename)
        if self.args.tests:
            sources.extend(self.args.tests)
        sources = compiletools.utils.ordered_unique(sources)

        return sources

    def _gather_build_outputs(self):
        """Gathers together object files and other outputs"""
        buildoutputs = []

        if self.args.static:
            staticlibrarypathname = self.namer.staticlibrary_pathname()
            buildoutputs.append(staticlibrarypathname)
            buildoutputs.append(os.path.join(self.namer.executable_dir(), os.path.basename(staticlibrarypathname)))

        if self.args.dynamic:
            dynamiclibrarypathname = self.namer.dynamiclibrary_pathname()
            buildoutputs.append(dynamiclibrarypathname)
            buildoutputs.append(
                os.path.join(
                    self.namer.executable_dir(),
                    os.path.basename(dynamiclibrarypathname),
                )
            )

        buildoutputs.extend(self.namer.all_executable_pathnames())
        if self.args.filename:
            allcopiedexes = {
                os.path.join(self.namer.executable_dir(), self.namer.executable_name(source))
                for source in self.args.filename
            }
            buildoutputs.extend(allcopiedexes)

        buildoutputs.extend(self.namer.all_test_pathnames())
        buildoutputs = compiletools.utils.ordered_unique(buildoutputs)

        return buildoutputs

    def create(self):
        if self._uptodate():
            return

        # Pre-discover all source files to optimize subsequent dependency lookups
        # This ensures that all hunter.required_source_files() calls benefit from cached results
        if self.args.verbose >= 7:
            print("Makefile: Pre-discovering all source files for optimization")
        self.hunter.huntsource()

        # Find the realpaths of the given filenames (to avoid this being
        # duplicated many times)
        os.makedirs(self.namer.executable_dir(), exist_ok=True)
        rule = self._create_all_rule()
        self.rules[rule.target] = rule
        buildoutputs = self._gather_build_outputs()
        rule = self._create_build_rule(buildoutputs)
        self.rules[rule.target] = rule

        realpath_sources = []
        if self.args.filename:
            realpath_sources += sorted(compiletools.wrappedos.realpath(source) for source in self.args.filename)
        if self.args.tests:
            realpath_tests = sorted(compiletools.wrappedos.realpath(source) for source in self.args.tests)
            realpath_sources += realpath_tests

        if self.args.filename or self.args.tests:
            allexes = {self.namer.executable_pathname(source) for source in realpath_sources}
            for exe in allexes:
                cprule = self._create_cp_rule(exe)
                if cprule:
                    self.rules[cprule.target] = cprule

            link_rules = self._create_link_rules_for_sources(realpath_sources, exe_static_dynamic="Exe")
            for rule in link_rules:
                self.rules[rule.target] = rule

        if self.args.tests:
            test_rules = self._create_test_rules(realpath_tests)
            for rule in test_rules:
                self.rules[rule.target] = rule
            if self.args.serialisetests:
                rule = self._create_tests_not_parallel_rule()
                self.rules[rule.target] = rule

        if self.args.static:
            libraryname = self.namer.staticlibrary_pathname(compiletools.wrappedos.realpath(self.args.static[0]))
            cprule = self._create_cp_rule(libraryname)
            if cprule:
                self.rules[cprule.target] = cprule
            realpath_static = {compiletools.wrappedos.realpath(filename) for filename in self.args.static}
            static_rules = self._create_link_rules_for_sources(
                realpath_static,
                exe_static_dynamic="StaticLibrary",
                libraryname=libraryname,
            )
            for rule in static_rules:
                self.rules[rule.target] = rule

        if self.args.dynamic:
            libraryname = self.namer.dynamiclibrary_pathname(compiletools.wrappedos.realpath(self.args.dynamic[0]))
            cprule = self._create_cp_rule(libraryname)
            if cprule:
                self.rules[cprule.target] = cprule
            realpath_dynamic = {compiletools.wrappedos.realpath(filename) for filename in self.args.dynamic}
            dynamic_rules = self._create_link_rules_for_sources(
                realpath_dynamic,
                exe_static_dynamic="DynamicLibrary",
                libraryname=libraryname,
            )
            for rule in dynamic_rules:
                self.rules[rule.target] = rule

        if self.args.filename or self.args.tests:
            compile_rules = self._create_compile_rules_for_sources(realpath_sources)
            for rule in compile_rules:
                self.rules[rule.target] = rule
        if self.args.static and realpath_static:
            static_compile_rules = self._create_compile_rules_for_sources(realpath_static)
            for rule in static_compile_rules:
                self.rules[rule.target] = rule
        if self.args.dynamic and realpath_dynamic:
            dynamic_compile_rules = self._create_compile_rules_for_sources(realpath_dynamic)
            for rule in dynamic_compile_rules:
                self.rules[rule.target] = rule

        clean_rules = self._create_clean_rules(buildoutputs)
        for rule in clean_rules:
            self.rules[rule.target] = rule

        if self.args.build_only_changed:
            changed_files = set(self.args.build_only_changed.split(" "))
            targets = set()
            done = False
            while not done:
                done = True
                for rule in self.rules.values():
                    if rule.target in targets:
                        continue
                    relevant_changed_files = set(rule.prerequisites.split(" ")).intersection(changed_files)
                    if not relevant_changed_files:
                        continue
                    changed_files.add(rule.target)
                    targets.add(rule.target)
                    done = False
                    if self.args.verbose >= 3:
                        print(
                            "Building {} because it depends on changed: {}".format(
                                rule.target, list(relevant_changed_files)
                            )
                        )
            new_rules = {}
            for rule in self.rules.values():
                if not rule.phony:
                    new_rules[rule.target] = rule
                else:
                    rule.prerequisites = " ".join(set(rule.prerequisites.split()).intersection(targets))
                    new_rules[rule.target] = rule
            self.rules = new_rules

        self.write(self.args.makefilename)
        return self.args.makefilename

    def _create_object_directory(self):
        return Rule(
            target=self.args.objdir,
            prerequisites="",
            recipe=" ".join(["mkdir -p", self.args.objdir]),
        )

    def _create_compile_rule_for_source(self, filename):
        """For a given source file return the compile rule required for the Makefile"""
        if self.args.verbose >= 9:
            print("MakefileCreator::_create_compile_rule_for_source" + filename)

        if compiletools.utils.is_header(filename):
            sys.stderr.write("Error.  Trying to create a compile rule for a header file: ", filename)

        deplist = self.hunter.header_dependencies(filename)
        prerequisites = [filename] + sorted([str(dep) for dep in deplist])

        # Get magicflags and full macro state hash (always required for new naming scheme)
        magicflags = self.hunter.magicflags(filename)
        macro_state_hash = self.hunter.macro_state_hash(filename)

        # Compute dependency hash for object naming
        dep_hash = self.namer.compute_dep_hash(deplist)

        self.object_directories.add(self.namer.object_dir(filename))
        # Pass precomputed dep_hash (not list) to keep lru_cache working
        obj_name = self.namer.object_pathname(filename, macro_state_hash, dep_hash)
        self.objects.add(obj_name)

        recipe = ""

        if self.args.verbose >= 1:
            recipe = " ".join(["@echo ...", filename, ";"])

        magic_cpp_flags = magicflags.get(sz.Str("CPPFLAGS"), [])
        if compiletools.utils.is_c_source(filename):
            magic_c_flags = magicflags.get(sz.Str("CFLAGS"), [])
            compile_flags = [self.args.CC, self.args.CFLAGS] + [str(flag) for flag in magic_cpp_flags] + [str(flag) for flag in magic_c_flags]
        else:
            magic_cxx_flags = magicflags.get(sz.Str("CXXFLAGS"), [])
            compile_flags = [self.args.CXX, self.args.CXXFLAGS] + [str(flag) for flag in magic_cpp_flags] + [str(flag) for flag in magic_cxx_flags]

        # Build compile command without -o flag (ct-lock-helper adds it)
        compile_cmd_base = " ".join(compile_flags + ["-c", filename])

        # Wrap with locking if shared_objects enabled
        compile_cmd = self._wrap_compile_with_lock(compile_cmd_base, obj_name)
        recipe += compile_cmd

        if self.args.verbose >= 3:
            print("Creating rule for ", obj_name)

        # The order_only_prerequisite is to create the object directory
        return Rule(
            target=obj_name,
            prerequisites=" ".join(prerequisites),
            order_only_prerequisites=self.args.objdir,
            recipe=recipe,
        )


    def _create_link_rules_for_sources(self, sources, exe_static_dynamic, libraryname=None):
        """For all the given source files return the set of rules required
        for the Makefile that will _link_ the source files into executables.
        """

        # The set of rules needed to turn the source file into an executable
        # (or library as appropriate)
        rules_for_source = {}

        # Output all the link rules
        if self.args.verbose >= 3:
            print("Creating link rule for ", sources)
        
        linkrulecreatorclass = globals()[exe_static_dynamic + "LinkRuleCreator"]
        linkrulecreatorobject = linkrulecreatorclass(args=self.args, namer=self.namer, hunter=self.hunter)
        link_rules = linkrulecreatorobject(libraryname=libraryname, sources=sources)
        for rule in link_rules:
            rules_for_source[rule.target] = rule

        return list(rules_for_source.values())

    def _create_compile_rules_for_sources(self, sources):
        """For all the given source files return the set of rules required
        for the Makefile that will compile the source files into object files.
        """

        # The set of rules needed to turn the source file into an executable
        # (or library as appropriate)
        rules_for_source = {}
        rule = self._create_object_directory()
        rules_for_source[rule.target] = rule

        # Collect all source files that need compile rules
        all_compile_sources = set()
        for source in sources:
            completesources = self.hunter.required_source_files(source)
            all_compile_sources.update(completesources)

        # Create compile rules for each unique source (avoids duplication)
        for source_file in all_compile_sources:
            rule = self._create_compile_rule_for_source(source_file)
            rules_for_source[rule.target] = rule

        return list(rules_for_source.values())

    def write(self, makefile_name="Makefile"):
        """Take a list of rules and write the rules to a Makefile"""
        with compiletools.filesystem_utils.atomic_output_file(makefile_name, mode="w", encoding="utf-8") as mfile:
            mfile.write("# Makefile generated by ")
            mfile.write(str(self.args))
            mfile.write("\n\n")
            mfile.write(".DELETE_ON_ERROR:\n\n")
            for rule in self.rules.values():
                rule.write(mfile)

    def clear_cache(self):
        """Only useful in test scenarios where you need to reset to a pristine state"""
        compiletools.wrappedos.clear_cache()
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        self.namer.clear_cache()
        self.hunter.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()


def main(argv=None):
    cap = compiletools.apptools.create_parser(
        "Create a Makefile that will compile the given source file into an executable (or library)", argv=argv
    )
    MakefileCreator.add_arguments(cap)
    compiletools.hunter.add_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)
    
    
    # Create HeaderDeps and other components
    headerdeps = compiletools.headerdeps.create(args)
    magicparser = compiletools.magicflags.create(args, headerdeps)
    hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)
    makefile_creator = MakefileCreator(args, hunter)
    makefile_creator.create()

    # And clean up for the test cases where main is called more than once
    makefile_creator.clear_cache()
    return 0
