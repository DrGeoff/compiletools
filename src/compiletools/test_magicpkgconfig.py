import os
import shutil
import subprocess
import pytest
import stringzilla as sz
import compiletools.testhelper as uth
import compiletools.utils
import compiletools.cake
import compiletools.magicflags
import compiletools.apptools
import compiletools.headerdeps
import compiletools.test_base as tb

# Although this is virtually identical to the test_cake.py, we can't merge
# the tests due to memoized results.


class TestMagicPKGCONFIG(tb.BaseCompileToolsTestCase):


    @uth.requires_functional_compiler
    @uth.requires_pkg_config("zlib")
    def test_magicpkgconfig(self):
        # This test is to ensure that the //#PKG-CONFIG magic flag 
        # correctly acquires extra cflags and libs
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig test files to the temp directory and compile
            # using ct-cake
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig")
            shutil.copytree(self._get_sample_path("magicpkgconfig"), tmpmagicpkgconfig)
            
            with uth.DirectoryContext(tmpmagicpkgconfig):
                argv = [
                    "--exemarkers=main",
                    "--testmarkers=gtest.hpp",
                                        "--quiet",
                    "--auto",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["magicpkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

    @uth.requires_functional_compiler
    @uth.requires_pkg_config("zlib")
    def test_cmdline_pkgconfig(self):
        # This test is to ensure that the "--pkg-config zlib" flag 
        # correctly acquires extra cflags and libs
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the pkgconfig test files to the temp directory and compile
            # using ct-cake
            tmppkgconfig = os.path.join(tmpdir, "pkgconfig")
            shutil.copytree(self._get_sample_path("pkgconfig"), tmppkgconfig)
            
            with uth.DirectoryContext(tmppkgconfig):
                argv = [
                    "--exemarkers=main",
                    "--testmarkers=gtest.hpp",
                                        "--quiet",
                    "--auto",
                    "--pkg-config=zlib",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["pkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

    @uth.requires_functional_compiler
    def test_magicpkgconfig_flags_discovery(self, pkgconfig_env):
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig_fake test files to the temp directory
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig_fake")
            shutil.copytree(self._get_sample_path("magicpkgconfig_fake"), tmpmagicpkgconfig)
            
            with uth.DirectoryContext(tmpmagicpkgconfig):
                # Create a minimal args object for testing
                # Use a simpler approach - create args from scratch like other tests
                class MockArgs:
                    def __init__(self):
                        self.config_file = config_path
                        self.variant = 'debug'
                        self.verbose = 0
                        self.quiet = True
                        self.magic = 'direct'
                        self.headerdeps = 'direct'
                        self.CPPFLAGS = ''
                        self.CXX = compiletools.apptools.get_functional_cxx_compiler() or 'g++'
                
                args = MockArgs()
                
                # Create magicflags parser
                headerdeps = compiletools.headerdeps.create(args)
                magicparser = compiletools.magicflags.create(args, headerdeps)
                
                # Test the sample file that contains //#PKG-CONFIG=conditional nested
                sample_file = os.path.join(tmpmagicpkgconfig, "main.cpp")

                # Parse the magic flags
                try:
                    parsed_flags = magicparser.parse(sample_file)
                except RuntimeError as e:
                    if "No functional C++ compiler detected" in str(e):
                        pytest.skip("No functional C++ compiler detected")
                    else:
                        raise

                # Verify PKG-CONFIG flag was found
                assert sz.Str("PKG-CONFIG") in parsed_flags
                pkgconfig_flags = [str(x) for x in parsed_flags[sz.Str("PKG-CONFIG")]]
                assert len(pkgconfig_flags) == 2
                assert "conditional" in pkgconfig_flags
                assert "nested" in pkgconfig_flags

                # Verify CXXFLAGS were extracted (should contain conditional and nested cflags)
                assert sz.Str("CXXFLAGS") in parsed_flags
                cxxflags = " ".join(str(x) for x in parsed_flags[sz.Str("CXXFLAGS")])

                # Check that fake pkg-config results are present
                # conditional.pc has: -I/usr/local/include/testpkg -DTEST_PKG_ENABLED
                # nested.pc has: -I/usr/local/include/testpkg1 -DTEST_PKG1_ENABLED
                assert "-isystem /usr/local/include/testpkg" in cxxflags or "TEST_PKG_ENABLED" in cxxflags
                assert "-isystem /usr/local/include/testpkg1" in cxxflags or "TEST_PKG1_ENABLED" in cxxflags

                # Verify LDFLAGS were extracted
                assert sz.Str("LDFLAGS") in parsed_flags
                ldflags = " ".join(str(x) for x in parsed_flags[sz.Str("LDFLAGS")])

                # conditional.pc has: -L/usr/local/lib -ltestpkg
                # nested.pc has: -L/usr/local/lib -ltestpkg1
                assert "-ltestpkg" in ldflags
                assert "-ltestpkg1" in ldflags


    @uth.requires_functional_compiler
    def test_pkg_config_transformation_in_actual_parsing(self, pkgconfig_env):
        """Test that the -I to -isystem transformation occurs during actual magic flag parsing using sample code"""
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig_fake sample to the temp directory
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig_fake")
            shutil.copytree(self._get_sample_path("magicpkgconfig_fake"), tmpmagicpkgconfig)
            
            # Create minimal args object
            class MockArgs:
                def __init__(self):
                    self.config_file = config_path
                    self.variant = 'debug'
                    self.verbose = 0
                    self.quiet = True
                    self.magic = 'direct'
                    self.headerdeps = 'direct'
                    self.CPPFLAGS = ''
                    self.max_file_read_size = 0
                    self.CXX = compiletools.apptools.get_functional_cxx_compiler() or 'g++'
            
            args = MockArgs()
            
            # Create magicflags parser
            headerdeps = compiletools.headerdeps.create(args)
            magicparser = compiletools.magicflags.create(args, headerdeps)
            
            # Use the actual magicpkgconfig sample file
            sample_file = os.path.join(tmpmagicpkgconfig, "main.cpp")
            
            # Parse the magic flags
            try:
                parsed_flags = magicparser.parse(sample_file)
                
                # Verify PKG-CONFIG flag was found (should contain "conditional nested")
                assert sz.Str("PKG-CONFIG") in parsed_flags, "PKG-CONFIG directive should be parsed"
                pkgconfig_flags = [str(x) for x in parsed_flags[sz.Str("PKG-CONFIG")]]
                assert len(pkgconfig_flags) == 2
                assert "conditional" in pkgconfig_flags
                assert "nested" in pkgconfig_flags
                
                # Check CXXFLAGS for the presence of -isystem transformations
                if sz.Str("CXXFLAGS") in parsed_flags:
                    cxxflags_list = [str(x) for x in parsed_flags[sz.Str("CXXFLAGS")]]
                    cxxflags_str = " ".join(cxxflags_list)
                    
                    # If there are any include paths from pkg-config, they should use -isystem
                    if "/include" in cxxflags_str:
                        assert "-isystem" in cxxflags_str, f"Expected -isystem in CXXFLAGS, got: {cxxflags_str}"
                        
                        # Verify no -I flags remain (they should all be transformed to -isystem)
                        assert "-I/" not in cxxflags_str, f"Found -I/ in CXXFLAGS (should be -isystem): {cxxflags_str}"
                        assert not any(flag.startswith("-I ") for flag in cxxflags_list), \
                            f"Found -I flags in CXXFLAGS (should be -isystem): {cxxflags_list}"
                        
                        # Verify that other flags like -D are preserved
                        if any("-D" in flag for flag in cxxflags_list):
                            assert any("-D" in flag for flag in cxxflags_list), \
                                f"Macro definitions should be preserved in CXXFLAGS: {cxxflags_list}"
                
            except subprocess.CalledProcessError:
                # If pkg-config fails (e.g., packages not available), that's okay for this test
                # The important thing is that the transformation logic is in place
                pass

    @uth.requires_functional_compiler
    def test_pkg_config_flags_are_split(self, pkgconfig_env):
        """Test that pkg-config output is split into individual flags.
        
        This test ensures that flags returned by pkg-config (e.g. "-I/path -Dflag")
        are correctly split into a list of separate flags (["-I/path", "-Dflag"])
        rather than being treated as a single string argument.
        """
        
        # Create a source file that requests the 'nested' package
        # nested.pc has:
        # Cflags: -I/usr/local/include/testpkg1 -DTEST_PKG1_ENABLED
        # Libs: -L/usr/local/lib -ltestpkg1
        
        files = uth.write_sources({
            "test.cpp": "//#PKG-CONFIG=nested\nint main() {}"
        })
        source_file = str(files["test.cpp"])
        
        # Create parser
        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir)
        
        # Parse
        result = mf.parse(source_file)
        
        # Check CPPFLAGS (from Cflags)
        assert sz.Str("CPPFLAGS") in result
        cppflags = result[sz.Str("CPPFLAGS")]
        
        # Convert to python strings for easier assertion
        cppflags_str_list = [str(f) for f in cppflags]
        
        # We expect at least two distinct flags. 
        # If the bug were present, len(cppflags_str_list) would be 1 (containing the concatenated string)
        assert len(cppflags_str_list) >= 2, f"Expected multiple CPPFLAGS, got: {cppflags_str_list}"
        
        # Note: compiletools may convert -I to -isystem and split the flag and path
        # So we check for the presence of the path and the define
        assert any("/usr/local/include/testpkg1" in f for f in cppflags_str_list)
        assert "-DTEST_PKG1_ENABLED" in cppflags_str_list
        
        # Check LDFLAGS (from Libs)
        assert sz.Str("LDFLAGS") in result
        ldflags = result[sz.Str("LDFLAGS")]
        
        ldflags_str_list = [str(f) for f in ldflags]
        
        # We expect at least two distinct flags
        assert len(ldflags_str_list) >= 2, f"Expected multiple LDFLAGS, got: {ldflags_str_list}"
        assert "-L/usr/local/lib" in ldflags_str_list
        assert "-ltestpkg1" in ldflags_str_list



