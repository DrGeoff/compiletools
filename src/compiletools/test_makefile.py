import os
import subprocess

import compiletools.utils
import compiletools.makefile
import compiletools.testhelper as uth

class TestMakefile:
    def setup_method(self):
        uth.reset()

    def _create_makefile_and_make(self, tempdir):
        origdir = uth.ctdir()
        print("origdir=" + origdir)
        print(tempdir)
        samplesdir = uth.samplesdir()
        print("samplesdir=" + samplesdir)
        
        with uth.DirectoryContext(tempdir):
            with uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
                relativepaths = [
                    "numbers/test_direct_include.cpp",
                    "factory/test_factory.cpp",
                    "simple/helloworld_c.c",
                    "simple/helloworld_cpp.cpp",
                    "dottypaths/dottypaths.cpp",
                ]
                realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                with uth.ParserContext():  # Clear any existing parsers before calling main()
                    compiletools.makefile.main(["--config=" + temp_config_name] + realpaths)

                filelist = os.listdir(".")
                makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
                cmd = ["make", "-f"] + makefilename
                subprocess.check_output(cmd, universal_newlines=True)

                # Check that an executable got built for each cpp
                actual_exes = set()
                for root, dirs, files in os.walk(tempdir):
                    for ff in files:
                        if compiletools.utils.is_executable(os.path.join(root, ff)):
                            actual_exes.add(ff)
                            print(root + " " + ff)

                expected_exes = {
                    os.path.splitext(os.path.split(filename)[1])[0]
                    for filename in relativepaths
                }
                assert expected_exes == actual_exes

    @uth.requires_functional_compiler
    def test_makefile(self):
        with uth.TempDirContextNoChange() as tempdir1:
            self._create_makefile_and_make(tempdir1)

    @uth.requires_functional_compiler
    def test_static_library(self):
        _test_library("--static")

    @uth.requires_functional_compiler
    def test_dynamic_library(self):
        _test_library("--dynamic")

    @uth.requires_functional_compiler
    def test_shared_objects_propagates_compiler_errors(self):
        """Test that compiler errors fail the build when using --shared-objects.

        Regression test for bug where set -e was missing from locking recipes,
        causing compiler failures to be silently ignored.
        """
        with uth.TempDirContextWithChange() as tempdir:
            # Create a source file with intentional syntax error
            bad_source = os.path.join(tempdir, "test_syntax_error.cpp")
            with open(bad_source, "w") as f:
                f.write("""
// ct-exemarker
int main() {
    this_is_a_syntax_error;  // Intentional error
    return 0;
}
""")

            with uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
                with uth.ParserContext():
                    # Generate Makefile with --shared-objects enabled
                    compiletools.makefile.main([
                        "--config=" + temp_config_name,
                        bad_source,
                        "--shared-objects"
                    ])

                # Find generated Makefile
                filelist = os.listdir(".")
                makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
                assert makefilename, "Makefile should have been generated"

                # Verify Makefile uses ct-lock-helper for error propagation
                with open(makefilename[0], "r") as f:
                    makefile_content = f.read()
                    assert "ct-lock-helper" in makefile_content, \
                        "Makefile should use ct-lock-helper (which has set -euo pipefail)"

                # Attempt to build - this MUST fail
                cmd = ["make", "-f"] + makefilename
                result = subprocess.run(cmd, capture_output=True, text=True)

                # Verify build failed (non-zero exit code)
                assert result.returncode != 0, \
                    "Build should fail with non-zero exit code when compiler errors occur"

                # Verify error message is visible
                combined_output = result.stdout + result.stderr
                assert "error" in combined_output.lower(), \
                    "Compiler error message should be visible in output"

    def teardown_method(self):
        uth.reset()


def _test_library(static_dynamic):
    """ Manually specify what files to turn into the static (or dynamic)
        library and test linkage
    """
    samplesdir = uth.samplesdir()
    
    with uth.TempDirContextWithChange() as tempdir:
        with uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
            exerelativepath = "numbers/test_library.cpp"
            librelativepaths = [
                "numbers/get_numbers.cpp",
                "numbers/get_int.cpp",
                "numbers/get_double.cpp",
            ]
            exerealpath = os.path.join(samplesdir, exerelativepath)
            librealpaths = [os.path.join(samplesdir, filename) for filename in librelativepaths]
            argv = ["--config=" + temp_config_name, exerealpath, static_dynamic] + librealpaths
            compiletools.makefile.main(argv)

            # Figure out the name of the makefile and run make
            filelist = os.listdir(".")
            makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
            cmd = ["make", "-f"] + makefilename
            subprocess.check_output(cmd, universal_newlines=True)


