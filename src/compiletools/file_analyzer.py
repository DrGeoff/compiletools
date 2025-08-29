"""File analysis module for efficient pattern detection in source files.

This module provides SIMD-optimized file analysis with StringZilla when available,
falling back to traditional regex-based analysis for compatibility.
"""

import os
import re
import mmap
import bisect
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, List, Optional, Set
from io import open

import compiletools.wrappedos


def read_file_mmap(filepath, max_size=0):
    """Use memory-mapped I/O for large files with fallback to traditional reading.
    
    Args:
        filepath: Path to file to read
        max_size: Maximum bytes to read (0 = entire file)
        
    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = os.path.getsize(filepath)
        
        # Handle empty files (mmap fails on zero-byte files)
        if file_size == 0:
            return "", 0, False
        
        with open(filepath, 'rb') as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                if max_size > 0 and max_size < file_size:
                    data = mm[:max_size]
                    bytes_analyzed = max_size
                    was_truncated = True
                else:
                    data = mm[:]
                    bytes_analyzed = len(data)
                    was_truncated = False
                    
                text = data.decode('utf-8', errors='ignore')
                return text, bytes_analyzed, was_truncated
                
    except (OSError, IOError, ValueError):
        # Fallback to traditional reading on any mmap failure
        return read_file_traditional(filepath, max_size)


def read_file_traditional(filepath, max_size=0):
    """Traditional file reading fallback.
    
    Args:
        filepath: Path to file to read  
        max_size: Maximum bytes to read (0 = entire file)
        
    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = os.path.getsize(filepath)
        
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            if max_size > 0 and max_size < file_size:
                text = f.read(max_size)
                bytes_analyzed = len(text.encode('utf-8'))
                was_truncated = True
            else:
                text = f.read()
                bytes_analyzed = len(text.encode('utf-8'))
                was_truncated = False
                
        return text, bytes_analyzed, was_truncated
        
    except (OSError, IOError, ValueError):
        # Return empty content on any error
        return "", 0, False


@dataclass
class PreprocessorDirective:
    """A preprocessor directive with all its content."""
    line_num: int                    # Starting line number (0-based)
    byte_pos: int                    # Byte position in original file
    directive_type: str              # 'if', 'ifdef', 'ifndef', 'elif', 'else', 'endif', 'define', 'undef', 'include'
    full_text: List[str]             # All lines including continuations
    condition: Optional[str] = None  # The condition expression (for if/ifdef/ifndef/elif)
    macro_name: Optional[str] = None # Macro name (for define/undef/ifdef/ifndef)
    macro_value: Optional[str] = None # Macro value (for define)


@dataclass
class FileAnalysisResult:
    """Complete structured result without text field.
    
    Provides all information needed by consumers without requiring text reconstruction.
    """
    
    # Line-level data (for SimplePreprocessor) - required fields first
    lines: List[str]                        # All lines of the file
    line_byte_offsets: List[int]            # Byte offset where each line starts
    
    # Position arrays (for fast lookups) - required fields
    include_positions: List[int]            # Byte positions of #include directives
    magic_positions: List[int]              # Byte positions of //#KEY= patterns
    directive_positions: Dict[str, List[int]]  # Byte positions by directive type
    
    # Preprocessor directives (structured for SimplePreprocessor) - required fields
    directives: List[PreprocessorDirective]  # All directives with full context
    directive_by_line: Dict[int, PreprocessorDirective]  # Line number -> directive mapping
    
    # Metadata - required fields
    bytes_analyzed: int                     # Bytes analyzed from file
    was_truncated: bool                     # Whether file was truncated
    
    # Optional fields with defaults come last
    includes: List[Dict] = field(default_factory=list)
    # Each include dict contains:
    # {
    #   'line_num': int,           # Line number (0-based)
    #   'byte_pos': int,           # Byte position
    #   'full_line': str,          # Complete include line
    #   'filename': str,           # Extracted filename
    #   'is_system': bool,         # True for <>, False for ""
    #   'is_commented': bool,      # True if in comment
    # }
    
    magic_flags: List[Dict] = field(default_factory=list)
    # Each magic flag dict contains:
    # {
    #   'line_num': int,           # Line number (0-based)
    #   'byte_pos': int,           # Byte position
    #   'full_line': str,          # Complete line with //#KEY=value
    #   'key': str,                # The KEY part
    #   'value': str,              # The value part
    # }
    
    defines: List[Dict] = field(default_factory=list)
    # Each define dict contains:
    # {
    #   'line_num': int,           # Starting line number
    #   'byte_pos': int,           # Byte position
    #   'lines': List[str],        # All lines including continuations
    #   'name': str,               # Macro name
    #   'value': Optional[str],    # Macro value (if any)
    #   'is_function_like': bool,  # True for function-like macros
    #   'params': List[str],       # Parameters for function-like macros
    # }
    
    system_headers: Set[str] = field(default_factory=set)  # Unique system headers found
    quoted_headers: Set[str] = field(default_factory=set)  # Unique quoted headers found
    content_hash: str = ""                  # SHA1 of original content
    
    # Helper method for SimplePreprocessor compatibility
    def get_directive_line_numbers(self) -> Dict[str, Set[int]]:
        """Get line numbers for each directive type (for SimplePreprocessor)."""
        result = {}
        for dtype, positions in self.directive_positions.items():
            line_nums = set()
            for pos in positions:
                # Binary search in line_byte_offsets to find line number
                line_num = bisect.bisect_right(self.line_byte_offsets, pos) - 1
                line_nums.add(line_num)
            result[dtype] = line_nums
        return result


class FileAnalyzer(ABC):
    """Base class for file analysis implementations.
    
    Ensures both StringZilla and Legacy implementations produce identical structured data.
    """
    
    def __init__(self, filepath: str, max_read_size: int = 0, verbose: int = 0):
        """Initialize file analyzer.
        
        Args:
            filepath: Path to file to analyze
            max_read_size: Maximum bytes to read (0 = entire file)
            verbose: Verbosity level for debugging
        """
        self.filepath = compiletools.wrappedos.realpath(filepath)
        self.max_read_size = max_read_size
        self.verbose = verbose
        
    @abstractmethod
    def analyze(self) -> FileAnalysisResult:
        """Analyze file and return structured results.
        
        Returns:
            FileAnalysisResult with all pattern positions and content
        """
        pass
        
    def _should_read_entire_file(self, file_size: Optional[int] = None) -> bool:
        """Determine if entire file should be read based on configuration."""
        if self.max_read_size == 0:
            return True
        if file_size and file_size <= self.max_read_size:
            return True
        return False


class LegacyFileAnalyzer(FileAnalyzer):
    """Reference implementation using traditional regex/string operations."""
    
    def analyze(self) -> FileAnalysisResult:
        """Analyze file using regex patterns for compatibility."""
        try:
            mtime = compiletools.wrappedos.getmtime(self.filepath)
        except OSError:
            # File doesn't exist, return empty result directly
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
        return self._cached_analyze(mtime)
    
    @lru_cache(maxsize=None)
    def _cached_analyze(self, mtime: float) -> FileAnalysisResult:
        """Cached analysis implementation."""
        if not os.path.exists(self.filepath):
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
            
        try:
            file_size = os.path.getsize(self.filepath)
            read_entire_file = self._should_read_entire_file(file_size)
            
            # Use memory-mapped I/O for better performance
            if read_entire_file:
                text, bytes_analyzed, was_truncated = read_file_mmap(self.filepath, 0)
            else:
                text, bytes_analyzed, was_truncated = read_file_mmap(self.filepath, self.max_read_size)
                    
        except (IOError, OSError):
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
            
        # Split into lines and compute line byte offsets
        lines = text.split('\n')
        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode('utf-8')) + 1  # +1 for \n
        
        # Find pattern positions in the raw text (before preprocessing)
        include_positions = self._find_include_positions(text)
        magic_positions = self._find_magic_positions(text)
        directive_positions = self._find_directive_positions(text)
        
        # Extract structured directive information
        directives = []
        directive_by_line = {}
        processed_lines = set()
        
        for dtype, positions in directive_positions.items():
            for pos in positions:
                # Use binary search on pre-computed line offsets for O(log n) performance
                line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
                if line_num in processed_lines:
                    continue
                
                # Extract directive with continuations
                directive_lines = []
                current_line = line_num
                while current_line < len(lines):
                    line = lines[current_line]
                    directive_lines.append(line)
                    processed_lines.add(current_line)
                    if not line.rstrip().endswith('\\'):
                        break
                    current_line += 1
                
                # Parse directive
                directive = self._parse_directive_struct(dtype, pos, line_num, directive_lines)
                directives.append(directive)
                directive_by_line[line_num] = directive
        
        # Extract includes with full information
        includes = []
        for pos in include_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else ""
            
            # Check if commented
            is_commented = self._is_position_commented(text, pos)
            
            # Extract filename and type
            match = re.search(r'#include\s*([<"])([^>"]+)[>"]', line)
            if match:
                includes.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'full_line': line,
                    'filename': match.group(2),
                    'is_system': match.group(1) == '<',
                    'is_commented': is_commented
                })
        
        # Extract magic flags with full information
        magic_flags = []
        for pos in magic_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else ""
            
            # Parse magic flag
            match = re.search(r'//#([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)', line)
            if match:
                magic_flags.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'full_line': line,
                    'key': match.group(1),
                    'value': match.group(2).strip()
                })
        
        # Extract defines with full information
        defines = []
        for pos in directive_positions.get('define', []):
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            
            # Get all lines including continuations
            define_lines = []
            current_line = line_num
            while current_line < len(lines):
                line = lines[current_line]
                define_lines.append(line)
                if not line.rstrip().endswith('\\'):
                    break
                current_line += 1
            
            # Parse define
            joined = ' '.join(line.rstrip('\\').strip() for line in define_lines)
            match = re.match(r'^\s*#\s*define\s+(\w+)(?:\(([^)]*)\))?\s*(.*)?$', joined)
            if match:
                name = match.group(1)
                params_str = match.group(2)
                value = match.group(3)
                
                is_function_like = params_str is not None
                params = [p.strip() for p in params_str.split(',')] if params_str else []
                
                defines.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'lines': define_lines,
                    'name': name,
                    'value': value.strip() if value else None,
                    'is_function_like': is_function_like,
                    'params': params
                })
        
        # Extract unique headers
        system_headers = {inc['filename'] for inc in includes if inc['is_system']}
        quoted_headers = {inc['filename'] for inc in includes if not inc['is_system']}
        
        # Get content hash from global registry
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(self.filepath)
        
        return FileAnalysisResult(
            lines=lines,
            line_byte_offsets=line_byte_offsets,
            include_positions=include_positions,
            magic_positions=magic_positions,
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=bytes_analyzed,
            was_truncated=was_truncated,
            includes=includes,
            magic_flags=magic_flags,
            defines=defines,
            system_headers=system_headers,
            quoted_headers=quoted_headers,
            content_hash=content_hash
        )
        
    def _find_include_positions(self, text: str) -> List[int]:
        """Find positions of all #include statements."""
        positions = []
        # Pattern matches #include statements but not commented ones
        pattern = re.compile(
            r'/\*.*?\*/|//.*?$|^[\s]*#include[\s]*["<][\s]*([\S]*)[\s]*[">]',
            re.MULTILINE | re.DOTALL
        )
        
        for match in pattern.finditer(text):
            if match.group(1):  # Only if we captured an include filename
                positions.append(match.start())
                
        return positions
        
    def _find_magic_positions(self, text: str) -> List[int]:
        """Find positions of all //#KEY=value patterns."""
        positions = []
        # Pattern must match the exact behavior of magicflags.py regex:
        # ^[\s]*//#([\S]*?)[\s]*=[\s]*(.*)
        # This means optional whitespace at start, then //#, then key=value
        pattern = re.compile(r'^[\s]*//#([A-Za-z_][A-Za-z0-9_-]*)\s*=', re.MULTILINE)
        
        for match in pattern.finditer(text):
            pos = match.start()
            # Check if this position is inside a multi-line block comment
            if not self._is_inside_block_comment_legacy(text, pos):
                positions.append(pos)
            
        return positions
        
    def _is_inside_block_comment_legacy(self, text: str, pos: int) -> bool:
        """Check if position is inside a multi-line block comment (Legacy version)."""
        # Find the most recent /* and */ before this position
        last_block_start = text.rfind('/*', 0, pos)
        if last_block_start != -1:
            # Found a /* before this position
            # Check if there's a closing */ between the /* and our position
            last_block_end = text.rfind('*/', last_block_start, pos)
            if last_block_end == -1:
                # No closing */ found, so we're inside the block comment
                return True
                
        return False
        
    def _find_directive_positions(self, text: str) -> Dict[str, List[int]]:
        """Find positions of all preprocessor directives by type."""
        directive_positions = {}
        
        # Pattern to match preprocessor directives
        pattern = re.compile(r'^(\s*)#\s*([a-zA-Z_]+)', re.MULTILINE)
        
        for match in pattern.finditer(text):
            directive_name = match.group(2)
            if directive_name not in directive_positions:
                directive_positions[directive_name] = []
            # Position should be at the # character, not at start of whitespace
            hash_position = match.start() + len(match.group(1))  # Skip leading whitespace
            directive_positions[directive_name].append(hash_position)
            
        return directive_positions
    
    def _parse_directive_struct(self, dtype: str, pos: int, line_num: int, 
                                directive_lines: List[str]) -> PreprocessorDirective:
        """Parse a directive into structured form."""
        joined = ' '.join(line.rstrip('\\').strip() for line in directive_lines)
        
        directive = PreprocessorDirective(
            line_num=line_num,
            byte_pos=pos,
            directive_type=dtype,
            full_text=directive_lines
        )
        
        if dtype in ('ifdef', 'ifndef'):
            match = re.search(r'#\s*' + dtype + r'\s+(\w+)', joined)
            if match:
                directive.macro_name = match.group(1)
                
        elif dtype in ('if', 'elif'):
            match = re.search(r'#\s*' + dtype + r'\s+(.*)', joined)
            if match:
                directive.condition = match.group(1).strip()
                
        elif dtype == 'define':
            match = re.match(r'^\s*#\s*define\s+(\w+)(?:\s+(.*))?$', joined)
            if match:
                directive.macro_name = match.group(1)
                directive.macro_value = match.group(2).strip() if match.group(2) else None
                
        elif dtype == 'undef':
            match = re.search(r'#\s*undef\s+(\w+)', joined)
            if match:
                directive.macro_name = match.group(1)
        
        return directive
    
    def _is_position_commented(self, text: str, pos: int) -> bool:
        """Check if position is inside a comment (single-line or multi-line block)."""
        # Check for single-line comment on current line
        line_start = text.rfind('\n', 0, pos) + 1
        line_prefix = text[line_start:pos]
        
        # Look for // in the line prefix
        comment_pos = line_prefix.find('//')
        if comment_pos != -1:
            # Check if there's only whitespace before //
            before_comment = line_prefix[:comment_pos].strip()
            if before_comment == '':
                return True
            
        # Check for multi-line block comment
        # Find the most recent /* and */ before this position
        last_block_start = text.rfind('/*', 0, pos)
        if last_block_start != -1:
            # Found a /* before this position
            # Check if there's a closing */ between the /* and our position
            last_block_end = text.rfind('*/', last_block_start, pos)
            if last_block_end == -1:
                # No closing */ found, so we're inside the block comment
                return True
                
        return False


class StringZillaFileAnalyzer(FileAnalyzer):
    """SIMD-optimized implementation using StringZilla when available."""
    
    def __init__(self, filepath: str, max_read_size: int = 0, verbose: int = 0):
        super().__init__(filepath, max_read_size, verbose)
        try:
            import importlib.util
            if importlib.util.find_spec("stringzilla") is not None:
                self._stringzilla_available = True
            else:
                self._stringzilla_available = False
                raise ImportError("StringZilla not available, use LegacyFileAnalyzer")
        except ImportError:
            self._stringzilla_available = False
            raise ImportError("StringZilla not available, use LegacyFileAnalyzer")
    
    def analyze(self) -> FileAnalysisResult:
        """Analyze file using StringZilla SIMD optimization."""
        try:
            mtime = compiletools.wrappedos.getmtime(self.filepath)
        except OSError:
            # File doesn't exist, return empty result directly
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
        return self._cached_analyze(mtime)
    
    @lru_cache(maxsize=None)
    def _cached_analyze(self, mtime: float) -> FileAnalysisResult:
        """Cached analysis implementation."""
        if not self._stringzilla_available:
            raise RuntimeError("StringZilla not available")
            
        if not os.path.exists(self.filepath):
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
            
        try:
            from stringzilla import Str, File
            
            file_size = os.path.getsize(self.filepath)
            read_entire_file = self._should_read_entire_file(file_size)
            
            if read_entire_file:
                # Memory-map entire file and keep as Str for SIMD operations
                str_text = Str(File(self.filepath))
                text = str(str_text)  # Convert to string only for return value
                bytes_analyzed = len(text.encode('utf-8'))
                was_truncated = False
            else:
                # Read limited amount using mmap for better performance
                text, bytes_analyzed, was_truncated = read_file_mmap(self.filepath, self.max_read_size)
                # Create Str for limited read case
                str_text = Str(text)
                    
        except (IOError, OSError):
            return FileAnalysisResult(
                lines=[],
                line_byte_offsets=[],
                include_positions=[], 
                magic_positions=[],
                directive_positions={}, 
                directives=[],
                directive_by_line={},
                bytes_analyzed=0, 
                was_truncated=False
            )
            
        # Split into lines and compute line byte offsets
        lines = text.split('\n')
        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode('utf-8')) + 1  # +1 for \n
        
        # Use StringZilla SIMD operations directly on str_text
        include_positions = self._find_include_positions_simd(str_text)
        magic_positions = self._find_magic_positions_simd(str_text)
        directive_positions = self._find_directive_positions_simd(str_text)
        
        # Extract structured directive information (reuse LegacyFileAnalyzer logic)
        directives = []
        directive_by_line = {}
        processed_lines = set()
        
        for dtype, positions in directive_positions.items():
            for pos in positions:
                line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
                if line_num in processed_lines:
                    continue
                
                # Extract directive with continuations
                directive_lines = []
                current_line = line_num
                while current_line < len(lines):
                    line = lines[current_line]
                    directive_lines.append(line)
                    processed_lines.add(current_line)
                    if not line.rstrip().endswith('\\'):
                        break
                    current_line += 1
                
                # Parse directive
                directive = self._parse_directive_struct(dtype, pos, line_num, directive_lines)
                directives.append(directive)
                directive_by_line[line_num] = directive
        
        # Extract includes with full information
        includes = []
        for pos in include_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else ""
            
            # Check if commented (use StringZilla method)
            is_commented = self._is_position_commented(str_text, pos)
            
            # Extract filename and type
            match = re.search(r'#include\s*([<"])([^>"]+)[>"]', line)
            if match:
                includes.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'full_line': line,
                    'filename': match.group(2),
                    'is_system': match.group(1) == '<',
                    'is_commented': is_commented
                })
        
        # Extract magic flags with full information
        magic_flags = []
        for pos in magic_positions:
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            line = lines[line_num] if line_num < len(lines) else ""
            
            # Parse magic flag
            match = re.search(r'//#([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)', line)
            if match:
                magic_flags.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'full_line': line,
                    'key': match.group(1),
                    'value': match.group(2).strip()
                })
        
        # Extract defines with full information
        defines = []
        for pos in directive_positions.get('define', []):
            line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
            
            # Get all lines including continuations
            define_lines = []
            current_line = line_num
            while current_line < len(lines):
                line = lines[current_line]
                define_lines.append(line)
                if not line.rstrip().endswith('\\'):
                    break
                current_line += 1
            
            # Parse define
            joined = ' '.join(line.rstrip('\\').strip() for line in define_lines)
            match = re.match(r'^\s*#\s*define\s+(\w+)(?:\(([^)]*)\))?\s*(.*)?$', joined)
            if match:
                name = match.group(1)
                params_str = match.group(2)
                value = match.group(3)
                
                is_function_like = params_str is not None
                params = [p.strip() for p in params_str.split(',')] if params_str else []
                
                defines.append({
                    'line_num': line_num,
                    'byte_pos': pos,
                    'lines': define_lines,
                    'name': name,
                    'value': value.strip() if value else None,
                    'is_function_like': is_function_like,
                    'params': params
                })
        
        # Extract unique headers
        system_headers = {inc['filename'] for inc in includes if inc['is_system']}
        quoted_headers = {inc['filename'] for inc in includes if not inc['is_system']}
        
        # Get content hash from global registry
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(self.filepath)
        
        return FileAnalysisResult(
            lines=lines,
            line_byte_offsets=line_byte_offsets,
            include_positions=include_positions,
            magic_positions=magic_positions,
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=bytes_analyzed,
            was_truncated=was_truncated,
            includes=includes,
            magic_flags=magic_flags,
            defines=defines,
            system_headers=system_headers,
            quoted_headers=quoted_headers,
            content_hash=content_hash
        )
        
    def _find_include_positions_simd(self, str_text) -> List[int]:
        """Find positions of all #include statements using StringZilla."""
        positions = []
        
        # Find all #include occurrences
        start = 0
        while True:
            pos = str_text.find('#include', start)
            if pos == -1:
                break
                
            # Check if this #include is inside a comment
            if not self._is_position_commented(str_text, pos):
                positions.append(pos)
                
            start = pos + 8  # len('#include')
            
        return positions
        
    def _is_position_commented(self, str_text, pos: int) -> bool:
        """Check if position is inside a comment (single-line or multi-line block)."""
        # Check for single-line comment on current line
        line_start = str_text.rfind('\n', 0, pos) + 1
        # Use StringZilla slice directly for efficiency
        line_prefix_slice = str_text[line_start:pos]
        
        # Look for // in the line prefix using SIMD
        comment_pos = line_prefix_slice.find('//')
        if comment_pos != -1:
            # Check if there's only whitespace before //
            before_comment = str(line_prefix_slice[:comment_pos]).strip()
            if before_comment == '':
                return True
            
        # Check for multi-line block comment
        # Find the most recent /* and */ before this position
        last_block_start = str_text.rfind('/*', 0, pos)
        if last_block_start != -1:
            # Found a /* before this position
            # Check if there's a closing */ between the /* and our position
            last_block_end = str_text.rfind('*/', last_block_start, pos)
            if last_block_end == -1:
                # No closing */ found, so we're inside the block comment
                return True
                
        return False
        
    def _find_magic_positions_simd(self, str_text) -> List[int]:
        """Find positions of all //#KEY=value patterns using StringZilla."""
        positions = []
        
        # Find all //# occurrences (must be immediately adjacent, no space between // and #)
        start = 0
        while True:
            pos = str_text.find('//#', start)
            if pos == -1:
                break
                
            # Check if this //# is at start of line (after optional whitespace)
            line_start = str_text.rfind('\n', 0, pos) + 1
            line_prefix_slice = str_text[line_start:pos]
            
            # Use StringZilla to check if only whitespace (convert only when necessary)
            if str(line_prefix_slice).strip() == '':  # Only whitespace before //#
                # Check if we're inside a block comment (though //# starting a line is usually not)
                if not self._is_inside_block_comment(str_text, pos):
                    # Look for KEY=value pattern after //#
                    after_hash = pos + 3
                    line_end = str_text.find('\n', after_hash)
                    if line_end == -1:
                        line_end = len(str_text)
                        
                    # Use StringZilla slice and find = using SIMD
                    line_content_slice = str_text[after_hash:line_end]
                    equals_pos = line_content_slice.find('=')
                    if equals_pos != -1:
                        # Extract key part using StringZilla slice
                        key_slice = line_content_slice[:equals_pos]
                        key_part = str(key_slice).strip()
                        # Key must start with letter or underscore, contain only alphanumeric and underscores/dashes
                        if (key_part and 
                            (key_part[0].isalpha() or key_part[0] == '_') and 
                            all(c.isalnum() or c in '_-' for c in key_part)):
                            positions.append(pos)
                    
            start = pos + 3  # len('//#')
            
        return positions
        
    def _is_inside_block_comment(self, str_text, pos: int) -> bool:
        """Check if position is inside a multi-line block comment."""
        # Find the most recent /* and */ before this position
        last_block_start = str_text.rfind('/*', 0, pos)
        if last_block_start != -1:
            # Found a /* before this position
            # Check if there's a closing */ between the /* and our position
            last_block_end = str_text.rfind('*/', last_block_start, pos)
            if last_block_end == -1:
                # No closing */ found, so we're inside the block comment
                return True
                
        return False
        
    def _find_directive_positions_simd(self, str_text) -> Dict[str, List[int]]:
        """Find positions of all preprocessor directives using StringZilla."""
        directive_positions = {}
        
        # Find all # characters that could start directives
        start = 0
        while True:
            pos = str_text.find('#', start)
            if pos == -1:
                break
                
            # Check if this # is at start of line (ignoring whitespace)
            line_start = str_text.rfind('\n', 0, pos) + 1
            line_prefix = str(str_text[line_start:pos])
            
            if line_prefix.strip() == '':  # Only whitespace before #
                # Find the directive name
                directive_start = pos + 1
                while directive_start < len(str_text) and str_text[directive_start].isspace():
                    directive_start += 1
                    
                directive_end = directive_start
                while (directive_end < len(str_text) and 
                       (str_text[directive_end].isalnum() or str_text[directive_end] == '_')):
                    directive_end += 1
                    
                if directive_end > directive_start:
                    directive_name = str(str_text[directive_start:directive_end])
                    if directive_name not in directive_positions:
                        directive_positions[directive_name] = []
                    directive_positions[directive_name].append(pos)
                    
            start = pos + 1
            
        return directive_positions
    
    def _parse_directive_struct(self, dtype: str, pos: int, line_num: int, 
                                directive_lines: List[str]) -> PreprocessorDirective:
        """Parse a directive into structured form."""
        joined = ' '.join(line.rstrip('\\').strip() for line in directive_lines)
        
        directive = PreprocessorDirective(
            line_num=line_num,
            byte_pos=pos,
            directive_type=dtype,
            full_text=directive_lines
        )
        
        if dtype in ('ifdef', 'ifndef'):
            match = re.search(r'#\s*' + dtype + r'\s+(\w+)', joined)
            if match:
                directive.macro_name = match.group(1)
                
        elif dtype in ('if', 'elif'):
            match = re.search(r'#\s*' + dtype + r'\s+(.*)', joined)
            if match:
                directive.condition = match.group(1).strip()
                
        elif dtype == 'define':
            match = re.match(r'^\s*#\s*define\s+(\w+)(?:\s+(.*))?$', joined)
            if match:
                directive.macro_name = match.group(1)
                directive.macro_value = match.group(2).strip() if match.group(2) else None
                
        elif dtype == 'undef':
            match = re.search(r'#\s*undef\s+(\w+)', joined)
            if match:
                directive.macro_name = match.group(1)
        
        return directive


class CachedFileAnalyzer(FileAnalyzer):
    """Wrapper that adds caching to any FileAnalyzer implementation.
    
    This wrapper adds content-based caching on top of the existing
    mtime-based caching in the underlying analyzers.
    """
    
    def __init__(self, analyzer: FileAnalyzer, cache):
        """Initialize cached analyzer wrapper.
        
        Args:
            analyzer: The underlying FileAnalyzer to wrap
            cache: FileAnalyzerCache instance (required).
        """
        self._analyzer = analyzer
        self.filepath = analyzer.filepath
        self.max_read_size = analyzer.max_read_size
        self.verbose = analyzer.verbose
        self._cache = cache
    
    def analyze(self) -> FileAnalysisResult:
        """Analyze file with caching based on content hash."""
        # Get content hash from global registry
        from compiletools.global_hash_registry import get_file_hash
        content_hash = get_file_hash(self.filepath)
        
        if not content_hash:
            # File not in registry - this is an error condition
            raise RuntimeError(f"File not found in global hash registry: {self.filepath}. "
                              "This indicates the file was not present during startup or "
                              "the global hash registry was not properly initialized.")
        
        # Try to get from cache
        cached_result = self._cache.get(self.filepath, content_hash)
        
        if cached_result is not None:
            if self.verbose >= 3:
                print(f"Cache hit for {os.path.basename(self.filepath)}")
            return cached_result
        
        # Cache miss, perform analysis
        if self.verbose >= 3:
            print(f"Cache miss for {os.path.basename(self.filepath)}")
        
        result = self._analyzer.analyze()
        
        # Store in cache if analysis succeeded
        if result.bytes_analyzed > 0:
            self._cache.put(self.filepath, content_hash, result)
        
        return result


def create_shared_analysis_cache(args=None, cache_type: Optional[str] = None):
    """Factory function to create a shared cache for file analysis across multiple components.
    
    Args:
        args: Arguments object with cache configuration
        cache_type: Override cache type (if not using args)
        
    Returns:
        FileAnalyzerCache instance or None if caching disabled
    """
    if cache_type is None and args is not None:
        import compiletools.dirnamer
        cache_type = compiletools.dirnamer.get_cache_type(args=args)
    
    if cache_type:
        from compiletools.file_analyzer_cache import create_cache
        return create_cache(cache_type)
    else:
        return None


def create_file_analyzer(filepath: str, max_read_size: int = 0, verbose: int = 0, 
                        cache_type: Optional[str] = None, cache: Optional['compiletools.file_analyzer_cache.FileAnalyzerCache'] = None) -> FileAnalyzer:
    """Factory function to create appropriate FileAnalyzer implementation.
    
    Args:
        filepath: Path to file to analyze
        max_read_size: Maximum bytes to read (0 = entire file)
        verbose: Verbosity level for debugging
        cache_type: Type of cache to use ('null', 'memory', 'disk', 'sqlite', 'redis').
                   If None, no external caching is added (uses only internal mtime cache).
        cache: Existing cache instance to reuse. If provided, cache_type is ignored.
        
    Returns:
        FileAnalyzer instance, optionally wrapped with caching
    """
    # Create base analyzer
    try:
        analyzer = StringZillaFileAnalyzer(filepath, max_read_size, verbose)
    except ImportError:
        if verbose >= 3:
            print("StringZilla not available, using legacy file analyzer")
        analyzer = LegacyFileAnalyzer(filepath, max_read_size, verbose)
    
    # Add caching if requested
    if cache is not None:
        # Use provided cache instance
        analyzer = CachedFileAnalyzer(analyzer, cache)
    elif cache_type is not None:
        # Create new cache instance
        from compiletools.file_analyzer_cache import create_cache
        cache = create_cache(cache_type)
        analyzer = CachedFileAnalyzer(analyzer, cache)
    
    return analyzer


