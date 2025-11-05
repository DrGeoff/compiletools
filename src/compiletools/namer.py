import os
import functools
import compiletools.wrappedos
import compiletools.git_utils
import compiletools.utils
import compiletools.apptools
import compiletools.configutils


class Namer(object):

    """ From a source filename, calculate related names
        like executable name, object name, etc.
    """

    def __init__(self, args, argv=None, variant=None, exedir=None):
        self.args = args
        self._project = compiletools.git_utils.Project(args)
        self._cached_macros = None

    @staticmethod
    def add_arguments(cap, argv=None, variant=None):
        compiletools.apptools.add_common_arguments(cap, argv=argv, variant=variant)
        if variant is None:
            variant = "unsupplied"
        compiletools.apptools.add_output_directory_arguments(cap, variant=variant)

    def topbindir(self):
        """
        Return the top-level directory for executable placement.
        
        For relative paths containing subdirectories (variant-style builds),
        return the parent directory to place executables in the top-level.
        For absolute paths, return the full path as specified by the user.
        
        Examples:
            bin/gcc.release → "bin/"
            bin.special/gcc.release → "bin.special/"
            /opt/local/bin → "/opt/local/bin"
        """
        if not os.path.isabs(self.args.bindir) and os.sep in self.args.bindir:
            return self.args.bindir.split(os.sep)[0] + os.sep
        else:
            return self.args.bindir

    def _outputdir(self, defaultdir, sourcefilename=None):
        """ Used by object_dir and executable_dir.
            defaultdir must be either self.args.objdir or self.args.bindir
        """
        if sourcefilename:
            project_pathname = self._project.pathname(sourcefilename)
            relative = os.path.join(defaultdir, compiletools.wrappedos.dirname(project_pathname))
        else:
            relative = defaultdir
        return compiletools.wrappedos.realpath(relative)

    @functools.lru_cache(maxsize=None)
    def object_dir(self, sourcefilename=None):
        """ This function allows for alternative behaviour to be explore.
            Previously we tried replicating the source directory structure
            to keep object files separated.  The mkdir involved slowed 
            down the build process by about 25%.
        """
        return self.args.objdir

    def compute_dep_hash(self, header_list):
        """Compute 14-char hash of header dependencies.

        Uses XOR of header content hashes for order-independent,
        deterministic hash. Leverages global_hash_registry.

        This is a PUBLIC method so callers can compute the hash once
        and pass it to multiple methods (object_name, object_pathname).

        Args:
            header_list: List of header file paths (strings, stringzilla.Str, or Path objects)

        Returns:
            14-character hex string representing dependency set

        Notes:
            - Missing files (e.g., generated headers) are treated as zero hash
            - XOR with 0 is no-op, so missing files don't corrupt the hash
            - When generated file appears, next build will pick up correct hash
        """
        from compiletools.global_hash_registry import get_file_hash

        if not header_list:
            return '0' * 14  # No dependencies

        # Coerce to str (handles str, stringzilla.Str, Path objects)
        # Use str() not os.fspath() - stringzilla.Str is not os.PathLike
        header_paths = [str(h) for h in header_list]

        # Defensive: deduplicate in case caller didn't (should already be unique)
        unique_paths = list(dict.fromkeys(header_paths))  # Preserve order

        if len(unique_paths) != len(header_paths) and self.args.verbose >= 5:
            # Log if duplicates detected (shouldn't happen with proper callers)
            import sys
            print("Warning: Duplicate headers in dep hash computation", file=sys.stderr)

        # XOR all header hashes (order-independent via sorting)
        combined = 0
        for header_path in sorted(unique_paths):
            try:
                file_hash = get_file_hash(header_path)
                # Use first 56 bits (14 hex chars)
                combined ^= int(file_hash[:14], 16)
            except FileNotFoundError:
                # Generated header doesn't exist yet - treat as zero hash (XOR with 0 is identity)
                if self.args.verbose >= 5:
                    import sys
                    print(f"Warning: Header not found (generated?): {header_path}", file=sys.stderr)

        return format(combined, '014x')

    @functools.lru_cache(maxsize=None)
    def object_name(self, sourcefilename, macro_state_hash, dep_hash):
        """Return the name (not the path) of the object file for the given source.

        Naming scheme: {basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o
        - basename: filename without path or extension
        - file_hash_12: 12-char hex from global hash registry (source file)
        - dep_hash_14: 14-char hex XOR of header dependencies (MIDDLE POSITION)
        - macro_state_hash_16: 16-char hex of full macro state (core + variable)

        This naming scheme is content-addressable and safe for shared caching:
        - Different file content → different file_hash
        - Different dependencies → different dep_hash
        - Different macro state → different macro_state_hash
        - Same basename in different dirs → different file_hash

        Args:
            sourcefilename: Path to source file
            macro_state_hash: Required 16-char hex hash of full macro state (core + variable).
                             No default - fail fast if not provided.
            dep_hash: Required 14-char hex hash of dependencies (precomputed via compute_dep_hash)

        Returns:
            Object filename like: file_a1b2c3d4e5f6_1234567890abcd_0123456789abcdef.o
        """
        from compiletools.global_hash_registry import get_file_hash

        # Extract just the basename (no directory path)
        _, name = os.path.split(sourcefilename)
        basename = os.path.splitext(name)[0]

        # Get file content hash (12 chars to match git short hash convention)
        file_hash = get_file_hash(sourcefilename)
        file_hash_short = file_hash[:12]

        # Use precomputed dependency hash (14 chars) - MIDDLE POSITION
        # Passed as parameter (not computed here) to keep lru_cache working

        # Use full 16-char macro state hash
        return f"{basename}_{file_hash_short}_{dep_hash}_{macro_state_hash}.o"

    @functools.lru_cache(maxsize=None)
    def object_pathname(self, sourcefilename, macro_state_hash, dep_hash):
        """Return full path to object file.

        Args:
            sourcefilename: Path to source file
            macro_state_hash: Required 16-char hex hash (no default)
            dep_hash: Required 14-char hex hash of dependencies (precomputed)
        """
        return "".join(
            [
                self.object_dir(sourcefilename),
                os.sep,
                self.object_name(sourcefilename, macro_state_hash, dep_hash),
            ]
        )

    @functools.lru_cache(maxsize=None)
    def executable_dir(self, sourcefilename=None):
        """ Similar to object_dir, this allows for alternative 
            behaviour experimentation.
        """
        return self.args.bindir

    @functools.lru_cache(maxsize=None)
    def executable_name(self, sourcefilename):
        name = os.path.split(sourcefilename)[1]
        return os.path.splitext(name)[0]

    @functools.lru_cache(maxsize=None)
    def executable_pathname(self, sourcefilename):
        return "".join(
            [
                self.executable_dir(sourcefilename),
                "/",
                self.executable_name(sourcefilename),
            ]
        )

    @functools.lru_cache(maxsize=None)
    def staticlibrary_name(self, sourcefilename=None):
        if sourcefilename is None and self.args.static:
            sourcefilename = self.args.static[0]
        name = os.path.split(sourcefilename)[1]
        return "lib" + os.path.splitext(name)[0] + ".a"

    @functools.lru_cache(maxsize=None)
    def staticlibrary_pathname(self, sourcefilename=None):
        """ Put static libraries in the same directory as executables """
        if sourcefilename is None and self.args.static:
            sourcefilename = compiletools.wrappedos.realpath(self.args.static[0])
        return "".join(
            [
                self.executable_dir(sourcefilename),
                "/",
                self.staticlibrary_name(sourcefilename),
            ]
        )

    @functools.lru_cache(maxsize=None)
    def dynamiclibrary_name(self, sourcefilename=None):
        if sourcefilename is None and self.args.dynamic:
            sourcefilename = self.args.dynamic[0]
        name = os.path.split(sourcefilename)[1]
        return "lib" + os.path.splitext(name)[0] + ".so"

    @functools.lru_cache(maxsize=None)
    def dynamiclibrary_pathname(self, sourcefilename=None):
        """ Put dynamic libraries in the same directory as executables """
        if sourcefilename is None and self.args.dynamic:
            sourcefilename = compiletools.wrappedos.realpath(self.args.dynamic[0])
        return "".join(
            [
                self.executable_dir(sourcefilename),
                "/",
                self.dynamiclibrary_name(sourcefilename),
            ]
        )

    def compilation_database_pathname(self):
        """ Return the path for the compilation database, defaulting to git root """
        if hasattr(self.args, 'compilation_database_output') and self.args.compilation_database_output:
            # If user provided a path, use it (could be relative or absolute)
            if os.path.isabs(self.args.compilation_database_output):
                return self.args.compilation_database_output
            else:
                # Relative path - resolve from current directory
                return compiletools.wrappedos.realpath(self.args.compilation_database_output)
        else:
            # Default to git root
            gitroot = compiletools.git_utils.find_git_root()
            return os.path.join(gitroot, "compile_commands.json")

    def all_executable_pathnames(self):
        """ Use the filenames from the command line to determine the 
            executable names.
        """
        if self.args.filename:
            allexes = {
                self.executable_pathname(compiletools.wrappedos.realpath(source))
                for source in self.args.filename
            }
            return list(allexes)
        return []

    def all_test_pathnames(self):
        """ Use the test files from the command line to determine the 
            executable names.
        """
        if self.args.tests:
            alltestsexes = {
                self.executable_pathname(compiletools.wrappedos.realpath(source))
                for source in self.args.tests
            }
            return list(alltestsexes)
        return []

    def clear_cache(self):
        compiletools.wrappedos.clear_cache()
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        self.object_dir.cache_clear()
        self.object_name.cache_clear()
        self.object_pathname.cache_clear()
        self.executable_dir.cache_clear()
        self.executable_name.cache_clear()
        self.executable_pathname.cache_clear()
        self.staticlibrary_name.cache_clear()
        self.staticlibrary_pathname.cache_clear()
        self.dynamiclibrary_name.cache_clear()
        self.dynamiclibrary_pathname.cache_clear()
        self._cached_macros = None
