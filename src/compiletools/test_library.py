import os
import shutil
import compiletools.testhelper as uth
import compiletools.cake
import compiletools.utils


class TestLibrary:
    def setup_method(self):
        pass

    @uth.requires_functional_compiler
    def test_build_and_link_static_library(self):
        with uth.TempDirContextWithChange() as tmpdir:
            # Mimic the build.sh and create the library in a 'mylib' subdirectory
            # Copy the sample source files into the test build location
            mylibdir = os.path.join(tmpdir, "mylib")
            shutil.copytree(os.path.join(uth.samplesdir(), "library/mylib"), mylibdir)

            # Add unique comments to copied files to avoid hash collision with originals
            for root, dirs, files in os.walk(mylibdir):
                for filename in files:
                    if filename.endswith(('.cpp', '.hpp')):
                        filepath = os.path.join(root, filename)
                        with open(filepath, 'r') as f:
                            content = f.read()
                        with open(filepath, 'w') as f:
                            f.write(f'// Test copy: {filename}\n{content}')

            # Build the library
            temp_config_name = uth.create_temp_config(tmpdir)
            uth.create_temp_ct_conf(tmpdir, defaultvariant=temp_config_name[:-5])
            argv = [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--config=" + temp_config_name,
                "--static",
                os.path.join(tmpdir, "mylib/get_numbers.cpp"),
            ]

            with uth.DirectoryContext(mylibdir):
                with uth.ParserContext():
                    compiletools.cake.main(argv)

            # Copy the main that will link to the library into the test build location
            relativepaths = ["library/main.cpp"]
            realpaths = [
                os.path.join(uth.samplesdir(), filename) for filename in relativepaths
            ]
            for ff in realpaths:
                shutil.copy2(ff, tmpdir)

            # Build the exe, linking against the library
            argv = ["--config=" + temp_config_name] + realpaths
            with uth.ParserContext():
                compiletools.cake.main(argv)

            # Check that an executable got built for each cpp
            actual_exes = set()
            for root, dirs, files in os.walk(tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            expected_exes = {
                os.path.splitext(os.path.split(filename)[1])[0]
                for filename in relativepaths
            }
            assert expected_exes == actual_exes

    def teardown_method(self):
        uth.reset()


