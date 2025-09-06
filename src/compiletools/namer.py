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

    @functools.lru_cache(maxsize=None)
    def object_name(self, sourcefilename, macro_hash=None):
        """ Return the name (not the path) of the object file
            for the given source.
            
            Args:
                sourcefilename: Path to source file
                macro_hash: Pre-computed hash of macro state (for shared objects)
        """
        directory, name = os.path.split(sourcefilename)
        basename = os.path.splitext(name)[0]
        base_name = "".join([directory.replace("/", "@@"), "@@", basename])
        
        # Check if shared objects mode is enabled
        shared_objects = compiletools.configutils.extract_item_from_ct_conf(
            'shared-objects', default='false'
        ).lower() in ('true', '1', 'yes', 'on')
        
        if shared_objects and macro_hash:
            return self._shared_object_name(sourcefilename, base_name, macro_hash)
        else:
            return base_name + ".o"

    @functools.lru_cache(maxsize=None)
    def object_pathname(self, sourcefilename, macro_hash=None):
        return "".join(
            [self.object_dir(sourcefilename), "/", self.object_name(sourcefilename, macro_hash)]
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

    def _shared_object_name(self, sourcefilename, base_name, macro_hash):
        """Generate shared object name with file and macro state hashes."""
        from compiletools.global_hash_registry import get_file_hash
        
        # Get file content hash
        file_hash = get_file_hash(sourcefilename)
        if not file_hash:
            # Fallback for files not in git
            file_hash = "unknown"
        file_hash_short = file_hash[:12]
        
        return f"{base_name}_{file_hash_short}_{macro_hash}.o"

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
