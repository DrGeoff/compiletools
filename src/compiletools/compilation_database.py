import json
import os
from typing import List, Dict, Any

import compiletools.utils
import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.namer
import compiletools.configutils
import compiletools.timing


class CompilationDatabaseCreator:
    """Creates compile_commands.json files for clang tooling integration"""
    
    def __init__(self, args):
        self.args = args
        self.namer = compiletools.namer.Namer(args)
        self.headerdeps = compiletools.headerdeps.create(args)
        self.magicparser = compiletools.magicflags.create(args, self.headerdeps)
        self.hunter = compiletools.hunter.Hunter(args, self.headerdeps, self.magicparser)
        
    @staticmethod
    def add_arguments(cap):
        """Add command-line arguments"""
        cap.add("filename", nargs="*", help="Source file(s) to include in compilation database")
        
        cap.add(
            "--output", "-o",
            dest="compilation_database_output",
            default="compile_commands.json",
            help="Output filename for compilation database (default: compile_commands.json)"
        )
        
        cap.add(
            "--relative-paths",
            dest="compilation_database_relative",
            action="store_true", 
            help="Use relative paths instead of absolute paths"
        )

    def _get_compiler_command(self, source_file: str) -> List[str]:
        """Generate compiler command arguments for a source file"""
        
        # Determine compiler based on file extension  
        if source_file.endswith(('.cpp', '.cxx', '.cc', '.C', '.CC')):
            compiler = self.args.CXX
        else:
            compiler = self.args.CC
        
        # Build arguments list
        args = [compiler]
        
        # Add standard flags
        if hasattr(self.args, 'CPPFLAGS') and self.args.CPPFLAGS:
            if isinstance(self.args.CPPFLAGS, list):
                args.extend(self.args.CPPFLAGS)
            else:
                args.extend(self.args.CPPFLAGS.split())
                
        if source_file.endswith(('.cpp', '.cxx', '.cc', '.C', '.CC')):
            if hasattr(self.args, 'CXXFLAGS') and self.args.CXXFLAGS:
                if isinstance(self.args.CXXFLAGS, list):
                    args.extend(self.args.CXXFLAGS)
                else:
                    args.extend(self.args.CXXFLAGS.split())
        else:
            if hasattr(self.args, 'CFLAGS') and self.args.CFLAGS:
                args.extend(self.args.CFLAGS.split())
            
        # Add magic flags for this specific file
        try:
            magic_cppflags = self.magicparser.getmagic_cppflags_for_file(source_file)
            if magic_cppflags:
                args.extend(magic_cppflags.split())
                
            magic_cxxflags = self.magicparser.getmagic_cxxflags_for_file(source_file)
            if magic_cxxflags:
                args.extend(magic_cxxflags.split())
        except AttributeError:
            # Magic flags methods may not exist
            pass
        
        # Add compile-only flag
        args.extend(["-c"])
        
        # Add the source file
        if self.args.compilation_database_relative:
            args.append(os.path.relpath(source_file, os.getcwd()))
        else:
            args.append(os.path.abspath(source_file))
            
        return args

    def _create_command_object(self, source_file: str) -> Dict[str, Any]:
        """Create a single command object for the compilation database"""
        
        # Directory is always absolute (working directory)
        directory = os.path.abspath(os.getcwd())
            
        # Get file path - relative or absolute based on option
        if self.args.compilation_database_relative:
            file_path = os.path.relpath(source_file, os.getcwd())
        else:
            file_path = os.path.abspath(source_file)
            
        # Generate arguments
        arguments = self._get_compiler_command(source_file)
        
        return {
            "directory": directory,
            "file": file_path,
            "arguments": arguments
        }

    def create_compilation_database(self) -> List[Dict[str, Any]]:
        """Create the compilation database as a list of command objects"""
        
        commands = []
        
        # Use explicitly provided files if available, otherwise hunt for sources
        if hasattr(self.args, 'filename') and self.args.filename:
            source_files = self.args.filename
        else:
            try:
                # Hunt for source files
                self.hunter.huntsource()
                source_files = self.hunter.getsources()
                
                # Also process test files if they exist
                if hasattr(self.hunter, 'gettestsources'):
                    test_files = self.hunter.gettestsources()
                    source_files.extend(test_files)
                    
            except Exception as e:
                if self.args.verbose:
                    print(f"Warning: Error during source hunting: {e}")
                source_files = []
        
        # Process each source file
        for source_file in source_files:
            if os.path.exists(source_file):
                command_obj = self._create_command_object(source_file)
                commands.append(command_obj)
            
        return commands

    def write_compilation_database(self, output_file: str = None):
        """Write the compilation database to file"""
        
        if output_file is None:
            output_file = self.args.compilation_database_output
            
        # Create the command objects
        commands = self.create_compilation_database()
        
        # Write JSON file
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(commands, f, indent=2, ensure_ascii=False)
                
            if self.args.verbose:
                print(f"Written compilation database with {len(commands)} entries to {output_file}")
                
        except Exception as e:
            print(f"Error writing compilation database: {e}")
            raise


def main(argv=None):
    """Main entry point for ct-compilation-database"""
    
    cap = compiletools.apptools.create_parser(
        "Generate compile_commands.json for clang tooling", argv=argv
    )
    
    # Add compilation database specific arguments
    CompilationDatabaseCreator.add_arguments(cap)
    
    # Add standard compiletools arguments  
    compiletools.hunter.add_arguments(cap)
    
    # Parse arguments
    args = compiletools.apptools.parseargs(cap, argv)
    
    # Create shared cache for all file analysis components
    from compiletools.file_analyzer import create_shared_analysis_cache
    shared_file_analyzer_cache = create_shared_analysis_cache(args)
    
    # Create and run the compilation database creator
    creator = CompilationDatabaseCreator(args)
    creator.write_compilation_database()
    
    return 0

