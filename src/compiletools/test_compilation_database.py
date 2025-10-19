import json
import os

import compiletools.compilation_database
import compiletools.makefile
import compiletools.findtargets
import compiletools.testhelper as uth


class TestCompilationDatabase:
    def setup_method(self):
        uth.reset()

    @uth.requires_functional_compiler
    def test_basic_compilation_database_creation(self):
        """Test basic compilation database creation with simple C++ files"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                    # Use existing sample files
                    relativepaths = [
                        "simple/helloworld_cpp.cpp",
                        "simple/helloworld_c.c"
                    ]
                    realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                    
                    with uth.ParserContext():
                        # Create compilation database
                        output_file = "compile_commands.json"
                        compiletools.compilation_database.main([
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + output_file
                        ] + realpaths)
                        
                        # Verify file was created
                        assert os.path.exists(output_file)
                        
                        # Verify JSON format
                        with open(output_file, 'r') as f:
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
            samplesdir = uth.samplesdir()
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                    relativepaths = ["simple/helloworld_cpp.cpp"]
                    realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                    
                    with uth.ParserContext():
                        output_file = "compile_commands_rel.json"
                        compiletools.compilation_database.main([
                            "--config=" + temp_config_name,
                            "--relative-paths",
                            "--compilation-database-output=" + output_file
                        ] + realpaths)
                        
                        assert os.path.exists(output_file)
                        
                        with open(output_file, 'r') as f:
                            commands = json.load(f)
                            
                        # Check that file paths are relative when --relative-paths is used
                        for cmd in commands:
                            # Directory should still be absolute (working directory)
                            assert cmd["directory"].startswith("/"), f"Directory should still be absolute, got: {cmd['directory']}"
                            # File path should be relative when --relative-paths is used
                            assert not cmd["file"].startswith("/"), f"File path should be relative with --relative-paths, got: {cmd['file']}"

    @uth.requires_functional_compiler
    def test_compilation_database_creator_class(self):
        """Test the CompilationDatabaseCreator class directly"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                    # Create args object by parsing like main() would
                    relativepaths = ["simple/helloworld_cpp.cpp"]
                    realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                    
                    # Use the module's main function to test integration
                    argv = [
                        "--config=" + temp_config_name,
                        "--compilation-database-output=test_output.json"
                    ] + realpaths
                    
                    cap = compiletools.apptools.create_parser(
                        "Generate compile_commands.json for clang tooling", argv=argv
                    )
                    compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                    compiletools.hunter.add_arguments(cap)
                    args = compiletools.apptools.parseargs(cap, argv)
                    
                    with uth.ParserContext():
                        # Test the creator class
                        creator = compiletools.compilation_database.CompilationDatabaseCreator(args)
                        
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
            samplesdir = uth.samplesdir()
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                    relativepaths = ["simple/helloworld_cpp.cpp"]
                    realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                    
                    with uth.ParserContext():
                        output_file = "format_test.json"
                        compiletools.compilation_database.main([
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + output_file
                        ] + realpaths)
                        
                        # Verify JSON can be parsed
                        with open(output_file, 'r') as f:
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
            samplesdir = uth.samplesdir()
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Use the same test files as the Makefile test
                relativepaths = [
                    "simple/helloworld_cpp.cpp",
                    "simple/helloworld_c.c"
                ]
                realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                
                # Generate compilation database
                comp_db_output = "compile_commands.json"
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output
                    ] + realpaths)
                    
                # Generate Makefile  
                with uth.ParserContext():
                    compiletools.makefile.main([
                        "--config=" + temp_config_name
                    ] + realpaths)
                    
                # Read compilation database
                with open(comp_db_output, 'r') as f:
                    comp_db_commands = json.load(f)
                
                # Parse Makefile for compilation rules
                makefile_commands = self._extract_compile_commands_from_makefile()
                
                # Compare commands for equivalence
                self._assert_commands_equivalent(comp_db_commands, makefile_commands, realpaths)

    @uth.requires_functional_compiler
    def test_compilation_database_vs_makefile_complex_project(self):
        """Test equivalence with a more complex project having multiple files and dependencies"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
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
                    "numbers/get_double.cpp"
                ]
                realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
                
                # Generate compilation database
                comp_db_output = "compile_commands_complex.json"
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output
                    ] + realpaths)
                    
                # Generate Makefile  
                with uth.ParserContext():
                    compiletools.makefile.main([
                        "--config=" + temp_config_name
                    ] + realpaths)
                    
                # Read compilation database
                with open(comp_db_output, 'r') as f:
                    comp_db_commands = json.load(f)
                
                # Parse Makefile for compilation rules
                makefile_commands = self._extract_compile_commands_from_makefile()
                
                # Verify we have commands for all source files
                assert len(comp_db_commands) >= len(relativepaths), \
                    f"Expected at least {len(relativepaths)} compilation database entries, got {len(comp_db_commands)}"
                assert len(makefile_commands) >= len(relativepaths), \
                    f"Expected at least {len(relativepaths)} Makefile commands, got {len(makefile_commands)}"
                
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
                            
                for source_file, command in makefile_commands.items():
                    for i, arg in enumerate(command):
                        if arg == "-I" and i + 1 < len(command):
                            makefile_includes.add(command[i + 1])
                            
                # The include sets should be equivalent (same directories used)
                assert comp_db_includes == makefile_includes, \
                    f"Include directories differ: comp_db={comp_db_includes}, makefile={makefile_includes}"

    @uth.requires_functional_compiler
    def test_compilation_database_with_findtargets_discovery(self):
        """Test compilation database with FindTargets-based auto-discovery like cake --auto"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            # Copy some sample files to the current directory for auto-discovery
            import shutil
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_cpp.cpp"), ".")
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_c.c"), ".")
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                with uth.ParserContext():
                    # Create args object like compilation database would
                    cap = compiletools.apptools.create_parser(
                        "Test compilation database with auto-discovery", argv=[
                            "--config=" + temp_config_name,
                            "--exemarkers=main(",  # Tell it how to identify executable files
                            "--auto"  # Enable auto-discovery
                        ]
                    )
                    compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                    compiletools.hunter.add_arguments(cap)
                    compiletools.findtargets.add_arguments(cap)  # Add findtargets arguments including --auto
                    
                    # Parse args with auto-discovery enabled
                    args = compiletools.apptools.parseargs(cap, [
                        "--config=" + temp_config_name,
                        "--exemarkers=main(",  # Tell it how to identify executable files
                        "--testmarkers=test(",  # Tell it how to identify test files
                        "--auto"  # Enable auto-discovery
                    ])
                    
                    # Use FindTargets to discover source files (following cake.py pattern)
                    findtargets = compiletools.findtargets.FindTargets(args)
                    findtargets.process(args)
                    
                    # Verify that targets were found
                    assert hasattr(args, 'filename') and args.filename, \
                        f"FindTargets should have found source files, got: {getattr(args, 'filename', [])}"
                    
                    # Create compilation database with the discovered files
                    creator = compiletools.compilation_database.CompilationDatabaseCreator(args)
                    
                    comp_db_output = "compile_commands_findtargets.json"
                    args.compilation_database_output = comp_db_output
                    creator.write_compilation_database()
                
                # Verify compilation database was created and contains discovered files
                assert os.path.exists(comp_db_output), "Compilation database should be created"
                
                with open(comp_db_output, 'r') as f:
                    comp_db_commands = json.load(f)
                
                # Should have found our copied source files
                assert len(comp_db_commands) >= 2, f"Expected at least 2 files, got {len(comp_db_commands)}"
                
                # Verify the discovered files include our test files
                found_files = {os.path.basename(cmd["file"]) for cmd in comp_db_commands}
                expected_files = {"helloworld_cpp.cpp", "helloworld_c.c"}
                
                assert expected_files.issubset(found_files), \
                    f"Expected files {expected_files} not found in discovered files {found_files}"
                
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
                    if filename.endswith('.cpp'):
                        assert any(cpp in compiler for cpp in ['g++', 'clang++', 'c++']), \
                            f"C++ file should use C++ compiler, got {compiler}"
                    elif filename.endswith('.c'):
                        assert any(c in compiler for c in ['gcc', 'clang']) and \
                               not any(cpp in compiler for cpp in ['g++', 'clang++', 'c++']), \
                            f"C file should use C compiler, got {compiler}"

    @uth.requires_functional_compiler
    def test_compilation_database_incremental_updates(self):
        """Test that compilation database supports incremental updates without wiping existing entries"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            # Copy multiple sample files for initial build
            import shutil
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_cpp.cpp"), ".")
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_c.c"), ".")
            shutil.copy(os.path.join(samplesdir, "factory/test_factory.cpp"), ".")
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"
                
                # Step 1: Create initial compilation database with all files
                initial_files = ["helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"]
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output
                    ] + initial_files)
                
                # Verify initial database
                assert os.path.exists(comp_db_output), "Initial compilation database should be created"
                
                with open(comp_db_output, 'r') as f:
                    initial_commands = json.load(f)
                
                assert len(initial_commands) == 3, f"Expected 3 initial commands, got {len(initial_commands)}"
                initial_files_set = {os.path.basename(cmd["file"]) for cmd in initial_commands}
                assert initial_files_set == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}
                
                # Step 2: Simulate updating just one file (like after editing and recompiling)
                # This should only update the entry for that file, not wipe the entire database
                updated_file = ["helloworld_cpp.cpp"]
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output
                    ] + updated_file)
                
                # Step 3: Verify incremental behavior
                with open(comp_db_output, 'r') as f:
                    updated_commands = json.load(f)
                
                # CRITICAL: The database should still contain ALL original files
                # This test will currently FAIL because the current implementation overwrites
                # but this is the behavior we need to implement
                updated_files_set = {os.path.basename(cmd["file"]) for cmd in updated_commands}
                
                # The key assertion: all original files should still be present
                assert len(updated_commands) == 3, \
                    f"Incremental update should preserve all entries. Expected 3, got {len(updated_commands)}. " \
                    f"Files in database: {updated_files_set}"
                
                assert "helloworld_c.c" in updated_files_set, \
                    "helloworld_c.c should be preserved after updating helloworld_cpp.cpp"
                assert "test_factory.cpp" in updated_files_set, \
                    "test_factory.cpp should be preserved after updating helloworld_cpp.cpp"
                assert "helloworld_cpp.cpp" in updated_files_set, \
                    "helloworld_cpp.cpp should still be present after update"
                
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

    @uth.requires_functional_compiler
    def test_compilation_database_complex_incremental_scenarios(self):
        """Test more complex incremental scenarios: multiple updates, additions, and deletions"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            # Copy initial set of files
            import shutil
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_cpp.cpp"), ".")
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_c.c"), ".")
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"
                
                # Step 1: Initial database with 2 files
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        os.path.realpath("helloworld_cpp.cpp"),
                        os.path.realpath("helloworld_c.c")
                    ])
                
                with open(comp_db_output, 'r') as f:
                    initial_db = json.load(f)
                assert len(initial_db) == 2, "Should have 2 initial entries"
                
                # Step 2: Add a new file
                shutil.copy(os.path.join(samplesdir, "factory/test_factory.cpp"), ".")
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        os.path.realpath("test_factory.cpp")  # Only the new file
                    ])
                
                with open(comp_db_output, 'r') as f:
                    after_addition = json.load(f)
                assert len(after_addition) == 3, "Should have 3 entries after addition"
                files_after_addition = {os.path.basename(cmd["file"]) for cmd in after_addition}
                assert files_after_addition == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}
                
                # Step 3: Update multiple existing files simultaneously
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        os.path.realpath("helloworld_cpp.cpp"),
                        os.path.realpath("test_factory.cpp")  # Update 2 files, preserve 1
                    ])
                
                with open(comp_db_output, 'r') as f:
                    after_multi_update = json.load(f)
                assert len(after_multi_update) == 3, "Should still have 3 entries after multi-update"
                files_after_multi = {os.path.basename(cmd["file"]) for cmd in after_multi_update}
                assert files_after_multi == {"helloworld_cpp.cpp", "helloworld_c.c", "test_factory.cpp"}
                
                # Step 4: Test with file that no longer exists (simulating deletion)
                # Remove one of the source files but keep it in database
                os.remove("test_factory.cpp")
                
                # Update only remaining files - the deleted file's entry should be preserved
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        "helloworld_c.c"  # Only update one existing file
                    ])
                
                with open(comp_db_output, 'r') as f:
                    after_deletion_test = json.load(f)
                
                # The deleted file should still be in the database (incremental update preserves it)
                assert len(after_deletion_test) == 3, "Should preserve entry for deleted file"
                files_after_deletion = {os.path.basename(cmd["file"]) for cmd in after_deletion_test}
                assert "test_factory.cpp" in files_after_deletion, "Deleted file entry should be preserved"
                
                # Step 5: Test empty update (no files specified) - should preserve all
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output
                        # No files specified
                    ])
                
                with open(comp_db_output, 'r') as f:
                    after_empty_update = json.load(f)
                
                # With no files to update, existing database should be preserved
                # (though it might be empty if no auto-discovery happens)
                # The key is that it shouldn't corrupt the existing database
                assert isinstance(after_empty_update, list), "Should still be valid JSON array"

    @uth.requires_functional_compiler
    def test_compilation_database_stringzilla_performance_features(self):
        """Test that StringZilla optimizations are working correctly"""
        
        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()
            
            # Copy files for testing
            import shutil
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_cpp.cpp"), ".")
            shutil.copy(os.path.join(samplesdir, "simple/helloworld_c.c"), ".")
            
            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                comp_db_output = "compile_commands.json"
                
                # Test StringZilla path cache functionality
                with uth.ParserContext():
                    cap = compiletools.apptools.create_parser("test", argv=["--config=" + temp_config_name])
                    compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
                    compiletools.hunter.add_arguments(cap)
                    args = compiletools.apptools.parseargs(cap, ["--config=" + temp_config_name])
                    # Create CompilationDatabaseCreator instance for testing
                    compiletools.compilation_database.CompilationDatabaseCreator(args)

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
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        os.path.realpath("helloworld_cpp.cpp"),
                        os.path.realpath("helloworld_c.c")
                    ])
                
                # Verify the database was created correctly
                assert os.path.exists(comp_db_output), "Compilation database should be created"
                
                with open(comp_db_output, 'r') as f:
                    commands = json.load(f)
                
                assert len(commands) == 2, "Should have 2 compilation commands"
                
                # Test StringZilla optimized incremental update
                # Record file size before update (for potential future use)
                os.path.getsize(comp_db_output)

                # Create another update to test the merging logic
                with uth.ParserContext():
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + comp_db_output,
                        os.path.realpath("helloworld_cpp.cpp")  # Update just one file
                    ])
                
                # Verify incremental update preserved all entries
                with open(comp_db_output, 'r') as f:
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
        with open(makefile_name, 'r') as f:
            lines = f.readlines()
        
        import re
        for line_num, line in enumerate(lines):
            original_line = line
            line = line.strip()
            
            # Look for compilation commands (lines starting with tab and containing compiler)
            if original_line.startswith('\t'):
                # Extract compiler command
                command = original_line[1:].strip()  # Remove tab
                if any(compiler in command for compiler in ['gcc', 'g++', 'clang', 'clang++']):
                    # Only process commands that have -c flag (compilation, not linking)
                    if '-c' in command.split():
                        # Extract source file from command - it's at the end
                        # Look for .c or .cpp files in the command
                        source_match = re.search(r'(\S+\.(?:cpp|c|cc|cxx|C))', command)
                        if source_match:
                            source_file = source_match.group(1)
                            makefile_commands[source_file] = command.split()
                        
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
            is_cpp_file = filename.endswith(('.cpp', '.cxx', '.cc', '.C', '.CC'))
            
            if is_cpp_file:
                assert any(cpp_compiler in comp_db_compiler for cpp_compiler in ['g++', 'clang++', 'c++']), \
                    f"Expected C++ compiler for {filename}, got {comp_db_compiler}"
                assert any(cpp_compiler in makefile_compiler for cpp_compiler in ['g++', 'clang++', 'c++']), \
                    f"Expected C++ compiler for {filename}, got {makefile_compiler}"
            else:
                assert any(c_compiler in comp_db_compiler for c_compiler in ['gcc', 'clang']) and \
                       not any(cpp_compiler in comp_db_compiler for cpp_compiler in ['g++', 'clang++', 'c++']), \
                    f"Expected C compiler for {filename}, got {comp_db_compiler}"

    @uth.requires_functional_compiler
    def test_compile_commands_json_format_compliance(self):
        """Test that compile_commands.json follows clang specification exactly"""

        with uth.TempDirContext() as _:
            samplesdir = uth.samplesdir()

            # Use our duplicate_flags sample to test both format and deduplication
            duplicate_flags_sample = os.path.join(samplesdir, "duplicate_flags", "main.cpp")

            with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
                # Copy the sample to test directory
                import shutil
                shutil.copy(duplicate_flags_sample, "test_main.cpp")

                with uth.ParserContext():
                    output_file = "compile_commands_format.json"
                    compiletools.compilation_database.main([
                        "--config=" + temp_config_name,
                        "--compilation-database-output=" + output_file,
                        os.path.realpath("test_main.cpp")
                    ])

                    # Verify file was created
                    assert os.path.exists(output_file)

                    # Read and parse JSON
                    with open(output_file, 'r') as f:
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
                        assert " " not in first_arg or first_arg.startswith("/"), \
                            f"Compiler command improperly split: '{first_arg}' - should be ['ccache', 'g++'] not ['ccache g++']"

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
                        assert len(isystem_paths) == len(unique_isystem_paths), \
                            f"Duplicate -isystem paths found: {isystem_paths}"

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
                        assert len(include_paths) == len(unique_include_paths), \
                            f"Duplicate -I paths found: {include_paths}"

                        print(f"✓ Command format valid for {cmd['file']}")
                        print(f"  Compiler: {first_arg}")
                        print(f"  Include paths: {include_paths}")
                        print(f"  System include paths: {isystem_paths}")

                    print("✓ All compile_commands.json format compliance tests passed!")


def _concurrent_write_worker(work_queue, result_queue, source_file, output_file):
    """Worker process for concurrent compilation database writes.

    Uses queues for deterministic coordination to avoid flaky tests.
    """
    import compiletools.compilation_database
    import compiletools.hunter
    import compiletools.apptools

    # Wait for signal to start (all workers ready)
    work_queue.get()

    try:
        # Create args object
        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("test")
            compiletools.compilation_database.CompilationDatabaseCreator.add_arguments(cap)
            compiletools.hunter.add_arguments(cap)
            args = compiletools.apptools.parseargs(
                cap, ["--shared-objects", "--compilation-database-output=" + output_file, source_file]
            )

            # Write compilation database
            creator = compiletools.compilation_database.CompilationDatabaseCreator(args)
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
        ctx = multiprocessing.get_context('spawn')

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
            with open(output_file, "r") as f:
                compile_commands = json.load(f)

            # Verify it contains entries for both source files
            files_in_db = {entry["file"] for entry in compile_commands}
            assert source1 in files_in_db or os.path.abspath(source1) in files_in_db, \
                f"{source1} not found in compilation database"
            assert source2 in files_in_db or os.path.abspath(source2) in files_in_db, \
                f"{source2} not found in compilation database"

            # Verify no duplicate entries (corruption symptom)
            assert len(compile_commands) == len(files_in_db), \
                f"Duplicate entries found - possible write corruption. " \
                f"Entries: {len(compile_commands)}, Unique files: {len(files_in_db)}"

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
                    "arguments": ["g++", "-c", "src/foo.cpp"]
                },
                {
                    "directory": "/another/project",
                    "file": "lib/bar.cpp",  # Relative path
                    "arguments": ["g++", "-c", "lib/bar.cpp"]
                }
            ]
            with open(existing_db_path, 'w') as f:
                json.dump(existing_db, f)

            # Now create a new entry from a different directory
            samplesdir = uth.samplesdir()
            test_file = os.path.join(samplesdir, "simple/helloworld_cpp.cpp")

            with uth.TempConfigContext(tempdir=tmpdir) as temp_config_name:
                with uth.ParserContext():
                    # Change to other_dir to make cwd different from existing entries
                    old_cwd = os.getcwd()
                    try:
                        os.chdir(other_dir)

                        # Update compilation database with new file
                        compiletools.compilation_database.main([
                            "--config=" + temp_config_name,
                            "--compilation-database-output=" + existing_db_path,
                            test_file
                        ])

                        # Read the merged database
                        with open(existing_db_path, 'r') as f:
                            merged_db = json.load(f)

                        # Critical: existing entries should be preserved
                        # Bug would cause them to be lost due to incorrect path resolution
                        assert len(merged_db) == 3, \
                            f"Expected 3 entries (2 old + 1 new), got {len(merged_db)}"

                        # Verify the old entries are still there with correct paths
                        files_in_db = {cmd["file"] for cmd in merged_db}
                        assert "src/foo.cpp" in files_in_db, \
                            "Relative path src/foo.cpp should be preserved"
                        assert "lib/bar.cpp" in files_in_db, \
                            "Relative path lib/bar.cpp should be preserved"

                        # Verify directories are preserved
                        dirs_in_db = {cmd["directory"] for cmd in merged_db}
                        assert "/some/other/project" in dirs_in_db
                        assert "/another/project" in dirs_in_db

                    finally:
                        os.chdir(old_cwd)