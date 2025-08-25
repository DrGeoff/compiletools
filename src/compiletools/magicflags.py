import sys
import os
import subprocess
import re
from collections import defaultdict
import compiletools.utils
import compiletools.git_utils
import compiletools.headerdeps
import compiletools.wrappedos
import compiletools.configutils
import compiletools.apptools
import compiletools.compiler_macros
import compiletools.dirnamer
from compiletools.file_analyzer import create_file_analyzer
import compiletools.timing


def create(args, headerdeps):
    """MagicFlags Factory"""
    classname = args.magic.title() + "MagicFlags"
    if args.verbose >= 4:
        print("Creating " + classname + " to process magicflags.")
    magicclass = globals()[classname]
    magicobject = magicclass(args, headerdeps)
    return magicobject


def add_arguments(cap, variant=None):
    """Add the command line arguments that the MagicFlags classes require"""
    compiletools.apptools.add_common_arguments(cap, variant=variant)
    compiletools.preprocessor.PreProcessor.add_arguments(cap)
    alldepscls = [
        st[:-10].lower() for st in dict(globals()) if st.endswith("MagicFlags")
    ]
    cap.add(
        "--magic",
        choices=alldepscls,
        default="direct",
        help="Methodology for reading file when processing magic flags",
    )
    cap.add(
        "--max-file-read-size",
        type=int,
        default=0,
        help="Maximum bytes to read from files (0 = entire file)",
    )


class MagicFlagsBase:
    """A magic flag in a file is anything that starts
    with a //# and ends with an =
    E.g., //#key=value1 value2

    Note that a magic flag is a C++ comment.

    This class is a map of filenames
    to the map of all magic flags for that file.
    Each magic flag has a list of values preserving order.
    E.g., { '/somepath/libs/base/somefile.hpp':
               {'CPPFLAGS':['-D', 'MYMACRO', '-D', 'MACRO2'],
                'CXXFLAGS':['-fsomeoption'],
                'LDFLAGS':['-lsomelib']}}
    This function will extract all the magics flags from the given
    source (and all its included headers).
    source_filename must be an absolute path
    """

    def __init__(self, args, headerdeps):
        self._args = args
        self._headerdeps = headerdeps
        
        # Always use the file analyzer cache from HeaderDeps
        self.file_analyzer_cache = self._headerdeps.get_file_analyzer_cache()

        # The magic pattern is //#key=value with whitespace ignored
        self.magicpattern = re.compile(
            r"^[\s]*//#([\S]*?)[\s]*=[\s]*(.*)", re.MULTILINE
        )

    def readfile(self, filename):
        """Derived classes implement this method"""
        raise NotImplementedError

    def __call__(self, filename):
        with compiletools.timing.time_operation(f"magic_flags_analysis_{os.path.basename(filename)}"):
            return self.parse(filename)

    def _handle_source(self, flag, text, filename, magic):
        # Find the include before the //#SOURCE=
        result = re.search(
            r'# \d.* "(/\S*?)".*?//#SOURCE\s*=\s*' + flag, text, re.DOTALL
        )
        # Now adjust the flag to include the full path
        newflag = compiletools.wrappedos.realpath(
            os.path.join(compiletools.wrappedos.dirname(result.group(1)), flag.strip())
        )
        if self._args.verbose >= 9:
            print(
                " ".join(
                    [
                        "Adjusting source magicflag from flag=",
                        flag,
                        "to",
                        newflag,
                    ]
                )
            )

        if not compiletools.wrappedos.isfile(newflag):
            raise IOError(
                filename
                + " specified "
                + magic
                + "='"
                + newflag
                + "' but it does not exist"
            )

        return newflag

    def _handle_include(self, flag):
        flagsforfilename = {}
        flagsforfilename.setdefault("CPPFLAGS", []).append("-I " + flag)
        flagsforfilename.setdefault("CFLAGS", []).append("-I " + flag)
        flagsforfilename.setdefault("CXXFLAGS", []).append("-I " + flag)
        if self._args.verbose >= 9:
            print(f"Added -I {flag} to CPPFLAGS, CFLAGS, and CXXFLAGS")
        return flagsforfilename

    def _handle_pkg_config(self, flag):
        flagsforfilename = defaultdict(list)
        for pkg in flag.split():
            # TODO: when we move to python 3.7, use text=True rather than universal_newlines=True and capture_output=True,
            with compiletools.timing.time_operation(f"pkg_config_cflags_{pkg}"):
                cflags_raw = subprocess.run(
                    ["pkg-config", "--cflags", pkg],
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                ).stdout.rstrip()
                
                # Replace -I flags with -isystem, but only when -I is a standalone flag
                # This helps the CppHeaderDeps avoid searching packages
                cflags = re.sub(r'-I(?=\s|/|$)', '-isystem', cflags_raw)
            
            with compiletools.timing.time_operation(f"pkg_config_libs_{pkg}"):
                libs = subprocess.run(
                    ["pkg-config", "--libs", pkg],
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                ).stdout.rstrip()
            flagsforfilename["CPPFLAGS"].append(cflags)
            flagsforfilename["CFLAGS"].append(cflags)
            flagsforfilename["CXXFLAGS"].append(cflags)
            flagsforfilename["LDFLAGS"].append(libs)
            if self._args.verbose >= 9:
                print(f"Magic PKG-CONFIG = {pkg}:")
                print(f"\tadded {cflags} to CPPFLAGS, CFLAGS, and CXXFLAGS")
                print(f"\tadded {libs} to LDFLAGS")
        return flagsforfilename

    def _parse(self, filename):
        if self._args.verbose >= 4:
            print("Parsing magic flags for " + filename)

        # We assume that headerdeps _always_ exist
        # before the magic flags are called.
        # When used in the "usual" fashion this is true.
        # However, it is possible to call directly so we must
        # ensure that the headerdeps exist manually.
        with compiletools.timing.time_operation(f"magic_flags_headerdeps_{os.path.basename(filename)}"):
            self._headerdeps.process(filename)

        with compiletools.timing.time_operation(f"magic_flags_readfile_{os.path.basename(filename)}"):
            text = self.readfile(filename)
        
        with compiletools.timing.time_operation(f"magic_flags_parsing_{os.path.basename(filename)}"):
            flagsforfilename = defaultdict(list)

            for match in self.magicpattern.finditer(text):
                magic, flag = match.groups()

                # If the magic was SOURCE then fix up the path in the flag
                if magic == "SOURCE":
                    flag = self._handle_source(flag, text, filename, magic)

                # If the magic was INCLUDE then modify that into the equivalent CPPFLAGS, CFLAGS, and CXXFLAGS
                if magic == "INCLUDE":
                    with compiletools.timing.time_operation(f"magic_flags_include_handling_{flag}"):
                        extrafff = self._handle_include(flag)
                        for key, values in extrafff.items():
                            for value in values:
                                flagsforfilename[key].append(value)

                # If the magic was PKG-CONFIG then call pkg-config
                if magic == "PKG-CONFIG":
                    with compiletools.timing.time_operation(f"magic_flags_pkgconfig_{flag}"):
                        extrafff = self._handle_pkg_config(flag)
                        for key, values in extrafff.items():
                            for value in values:
                                flagsforfilename[key].append(value)

                flagsforfilename[magic].append(flag)
                if self._args.verbose >= 5:
                    print(
                        "Using magic flag {0}={1} extracted from {2}".format(
                            magic, flag, filename
                        )
                    )
            
            # Deduplicate all flags while preserving order
            for key in flagsforfilename:
                flagsforfilename[key] = compiletools.utils.ordered_unique(flagsforfilename[key])

        return flagsforfilename

    @staticmethod
    def clear_cache():
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        compiletools.wrappedos.clear_cache()
        compiletools.apptools.clear_cache()
        DirectMagicFlags.clear_cache()
        CppMagicFlags.clear_cache()


class DirectMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        # Track defined macros during processing
        self.defined_macros = set()
        # Track macro values for expression evaluation
        self.macro_values = {}

    def _add_macros_from_command_line_flags(self):
        """Extract -D macros from command-line CPPFLAGS and CXXFLAGS and add them to defined_macros"""
        import compiletools.apptools
        
        # Extract macros from CPPFLAGS and CXXFLAGS only (excluding CFLAGS to match original behavior)
        macros = compiletools.apptools.extract_command_line_macros(
            self._args,
            flag_sources=['CPPFLAGS', 'CXXFLAGS'],
            include_compiler_macros=False,  # Don't include compiler macros here, done separately
            verbose=self._args.verbose
        )
        
        # Update both storage mechanisms to maintain compatibility
        for macro_name, macro_value in macros.items():
            self.defined_macros.add(macro_name)
            self.macro_values[macro_name] = macro_value

    def _process_conditional_compilation(self, text):
        """Process conditional compilation directives and return only active sections"""
        from compiletools.simple_preprocessor import SimplePreprocessor
        
        # Use our macro state directly for SimplePreprocessor
        preprocessor = SimplePreprocessor(self.macro_values, verbose=self._args.verbose)
        
        # Process the text with full preprocessor functionality
        processed_text = preprocessor.process(text)
        
        # Update our internal state from preprocessor results
        self.defined_macros.clear()
        self.macro_values.clear()
        for name, value in preprocessor.macros.items():
            self.defined_macros.add(name)
            self.macro_values[name] = value
        
        return processed_text

    def readfile(self, filename):
        """Read the first chunk of the file and all the headers it includes"""
        # Reset defined macros for each new parse
        self.defined_macros = set()
        self.macro_values = {}
        
        # Add macros from command-line CPPFLAGS and CXXFLAGS (e.g., from --append-CPPFLAGS/--append-CXXFLAGS)
        self._add_macros_from_command_line_flags()
        
        # Get compiler, platform, and architecture macros dynamically
        compiler = getattr(self._args, 'CXX', 'g++')
        macros = compiletools.compiler_macros.get_compiler_macros(compiler, self._args.verbose)
        for macro_name, macro_value in macros.items():
            self.defined_macros.add(macro_name)
            self.macro_values[macro_name] = macro_value
        
        headers = self._headerdeps.process(filename)
        
        # Process files iteratively until no new macros are discovered
        # This handles cases where macros defined in one file affect conditional
        # compilation in other files
        previous_macros = set()
        max_iterations = 5  # Prevent infinite loops
        iteration = 0
        
        while previous_macros != self.defined_macros and iteration < max_iterations:
            previous_macros = self.defined_macros.copy()
            iteration += 1
            
            if self._args.verbose >= 9:
                print(f"DirectMagicFlags::readfile iteration {iteration}, known macros: {self.defined_macros}")
            
            text = ""
            # Process files in dependency order
            # Combine headers with filename, handling both list and set types
            all_files = list(headers) + [filename] if filename not in headers else list(headers)
            for fname in all_files:
                if self._args.verbose >= 9:
                    print("DirectMagicFlags::readfile is processing " + fname)
                
                # To match the output of the C Pre Processor we insert
                # the filename before the text
                file_header = '# 1 "' + compiletools.wrappedos.realpath(fname) + '"\n'
                
                # Read file content using FileAnalyzer respecting max_file_read_size configuration
                max_read_size = getattr(self._args, 'max_file_read_size', 0)
                
                # Use FileAnalyzer for efficient file reading with shared cache
                # Note: create_file_analyzer() handles StringZilla/Legacy fallback internally
                analyzer = create_file_analyzer(fname, max_read_size, self._args.verbose, cache=self.file_analyzer_cache)
                analysis_result = analyzer.analyze()
                file_content = analysis_result.text
                
                # Process conditional compilation for this file
                processed_content = self._process_conditional_compilation(file_content)
                
                text += file_header + processed_content

        return text

    def parse(self, filename):
        return self._parse(filename)

    @staticmethod
    def clear_cache():
        pass


class CppMagicFlags(MagicFlagsBase):
    def __init__(self, args, headerdeps):
        MagicFlagsBase.__init__(self, args, headerdeps)
        self.preprocessor = compiletools.preprocessor.PreProcessor(args)

    def readfile(self, filename):
        """Preprocess the given filename but leave comments"""
        extraargs = "-C -E"
        return self.preprocessor.process(
            realpath=filename, extraargs=extraargs, redirect_stderr_to_stdout=True
        )

    def parse(self, filename):
        return self._parse(filename)

    @staticmethod
    def clear_cache():
        pass


class NullStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        print("{}: {}".format(self.adjust(realpath), str(magicflags)))


class PrettyStyle(compiletools.git_utils.NameAdjuster):
    def __init__(self, args):
        compiletools.git_utils.NameAdjuster.__init__(self, args)

    def __call__(self, realpath, magicflags):
        sys.stdout.write("\n{}".format(self.adjust(realpath)))
        try:
            for key in magicflags:
                sys.stdout.write("\n\t{}:".format(key))
                for flag in magicflags[key]:
                    sys.stdout.write(" {}".format(flag))
        except TypeError:
            sys.stdout.write("\n\tNone")


def main(argv=None):
    cap = compiletools.apptools.create_parser(
        "Parse a file and show the magicflags it exports", argv=argv
    )
    compiletools.headerdeps.add_arguments(cap)
    add_arguments(cap)
    cap.add("filename", help='File/s to extract magicflags from"', nargs="+")

    # Figure out what style classes are available and add them to the command
    # line options
    styles = [st[:-5].lower() for st in dict(globals()) if st.endswith("Style")]
    cap.add("--style", choices=styles, default="pretty", help="Output formatting style")

    args = compiletools.apptools.parseargs(cap, argv)
    headerdeps = compiletools.headerdeps.create(args)
    magicparser = create(args, headerdeps)

    styleclass = globals()[args.style.title() + "Style"]
    styleobject = styleclass(args)

    for fname in args.filename:
        realpath = compiletools.wrappedos.realpath(fname)
        styleobject(realpath, magicparser.parse(realpath))

    print()
    return 0
