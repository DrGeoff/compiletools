import os
import shutil

import compiletools.cake
import compiletools.testhelper as uth
import compiletools.utils

# Although this is virtually identical to the test_cake.py, we can't merge
# the tests due to memoized results.


class TestSerialiseTests:
    @uth.requires_functional_compiler
    def test_serialisetests(self):
        # This test is to ensure that --serialise-tests actually does so

        with uth.TempDirContextWithChange() as tmpdir:
            # Copy the serialise_tests test files to the temp directory and compile
            # using ct-cake
            tmpserialisetests = os.path.join(tmpdir, "serialise_tests")
            shutil.copytree(os.path.join(uth.samplesdir(), "serialise_tests"), tmpserialisetests)

            with uth.DirectoryContext(tmpserialisetests):
                temp_config_name = uth.create_temp_config(tmpserialisetests)
                argv = [
                    "--exemarkers=main",
                    "--testmarkers=gtest.hpp",
                    "--quiet",
                    "--auto",
                    "--serialise-tests",
                    "--config=" + temp_config_name,
                ]

                with uth.ParserContext():
                    compiletools.cake.main(argv)

                # Verify test executables were built
                built_exes = set()
                for root, _dirs, files in os.walk(tmpserialisetests):
                    for f in files:
                        if compiletools.utils.is_executable(os.path.join(root, f)):
                            built_exes.add(f)

                assert len(built_exes) >= 2, (
                    f"Expected at least 2 test executables, got {built_exes}"
                )

    def teardown_method(self):
        uth.reset()
