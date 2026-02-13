import os
import pytest
import configargparse
import compiletools.testhelper as uth
import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.utils
import compiletools.configutils
import compiletools.wrappedos
import compiletools.git_utils
import compiletools.compiler_macros


class BaseCompileToolsTestCase:
    """Base test case with common setup/teardown for compiletools tests"""
    
    def _clear_all_caches(self):
        """Clear all LRU and module caches to ensure test isolation"""
        compiletools.wrappedos.clear_cache()
        compiletools.utils.clear_cache()
        compiletools.git_utils.clear_cache()
        compiletools.configutils.clear_cache()
        compiletools.apptools.clear_cache()
        compiletools.compiler_macros.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        # Clear preprocessing cache
        from compiletools.preprocessing_cache import clear_cache
        clear_cache()
        # Note: namer and hunter caches are cleared per-instance, not globally
    
    def setup_method(self):
        self._clear_all_caches()  # Clear before setup
        self._temp_context = uth.TempDirectoryContext(change_dir=True)
        self._tmpdir = self._temp_context.__enter__()
        uth.delete_existing_parsers()
        compiletools.apptools.resetcallbacks()
        
    def teardown_method(self):
        if hasattr(self, '_temp_context'):
            self._temp_context.__exit__(None, None, None)
        uth.delete_existing_parsers()
        compiletools.apptools.resetcallbacks()
        self._clear_all_caches()  # Clear after teardown
        
    def _verify_one_exe_per_main(self, relativepaths, search_dir=None):
        """Common executable verification logic"""
        search_directory = search_dir or self._tmpdir
        actual_exes = set()
        for root, dirs, files in os.walk(search_directory):
            for ff in files:
                if compiletools.utils.is_executable(os.path.join(root, ff)):
                    actual_exes.add(ff)

        expected_exes = {
            os.path.splitext(os.path.split(filename)[1])[0]
            for filename in relativepaths
            if compiletools.utils.is_source(filename)
        }
        assert expected_exes == actual_exes

    def _get_sample_path(self, relative_path):
        """Helper to get full path for sample file"""
        return os.path.join(uth.samplesdir(), relative_path)


def create_magic_parser(extraargs=None, tempdir=None):
    """Factory function for creating magic flag parsers"""
    if not extraargs:
        extraargs = []
    temp_config_name = uth.create_temp_config(tempdir)
    argv = ["--config=" + temp_config_name] + extraargs

    config_files = compiletools.configutils.config_files_from_variant(
        argv=argv, exedir=uth.cakedir()
    )

    # Check if parser already exists and use it, otherwise create new one
    try:
        cap = configargparse.getArgumentParser(
            description="TestMagicFlagsModule",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
    except ValueError:
        # Parser already exists, get it without parameters
        cap = configargparse.getArgumentParser()

    compiletools.apptools.add_common_arguments(cap)
    compiletools.headerdeps.add_arguments(cap)
    compiletools.magicflags.add_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)
    headerdeps = compiletools.headerdeps.create(args)
    return compiletools.magicflags.create(args, headerdeps)

@uth.requires_functional_compiler
def compare_direct_cpp_magic(test_case, relativepath, tempdir=None, expected_values=None,
                             parsers=None):
    """Utility to test that DirectMagicFlags and CppMagicFlags produce identical results

    Args:
        test_case: Test case instance for assertions
        relativepath: Path to test file relative to samples directory
        tempdir: Optional temporary directory for test execution
        expected_values: Optional dict of expected values to verify correctness
                        Format: {"LDFLAGS": ["-lm"], "SOURCE": ["path/to/file.cpp"]}
        parsers: Optional tuple of (magicparser_direct, magicparser_cpp) to reuse
    """

    with uth.TempDirContext() as _:
        if tempdir is not None:
            # If specific tempdir provided, copy current working dir content there
            os.chdir(tempdir)

        samplesdir = uth.samplesdir()
        realpath = os.path.join(samplesdir, relativepath)

        # Use provided parsers or create new ones
        if parsers:
            magicparser_direct, magicparser_cpp = parsers
            # Clear parser caches before reuse
            magicparser_direct.clear_cache()
            magicparser_cpp.clear_cache()
            try:
                result_direct = magicparser_direct.parse(realpath)
                result_cpp = magicparser_cpp.parse(realpath)
            except RuntimeError as e:
                if "No functional C++ compiler detected" in str(e):
                    pytest.skip("No functional C++ compiler detected")
                else:
                    raise
        else:
            # Test direct parser with isolated context
            with uth.ParserContext():
                magicparser_direct = create_magic_parser(["--magic", "direct"], tempdir=os.getcwd())
                try:
                    result_direct = magicparser_direct.parse(realpath)
                except RuntimeError as e:
                    if "No functional C++ compiler detected" in str(e):
                        pytest.skip("No functional C++ compiler detected")
                    else:
                        raise

            # Test cpp parser with fresh isolated context
            with uth.ParserContext():
                magicparser_cpp = create_magic_parser(["--magic", "cpp"], tempdir=os.getcwd())
                try:
                    result_cpp = magicparser_cpp.parse(realpath)
                except RuntimeError as e:
                    if "No functional C++ compiler detected" in str(e):
                        pytest.skip("No functional C++ compiler detected")
                    else:
                        raise

        # Results should be identical
        assert result_direct == result_cpp, \
                           f"DirectMagicFlags and CppMagicFlags gave different results for {relativepath}"

        # If expected values provided, verify correctness
        if expected_values:
            import stringzilla as sz
            for key, expected_list in expected_values.items():
                sz_key = sz.Str(key)
                assert sz_key in result_direct, f"Expected key '{key}' not found in result for {relativepath}"
                actual_list = [str(x) for x in result_direct[sz_key]]
                assert actual_list == expected_list, \
                    f"For {relativepath}, expected {key}={expected_list}, got {actual_list}"


def compare_direct_cpp_headers(test_case, filename, extraargs=None):
    """Utility to test that DirectHeaderDeps and CppHeaderDeps produce identical results"""
    if extraargs is None:
        extraargs = []
    realpath = compiletools.wrappedos.realpath(filename)

    with uth.TempConfigContext() as temp_config_name:
        argv = ["--config=" + temp_config_name] + extraargs

        cap = configargparse.getArgumentParser()
        compiletools.headerdeps.add_arguments(cap)
        argvdirect = argv + ["--headerdeps=direct"]
        argsdirect = compiletools.apptools.parseargs(cap, argvdirect)

        argvcpp = argv + ["--headerdeps", "cpp"]
        argscpp = compiletools.apptools.parseargs(cap, argvcpp)

        hdirect = compiletools.headerdeps.create(argsdirect)
        hcpp = compiletools.headerdeps.create(argscpp)
        hdirectresult = hdirect.process(realpath, frozenset())
        hcppresult = hcpp.process(realpath, frozenset())
        assert set(hdirectresult) == set(hcppresult)
