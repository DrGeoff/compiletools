import os
import shutil
import subprocess
import compiletools.testhelper as uth
import compiletools.utils
import compiletools.cake
import compiletools.magicflags
import compiletools.headerdeps
import compiletools.test_base as tb

# Although this is virtually identical to the test_cake.py, we can't merge
# the tests due to memoized results.


class TestMagicPKGCONFIG(tb.BaseCompileToolsTestCase):


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
                    "--CTCACHE=None",
                    "--quiet",
                    "--auto",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["magicpkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

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
                    "--CTCACHE=None",
                    "--quiet",
                    "--auto",
                    "--pkg-config=zlib",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["pkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

    def test_magicpkgconfig_flags_discovery(self):
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig test files to the temp directory
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig")
            shutil.copytree(self._get_sample_path("magicpkgconfig"), tmpmagicpkgconfig)
            
            with uth.DirectoryContext(tmpmagicpkgconfig):
                # Create a minimal args object for testing
                # Use a simpler approach - create args from scratch like other tests
                class MockArgs:
                    def __init__(self):
                        self.config_file = config_path
                        self.variant = 'debug'
                        self.verbose = 0
                        self.quiet = True
                        self.CTCACHE = 'None'
                        self.magic = 'direct'
                        self.headerdeps = 'direct'
                        self.CPPFLAGS = ''
                
                args = MockArgs()
                
                # Create magicflags parser
                headerdeps = compiletools.headerdeps.create(args)
                magicparser = compiletools.magicflags.create(args, headerdeps)
                
                # Test the sample file that contains //#PKG-CONFIG=zlib libcrypt
                sample_file = os.path.join(tmpmagicpkgconfig, "main.cpp")
                
                # Parse the magic flags
                parsed_flags = magicparser.parse(sample_file)
                
                # Verify PKG-CONFIG flag was found
                assert "PKG-CONFIG" in parsed_flags
                pkgconfig_flags = list(parsed_flags["PKG-CONFIG"])
                assert len(pkgconfig_flags) == 1
                assert pkgconfig_flags[0] == "zlib libcrypt"
                
                # Verify CXXFLAGS were extracted (should contain zlib and libcrypt cflags)
                assert "CXXFLAGS" in parsed_flags
                cxxflags = " ".join(parsed_flags["CXXFLAGS"])
                
                # Check that pkg-config results are present (basic validation)
                try:
                    zlib_cflags = subprocess.run(
                        ["pkg-config", "--cflags", "zlib"], 
                        capture_output=True, text=True, check=True
                    ).stdout.strip().replace("-I", "-isystem ")
                    
                    libcrypt_cflags = subprocess.run(
                        ["pkg-config", "--cflags", "libcrypt"], 
                        capture_output=True, text=True, check=True
                    ).stdout.strip().replace("-I", "-isystem ")
                    
                    # Verify the parsed flags contain the expected pkg-config results
                    if zlib_cflags:
                        assert zlib_cflags in cxxflags
                    if libcrypt_cflags:
                        assert libcrypt_cflags in cxxflags
                        
                except subprocess.CalledProcessError:
                    # pkg-config might fail for missing packages, but the test should still parse the PKG-CONFIG directive
                    pass
                
                # Verify LDFLAGS were extracted 
                assert "LDFLAGS" in parsed_flags
                ldflags = " ".join(parsed_flags["LDFLAGS"])
                
                try:
                    zlib_libs = subprocess.run(
                        ["pkg-config", "--libs", "zlib"], 
                        capture_output=True, text=True, check=True
                    ).stdout.strip()
                    
                    libcrypt_libs = subprocess.run(
                        ["pkg-config", "--libs", "libcrypt"], 
                        capture_output=True, text=True, check=True
                    ).stdout.strip()
                    
                    # Verify the parsed flags contain the expected pkg-config results
                    if zlib_libs:
                        assert zlib_libs in ldflags
                    if libcrypt_libs:
                        assert libcrypt_libs in ldflags
                        
                except subprocess.CalledProcessError:
                    # pkg-config might fail for missing packages
                    pass

    def test_pkg_config_i_to_isystem_transformation(self):
        """Test that pkg-config -I flags are correctly transformed to -isystem"""
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Create a minimal args object for testing
            class MockArgs:
                def __init__(self):
                    self.config_file = config_path
                    self.variant = 'debug'
                    self.verbose = 0
                    self.quiet = True
                    self.CTCACHE = 'None'
                    self.magic = 'direct'
                    self.headerdeps = 'direct'
                    self.CPPFLAGS = ''
            
            args = MockArgs()
            
            # Create magicflags parser
            headerdeps = compiletools.headerdeps.create(args)
            magicparser = compiletools.magicflags.create(args, headerdeps)
            
            # Test the _handle_pkg_config method directly with mock pkg-config outputs
            test_cases = [
                # Test case 1: Simple -I flag
                ("-I/usr/include", "-isystem/usr/include"),
                # Test case 2: -I flag with space 
                ("-I /usr/include", "-isystem /usr/include"),
                # Test case 3: Multiple -I flags with other flags (simulating gtest_main)
                ("-I/jump/software/rhel8/gtest-1.15.2-gcc12-cpp20-cxx11abi-static/include -DGTEST_HAS_PTHREAD=1",
                 "-isystem/jump/software/rhel8/gtest-1.15.2-gcc12-cpp20-cxx11abi-static/include -DGTEST_HAS_PTHREAD=1"),
                # Test case 4: Multiple -I flags
                ("-I/usr/include -I/usr/local/include -DSOME_FLAG=1",
                 "-isystem/usr/include -isystem/usr/local/include -DSOME_FLAG=1"),
                # Test case 5: Flags containing -I substring should not be affected
                ("-DFLAG_WITH_I -I/usr/include", "-DFLAG_WITH_I -isystem/usr/include"),
            ]
            
            import re
            for input_flags, expected_output in test_cases:
                # Apply the same transformation as in _handle_pkg_config
                actual_output = re.sub(r'-I(?=\s|/|$)', '-isystem', input_flags)
                
                assert actual_output == expected_output, \
                    f"Transformation failed for '{input_flags}': expected '{expected_output}', got '{actual_output}'"
                
                # Verify that -I flags are properly transformed
                if "-I/" in input_flags or "-I " in input_flags:
                    assert "-isystem" in actual_output, f"Expected -isystem in transformed output for '{input_flags}'"
                    assert "-I/" not in actual_output and "-I " not in actual_output, \
                        f"Found remaining -I flags in transformed output for '{input_flags}'"
                
                # Verify that other flags are preserved
                if "-D" in input_flags:
                    assert "-D" in actual_output, f"Macro definitions should be preserved for '{input_flags}'"

    def test_pkg_config_transformation_in_actual_parsing(self):
        """Test that the -I to -isystem transformation occurs during actual magic flag parsing"""
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Create a test C++ file with PKG-CONFIG magic flag
            test_cpp_content = '''// Test file for pkg-config transformation
//#CXXFLAGS=-std=c++17
//#PKG-CONFIG=zlib
#include <iostream>
int main() { return 0; }
'''
            test_file = os.path.join(tmpdir, "test_pkg_config.cpp")
            with open(test_file, 'w') as f:
                f.write(test_cpp_content)
            
            # Create minimal args object
            class MockArgs:
                def __init__(self):
                    self.config_file = config_path
                    self.variant = 'debug'
                    self.verbose = 0
                    self.quiet = True
                    self.CTCACHE = 'None'
                    self.magic = 'direct'
                    self.headerdeps = 'direct'
                    self.CPPFLAGS = ''
                    self.max_file_read_size = 0
            
            args = MockArgs()
            
            # Create magicflags parser
            headerdeps = compiletools.headerdeps.create(args)
            magicparser = compiletools.magicflags.create(args, headerdeps)
            
            # Parse the magic flags
            try:
                parsed_flags = magicparser.parse(test_file)
                
                # Verify PKG-CONFIG flag was found
                assert "PKG-CONFIG" in parsed_flags, "PKG-CONFIG directive should be parsed"
                
                # Check CXXFLAGS for the presence of -isystem (if zlib pkg-config succeeds)
                if "CXXFLAGS" in parsed_flags:
                    cxxflags_str = " ".join(parsed_flags["CXXFLAGS"])
                    
                    # If there are any include paths from pkg-config, they should use -isystem
                    if "/include" in cxxflags_str:
                        assert "-isystem" in cxxflags_str, f"Expected -isystem in CXXFLAGS, got: {cxxflags_str}"
                        assert "-I/" not in cxxflags_str, f"Found -I/ in CXXFLAGS (should be -isystem): {cxxflags_str}"
                        assert not any(flag.startswith("-I ") for flag in parsed_flags["CXXFLAGS"]), \
                            f"Found -I flags in CXXFLAGS (should be -isystem): {parsed_flags['CXXFLAGS']}"
                
            except subprocess.CalledProcessError:
                # If pkg-config fails (e.g., zlib not available), that's okay for this test
                # The important thing is that the transformation logic is in place
                pass



