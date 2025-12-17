import json
import os
from typing import List, Dict, Any

import stringzilla as sz
import compiletools.utils
import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.namer
import compiletools.configutils
import compiletools.wrappedos
import compiletools.filesystem_utils
import compiletools.findtargets
from compiletools.locking import FileLock


class CompilationDatabaseCreator:
    """Creates compile_commands.json files for clang tooling integration"""
    
    def __init__(self, args, namer=None, headerdeps=None, magicparser=None, hunter=None):
        self.args = args

        # Use provided objects or create new ones
        self.namer = namer if namer is not None else compiletools.namer.Namer(args)
        self.headerdeps = headerdeps if headerdeps is not None else compiletools.headerdeps.create(args)
        self.magicparser = magicparser if magicparser is not None else compiletools.magicflags.create(args, self.headerdeps)
        self.hunter = hunter if hunter is not None else compiletools.hunter.Hunter(args, self.headerdeps, self.magicparser)
            
    @staticmethod
    def add_arguments(cap):
        """Add command-line arguments for standalone ct-compilation-database"""
        # Add standard target arguments that define what sources to process
        compiletools.apptools.add_target_arguments_ex(cap)

        cap.add(
            "--compilation-database-output",
            dest="compilation_database_output",
            default=None,
            help="Output filename for compilation database (default: <gitroot>/compile_commands.json)"
        )

        cap.add(
            "--relative-paths",
            dest="compilation_database_relative",
            action="store_true",
            help="Use relative paths instead of absolute paths"
        )

        compiletools.utils.add_boolean_argument(
            parser=cap,
            name="shared-objects",
            dest="shared_objects",
            default=False,
            help="Enable file locking for concurrent compilation database writes",
        )

    def _get_compiler_command(self, source_file: str) -> List[str]:
        """Generate compiler command arguments for a source file with StringZilla optimization"""

        # Determine compiler based on file extension
        if compiletools.utils.is_cpp_source(source_file):
            compiler = self.args.CXX
        else:
            compiler = self.args.CC

        # Build arguments list - properly split compiler command if it contains multiple tokens
        args = []
        if compiler:
            # Split compiler command (e.g., "ccache g++" -> ["ccache", "g++"])
            compiler_parts = compiletools.utils.split_command_cached(compiler)
            args.extend(compiler_parts)
        else:
            # If no compiler is set, we can't generate a valid command
            return []
        
        # Get magic flags for this specific file
        try:
            magic_flags = self.magicparser.parse(source_file)
        except Exception as e:
            # Magic flags parsing may fail for various reasons
            if self.args.verbose >= 2:
                print(f"Warning: Could not parse magic flags for {source_file}: {e}")
            magic_flags = {}

        # Combine and deduplicate all flag sources
        import stringzilla as sz
        if compiletools.utils.is_cpp_source(source_file):
            # C++ source: combine CPPFLAGS + CXXFLAGS from both args and magic flags
            combined_flags = compiletools.utils.combine_and_deduplicate_compiler_flags(
                getattr(self.args, 'CPPFLAGS', None),
                magic_flags.get(sz.Str("CPPFLAGS"), []),
                getattr(self.args, 'CXXFLAGS', None),
                magic_flags.get(sz.Str("CXXFLAGS"), [])
            )
        else:
            # C source: combine CPPFLAGS + CFLAGS from both args and magic flags
            combined_flags = compiletools.utils.combine_and_deduplicate_compiler_flags(
                getattr(self.args, 'CPPFLAGS', None),
                magic_flags.get(sz.Str("CPPFLAGS"), []),
                getattr(self.args, 'CFLAGS', None),
                magic_flags.get(sz.Str("CFLAGS"), [])
            )

        args.extend(combined_flags)
        
        # Add compile-only flag
        args.extend(["-c"])
        
        # Add the source file
        if self.args.compilation_database_relative:
            args.append(os.path.relpath(source_file, os.getcwd()))
        else:
            args.append(os.path.realpath(source_file))
            
        return args

    def _create_command_object(self, source_file: str) -> Dict[str, Any]:
        """Create a single command object for the compilation database"""

        # Directory is always absolute (working directory)
        directory = os.path.realpath(os.getcwd())

        # Get file path - relative or absolute based on option
        if self.args.compilation_database_relative:
            file_path = os.path.relpath(source_file, os.getcwd())
        else:
            file_path = os.path.realpath(source_file)

        # Generate arguments
        arguments = self._get_compiler_command(source_file)

        # Skip files with empty arguments arrays - they provide no compile context
        if not arguments:
            return None

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
                if command_obj is not None:
                    commands.append(command_obj)
                elif self.args.verbose >= 2:
                    print(f"Warning: Skipping source file with empty arguments: {source_file}")
            elif self.args.verbose >= 2:
                print(f"Warning: Source file does not exist: {source_file}")

        return commands

    def write_compilation_database(self, output_file: str = None):
        """Write the compilation database to file with incremental update support"""

        if output_file is None:
            output_file = self.namer.compilation_database_pathname()

        # Create new commands OUTSIDE lock - this is expensive (source hunting, parsing)
        # Only need lock for the actual read-merge-write operation
        new_commands = self.create_compilation_database()

        # Use same --shared-objects flag as makefile.py
        # FileLock is no-op if args.shared_objects is False
        # Lock held only for quick read-merge-write to minimize blocking
        with FileLock(output_file, self.args):
            self._write_database_impl(output_file, new_commands)

    def _write_database_impl(self, output_file: str, new_commands: List[Dict[str, Any]]):
        """Implementation of database write (extracted for locking)

        Args:
            output_file: Path to compile_commands.json
            new_commands: Pre-computed command objects (created outside lock)
        """
        # For incremental updates: read existing database and merge
        existing_commands = []
        if os.path.exists(output_file):
            try:
                # Use filesystem-safe reading (mmap on local, regular I/O on network filesystems)
                # No need for respect_locks since we're already inside FileLock context
                content_str = compiletools.filesystem_utils.safe_read_text_file(
                    output_file,
                    encoding='utf-8'
                )
                if len(content_str) > 0:
                    existing_commands = json.loads(str(content_str))
                    if self.args.verbose:
                        print(f"Loaded existing compilation database: {len(existing_commands)} entries")
                else:
                    existing_commands = []
            except Exception as e:
                if self.args.verbose:
                    print(f"Warning: Could not read existing compilation database: {e}")
                existing_commands = []


        # Merge: Keep existing entries for files we're not updating using StringZilla operations
        merged_commands = []

        # Use StringZilla for optimal path processing performance
        # Build set of normalized file paths from new commands
        new_files_normalized = set()
        for cmd in new_commands:
            # Convert to StringZilla for optimal caching, then back to str for set
            file_sz = sz.Str(cmd["file"])
            normalized_sz = compiletools.wrappedos.realpath_sz(file_sz)
            new_files_normalized.add(str(normalized_sz))

        # Process existing commands with StringZilla optimization
        for existing_cmd in existing_commands:
            existing_file = existing_cmd["file"]

            # Resolve relative paths against their "directory" context, not cwd
            if not os.path.isabs(existing_file):
                base_dir = existing_cmd.get("directory", os.getcwd())
                existing_file = os.path.join(base_dir, existing_file)

            # Convert to StringZilla for consistent processing
            existing_file_sz = sz.Str(existing_file)
            existing_normalized_sz = compiletools.wrappedos.realpath_sz(existing_file_sz)
            if str(existing_normalized_sz) not in new_files_normalized:
                merged_commands.append(existing_cmd)

        # Add all new/updated entries
        merged_commands.extend(new_commands)

        # Write merged JSON file
        try:
            # Ensure the output directory exists
            output_dir = compiletools.wrappedos.dirname(output_file)
            if output_dir and not compiletools.wrappedos.isdir(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            # Use atomic write to prevent SIGBUS for concurrent mmap readers (e.g., clangd)
            json_content = json.dumps(merged_commands, indent=2, ensure_ascii=False)
            compiletools.filesystem_utils.atomic_write(output_file, json_content)

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

    # Add findtargets arguments to support --auto mode
    compiletools.findtargets.add_arguments(cap)

    # Parse arguments
    args = compiletools.apptools.parseargs(cap, argv)

    # Handle --auto mode: discover targets if no explicit targets provided
    if args.auto and not any([args.filename, args.static, args.dynamic, args.tests]):
        if args.verbose >= 2:
            print("Auto-detecting targets...")
        findtargets = compiletools.findtargets.FindTargets(args)
        findtargets.process(args)
        # Re-run substitutions after targets are discovered
        compiletools.apptools.substitutions(args, verbose=0)

    # Create and run the compilation database creator
    creator = CompilationDatabaseCreator(args)
    creator.write_compilation_database()

    return 0

