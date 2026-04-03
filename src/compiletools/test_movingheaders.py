import os
import shutil

import compiletools.cake
import compiletools.test_base
import compiletools.testhelper as uth
import compiletools.utils


# Although this is virtually identical to the test_cake.py, we can't merge the tests due to memoized results.
class TestMovingHeaders(compiletools.test_base.BaseCompileToolsTestCase):
    @uth.requires_functional_compiler
    def test_moving_headers(self):
        # The concept of this test is to check that ct-cake copes with header files being changed directory

        with uth.TempDirContextWithChange() as tmpdir:
            # Setup
            os.mkdir(os.path.join(tmpdir, "subdir"))

            # Copy the movingheaders test files to the temp directory and compile using cake
            relativepaths = ["movingheaders/main.cpp", "movingheaders/someheader.hpp"]
            realpaths = [self._get_sample_path(filename) for filename in relativepaths]
            for ff in realpaths:
                shutil.copy2(ff, tmpdir)

            temp_config_name = uth.create_temp_config(tmpdir)
            argv = [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--quiet",
                "--auto",
                "--include=subdir",
                "--config=" + temp_config_name,
            ]
            with uth.ParserContext():
                compiletools.cake.main(argv)

            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

            # Now move the header file to "subdir"  since it is already included in the path, all should be well
            old_header = os.path.join(tmpdir, "someheader.hpp")
            new_header = os.path.join(tmpdir, "subdir/someheader.hpp")
            os.rename(old_header, new_header)

            shutil.rmtree(os.path.join(tmpdir, "bin"), ignore_errors=True)

            # In real usage, each ct-cake run starts with a fresh BuildContext,
            # so all caches (hash registry, file analyzer, preprocessing) are clean.
            # cake.main() creates its own BuildContext, so no manual clearing needed.
            from compiletools.headerdeps import HeaderDepsBase
            from compiletools.magicflags import MagicFlagsBase

            HeaderDepsBase.clear_cache()
            MagicFlagsBase.clear_cache()

            with uth.ParserContext():
                compiletools.cake.main(argv)

            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)
