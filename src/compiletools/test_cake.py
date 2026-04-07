import os
import os.path
import shutil

import compiletools.apptools
import compiletools.cake
import compiletools.namer

# import pdb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.test_base import BaseCompileToolsTestCase


class TestCake(BaseCompileToolsTestCase):
    def setup_method(self):
        super().setup_method()  # Call base class setup to clear caches
        self._config_name = None

    def _create_argv(self):
        assert self._config_name is not None
        return [
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--auto",
            "--config=" + self._config_name,
        ]

    def _call_ct_cake(self, extraargv=None):
        if extraargv is None:
            extraargv = []
        uth.reset()
        compiletools.cake.main(self._create_argv() + extraargv)

    @uth.requires_functional_compiler
    def test_no_git_root(self):
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()

            # Copy a known cpp file to a non-git directory and compile using cake
            relativepaths = ["simple/helloworld_cpp.cpp"]
            realpaths = [os.path.join(uth.samplesdir(), filename) for filename in relativepaths]
            for ff in realpaths:
                shutil.copy2(ff, self._tmpdir)

            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            self._call_ct_cake()

            # Check that an executable got built for each cpp
            actual_exes = set()
            for root, _dirs, files in os.walk(self._tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            expected_exes = {os.path.splitext(os.path.split(filename)[1])[0] for filename in relativepaths}
            assert expected_exes == actual_exes

            # Check that compilation database was created
            assert os.path.exists("compile_commands.json"), "Compilation database should be created by default"

            # Verify compilation database content
            import json

            with open("compile_commands.json") as f:
                commands = json.load(f)
            assert isinstance(commands, list), "Compilation database should be a JSON array"
            assert len(commands) >= len(relativepaths), f"Expected at least {len(relativepaths)} compilation commands"

            # Verify each command has required fields
            for cmd in commands:
                assert "directory" in cmd, "Each command should have 'directory' field"
                assert "file" in cmd, "Each command should have 'file' field"
                assert "arguments" in cmd, "Each command should have 'arguments' field"

    @uth.requires_functional_compiler
    def test_no_compilation_database(self):
        """Test that compilation database can be disabled with --no-compilation-database"""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()

            # Copy a known cpp file to a non-git directory and compile using cake
            relativepaths = ["simple/helloworld_cpp.cpp"]
            realpaths = [os.path.join(uth.samplesdir(), filename) for filename in relativepaths]
            for ff in realpaths:
                shutil.copy2(ff, self._tmpdir)

            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            # Call ct-cake with --no-compilation-database
            self._call_ct_cake(extraargv=["--no-compilation-database"])

            # Check that an executable got built
            actual_exes = set()
            for root, _dirs, files in os.walk(self._tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            expected_exes = {os.path.splitext(os.path.split(filename)[1])[0] for filename in relativepaths}
            assert expected_exes == actual_exes

            # Check that compilation database was NOT created
            assert not os.path.exists("compile_commands.json"), (
                "Compilation database should not be created with --no-compilation-database"
            )

    @uth.requires_functional_compiler
    def test_custom_compilation_database_output(self):
        """Test that compilation database can be written to a custom filename"""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()

            # Copy a known cpp file to a non-git directory and compile using cake
            relativepaths = ["simple/helloworld_cpp.cpp"]
            realpaths = [os.path.join(uth.samplesdir(), filename) for filename in relativepaths]
            for ff in realpaths:
                shutil.copy2(ff, self._tmpdir)

            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            # Call ct-cake with custom compilation database output
            custom_output = "my_compile_commands.json"
            self._call_ct_cake(extraargv=[f"--compilation-database-output={custom_output}"])

            # Check that an executable got built
            actual_exes = set()
            for root, _dirs, files in os.walk(self._tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            expected_exes = {os.path.splitext(os.path.split(filename)[1])[0] for filename in relativepaths}
            assert expected_exes == actual_exes

            # Check that compilation database was created with custom name
            assert os.path.exists(custom_output), f"Custom compilation database {custom_output} should be created"
            assert not os.path.exists("compile_commands.json"), (
                "Default compilation database should not be created when custom output is specified"
            )

            # Verify custom compilation database content
            import json

            with open(custom_output) as f:
                commands = json.load(f)
            assert isinstance(commands, list), "Custom compilation database should be a JSON array"
            assert len(commands) >= len(relativepaths), f"Expected at least {len(relativepaths)} compilation commands"

    def _get_file_contents(self):
        """Define all available source file contents"""
        return {
            "deeper.cpp": """
                #include "deeper.hpp"

                int deeper_func(const int value)
                {
                    return 42;
                }
            """,
            "deeper.hpp": """
                int deeper_func(const int value);
            """,
            "extra.cpp": """
                #include "extra.hpp"

                int extra_func(const int value)
                {
                    return 24;
                }
            """,
            "extra.hpp": """
                int extra_func(const int value);
            """,
            "main.cpp": """
                #include "extra.hpp"

                int main(int argc, char* argv[])
                {
                    return extra_func(42);
                }
            """,
        }

    def _create_source_files(self, files=None):
        """Create source files, optionally selecting which ones

        Args:
            files: List of filenames to create. If None, creates all available files.

        Returns:
            Dictionary of {relative_path: Path} for created files
        """
        file_contents = self._get_file_contents()

        if files is None:
            files = list(file_contents.keys())

        # Validate that all requested files are available
        unavailable = set(files) - set(file_contents.keys())
        if unavailable:
            raise ValueError(f"Unavailable files requested: {unavailable}")

        return uth.write_sources({f: file_contents[f] for f in files})

    # Convenience methods for individual file creation
    def _create_main_cpp(self):
        """Create just main.cpp"""
        return self._create_source_files(["main.cpp"])

    def _create_extra_files(self):
        """Create extra.cpp and extra.hpp"""
        return self._create_source_files(["extra.cpp", "extra.hpp"])

    def _create_deeper_files(self):
        """Create deeper.cpp and deeper.hpp"""
        return self._create_source_files(["deeper.cpp", "deeper.hpp"])

    def _inject_deeper_hpp_into_extra_hpp(self):
        data = []
        with open("extra.hpp") as infile:
            data = ['#include "deeper.hpp"\n'] + infile.readlines()

        with open("extra.hpp", "w") as outfile:
            outfile.writelines(data)

    def _create_recompile_test_files(self, deeper_is_included=False):
        """Create a simple C++ program containing a main.cpp, extra.hpp,
        extra.cpp, and extra.hpp in turn includes deeper.hpp which has an
        associated deeper.cpp.
        This will allow us to test that editing any of those files
        triggers a recompile.
        """
        return self._create_source_files()

    def _grab_timestamps(self, deeper_is_included=False):
        """There are 8 files we want timestamps for.
        main.cpp, extra.hpp, extra.cpp, deeper.hpp, deeper.cpp, deeper.o, main.o, extra.o
        and the executable called "main".

        This must be called inside the directory where main.cpp lives.
        """

        # Create a namer so that we get the names of the object files correct
        argv = self._create_argv()
        cap = compiletools.apptools.create_parser("test cake", argv=argv)
        compiletools.cake.Cake.add_arguments(cap)
        args = compiletools.apptools.parseargs(cap, argv, verbose=0, context=BuildContext())
        nmr = compiletools.namer.Namer(args, context=BuildContext())

        # These are the basic filenames
        fnames = [
            os.path.realpath("main.cpp"),
            os.path.realpath("extra.hpp"),
            os.path.realpath("extra.cpp"),
        ]

        if deeper_is_included:
            fnames.append(os.path.realpath("deeper.hpp"))
            fnames.append(os.path.realpath("deeper.cpp"))

        # Add in the object filenames (only cpp have object files)
        # Object files now have content-addressable names with hashes, so we glob for them
        import glob

        objdir = nmr.object_dir(fnames[0])
        for fname in [name for name in fnames if "cpp" in name]:
            _, name = os.path.split(fname)
            basename = os.path.splitext(name)[0]
            # Find object files matching the pattern: basename_<file_hash>_<dep_hash>_<macro_state_hash>.o
            # Pattern {basename}_*_*.o matches both old (2 underscores) and new (3 underscores) formats
            pattern = os.path.join(objdir, f"{basename}_*_*.o")
            matching_objs = glob.glob(pattern)
            fnames.extend(matching_objs)

        # Add the executable name
        fnames.append(nmr.executable_pathname("main.cpp"))

        timestamps = {}
        for fname in fnames:
            timestamps[fname] = os.path.getmtime(fname)

        return timestamps

    def _verify_timestamps(self, expected_changes, prets, postts):
        """Pass in the list of files that are expected to have newer
        timestamps, the pre compiling timestamps and the
        post compiling timestamps
        """
        import os

        # For files in prets that still exist in postts, verify they changed or didn't as expected
        for fname in prets:
            # Skip files that no longer exist (e.g., old object files with different content hashes)
            if fname not in postts:
                continue

            # Due to the name munging it is slightly convoluted to
            # figure out if the filename is in the expected changes list
            expected_to_change = False
            is_object_file = False
            source_file_changed = False
            for ec in expected_changes:
                # Handle both plain files and object files with hash-based names
                # Object files now have format: basename_filehash_macrostatehash.o
                if ec.endswith(".o"):
                    # For object files, match basename prefix (e.g., "main.o" matches "main_*_*.o")
                    basename = ec[:-2]  # Remove ".o"
                    file_basename = os.path.basename(fname)
                    if file_basename.startswith(basename + "_") and file_basename.endswith(".o"):
                        expected_to_change = True
                        is_object_file = True
                        # Check if the corresponding source file also changed
                        source_file_changed = basename + ".cpp" in expected_changes
                elif fname.endswith(ec):
                    # For non-object files, use exact suffix match
                    expected_to_change = True

            if expected_to_change and is_object_file and source_file_changed:
                # Source file changed → new object file created with different file hash.
                # The old object file remains unchanged. We verify new object file
                # creation separately below, so skip timestamp check here.
                pass
            elif expected_to_change and is_object_file:
                # Only header changed (not source) → new object file created with different dep_hash.
                # The old object file remains unchanged. We verify new object file
                # creation separately below, so skip timestamp check here.
                pass
            elif expected_to_change:
                # For non-object files expected to change, check that timestamp increased
                assert postts[fname] > prets[fname]
            else:
                # File not expected to change - verify timestamp is the same
                print("verify " + fname)
                assert round(abs(postts[fname] - prets[fname]), 7) == 0

        # For object files whose source file changed, verify a new one was created
        for ec in expected_changes:
            if ec.endswith(".o"):
                basename = ec[:-2]
                # Check for new object file if source OR object file changed
                # (headers trigger new objects via dep_hash, sources via file_hash)
                if basename + ".cpp" in expected_changes or ec in expected_changes:
                    # Find new object files in postts that weren't in prets
                    new_objs = [
                        f
                        for f in postts
                        if f not in prets
                        and os.path.basename(f).startswith(basename + "_")
                        and os.path.basename(f).endswith(".o")
                    ]
                    # At least one new object file should have been created for this source
                    assert len(new_objs) > 0, f"Expected new object file for {ec} but none found"

    def _compile_edit_compile(self, files_to_edit, expected_changes, deeper_is_included=False):
        """Test that the compile, edit, compile cycle works as you expect"""
        with uth.TempDirContext() as _:
            self._tmpdir = os.getcwd()
            self._create_recompile_test_files(deeper_is_included)

            # Do an initial build
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            self._call_ct_cake(extraargv=[])

            # Grab the timestamps on the build products so that later we can test that only the expected ones changed
            # deeper_is_included must be false at this point becuase the option to inject it comes later/ver
            prets = self._grab_timestamps(deeper_is_included=False)

            # Edit the files for this test
            if deeper_is_included:
                self._inject_deeper_hpp_into_extra_hpp()

            uth.touch(*files_to_edit)

            # Rebuild
            self._call_ct_cake(extraargv=[])

            # Grab the timestamps on the build products for comparison
            postts = self._grab_timestamps(deeper_is_included)

            # Check that only the expected timestamps have changed
            self._verify_timestamps(expected_changes, prets, postts)

    @uth.requires_functional_compiler
    def test_source_edit_recompiles(self):
        """Make sure that when the source file is altered that a rebuild occurs"""
        self._compile_edit_compile(["main.cpp"], ["main.cpp", "main.o", "main"])

    @uth.requires_functional_compiler
    def test_header_edit_recompiles(self):
        """Make sure that when a header file is altered that a rebuild occurs"""
        self._compile_edit_compile(["extra.hpp"], ["extra.hpp", "extra.o", "main.o", "main"])

    @uth.requires_functional_compiler
    def test_dependent_source_edit_recompiles(self):
        """Make sure that when an implied source file is altered that a rebuild occurs"""
        self._compile_edit_compile(["extra.cpp"], ["extra.cpp", "extra.o", "main"])

    @uth.requires_functional_compiler
    def test_deeper_include_edit_recompiles(self):
        """Make sure that when a deeper include file is put into extra.hpp that a rebuild occurs"""
        self._compile_edit_compile(
            ["extra.hpp"],
            ["extra.hpp", "deeper.hpp", "deeper.o", "extra.o", "main.o", "main"],
            deeper_is_included=True,
        )

    @uth.requires_functional_compiler
    def test_platform_has_include_builds(self):
        """Test that __has_include platform selection compiles and links.

        platform_main.cpp uses __has_include to pick between linux_func and
        windows_func.  Both define the same symbol (platform_func), so if
        both were compiled the linker would fail with a duplicate definition.
        A successful build proves exactly one platform was selected.
        """
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()

            sample_dir = os.path.join(uth.samplesdir(), "platform_has_include")
            for f in os.listdir(sample_dir):
                shutil.copy2(os.path.join(sample_dir, f), self._tmpdir)

            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            self._call_ct_cake()

            actual_exes = set()
            for root, _dirs, files in os.walk(self._tmpdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)

            assert "platform_main" in actual_exes

    def test_main_nothing_to_do(self):
        """Test that main() returns 0 when there's nothing to do."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            # Explicitly disable --auto and provide no filenames → "Nothing for cake to do"
            result = compiletools.cake.main(["--config=" + self._config_name, "--no-auto"])
            assert result == 0

    def test_main_oserror(self):
        """Test that main() handles OSError and returns 1."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            with patch("compiletools.cake.Cake.process", side_effect=OSError(2, "No such file", "missing.cpp")):
                result = compiletools.cake.main(self._create_argv())
                assert result == 1

    def test_main_generic_exception(self):
        """Test that main() handles generic exceptions and returns 1."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            with patch("compiletools.cake.Cake.process", side_effect=RuntimeError("something broke")):
                result = compiletools.cake.main(self._create_argv())
                assert result == 1

    def test_main_oserror_verbose_reraises(self):
        """Test that main() re-raises OSError when verbose >= 2."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            import pytest

            with pytest.raises(OSError):
                with patch(
                    "compiletools.cake.Cake.process",
                    side_effect=OSError(2, "No such file", "missing.cpp"),
                ):
                    compiletools.cake.main(self._create_argv() + ["-vvv"])

    def test_main_generic_exception_verbose_reraises(self):
        """Test that main() re-raises generic exceptions when verbose >= 2."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            import pytest

            with pytest.raises(RuntimeError, match="something broke"):
                with patch(
                    "compiletools.cake.Cake.process",
                    side_effect=RuntimeError("something broke"),
                ):
                    compiletools.cake.main(self._create_argv() + ["-vvv"])

    def test_signal_handler_exits(self):
        """Test that signal_handler calls sys.exit(0)."""
        import pytest

        with pytest.raises(SystemExit) as exc_info:
            compiletools.cake.signal_handler(None, None)
        assert exc_info.value.code == 0

    def test_hide_makefilename_moves_makefile(self):
        """Test that _hide_makefilename moves the makefile into executable_dir."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            # Set verbose > 4 to cover the print branch
            args.verbose = 5
            args.makefilename = "Makefile"
            compiletools.cake.Cake._hide_makefilename(args)
            # makefilename should now include the executable dir
            namer = compiletools.namer.Namer(args, context=BuildContext())
            assert namer.executable_dir() in args.makefilename

    def _make_cake_args(self, extra=None):
        """Helper to create parsed args using cake.main's parser approach.
        Mirrors the setup in cake.main().
        """
        if extra is None:
            extra = []
        argv = self._create_argv() + extra
        uth.reset()
        cap = compiletools.apptools.create_parser("test cake", argv=argv)
        compiletools.cake.Cake.add_arguments(cap)
        compiletools.cake.Cake.registercallback()
        return compiletools.apptools.parseargs(cap, argv, context=BuildContext())

    def test_copyexes_with_output_filename(self):
        """Test _copyexes when args.output is set with a filename target."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.output = os.path.join(self._tmpdir, "myexe")
            args.filename = ["test.cpp"]
            args.static = None
            args.dynamic = None
            args.verbose = 1

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()

            # Create a fake executable to copy
            src_exe = cake.namer.executable_pathname("test.cpp")
            os.makedirs(os.path.dirname(src_exe), exist_ok=True)
            with open(src_exe, "w") as f:
                f.write("fake")

            cake._copyexes()
            assert os.path.exists(args.output)

    def test_copyexes_with_output_static(self):
        """Test _copyexes when args.output is set with a static library."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.output = os.path.join(self._tmpdir, "mylib.a")
            args.filename = None
            args.static = ["test.cpp"]
            args.dynamic = None
            args.verbose = 1

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()

            src_lib = cake.namer.staticlibrary_pathname()
            os.makedirs(os.path.dirname(src_lib), exist_ok=True)
            with open(src_lib, "w") as f:
                f.write("fake")

            cake._copyexes()
            assert os.path.exists(args.output)

    def test_copyexes_with_output_dynamic(self):
        """Test _copyexes when args.output is set with a dynamic library."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.output = os.path.join(self._tmpdir, "mylib.so")
            args.filename = None
            args.static = None
            args.dynamic = ["test.cpp"]
            args.verbose = 1

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()

            src_lib = cake.namer.dynamiclibrary_pathname()
            os.makedirs(os.path.dirname(src_lib), exist_ok=True)
            with open(src_lib, "w") as f:
                f.write("fake")

            cake._copyexes()
            assert os.path.exists(args.output)

    def test_copyexes_no_output_static(self):
        """Test _copyexes without output but with static library."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.output = None
            args.filename = None
            args.static = ["test.cpp"]
            args.dynamic = None
            args.verbose = 1

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()

            src_lib = cake.namer.staticlibrary_pathname()
            os.makedirs(os.path.dirname(src_lib), exist_ok=True)
            with open(src_lib, "w") as f:
                f.write("fake")
            outputdir = cake.namer.topbindir()
            os.makedirs(outputdir, exist_ok=True)

            cake._copyexes()
            expected = os.path.join(outputdir, cake.namer.staticlibrary_name())
            assert os.path.exists(expected)

    def test_copyexes_no_output_dynamic(self):
        """Test _copyexes without output but with dynamic library."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.output = None
            args.filename = None
            args.static = None
            args.dynamic = ["test.cpp"]
            args.verbose = 1

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()

            src_lib = cake.namer.dynamiclibrary_pathname()
            os.makedirs(os.path.dirname(src_lib), exist_ok=True)
            with open(src_lib, "w") as f:
                f.write("fake")
            outputdir = cake.namer.topbindir()
            os.makedirs(outputdir, exist_ok=True)

            cake._copyexes()
            expected = os.path.join(outputdir, cake.namer.dynamiclibrary_name())
            assert os.path.exists(expected)

    def test_callfilelist(self):
        """Test that _callfilelist actually invokes the filelist processor."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._create_source_files(["main.cpp", "extra.cpp", "extra.hpp"])
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args(["--filelist"])

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()
            # Should run without error and produce output
            cake._callfilelist()

    def test_call_compilation_database_skipped_on_clean(self):
        """Test that _call_compilation_database returns early when clean is True."""
        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args()
            args.clean = True

            cake = compiletools.cake.Cake(args)
            cake._createctobjs()
            # Should return early without error
            cake._call_compilation_database()

    def test_process_filelist_branch(self):
        """Test that process() calls _callfilelist when args.filelist is set."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._create_source_files(["main.cpp", "extra.cpp", "extra.hpp"])
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args(["--filelist"])

            cake = compiletools.cake.Cake(args)
            with patch.object(cake, "_callfilelist") as mock_filelist:
                with patch.object(cake, "_call_backend") as mock_backend:
                    cake.process()
                    mock_filelist.assert_called_once()
                    mock_backend.assert_not_called()

    def test_process_verbose_high(self):
        """Test that process() prints debug messages when verbose > 4."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._create_source_files(["main.cpp", "extra.cpp", "extra.hpp"])
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            args = self._make_cake_args(["-vvvvv"])

            cake = compiletools.cake.Cake(args)
            with patch.object(cake, "_call_backend"):
                with patch("builtins.print") as mock_print:
                    cake.process()
                    calls = [str(c) for c in mock_print.call_args_list]
                    assert any("Early scanning" in c for c in calls)

    def test_process_single_static_expands(self):
        """Test that process() expands a single static library file."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._create_source_files(["main.cpp", "extra.cpp", "extra.hpp"])
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            uth.reset()
            argv = [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--static",
                os.path.join(self._tmpdir, "main.cpp"),
                "--config=" + self._config_name,
            ]
            cap = compiletools.apptools.create_parser("test", argv=argv)
            compiletools.cake.Cake.add_arguments(cap)
            compiletools.cake.Cake.registercallback()
            args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())

            cake = compiletools.cake.Cake(args)
            with patch.object(cake, "_call_backend"):
                cake.process()
                assert len(args.static) >= 1

    def test_process_single_dynamic_expands(self):
        """Test that process() expands a single dynamic library file."""
        from unittest.mock import patch

        with uth.TempDirContext():
            self._tmpdir = os.getcwd()
            self._create_source_files(["main.cpp", "extra.cpp", "extra.hpp"])
            self._config_name = uth.create_temp_config(self._tmpdir)
            uth.create_temp_ct_conf(
                tempdir=self._tmpdir,
                defaultvariant=os.path.basename(self._config_name)[:-5],
            )
            uth.reset()
            argv = [
                "--exemarkers=main",
                "--testmarkers=unittest.hpp",
                "--dynamic",
                os.path.join(self._tmpdir, "main.cpp"),
                "--config=" + self._config_name,
            ]
            cap = compiletools.apptools.create_parser("test", argv=argv)
            compiletools.cake.Cake.add_arguments(cap)
            compiletools.cake.Cake.registercallback()
            args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())

            cake = compiletools.cake.Cake(args)
            with patch.object(cake, "_call_backend"):
                cake.process()
                assert len(args.dynamic) >= 1

    def teardown_method(self):
        uth.reset()
