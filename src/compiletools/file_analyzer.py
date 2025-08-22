"""File analysis module for efficient pattern detection in source files.

This module provides SIMD-optimized file analysis with StringZilla when available,
falling back to traditional regex-based analysis for compatibility.
"""

import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional
from io import open

import compiletools.wrappedos


@dataclass
class FileAnalysisResult:
    """Standardized result from file analysis containing pattern positions and content."""
    text: str                               # Actual text content (respects max_read_size)
    include_positions: List[int]            # Positions of #include statements  
    magic_positions: List[int]              # Positions of //#KEY= patterns
    directive_positions: Dict[str, List[int]]  # All preprocessor directive positions by type
    bytes_analyzed: int                     # How much of file was actually processed
    was_truncated: bool                     # Whether file was larger than max_read_size


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
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
            )
        return self._cached_analyze(mtime)
    
    @lru_cache(maxsize=None)
    def _cached_analyze(self, mtime: float) -> FileAnalysisResult:
        """Cached analysis implementation."""
        if not os.path.exists(self.filepath):
            return FileAnalysisResult(
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
            )
            
        try:
            file_size = os.path.getsize(self.filepath)
            read_entire_file = self._should_read_entire_file(file_size)
            
            with open(self.filepath, encoding="utf-8", errors="ignore") as f:
                if read_entire_file:
                    text = f.read()
                    bytes_analyzed = len(text.encode('utf-8'))
                    was_truncated = False
                else:
                    text = f.read(self.max_read_size)
                    bytes_analyzed = len(text.encode('utf-8'))
                    was_truncated = not read_entire_file and file_size > bytes_analyzed
                    
        except (IOError, OSError):
            return FileAnalysisResult(
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
            )
            
        # Find pattern positions in the raw text (before preprocessing)
        # Note: Conditional compilation should be handled by the caller
        include_positions = self._find_include_positions(text)
        magic_positions = self._find_magic_positions(text)
        directive_positions = self._find_directive_positions(text)
        
        return FileAnalysisResult(
            text=text,
            include_positions=include_positions,
            magic_positions=magic_positions,
            directive_positions=directive_positions,
            bytes_analyzed=bytes_analyzed,
            was_truncated=was_truncated
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
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
            )
        return self._cached_analyze(mtime)
    
    @lru_cache(maxsize=None)
    def _cached_analyze(self, mtime: float) -> FileAnalysisResult:
        """Cached analysis implementation."""
        if not self._stringzilla_available:
            raise RuntimeError("StringZilla not available")
            
        if not os.path.exists(self.filepath):
            return FileAnalysisResult(
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
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
                # Read limited amount
                with open(self.filepath, encoding="utf-8", errors="ignore") as f:
                    text = f.read(self.max_read_size)
                    bytes_analyzed = len(text.encode('utf-8'))
                    was_truncated = not read_entire_file and file_size > bytes_analyzed
                # Create Str for limited read case
                str_text = Str(text)
                    
        except (IOError, OSError):
            return FileAnalysisResult(
                text="", include_positions=[], magic_positions=[],
                directive_positions={}, bytes_analyzed=0, was_truncated=False
            )
            
        # Use StringZilla SIMD operations directly on str_text
        # Note: Conditional compilation should be handled by the caller
        include_positions = self._find_include_positions_simd(str_text)
        magic_positions = self._find_magic_positions_simd(str_text)
        directive_positions = self._find_directive_positions_simd(str_text)
        
        return FileAnalysisResult(
            text=text,
            include_positions=include_positions,
            magic_positions=magic_positions,
            directive_positions=directive_positions,
            bytes_analyzed=bytes_analyzed,
            was_truncated=was_truncated
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


class CachedFileAnalyzer(FileAnalyzer):
    """Wrapper that adds caching to any FileAnalyzer implementation.
    
    This wrapper adds content-based caching on top of the existing
    mtime-based caching in the underlying analyzers.
    """
    
    def __init__(self, analyzer: FileAnalyzer, cache=None):
        """Initialize cached analyzer wrapper.
        
        Args:
            analyzer: The underlying FileAnalyzer to wrap
            cache: FileAnalyzerCache instance. If None, uses default disk cache.
        """
        self._analyzer = analyzer
        self.filepath = analyzer.filepath
        self.max_read_size = analyzer.max_read_size
        self.verbose = analyzer.verbose
        
        if cache is None:
            # Default to disk cache
            from compiletools.file_analyzer_cache import create_cache
            self._cache = create_cache('disk')
        else:
            self._cache = cache
    
    def analyze(self) -> FileAnalysisResult:
        """Analyze file with caching based on content hash."""
        # Compute content hash for cache key
        content_hash = self._cache.compute_content_hash(self.filepath)
        
        if not content_hash:
            # File doesn't exist or can't be read, delegate to analyzer
            return self._analyzer.analyze()
        
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


def create_file_analyzer(filepath: str, max_read_size: int = 0, verbose: int = 0, 
                        cache_type: Optional[str] = None, cache: Optional['FileAnalyzerCache'] = None) -> FileAnalyzer:
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


