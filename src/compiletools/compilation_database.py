import json
import os
from typing import List, Dict, Any
from functools import lru_cache

import stringzilla as sz
import compiletools.utils
import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.namer
import compiletools.configutils
import compiletools.wrappedos


class CompilationDatabaseCreator:
    """Creates compile_commands.json files for clang tooling integration"""
    
    def __init__(self, args, file_analyzer_cache=None, namer=None, headerdeps=None, magicparser=None, hunter=None):
        self.args = args
        
        # Use provided objects or create new ones
        self.namer = namer if namer is not None else compiletools.namer.Namer(args)
        self.headerdeps = headerdeps if headerdeps is not None else compiletools.headerdeps.create(args, file_analyzer_cache=file_analyzer_cache)
        self.magicparser = magicparser if magicparser is not None else compiletools.magicflags.create(args, self.headerdeps)
        self.hunter = hunter if hunter is not None else compiletools.hunter.Hunter(args, self.headerdeps, self.magicparser)
            
    def _normalize_path_sz(self, file_path: str) -> str:
        """Normalize file path using wrappedos with shared StringZilla-aware cache"""
        # Use enhanced wrappedos.abspath that handles both Python str and StringZilla Str
        return compiletools.wrappedos.abspath(file_path)
    
        
    @staticmethod
    def add_arguments(cap):
        """Add command-line arguments for standalone ct-compilation-database"""
        # Add standard target arguments that define what sources to process
        compiletools.apptools.add_target_arguments_ex(cap)

        cap.add(
            "--compilation-database-output",
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
        """Generate compiler command arguments for a source file with StringZilla optimization"""
        
        # Determine compiler based on file extension
        if compiletools.utils.is_cpp_source(source_file):
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
                args.extend(compiletools.utils.cached_shlex_split(self.args.CPPFLAGS))
                
        if compiletools.utils.is_cpp_source(source_file):
            if hasattr(self.args, 'CXXFLAGS') and self.args.CXXFLAGS:
                if isinstance(self.args.CXXFLAGS, list):
                    args.extend(self.args.CXXFLAGS)
                else:
                    args.extend(compiletools.utils.cached_shlex_split(self.args.CXXFLAGS))
        else:
            if hasattr(self.args, 'CFLAGS') and self.args.CFLAGS:
                args.extend(compiletools.utils.cached_shlex_split(self.args.CFLAGS))
            
        # Add magic flags for this specific file
        try:
            magic_cppflags = self.magicparser.getmagic_cppflags_for_file(source_file)
            if magic_cppflags:
                args.extend(compiletools.utils.cached_shlex_split(magic_cppflags))
                
            magic_cxxflags = self.magicparser.getmagic_cxxflags_for_file(source_file)
            if magic_cxxflags:
                args.extend(compiletools.utils.cached_shlex_split(magic_cxxflags))
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

        # Discover all source files using Hunter's project-level discovery
        try:
            # Hunt for all source files from command line arguments and dependencies
            self.hunter.huntsource()
            source_files = self.hunter.getsources()

            if self.args.verbose >= 6:
                print(f"CompilationDatabase: Processing {len(source_files)} source files")

        except Exception as e:
            if self.args.verbose:
                print(f"Warning: Error during source hunting: {e}")
            source_files = []

        # Process each source file
        for source_file in source_files:
            if os.path.exists(source_file):
                command_obj = self._create_command_object(source_file)
                commands.append(command_obj)
            elif self.args.verbose >= 2:
                print(f"Warning: Source file does not exist: {source_file}")

        return commands

    def write_compilation_database(self, output_file: str = None):
        """Write the compilation database to file with incremental update support"""
        
        if output_file is None:
            output_file = self.args.compilation_database_output
            
        # Create the command objects for current files
        new_commands = self.create_compilation_database()
        
        # For incremental updates: read existing database and merge using StringZilla
        existing_commands = []
        if os.path.exists(output_file):
            try:
                # Use StringZilla memory-mapped file reading
                file_content = sz.Str(sz.File(output_file))
                # JSON parsing requires conversion only at the boundary
                if len(file_content) > 0:
                    existing_commands = json.loads(file_content.decode('utf-8'))
                    if self.args.verbose:
                        print(f"Loaded existing compilation database with StringZilla: {len(existing_commands)} entries")
                else:
                    existing_commands = []
            except Exception as e:
                if self.args.verbose:
                    print(f"Warning: Could not read existing compilation database: {e}")
                existing_commands = []

        
        # Merge: Keep existing entries for files we're not updating using StringZilla operations
        merged_commands = []
        
        # Get set of files being updated using StringZilla-optimized path normalization
        new_files = {self._normalize_path_sz(cmd["file"]) for cmd in new_commands}
        
        # Always use StringZilla for consistent file path operations
        for existing_cmd in existing_commands:
            # Use StringZilla Str and wrappedos for efficient path operations
            existing_file_sz = sz.Str(existing_cmd["file"])
            existing_file_normalized = self._normalize_path_sz(existing_file_sz)
            if existing_file_normalized not in new_files:
                merged_commands.append(existing_cmd)
        
        # Add all new/updated entries
        merged_commands.extend(new_commands)
        
        # Write merged JSON file
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(merged_commands, f, indent=2, ensure_ascii=False)
                
            if self.args.verbose:
                print(f"Written compilation database with {len(merged_commands)} entries to {output_file}")
                print(f"  Updated: {len(new_commands)} entries")
                print(f"  Preserved: {len(merged_commands) - len(new_commands)} entries")
                
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
    creator = CompilationDatabaseCreator(args, file_analyzer_cache=shared_file_analyzer_cache)
    creator.write_compilation_database()
    
    return 0

