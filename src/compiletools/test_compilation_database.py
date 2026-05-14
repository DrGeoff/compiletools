import json
import os
import time
import types

import compiletools.apptools
import compiletools.compilation_database
import compiletools.findtargets
import compiletools.hunter
import compiletools.makefile_backend
import compiletools.testhelper as uth
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_context import BuildContext


class TestCompilationDatabase:
    def setup_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_basic_compilation_database_creation(self):
        """Test basic compilation database creation with simple C++ files"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Use existing sample files
                relativepaths = ["simple/helloworld_cpp.cpp", "simple/helloworld_c.c"]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                with uth.ParserContext():
                    # Create compilation database
                    output_file = "compile_commands.json"
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + output_file] + realpaths
                    )

                    # Verify file was created
                    assert os.path.exists(output_file)

                    # Verify JSON format
                    with open(output_file) as f:
                        commands = json.load(f)

                    assert isinstance(commands, list)
                    assert len(commands) >= 2  # At least our two test files

                    # Verify command structure
                    for cmd in commands:
                        assert isinstance(cmd, dict)
                        assert "directory" in cmd
                        assert "file" in cmd
                        assert "arguments" in cmd
                        assert isinstance(cmd["arguments"], list)
                        assert len(cmd["arguments"]) > 0
                        assert cmd["arguments"][0].endswith(("gcc", "g++", "clang", "clang++"))

    @uth.requires_functional_compiler
    def test_compilation_database_with_relative_paths(self):
        """Test compilation database creation with relative paths option"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                relativepaths = ["simple/helloworld_cpp.cpp"]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                with uth.ParserContext():
                    output_file = "compile_commands_rel.json"
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--relative-paths",
                            "--compilation-database-output=" + output_file,
                        ]
                        + realpaths
                    )

                    assert os.path.exists(output_file)

                    with open(output_file) as f:
                        commands = json.load(f)

                    # Check that file paths are relative when --relative-paths is used
                    for cmd in commands:
                        # Directory should still be absolute (working directory)
                        assert cmd["directory"].startswith("/"), (
                            f"Directory should still be absolute, got: {cmd['directory']}"
                        )
                        # File path should be relative when --relative-paths is used
                        assert not cmd["file"].startswith("/"), (
                            f"File path should be relative with --relative-paths, got: {cmd['file']}"
                        )

    @uth.requires_functional_compiler
    def test_compilation_database_creator_class(self):
        """Test the CompilationDatabaseCreator class directly"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Create args object by parsing like main() would
                relativepaths = ["simple/helloworld_cpp.cpp"]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                # Use the module's main function to test integration
                argv = ["--config=" + temp_config_name, "--compilation-database-output=test_output.json"] + realpaths

                cap = compiletools.apptools.create_parser("Generate compile_commands.json for clang tooling", argv=argv)
                compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                compiletools.hunter.add_arguments(cap)
                args = compiletools.apptools.parseargs(cap, argv, context=BuildContext())

                with uth.ParserContext():
                    # Test the creator class
                    creator = compiletools.compilation_database.CompilationDatabaseCreator(args, context=BuildContext())

                    # Test command object creation
                    if realpaths and os.path.exists(realpaths[0]):
                        cmd_obj = creator._create_command_object(realpaths[0])

                        assert isinstance(cmd_obj, dict)
                        assert "directory" in cmd_obj
                        assert "file" in cmd_obj
                        assert "arguments" in cmd_obj
                        assert isinstance(cmd_obj["arguments"], list)

                    # Test full database creation
                    commands = creator.create_compilation_database()
                    assert isinstance(commands, list)

                    # Test writing to file
                    creator.write_compilation_database()
                    assert os.path.exists(args.compilation_database_output)

    def test_json_format_compliance(self):
        """Test that generated JSON is valid and properly formatted"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                relativepaths = ["simple/helloworld_cpp.cpp"]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                with uth.ParserContext():
                    output_file = "format_test.json"
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + output_file] + realpaths
                    )

                    # Verify JSON can be parsed
                    with open(output_file) as f:
                        content = f.read()
                        commands = json.loads(content)

                    # Verify structure matches clang specification
                    for cmd in commands:
                        # Required fields
                        assert "directory" in cmd
                        assert "file" in cmd
                        assert "arguments" in cmd or "command" in cmd  # One of these required

                        # Verify arguments format (preferred)
                        if "arguments" in cmd:
                            assert isinstance(cmd["arguments"], list)
                            assert all(isinstance(arg, str) for arg in cmd["arguments"])

                        # Verify paths are valid
                        assert isinstance(cmd["directory"], str)
                        assert isinstance(cmd["file"], str)
                        assert len(cmd["directory"]) > 0
                        assert len(cmd["file"]) > 0

    @uth.requires_functional_compiler
    def test_compilation_database_vs_makefile_equivalence(self):
        """Test that compilation database generates equivalent commands to Makefile"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Use the same test files as the Makefile test
                relativepaths = ["simple/helloworld_cpp.cpp", "simple/helloworld_c.c"]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                # Generate compilation database
                comp_db_output = "compile_commands.json"
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + comp_db_output] + realpaths
                    )

                # Generate Makefile (disable file-locking so commands are directly comparable)
                with uth.ParserContext():
                    compiletools.makefile_backend.main(
                        ["--config=" + temp_config_name, "--no-file-locking"] + realpaths
                    )

                # Read compilation database
                with open(comp_db_output) as f:
                    comp_db_commands = json.load(f)

                # Parse Makefile for compilation rules
                makefile_commands = self._extract_compile_commands_from_makefile()

                # Compare commands for equivalence
                self._assert_commands_equivalent(comp_db_commands, makefile_commands, realpaths)

    @uth.requires_functional_compiler
    def test_compilation_database_vs_makefile_complex_project(self):
        """Test equivalence with a more complex project having multiple files and dependencies"""

        with uth.TempDirContext() as _:
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Use factory sample which has multiple source files with dependencies
                relativepaths = [
                    "factory/test_factory.cpp",
                    "factory/widget_factory.cpp",
                    "factory/a_widget.cpp",
                    "factory/z_widget.cpp",
                    # Also include numbers sample for additional complexity
                    "numbers/test_direct_include.cpp",
                    "numbers/get_numbers.cpp",
                    "numbers/get_int.cpp",
                    "numbers/get_double.cpp",
                ]
                realpaths = [uth.example_file(filename) for filename in relativepaths]

                # Generate compilation database
                comp_db_output = "compile_commands_complex.json"
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + comp_db_output] + realpaths
                    )

                # Generate Makefile (disable file-locking so commands are directly comparable)
                with uth.ParserContext():
                    compiletools.makefile_backend.main(
                        ["--config=" + temp_config_name, "--no-file-locking"] + realpaths
                    )

                # Read compilation database
                with open(comp_db_output) as f:
                    comp_db_commands = json.load(f)

                # Parse Makefile for compilation rules
                makefile_commands = self._extract_compile_commands_from_makefile()

                # Verify we have commands for all source files
                assert len(comp_db_commands) >= len(relativepaths), (
                    f"Expected at least {len(relativepaths)} compilation database entries, got {len(comp_db_commands)}"
                )
                assert len(makefile_commands) >= len(relativepaths), (
                    f"Expected at least {len(relativepaths)} Makefile commands, got {len(makefile_commands)}"
                )

                # Compare commands for equivalence
                self._assert_commands_equivalent(comp_db_commands, makefile_commands, realpaths)

                # Additional verification: check that header dependencies are handled
                # Both tools should produce the same set of include flags
                comp_db_includes = set()
                makefile_includes = set()

                for cmd in comp_db_commands:
                    args = cmd["arguments"]
                    for i, arg in enumerate(args):
                        if arg == "-I" and i + 1 < len(args):
                            comp_db_includes.add(args[i + 1])

                for _source_file, command in makefile_commands.items():
                    for i, arg in enumerate(command):
                        if arg == "-I" and i + 1 < len(command):
                            makefile_includes.add(command[i + 1])

                # The include sets should be equivalent (same directories used)
                assert comp_db_includes == makefile_includes, (
                    f"Include directories differ: comp_db={comp_db_includes}, makefile={makefile_includes}"
                )

    @uth.requires_functional_compiler
    def test_compilation_database_with_findtargets_discovery(self):
        """Test compilation database with FindTargets-based auto-discovery like cake --auto"""

        with uth.TempDirContext() as _:
            # Copy some sample files to the current directory for auto-discovery
            import shutil

            shutil.copy(uth.example_file("simple/helloworld_cpp.cpp"), ".")
            shutil.copy(uth.example_file("simple/helloworld_c.c"), ".")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                with uth.ParserContext():
                    # Create args object like compilation database would
                    cap = compiletools.apptools.create_parser(
                        "Test compilation database with auto-discovery",
                        argv=[
                            "--config=" + temp_config_name,
                            "--exemarkers=main(",  # Tell it how to identify executable files
                        ],
                    )
                    compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                    compiletools.hunter.add_arguments(cap)
                    compiletools.findtargets.add_arguments(cap)  # Add findtargets arguments including --auto

                    # Parse args with auto-discovery enabled
                    args = compiletools.apptools.parseargs(
                        cap,
                        [
                            "--config=" + temp_config_name,
                            "--exemarkers=main(",  # Tell it how to identify executable files
                            "--testmarkers=test(",  # Tell it how to identify test files
                        ],
                        context=BuildContext(),
                    )

                    # Use FindTargets to discover source files (following cake.py pattern)
                    findtargets = compiletools.findtargets.FindTargets(args, context=BuildContext())
                    findtargets.process(args)

                    # Verify that targets were found
                    assert hasattr(args, "filename") and args.filename, (
                        f"FindTargets should have found source files, got: {getattr(args, 'filename', [])}"
                    )

                    # Create compilation database with the discovered files
                    creator = compiletools.compilation_database.CompilationDatabaseCreator(args, context=BuildContext())

                    comp_db_output = "compile_commands_findtargets.json"
                    args.compilation_database_output = comp_db_output
                    creator.write_compilation_database()

                # Verify compilation database was created and contains discovered files
                assert os.path.exists(comp_db_output), "Compilation database should be created"

                with open(comp_db_output) as f:
                    comp_db_commands = json.load(f)

                # Should have found our copied source files
                assert len(comp_db_commands) >= 2, f"Expected at least 2 files, got {len(comp_db_commands)}"

                # Verify the discovered files include our test files
                found_files = {os.path.basename(cmd["file"]) for cmd in comp_db_commands}
                expected_files = {"helloworld_cpp.cpp", "helloworld_c.c"}

                assert expected_files.issubset(found_files), (
                    f"Expected files {expected_files} not found in discovered files {found_files}"
                )

                # Verify all commands are valid compilation commands
                for cmd in comp_db_commands:
                    assert "arguments" in cmd, "Each command should have arguments"
                    assert "directory" in cmd, "Each command should have directory"
                    assert "file" in cmd, "Each command should have file"

                    args_list = cmd["arguments"]
                    assert len(args_list) > 0, "Arguments should not be empty"
                    assert "-c" in args_list, "Should be compilation command with -c flag"

                    # Check compiler is appropriate for file type
                    filename = os.path.basename(cmd["file"])
                    compiler = args_list[0]
                    if filename.endswith(".cpp"):
                        assert any(cpp in compiler for cpp in ["g++", "clang++", "c++"]), (
                            f"C++ file should use C++ compiler, got {compiler}"
                        )
                    elif filename.endswith(".c"):
                        assert any(c in compiler for c in ["gcc", "clang"]) and not any(
                            cpp in compiler for cpp in ["g++", "clang++", "c++"]
                        ), f"C file should use C compiler, got {compiler}"

    @uth.requires_functional_compiler
    def test_compilation_database_incremental_updates(self):
        """Test that compilation database supports incremental updates without wiping existing entries"""

        with uth.TempDirContext() as _:
            # Copy multiple sample files for initial build
            import shutil

            shutil.copy(uth.example_file("simple/helloworld_cpp.cpp"), ".")
            shutil.copy(uth.example_file("simple/helloworld_c.c"), ".")
            shutil.copy(uth.example_file("factory/test_factory.cpp"), ".")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"

                # Step 1: Create initial compilation database with all files
                initial_files = ["helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"]
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + comp_db_output]
                        + initial_files
                    )

                # Verify initial database
                assert os.path.exists(comp_db_output), "Initial compilation database should be created"

                with open(comp_db_output) as f:
                    initial_commands = json.load(f)

                assert len(initial_commands) == 3, f"Expected 3 initial commands, got {len(initial_commands)}"
                initial_files_set = {os.path.basename(cmd["file"]) for cmd in initial_commands}
                assert initial_files_set == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}

                # Step 2: Simulate updating just one file (like after editing and recompiling)
                # This should only update the entry for that file, not wipe the entire database
                updated_file = ["helloworld_cpp.cpp"]
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + comp_db_output]
                        + updated_file
                    )

                # Step 3: Verify incremental behavior
                with open(comp_db_output) as f:
                    updated_commands = json.load(f)

                # CRITICAL: The database should still contain ALL original files
                # This test will currently FAIL because the current implementation overwrites
                # but this is the behavior we need to implement
                updated_files_set = {os.path.basename(cmd["file"]) for cmd in updated_commands}

                # The key assertion: all original files should still be present
                assert len(updated_commands) == 3, (
                    f"Incremental update should preserve all entries. Expected 3, got {len(updated_commands)}. "
                    f"Files in database: {updated_files_set}"
                )

                assert "helloworld_c.c" in updated_files_set, (
                    "helloworld_c.c should be preserved after updating helloworld_cpp.cpp"
                )
                assert "test_factory.cpp" in updated_files_set, (
                    "test_factory.cpp should be preserved after updating helloworld_cpp.cpp"
                )
                assert "helloworld_cpp.cpp" in updated_files_set, (
                    "helloworld_cpp.cpp should still be present after update"
                )

                # Additional verification: the updated file should have current timestamp/flags
                # while other files should remain unchanged
                cpp_entry = next(cmd for cmd in updated_commands if "helloworld_cpp.cpp" in cmd["file"])
                c_entry = next(cmd for cmd in updated_commands if "helloworld_c.c" in cmd["file"])
                factory_entry = next(cmd for cmd in updated_commands if "test_factory.cpp" in cmd["file"])

                # All entries should still be valid compilation commands
                for entry in [cpp_entry, c_entry, factory_entry]:
                    assert "arguments" in entry
                    assert "directory" in entry
                    assert "file" in entry
                    assert "-c" in entry["arguments"], "Should be compilation command"

    def test_no_op_write_does_not_advance_file_mtime(self):
        """A second _write_database_impl call with identical commands must not rewrite the database.

        clangd, ccls, and `find -newer`-based watchers treat any inode/mtime
        change on compile_commands.json as an invalidation event. Re-emitting
        byte-identical content forces them to re-index the world for nothing.

        Drives _write_database_impl directly (rather than via main()) for the
        same reason as test_content_change_still_writes: the method reads only
        self.args.verbose from self, so __new__ + SimpleNamespace keeps the
        test independent of unrelated FileAnalyzer / Hunter changes that would
        otherwise be in the call path.
        """

        with uth.TempDirContext() as _:
            cwd = os.getcwd()
            output_file = os.path.join(cwd, "compile_commands.json")

            creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
                compiletools.compilation_database.CompilationDatabaseCreator
            )
            creator.args = types.SimpleNamespace(verbose=0)

            # Synthetic source paths: _write_database_impl runs realpath_sz on
            # cmd["file"] for de-duplication but does not require the files to
            # exist on disk; absolute paths inside the temp dir keep realpath
            # resolution deterministic.
            file_a = os.path.join(cwd, "a.cpp")
            file_b = os.path.join(cwd, "b.cpp")
            file_c = os.path.join(cwd, "c.cpp")

            commands = [
                {"directory": cwd, "file": file_a, "arguments": ["c++", "-c", file_a, "-O0"]},
                {"directory": cwd, "file": file_b, "arguments": ["c++", "-c", file_b, "-O0"]},
                {"directory": cwd, "file": file_c, "arguments": ["c++", "-c", file_c, "-O0"]},
            ]

            creator._write_database_impl(output_file, commands)

            assert os.path.exists(output_file), "First write should create the database"
            first_stat = os.stat(output_file)
            first_ino = first_stat.st_ino
            first_mtime_ns = first_stat.st_mtime_ns
            first_size = first_stat.st_size

            # Sleep long enough that any rewrite would advance mtime even on
            # filesystems with coarse mtime resolution.
            time.sleep(0.05)

            creator._write_database_impl(output_file, commands)

            second_stat = os.stat(output_file)
            assert second_stat.st_ino == first_ino, (
                f"No-op rewrite changed inode ({first_ino} -> {second_stat.st_ino}); "
                f"compile_commands.json must not be replaced when content is unchanged "
                f"or clangd / find -newer watchers will re-index unnecessarily."
            )
            assert second_stat.st_mtime_ns == first_mtime_ns, (
                f"No-op rewrite advanced mtime ({first_mtime_ns} -> {second_stat.st_mtime_ns}); "
                f"compile_commands.json must be left untouched when the rendered JSON matches "
                f"the existing on-disk content."
            )
            assert second_stat.st_size == first_size, (
                f"No-op rewrite changed size ({first_size} -> {second_stat.st_size})."
            )

    @uth.requires_functional_compiler
    def test_no_op_symlink_update_does_not_advance_mtime(self):
        """A second main() call must not replace the compile_commands.json symlink.

        The unconditional os.symlink + os.replace cycle in _update_symlink
        creates a fresh symlink inode on every build, advancing the symlink's
        mtime even when the readlink target is unchanged. clangd and other
        IDE indexers re-stat the symlink and treat any mtime change as an
        invalidation, re-indexing the world for nothing.

        Drives _update_symlink directly (rather than via main()) so the assertion
        targets the production guard exactly and is not entangled with cdb
        building. The full end-to-end coverage lives in
        test_symlink_repoints_on_subsequent_variant_build /
        test_default_pathname_is_variant_qualified. We construct a minimal
        CompilationDatabaseCreator via __new__ rather than driving main()
        because _update_symlink reads only self.args.verbose from self; this
        keeps the test independent of unrelated FileAnalyzer / Hunter changes
        that would otherwise be in the call path.
        """

        with uth.TempDirContext() as _:
            cwd = os.getcwd()
            target_path = os.path.join(cwd, "compile_commands.gcc.debug.json")
            symlink_path = os.path.join(cwd, "compile_commands.json")

            # The on-disk target file must exist for relpath() to be meaningful;
            # _update_symlink doesn't read it but a stale symlink that points
            # at nothing wouldn't model the real production scenario.
            with open(target_path, "w") as f:
                f.write("[]")

            # Build a minimal stand-in for CompilationDatabaseCreator to invoke
            # the bound _update_symlink method without engaging hunter / args
            # plumbing that the surrounding test infra assembles.
            creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
                compiletools.compilation_database.CompilationDatabaseCreator
            )
            creator.args = types.SimpleNamespace(verbose=0)

            creator._update_symlink(symlink_path, target_path)
            assert os.path.islink(symlink_path), f"First _update_symlink call should create symlink at {symlink_path}"

            first_lstat = os.lstat(symlink_path)
            first_ino = first_lstat.st_ino
            first_mtime_ns = first_lstat.st_mtime_ns
            first_target = os.readlink(symlink_path)

            # Sleep long enough that any replace would advance mtime even
            # on filesystems with coarse mtime resolution.
            time.sleep(0.05)

            creator._update_symlink(symlink_path, target_path)

            second_lstat = os.lstat(symlink_path)
            second_target = os.readlink(symlink_path)

            assert second_target == first_target, (
                f"No-op rebuild changed symlink target ({first_target!r} -> {second_target!r})."
            )
            assert second_lstat.st_ino == first_ino, (
                f"No-op rebuild replaced symlink inode ({first_ino} -> {second_lstat.st_ino}); "
                f"the symlink must not be re-created when its readlink target is unchanged "
                f"or clangd / IDE indexers will re-index on every ct-cake invocation."
            )
            assert second_lstat.st_mtime_ns == first_mtime_ns, (
                f"No-op rebuild advanced symlink mtime ({first_mtime_ns} -> {second_lstat.st_mtime_ns}); "
                f"compile_commands.json (symlink) must be left untouched when its readlink "
                f"target is unchanged."
            )

    def test_content_change_still_writes(self):
        """A content-changing _write_database_impl call must update the file.

        Companion to test_no_op_write_does_not_advance_file_mtime: that test
        verifies the no-op-skip path; this one verifies the symmetric positive
        case so an over-eager content-equality guard (e.g., one that compares
        only the entry count, or that hashes a stale rendering) cannot silently
        suppress a real flag change. Suppressing a content-changing write would
        leave clangd indexing stale flags forever.

        Drives _write_database_impl directly (rather than via main()) for the
        same reason as test_no_op_symlink_update_does_not_advance_mtime: the
        method reads only self.args.verbose from self, so __new__ +
        SimpleNamespace keeps the test independent of unrelated FileAnalyzer /
        Hunter changes that would otherwise be in the call path.
        """

        with uth.TempDirContext() as _:
            cwd = os.getcwd()
            output_file = os.path.join(cwd, "compile_commands.json")

            creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
                compiletools.compilation_database.CompilationDatabaseCreator
            )
            creator.args = types.SimpleNamespace(verbose=0)

            # Synthetic source paths: _write_database_impl runs realpath_sz on
            # cmd["file"] for de-duplication but does not require the files to
            # exist on disk; absolute paths inside the temp dir keep realpath
            # resolution deterministic.
            file_a = os.path.join(cwd, "a.cpp")
            file_b = os.path.join(cwd, "b.cpp")
            file_c = os.path.join(cwd, "c.cpp")

            initial_commands = [
                {"directory": cwd, "file": file_a, "arguments": ["c++", "-c", file_a, "-O0"]},
                {"directory": cwd, "file": file_b, "arguments": ["c++", "-c", file_b, "-O0"]},
            ]
            creator._write_database_impl(output_file, initial_commands)

            assert os.path.exists(output_file), "First write should create the database"
            first_stat = os.stat(output_file)
            first_ino = first_stat.st_ino
            first_mtime_ns = first_stat.st_mtime_ns
            with open(output_file) as fh:
                first_content = fh.read()
            first_entries = json.loads(first_content)
            assert len(first_entries) == 2, "First write should contain both initial entries"

            # Sleep long enough that any rewrite advances mtime even on
            # filesystems with coarse mtime resolution.
            time.sleep(0.05)

            # New command set: adds an entry AND modifies an existing one's
            # arguments. Either change alone must trigger a rewrite.
            updated_commands = [
                {"directory": cwd, "file": file_a, "arguments": ["c++", "-c", file_a, "-O2", "-DNDEBUG"]},
                {"directory": cwd, "file": file_b, "arguments": ["c++", "-c", file_b, "-O0"]},
                {"directory": cwd, "file": file_c, "arguments": ["c++", "-c", file_c, "-O0"]},
            ]
            creator._write_database_impl(output_file, updated_commands)

            second_stat = os.stat(output_file)
            with open(output_file) as fh:
                second_content = fh.read()
            second_entries = json.loads(second_content)

            assert second_stat.st_mtime_ns > first_mtime_ns, (
                f"Content-changing rewrite did NOT advance mtime "
                f"({first_mtime_ns} -> {second_stat.st_mtime_ns}); the content-equality "
                f"guard is suppressing a legitimate write, which would leave clangd / IDE "
                f"indexers reading stale flags forever."
            )
            assert second_content != first_content, (
                "Content-changing rewrite left file content unchanged; the on-disk JSON "
                "is stale relative to the merged_commands the producer just rendered."
            )
            assert second_stat.st_size == len(second_content.encode("utf-8")), (
                f"File size {second_stat.st_size} does not match rendered content length "
                f"{len(second_content.encode('utf-8'))}; partial write or stale stat."
            )
            # Sanity: the new entry and modified arguments are actually present.
            files_present = {os.path.realpath(e["file"]) for e in second_entries}
            assert os.path.realpath(file_c) in files_present, (
                "Newly-added entry for c.cpp is missing from the rewritten database."
            )
            a_entry = next(e for e in second_entries if os.path.realpath(e["file"]) == os.path.realpath(file_a))
            assert "-O2" in a_entry["arguments"], (
                "Modified arguments for a.cpp were not persisted to the rewritten database."
            )
            # Inode change is incidental (atomic_write uses mkstemp + os.replace),
            # but verify it for symmetry with the no-op test's inode assertion.
            assert second_stat.st_ino != first_ino, (
                f"Content-changing rewrite preserved inode ({first_ino}); the producer should "
                f"have written via mkstemp + os.replace, allocating a fresh inode."
            )

    def test_changed_symlink_target_does_advance_mtime(self):
        """A target-changing _update_symlink call must replace the symlink.

        Companion to test_no_op_symlink_update_does_not_advance_mtime: that
        test verifies the no-op-skip path; this one verifies the symmetric
        positive case so an over-eager readlink-equality guard cannot silently
        skip a legitimate retarget. Skipping a real retarget would leave
        compile_commands.json pointing at the previous variant's database, so
        clangd would index against the wrong flags after a --variant switch.

        Same direct-invocation rationale as the no-op companion: _update_symlink
        reads only self.args.verbose from self, so __new__ + SimpleNamespace
        keeps the test independent of unrelated FileAnalyzer / Hunter changes.
        """

        with uth.TempDirContext() as _:
            cwd = os.getcwd()
            target_a = os.path.join(cwd, "compile_commands.gcc.debug.json")
            target_b = os.path.join(cwd, "compile_commands.clang.release.json")
            symlink_path = os.path.join(cwd, "compile_commands.json")

            for path in (target_a, target_b):
                with open(path, "w") as fh:
                    fh.write("[]")

            creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
                compiletools.compilation_database.CompilationDatabaseCreator
            )
            creator.args = types.SimpleNamespace(verbose=0)

            creator._update_symlink(symlink_path, target_a)
            assert os.path.islink(symlink_path), f"First _update_symlink call should create symlink at {symlink_path}"
            first_lstat = os.lstat(symlink_path)
            first_ino = first_lstat.st_ino
            first_mtime_ns = first_lstat.st_mtime_ns
            first_target = os.readlink(symlink_path)
            assert first_target == os.path.relpath(target_a, cwd), (
                f"First symlink target {first_target!r} does not match expected relative form of {target_a!r}"
            )

            # Sleep long enough that any replace advances mtime even on
            # filesystems with coarse mtime resolution.
            time.sleep(0.05)

            creator._update_symlink(symlink_path, target_b)

            second_lstat = os.lstat(symlink_path)
            second_target = os.readlink(symlink_path)
            expected_relative_b = os.path.relpath(target_b, cwd)

            assert second_target == expected_relative_b, (
                f"Target-changing _update_symlink left readlink unchanged "
                f"({first_target!r} -> {second_target!r}, expected {expected_relative_b!r}); "
                f"the readlink-equality guard is suppressing a legitimate retarget, which "
                f"would point compile_commands.json at the wrong variant's database."
            )
            assert second_lstat.st_mtime_ns > first_mtime_ns, (
                f"Target-changing _update_symlink did NOT advance mtime "
                f"({first_mtime_ns} -> {second_lstat.st_mtime_ns}); the symlink replace "
                f"path must run when the readlink target differs."
            )
            assert second_lstat.st_ino != first_ino, (
                f"Target-changing _update_symlink preserved symlink inode ({first_ino}); "
                f"os.symlink + os.replace must have allocated a fresh inode."
            )

    @uth.requires_functional_compiler
    def test_compilation_database_complex_incremental_scenarios(self):
        """Test more complex incremental scenarios: multiple updates, additions, and deletions"""

        with uth.TempDirContext() as _:
            # Copy initial set of files
            import shutil

            shutil.copy(uth.example_file("simple/helloworld_cpp.cpp"), ".")
            shutil.copy(uth.example_file("simple/helloworld_c.c"), ".")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"

                # Step 1: Initial database with 2 files
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            os.path.realpath("helloworld_cpp.cpp"),
                            os.path.realpath("helloworld_c.c"),
                        ]
                    )

                with open(comp_db_output) as f:
                    initial_db = json.load(f)
                assert len(initial_db) == 2, "Should have 2 initial entries"

                # Step 2: Add a new file
                shutil.copy(uth.example_file("factory/test_factory.cpp"), ".")
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            os.path.realpath("test_factory.cpp"),  # Only the new file
                        ]
                    )

                with open(comp_db_output) as f:
                    after_addition = json.load(f)
                assert len(after_addition) == 3, "Should have 3 entries after addition"
                files_after_addition = {os.path.basename(cmd["file"]) for cmd in after_addition}
                assert files_after_addition == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}

                # Step 3: Update multiple existing files simultaneously
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            os.path.realpath("helloworld_cpp.cpp"),
                            os.path.realpath("test_factory.cpp"),  # Update 2 files, preserve 1
                        ]
                    )

                with open(comp_db_output) as f:
                    after_multi_update = json.load(f)
                assert len(after_multi_update) == 3, "Should still have 3 entries after multi-update"
                files_after_multi = {os.path.basename(cmd["file"]) for cmd in after_multi_update}
                assert files_after_multi == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}

                # Step 4: Test with file that no longer exists (simulating deletion)
                # Remove one of the source files but keep it in database
                os.remove("test_factory.cpp")

                # Update only remaining files - the deleted file's entry should be preserved
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            "helloworld_c.c",  # Only update one existing file
                        ]
                    )

                with open(comp_db_output) as f:
                    after_deletion_test = json.load(f)

                # The deleted file should still be in the database (incremental update preserves it)
                assert len(after_deletion_test) == 3, "Should preserve entry for deleted file"
                files_after_deletion = {os.path.basename(cmd["file"]) for cmd in after_deletion_test}
                assert "test_factory.cpp" in files_after_deletion, "Deleted file entry should be preserved"

                # Step 5: Test empty update (no files specified) - should preserve all
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            # No files specified
                        ]
                    )

                with open(comp_db_output) as f:
                    after_empty_update = json.load(f)

                # With no files to update, existing database should be preserved
                # (though it might be empty if no auto-discovery happens)
                # The key is that it shouldn't corrupt the existing database
                assert isinstance(after_empty_update, list), "Should still be valid JSON array"

    @uth.requires_functional_compiler
    def test_compilation_database_stringzilla_performance_features(self):
        """Test that StringZilla optimizations are working correctly"""

        with uth.TempDirContext() as _:
            # Copy files for testing
            import shutil

            shutil.copy(uth.example_file("simple/helloworld_cpp.cpp"), ".")
            shutil.copy(uth.example_file("simple/helloworld_c.c"), ".")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"

                # Test StringZilla path cache functionality
                with uth.ParserContext():
                    cap = compiletools.apptools.create_parser("test", argv=["--config=" + temp_config_name])
                    compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                    compiletools.hunter.add_arguments(cap)
                    args = compiletools.apptools.parseargs(
                        cap, ["--config=" + temp_config_name], context=BuildContext()
                    )
                    # Create CompilationDatabaseCreator instance for testing
                    compiletools.compilation_database.CompilationDatabaseCreator(args, context=BuildContext())

                    # Test path normalization caching with enhanced wrappedos
                    path1 = compiletools.wrappedos.realpath("./test.cpp")
                    path2 = compiletools.wrappedos.realpath("./test.cpp")  # Should hit wrappedos cache
                    assert path1 == path2, "Path normalization should be consistent"

                    # Test StringZilla API - should leverage shared cache
                    import stringzilla as sz

                    path3_sz = compiletools.wrappedos.realpath_sz(sz.Str("./test.cpp"))
                    path3 = str(path3_sz)  # Convert for comparison
                    assert path1 == path3, "StringZilla and Python string should produce same result"

                    # Test that wrappedos lru_cache is working (cache_info available)
                    cache_info = compiletools.wrappedos.realpath.cache_info()
                    assert cache_info.hits >= 2, "wrappedos lru_cache should have multiple hits from shared usage"

                    # Test C++ file detection
                    assert compiletools.utils.is_cpp_source("test.cpp"), "Should detect .cpp as C++"
                    assert compiletools.utils.is_cpp_source("test.cxx"), "Should detect .cxx as C++"
                    assert not compiletools.utils.is_cpp_source("test.c"), "Should detect .c as C"
                    assert not compiletools.utils.is_cpp_source("test.h"), "Should not detect .h as source"

                # Create a compilation database to test StringZilla file handling
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            os.path.realpath("helloworld_cpp.cpp"),
                            os.path.realpath("helloworld_c.c"),
                        ]
                    )

                # Verify the database was created correctly
                assert os.path.exists(comp_db_output), "Compilation database should be created"

                with open(comp_db_output) as f:
                    commands = json.load(f)

                assert len(commands) == 2, "Should have 2 compilation commands"

                # Test StringZilla optimized incremental update
                # Record file size before update (for potential future use)
                os.path.getsize(comp_db_output)

                # Create another update to test the merging logic
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + comp_db_output,
                            os.path.realpath("helloworld_cpp.cpp"),  # Update just one file
                        ]
                    )

                # Verify incremental update preserved all entries
                with open(comp_db_output) as f:
                    updated_commands = json.load(f)

                assert len(updated_commands) == 2, "Incremental update should preserve all entries"

                # Verify StringZilla was used appropriately based on file size
                # (The actual StringZilla usage is internal, but we can verify the results are correct)

    def _extract_compile_commands_from_makefile(self):
        """Extract compilation commands from generated Makefile"""
        makefile_commands = {}

        # Find the Makefile
        filelist = os.listdir(".")
        makefile_name = next((f for f in filelist if f.startswith("Makefile")), None)
        assert makefile_name, "No Makefile found"

        # Parse Makefile to extract compile commands
        with open(makefile_name) as f:
            lines = f.readlines()

        import re

        source_token_re = re.compile(r".+\.(?:cpp|c|cc|cxx|C)\Z")

        for _line_num, line in enumerate(lines):
            original_line = line
            line = line.strip()

            # Look for compilation commands (lines starting with tab and containing compiler)
            if original_line.startswith("\t"):
                # Extract compiler command
                command = original_line[1:].strip()  # Remove tab
                if any(compiler in command for compiler in ["gcc", "g++", "clang", "clang++"]):
                    tokens = command.split()
                    # Only process commands that have -c flag (compilation, not linking)
                    if "-c" in tokens:
                        # Find the source file token: last whole-token ending in a C/C++ extension.
                        # Whole-token match avoids false positives like `master/.c` from a cwd path
                        # that contains `/.claude/`.
                        source_file = next(
                            (tok for tok in reversed(tokens) if source_token_re.match(tok)),
                            None,
                        )
                        if source_file:
                            makefile_commands[source_file] = tokens

        return makefile_commands

    def _assert_commands_equivalent(self, comp_db_commands, makefile_commands, source_files):
        """Assert that compilation database and Makefile commands are equivalent"""

        # Create mapping from source files to compilation database commands
        comp_db_by_file = {}
        for cmd in comp_db_commands:
            file_path = cmd["file"]
            # Normalize path to just filename for comparison
            filename = os.path.basename(file_path)
            comp_db_by_file[filename] = cmd["arguments"]

        # Normalize makefile commands by filename
        makefile_by_file = {}
        for source_file, command in makefile_commands.items():
            filename = os.path.basename(source_file)
            makefile_by_file[filename] = command

        # Check that we have commands for each source file
        for source_file in source_files:
            filename = os.path.basename(source_file)

            assert filename in comp_db_by_file, f"No compilation database entry for {filename}"
            assert filename in makefile_by_file, f"No Makefile command for {filename}"

            comp_db_args = comp_db_by_file[filename]
            makefile_args = makefile_by_file[filename]

            # Both should be compilation commands (contain -c flag)
            assert "-c" in comp_db_args, f"Compilation database missing -c flag for {filename}"
            assert "-c" in makefile_args, f"Makefile missing -c flag for {filename}"

            # Both should contain the source file
            source_found_in_comp_db = any(filename in arg for arg in comp_db_args)
            source_found_in_makefile = any(filename in arg for arg in makefile_args)

            assert source_found_in_comp_db, f"Source file {filename} not found in compilation database command"
            assert source_found_in_makefile, f"Source file {filename} not found in Makefile command"

            # Both should use the same compiler type (C vs C++)
            comp_db_compiler = comp_db_args[0]
            makefile_compiler = makefile_args[0]

            # Check compiler compatibility (both should be C++ or both C)
            is_cpp_file = filename.endswith((".cpp", ".cxx", ".cc", ".C", ".CC"))

            if is_cpp_file:
                assert any(cpp_compiler in comp_db_compiler for cpp_compiler in ["g++", "clang++", "c++"]), (
                    f"Expected C++ compiler for {filename}, got {comp_db_compiler}"
                )
                assert any(cpp_compiler in makefile_compiler for cpp_compiler in ["g++", "clang++", "c++"]), (
                    f"Expected C++ compiler for {filename}, got {makefile_compiler}"
                )
            else:
                assert any(c_compiler in comp_db_compiler for c_compiler in ["gcc", "clang"]) and not any(
                    cpp_compiler in comp_db_compiler for cpp_compiler in ["g++", "clang++", "c++"]
                ), f"Expected C compiler for {filename}, got {comp_db_compiler}"

    @uth.requires_functional_compiler
    def test_compile_commands_json_format_compliance(self):
        """Test that compile_commands.json follows clang specification exactly"""

        with uth.TempDirContext() as _:
            # Use our duplicate_flags sample to test both format and deduplication
            duplicate_flags_sample = uth.example_file("duplicate_flags/main.cpp")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Copy the sample to test directory
                import shutil

                shutil.copy(duplicate_flags_sample, "test_main.cpp")

                with uth.ParserContext():
                    output_file = "compile_commands_format.json"
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + output_file,
                            os.path.realpath("test_main.cpp"),
                        ]
                    )

                    # Verify file was created
                    assert os.path.exists(output_file)

                    # Read and parse JSON
                    with open(output_file) as f:
                        commands = json.load(f)

                    assert isinstance(commands, list), "Root should be JSON array"
                    assert len(commands) >= 1, "Should have at least one command"

                    for cmd in commands:
                        # Test required fields per clang spec
                        assert "directory" in cmd, "Missing required 'directory' field"
                        assert "file" in cmd, "Missing required 'file' field"
                        assert "arguments" in cmd, "Missing 'arguments' field (preferred over 'command')"

                        # Test field types
                        assert isinstance(cmd["directory"], str), "directory must be string"
                        assert isinstance(cmd["file"], str), "file must be string"
                        assert isinstance(cmd["arguments"], list), "arguments must be array"

                        # Test that arguments array is not empty
                        assert len(cmd["arguments"]) > 0, "arguments array must not be empty"

                        # Test compiler splitting - first argument should not be "ccache g++"
                        first_arg = cmd["arguments"][0]
                        assert " " not in first_arg or first_arg.startswith("/"), (
                            f"Compiler command improperly split: '{first_arg}' - should be ['ccache', 'g++'] not ['ccache g++']"
                        )

                        # Test for duplicate -isystem flags
                        args = cmd["arguments"]
                        isystem_paths = []
                        i = 0
                        while i < len(args):
                            if args[i] == "-isystem" and i + 1 < len(args):
                                isystem_paths.append(args[i + 1])
                                i += 2
                            elif args[i].startswith("-isystem") and len(args[i]) > 8:
                                isystem_paths.append(args[i][8:])
                                i += 1
                            else:
                                i += 1

                        # Check for duplicates
                        unique_isystem_paths = set(isystem_paths)
                        assert len(isystem_paths) == len(unique_isystem_paths), (
                            f"Duplicate -isystem paths found: {isystem_paths}"
                        )

                        # Test for duplicate -I flags
                        include_paths = []
                        i = 0
                        while i < len(args):
                            if args[i] == "-I" and i + 1 < len(args):
                                include_paths.append(args[i + 1])
                                i += 2
                            elif args[i].startswith("-I") and len(args[i]) > 2:
                                include_paths.append(args[i][2:])
                                i += 1
                            else:
                                i += 1

                        unique_include_paths = set(include_paths)
                        assert len(include_paths) == len(unique_include_paths), (
                            f"Duplicate -I paths found: {include_paths}"
                        )

                        print(f"✓ Command format valid for {cmd['file']}")
                        print(f"  Compiler: {first_arg}")
                        print(f"  Include paths: {include_paths}")
                        print(f"  System include paths: {isystem_paths}")

                    print("✓ All compile_commands.json format compliance tests passed!")


def _concurrent_write_worker(work_queue, result_queue, source_file, output_file):
    """Worker process for concurrent compilation database writes.

    Uses queues for deterministic coordination to avoid flaky tests.
    """
    import compiletools.apptools
    import compiletools.compilation_database
    import compiletools.hunter

    # Wait for signal to start (all workers ready)
    work_queue.get()

    try:
        # Create args object
        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("test")
            compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
            compiletools.hunter.add_arguments(cap)
            args = compiletools.apptools.parseargs(
                cap,
                ["--file-locking", "--compilation-database-output=" + output_file, source_file],
                context=BuildContext(),
            )

            # Write compilation database
            creator = compiletools.compilation_database.CompilationDatabaseCreator(args, context=BuildContext())
            creator.write_compilation_database()

            result_queue.put(("success", source_file))
    except Exception as e:
        result_queue.put(("error", str(e)))


class TestConcurrentCompilationDatabase:
    """Tests for concurrent compilation database writes."""

    def setup_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_concurrent_compilation_database_writes(self):
        """Test that concurrent writes don't corrupt compile_commands.json.

        Uses multiprocessing with barriers to ensure deterministic timing
        and avoid flaky test failures in CI.
        """
        import multiprocessing

        # Use spawn method to avoid fork() deprecation warnings
        ctx = multiprocessing.get_context("spawn")

        with uth.TempDirContext():
            # Create test source files
            source1 = "test1.cpp"
            source2 = "test2.cpp"
            with open(source1, "w") as f:
                f.write("int main() { return 1; }\n")
            with open(source2, "w") as f:
                f.write("int main() { return 2; }\n")

            output_file = "compile_commands.json"

            # Coordination queues for deterministic test execution
            work_queue = ctx.Queue()
            result_queue = ctx.Queue()

            # Launch worker processes
            num_workers = 2
            processes = []
            for source in [source1, source2]:
                p = ctx.Process(
                    target=_concurrent_write_worker,
                    args=(work_queue, result_queue, source, output_file),
                )
                p.start()
                processes.append(p)

            # Signal all workers to start simultaneously (barrier pattern)
            for _ in range(num_workers):
                work_queue.put("start")

            # Collect results with timeout to avoid CI hangs
            results = []
            for _ in range(num_workers):
                try:
                    result = result_queue.get(timeout=30)
                    results.append(result)
                except Exception:
                    # Timeout - kill all processes
                    for p in processes:
                        if p.is_alive():
                            p.terminate()
                    assert False, "Worker timeout - possible deadlock in locking code"

            # Wait for processes to complete
            for p in processes:
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
                    assert False, "Worker process didn't terminate - possible lock leak"

            # Verify all workers succeeded
            for status, info in results:
                assert status == "success", f"Worker failed: {info}"

            # Verify compile_commands.json is valid JSON (not corrupted)
            assert os.path.exists(output_file), "compile_commands.json was not created"
            with open(output_file) as f:
                compile_commands = json.load(f)

            # Verify it contains entries for both source files
            files_in_db = {entry["file"] for entry in compile_commands}
            assert source1 in files_in_db or os.path.realpath(source1) in files_in_db, (
                f"{source1} not found in compilation database"
            )
            assert source2 in files_in_db or os.path.realpath(source2) in files_in_db, (
                f"{source2} not found in compilation database"
            )

            # Verify no duplicate entries (corruption symptom)
            assert len(compile_commands) == len(files_in_db), (
                f"Duplicate entries found - possible write corruption. "
                f"Entries: {len(compile_commands)}, Unique files: {len(files_in_db)}"
            )

            print(f"✓ Concurrent write test passed with {len(compile_commands)} entries")

    @uth.requires_functional_compiler
    def test_merge_respects_existing_directory_field(self):
        """Test that merge resolves relative paths against their "directory" field, not cwd"""

        with uth.TempDirContext():
            # TempDirContext changes to the temp directory
            tmpdir = os.getcwd()

            # Create a different working directory to expose the bug
            other_dir = os.path.join(tmpdir, "other_location")
            os.makedirs(other_dir)

            # Create a fake existing compile_commands.json with relative paths
            # simulating what another tool or previous run might have created
            existing_db_path = os.path.join(tmpdir, "compile_commands.json")
            existing_db = [
                {
                    "directory": "/some/other/project",
                    "file": "src/foo.cpp",  # Relative path
                    "arguments": ["g++", "-c", "src/foo.cpp"],
                },
                {
                    "directory": "/another/project",
                    "file": "lib/bar.cpp",  # Relative path
                    "arguments": ["g++", "-c", "lib/bar.cpp"],
                },
            ]
            with open(existing_db_path, "w") as f:
                json.dump(existing_db, f)

            # Now create a new entry from a different directory
            test_file = uth.example_file("simple/helloworld_cpp.cpp")

            with uth.TempConfigContext(tempdir=tmpdir) as temp_config_name, uth.ParserContext():
                # Change to other_dir to make cwd different from existing entries
                old_cwd = os.getcwd()
                try:
                    os.chdir(other_dir)

                    # Update compilation database with new file
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--compilation-database-output=" + existing_db_path, test_file]
                    )

                    # Read the merged database
                    with open(existing_db_path) as f:
                        merged_db = json.load(f)

                    # Critical: existing entries should be preserved
                    # Bug would cause them to be lost due to incorrect path resolution
                    assert len(merged_db) == 3, f"Expected 3 entries (2 old + 1 new), got {len(merged_db)}"

                    # Verify the old entries are still there with correct paths
                    files_in_db = {cmd["file"] for cmd in merged_db}
                    assert "src/foo.cpp" in files_in_db, "Relative path src/foo.cpp should be preserved"
                    assert "lib/bar.cpp" in files_in_db, "Relative path lib/bar.cpp should be preserved"

                    # Verify directories are preserved
                    dirs_in_db = {cmd["directory"] for cmd in merged_db}
                    assert "/some/other/project" in dirs_in_db
                    assert "/another/project" in dirs_in_db

                finally:
                    os.chdir(old_cwd)

    @uth.requires_functional_compiler
    def test_default_pathname_is_variant_qualified(self):
        """Without --compilation-database-output, the file is compile_commands.<variant>.json
        and a sibling compile_commands.json symlink points at it."""

        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")

                with uth.ParserContext():
                    compiletools.compilation_database.main(["--config=" + temp_config_name, "--variant=gcc.debug", src])

                cwd = os.getcwd()
                variant_path = os.path.join(cwd, "compile_commands.gcc.debug.json")
                symlink_path = os.path.join(cwd, "compile_commands.json")

                assert os.path.exists(variant_path), (
                    f"Expected variant-qualified DB at {variant_path}; directory contains: {sorted(os.listdir(cwd))}"
                )
                assert os.path.islink(symlink_path), f"Expected {symlink_path} to be a symlink"
                # Symlink target must be relative for portability
                target = os.readlink(symlink_path)
                assert target == "compile_commands.gcc.debug.json", (
                    f"Symlink target should be relative basename, got {target!r}"
                )
                # And it must point at a real file
                assert os.path.realpath(symlink_path) == os.path.realpath(variant_path)

    @uth.requires_functional_compiler
    def test_symlink_repoints_on_subsequent_variant_build(self):
        """Building variant A then variant B leaves the symlink pointing at B,
        with both per-variant files preserved."""

        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")

                with uth.ParserContext():
                    compiletools.compilation_database.main(["--config=" + temp_config_name, "--variant=gcc.debug", src])
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--variant=clang.release", src]
                    )

                cwd = os.getcwd()
                debug_path = os.path.join(cwd, "compile_commands.gcc.debug.json")
                release_path = os.path.join(cwd, "compile_commands.clang.release.json")
                symlink_path = os.path.join(cwd, "compile_commands.json")

                # Both per-variant files survive
                assert os.path.exists(debug_path)
                assert os.path.exists(release_path)
                # Symlink follows the most recent build
                assert os.path.islink(symlink_path)
                assert os.readlink(symlink_path) == "compile_commands.clang.release.json"

    @uth.requires_functional_compiler
    def test_explicit_output_path_skips_symlink(self):
        """When the user supplies --compilation-database-output, no per-variant
        renaming and no symlink update happens — the literal path is honored."""

        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")
                explicit = "explicit_output.json"

                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--variant=gcc.debug",
                            "--compilation-database-output=" + explicit,
                            src,
                        ]
                    )

                cwd = os.getcwd()
                # Explicit path got the data
                assert os.path.exists(os.path.join(cwd, explicit))
                # No variant-qualified file or symlink was created
                assert not os.path.exists(os.path.join(cwd, "compile_commands.json"))
                assert not os.path.exists(os.path.join(cwd, "compile_commands.gcc.debug.json"))

    @uth.requires_functional_compiler
    def test_symlink_replaces_pre_existing_regular_file(self):
        """If compile_commands.json already exists as a regular file (e.g. from a
        pre-upgrade install), we replace it with a symlink rather than appending
        to it as if it were our merge target."""

        with uth.TempDirContext():
            cwd = os.getcwd()
            stale_path = os.path.join(cwd, "compile_commands.json")
            # Pre-existing regular file with garbage content
            with open(stale_path, "w") as f:
                f.write("[]")
            assert not os.path.islink(stale_path)

            with uth.TempConfigContext(tempdir=cwd) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")
                with uth.ParserContext():
                    compiletools.compilation_database.main(["--config=" + temp_config_name, "--variant=gcc.debug", src])

                assert os.path.islink(stale_path), (
                    "Pre-existing regular compile_commands.json must be replaced by a symlink"
                )
                assert os.readlink(stale_path) == "compile_commands.gcc.debug.json"

    @uth.requires_functional_compiler
    def test_multi_axis_variant_canonicalizes_in_filename(self):
        """A composite --variant (comma-separated or unsorted) canonicalizes
        before reaching the compilation-database output path, so the file is
        named compile_commands.<canonical>.json with the canonical dotted
        form and the bare-name symlink follows."""
        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")

                with uth.ParserContext():
                    # Comma-separated, non-canonical order: asan,release,mold,gcc.
                    # Should canonicalize to gcc.mold.release.asan via the
                    # variant-canonical-order in the bundled ct.conf.
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--variant=asan,release,mold,gcc", src]
                    )

                cwd = os.getcwd()
                variant_path = os.path.join(cwd, "compile_commands.gcc.mold.release.asan.json")
                symlink_path = os.path.join(cwd, "compile_commands.json")

                assert os.path.exists(variant_path), (
                    f"Expected canonical-name DB at {variant_path}; directory contains: {sorted(os.listdir(cwd))}"
                )
                assert os.path.islink(symlink_path), f"Expected {symlink_path} to be a symlink"
                # Symlink target must be the canonical-name relative basename.
                assert os.readlink(symlink_path) == "compile_commands.gcc.mold.release.asan.json"
                # And dot-separated equivalent input picks up the same file (idempotent).
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--variant=gcc.mold.release.asan", src]
                    )
                # No new variant-qualified file should appear — same canonical name.
                # (The bare compile_commands.json symlink doesn't count — that's the
                # consumer-facing pointer maintained by every build.)
                json_files = sorted(
                    f
                    for f in os.listdir(cwd)
                    if f.startswith("compile_commands.")
                    and f.endswith(".json")
                    and not os.path.islink(os.path.join(cwd, f))
                )
                assert json_files == ["compile_commands.gcc.mold.release.asan.json"], (
                    f"Composite variant should canonicalize to a single output filename; got {json_files}"
                )

    @uth.requires_functional_compiler
    def test_multi_axis_symlink_swaps_between_composites(self):
        """Two different multi-axis composites produce two side-by-side
        per-variant files; the symlink follows the most recent build."""
        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")

                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--variant=gcc,mold,debug", src]
                    )
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        ["--config=" + temp_config_name, "--variant=clang,gold,release,asan", src]
                    )

                cwd = os.getcwd()
                debug_path = os.path.join(cwd, "compile_commands.gcc.mold.debug.json")
                release_path = os.path.join(cwd, "compile_commands.clang.gold.release.asan.json")
                symlink_path = os.path.join(cwd, "compile_commands.json")

                # Both per-variant files survive
                assert os.path.exists(debug_path), f"missing {debug_path}: {sorted(os.listdir(cwd))}"
                assert os.path.exists(release_path), f"missing {release_path}: {sorted(os.listdir(cwd))}"
                # Symlink follows the most recent build (the 4-axis one).
                assert os.path.islink(symlink_path)
                assert os.readlink(symlink_path) == "compile_commands.clang.gold.release.asan.json"


class TestCompilationDatabaseModuleFlags:
    """C++20 modules: TU-level flags must appear in compile_commands.json
    so clangd / clang-tidy can resolve `import M;` / `import <h>;` lookups.
    Without them, IDEs report module imports as undefined."""

    def setup_method(self):
        uth.reset()

    def test_gcc_module_importer_gets_fmodules_ts(self):
        """A TU with `import math;` compiled under gcc must carry -fmodules-ts
        in its compile_commands.json entry; without it clangd / clang-tidy
        report the import as undefined. Unit-level test (no compiler required):
        constructs a CompilationDatabaseCreator and stubs the file-analysis
        result for a gcc CXX, mirroring test_clang_header_unit_importer_gets_fmodules.
        Avoiding the end-to-end path is deliberate: the CDB-on-disk variant
        only worked when get_functional_cxx_compiler() returned a g++ that
        accepted -std=c++20, which is environment-dependent."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
            compiletools.compilation_database.CompilationDatabaseCreator
        )
        creator.args = SimpleNamespace(CXX="g++")
        creator.hunter = MagicMock()
        creator.hunter._file_analysis_result = MagicMock(
            return_value=SimpleNamespace(
                module_exports=(),
                module_implements=(),
                module_imports=("math",),
                module_header_imports=(),
            )
        )

        flags = creator._module_kind_flags("/src/main.cpp")
        assert "-fmodules-ts" in flags, f"gcc TU with import math; needs -fmodules-ts, got {flags!r}"

    def test_clang_header_unit_importer_gets_fmodules(self):
        """A TU with `import <vector>;` compiled under clang must carry -fmodules
        in its compile_commands.json entry; without it clangd treats the import
        as an unknown header unit. Unit-level test (no compiler required):
        constructs a CompilationDatabaseCreator and stubs the file-analysis
        result for a clang CXX."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
            compiletools.compilation_database.CompilationDatabaseCreator
        )
        creator.args = SimpleNamespace(CXX="clang++")
        creator.hunter = MagicMock()
        creator.hunter._file_analysis_result = MagicMock(
            return_value=SimpleNamespace(
                module_exports=(),
                module_implements=(),
                module_imports=(),
                module_header_imports=("<vector>",),
            )
        )

        flags = creator._module_kind_flags("/src/main.cpp")
        assert "-fmodules" in flags, f"clang TU with import <vector>; needs -fmodules, got {flags!r}"

    def test_clang_import_std_gets_stdlib_libcxx(self):
        """`import std;` under clang requires -stdlib=libc++; the system std
        module is libc++-shipped today."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        creator = compiletools.compilation_database.CompilationDatabaseCreator.__new__(
            compiletools.compilation_database.CompilationDatabaseCreator
        )
        creator.args = SimpleNamespace(CXX="clang++")
        creator.hunter = MagicMock()
        creator.hunter._file_analysis_result = MagicMock(
            return_value=SimpleNamespace(
                module_exports=(),
                module_implements=(),
                module_imports=("std",),
                module_header_imports=(),
            )
        )

        flags = creator._module_kind_flags("/src/main.cpp")
        assert "-stdlib=libc++" in flags, f"clang TU with import std; needs -stdlib=libc++, got {flags!r}"

    @uth.requires_functional_compiler
    def test_non_module_tu_unchanged(self):
        """A TU with no module activity must NOT pick up module flags."""
        with uth.TempDirContext():
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                src = uth.example_file("simple/helloworld_cpp.cpp")
                with uth.ParserContext():
                    compiletools.compilation_database.main(
                        [
                            "--config=" + temp_config_name,
                            "--variant=gcc.debug",
                            src,
                        ]
                    )

                cdb_path = os.path.join(os.getcwd(), "compile_commands.gcc.debug.json")
                with open(cdb_path) as f:
                    entries = json.load(f)

                cpp_entries = [e for e in entries if e["file"].endswith("helloworld_cpp.cpp")]
                assert len(cpp_entries) == 1
                args = cpp_entries[0]["arguments"]
                for flag in ("-fmodules-ts", "-fmodules", "-stdlib=libc++"):
                    assert flag not in args, f"non-module TU should not have {flag}; args={args!r}"
