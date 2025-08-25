import json
import os
import tempfile

import compiletools.compilation_database
import compiletools.testhelper as uth


class TestCompilationDatabase:
    def setup_method(self):
        uth.reset()

    def test_basic_compilation_database_creation(self):
        """Test basic compilation database creation with simple C++ files"""
        
        with uth.TempDirContext() as _:
            origdir = uth.ctdir()
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
                            "--output=" + output_file
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
                            "--output=" + output_file
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
                        "--output=test_output.json"
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
                            "--output=" + output_file
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