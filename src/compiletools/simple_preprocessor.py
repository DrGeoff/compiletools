"""Simple C preprocessor for handling conditional compilation directives."""

from typing import List, Tuple
import stringzilla as sz
from compiletools.stringzilla_utils import is_alpha_or_underscore_sz
from collections import Counter
from functools import lru_cache
import hashlib

# Global statistics for profiling
_stats = {
    'call_count': 0,
    'files_processed': Counter(),
    'call_contexts': Counter(),
    'cache_hits': 0,
    'cache_misses': 0,
}

# Global preprocessor cache: (content_hash, macro_hash) -> active_lines (List[int])
_preprocessor_cache = {}


def compute_macro_hash(macros_dict) -> str:
    """Compute deterministic hash of macro state for caching.

    This is the CANONICAL macro hash implementation used throughout compiletools.
    All subsystems (SimplePreprocessor, MagicFlags, HeaderDeps) must use this
    function to ensure hash consistency.

    Args:
        macros_dict: Dictionary of macro definitions (dict[sz.Str, sz.Str])

    Returns:
        16-character hex hash of macro state (deterministic, sorted by key)

    Examples:
        >>> import stringzilla as sz
        >>> macros = {sz.Str("FOO"): sz.Str("1"), sz.Str("BAR"): sz.Str("2")}
        >>> hash1 = compute_macro_hash(macros)
        >>> len(hash1)
        16
    """
    if not macros_dict:
        # Empty macro state has consistent hash
        return hashlib.sha256(b"").hexdigest()[:16]

    # Sort by key for deterministic ordering
    macro_items = sorted(macros_dict.items())
    macro_parts = [f"{k}={v}" for k, v in macro_items]
    macro_string = "|".join(macro_parts)
    return hashlib.sha256(macro_string.encode('utf-8')).hexdigest()[:16]



class SimplePreprocessor:
    """A simple C preprocessor for handling conditional compilation directives.

    Capabilities:
    - Handles #if/#elif/#else/#endif, #ifdef/#ifndef, #define/#undef
    - Understands defined(MACRO) and defined MACRO forms
    - Supports C-style numeric literals: hex (0x), binary (0b), octal (0...)
    - Evaluates logical (&&, ||, ! and and/or/not), comparison, bitwise (&, |, ^, ~) and shift (<<, >>) operators
    - Strips // and /* ... */ comments from expressions in directives
    - Respects inactive branches (directives only alter state when active)
    - Provides recursive macro expansion helper for advanced use
    """
    
    def __init__(self, defined_macros: dict[sz.Str, sz.Str], verbose=0):
        # Caller must provide dict with sz.Str keys and values - no type conversion needed
        self.macros = defined_macros.copy()
        self.verbose = verbose
        
    def _create_macro_signature(self):
        """Create a hashable signature of the current macro state."""
        return tuple(sorted(self.macros.items()))

    def _strip_comments(self, expr):
        """Strip C/C++ style comments from expressions.

        - Removes // line comments
        - Removes /* block comments */ (non-nested)
        """
        # Strip C++ style line comments
        if '//' in expr:
            expr = expr[:expr.find('//')].strip()

        # Strip C-style block comments
        import re
        if '/*' in expr:
            expr = re.sub(r"/\*.*?\*/", " ", expr)
            expr = " ".join(expr.split())  # normalize whitespace
        return expr

    def _strip_comments_sz(self, expr_sz):
        """Strip C/C++ style comments from StringZilla expressions."""
        from compiletools.stringzilla_utils import strip_sz

        # Strip C++ style line comments
        comment_pos = expr_sz.find('//')
        if comment_pos >= 0:
            expr_sz = expr_sz[:comment_pos]
            expr_sz = strip_sz(expr_sz)

        # Strip C-style block comments (convert to str for regex, then back)
        if expr_sz.find('/*') >= 0:
            expr_str = str(expr_sz)
            import re
            expr_str = re.sub(r"/\*.*?\*/", " ", expr_str)
            expr_str = " ".join(expr_str.split())  # normalize whitespace
            expr_sz = sz.Str(expr_str)

        return expr_sz

    def _evaluate_expression_sz(self, expr_sz):
        """Evaluate a StringZilla expression using native StringZilla operations"""
        # Use StringZilla-native RECURSIVE macro expansion for better performance
        expanded_sz = self._recursive_expand_macros_sz(expr_sz)
        # Strip comments AFTER macro expansion to handle cases where comments were preserved through expansion
        final_sz = self._strip_comments_sz(expanded_sz)
        # For now, convert final expression to str for safe_eval, but this could be optimized
        expr_str = str(final_sz)
        result = self._safe_eval(expr_str)
        return result

    def _expand_macros_sz(self, expr_sz):
        """Replace macro names with their values using StringZilla operations"""
        # First handle defined() expressions to avoid expanding macros inside them
        expr_str = str(expr_sz)
        expr_str = self._expand_defined(expr_str)

        # Convert back to StringZilla for macro expansion
        result = sz.Str(expr_str)

        reserved = {sz.Str("and"), sz.Str("or"), sz.Str("not")}

        # Start from the beginning and find identifier patterns
        i = 0

        while i < len(result):
            # Skip non-identifier characters
            if not is_alpha_or_underscore_sz(result, i):
                i += 1
                continue

            # Find the end of the identifier
            identifier_start = i
            while i < len(result) and (is_alpha_or_underscore_sz(result, i) or
                                     (i > identifier_start and result[i:i+1].find_first_not_of('0123456789') == -1)):
                i += 1

            # Extract the identifier
            identifier = result[identifier_start:i]

            # Skip reserved words
            if identifier in reserved:
                continue

            # Check if it's a macro and replace it
            if identifier in self.macros:
                value = self.macros[identifier]
                # Replace in the result string
                before = result[:identifier_start]
                after = result[i:]
                result = before + value + after
                # Adjust position to account for replacement
                i = identifier_start + len(value)

        return result

    def _recursive_expand_macros_sz(self, expr_sz, max_iterations=10):
        """Recursively expand macros using StringZilla operations until no more changes occur"""
        previous_expr = sz.Str("")  # Initialize with empty StringZilla.Str instead of None
        iteration = 0

        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_macros_sz(expr_sz)
            iteration += 1

        return expr_sz

    def process_structured(self, file_result) -> List[int]:
        """Process FileAnalysisResult and return active line numbers using structured directive data.

        Args:
            file_result: FileAnalysisResult with structured directive information

        Returns:
            List of line numbers (0-based) that are active after conditional compilation
        """
        # Lookup filepath from content hash for logging
        from compiletools.global_hash_registry import get_filepath_by_hash
        filepath = get_filepath_by_hash(file_result.content_hash) or '<unknown>'

        # Track statistics
        _stats['call_count'] += 1
        _stats['files_processed'][filepath] += 1

        # Capture call context - get caller info
        import traceback
        stack = traceback.extract_stack()
        if len(stack) >= 2:
            caller = stack[-2]
            context = f"{caller.filename}:{caller.lineno} in {caller.name}"
            _stats['call_contexts'][context] += 1

        # Check cache
        content_hash = file_result.content_hash
        macro_hash = compute_macro_hash(self.macros)
        cache_key = (content_hash, macro_hash)

        if cache_key in _preprocessor_cache:
            _stats['cache_hits'] += 1
            active_lines = _preprocessor_cache[cache_key]

            # Reconstruct macro modifications by processing active #define/#undef directives
            # This ensures macro state is updated even on cache hits
            active_line_set = set(active_lines)
            for line_num, directive in file_result.directive_by_line.items():
                if line_num in active_line_set:
                    if directive.directive_type == 'define' and directive.macro_name:
                        macro_value = directive.macro_value if directive.macro_value is not None else "1"
                        self.macros[directive.macro_name] = macro_value
                    elif directive.directive_type == 'undef' and directive.macro_name:
                        if directive.macro_name in self.macros:
                            del self.macros[directive.macro_name]

            return active_lines

        _stats['cache_misses'] += 1

        line_count = file_result.line_count
        active_lines = []

        # Stack to track conditional compilation state
        # Each entry: (is_active, seen_else, any_condition_met)
        condition_stack = [(True, False, False)]

        # Convert directive_by_line to a sorted list for processing in order
        directive_lines = sorted(file_result.directive_by_line.keys())
        directive_iter = iter(directive_lines)
        next_directive_line = next(directive_iter, None)

        i = 0
        while i < line_count:
            # Check if current line has a directive
            if i == next_directive_line:
                directive = file_result.directive_by_line[i]
                
                # Handle multiline directives - skip continuation lines
                continuation_lines = directive.continuation_lines
                
                # Handle the directive
                handled = self._handle_directive_structured(directive, condition_stack, i + 1)
                
                # Include #define and #undef lines in active_lines even when handled (for macro extraction)
                # Also include unhandled directives (like #include) if in active context
                if condition_stack[-1][0]:
                    if directive.directive_type in ('define', 'undef') or handled is False:
                        active_lines.append(i)
                        # Add continuation lines too
                        for j in range(continuation_lines):
                            if i + j + 1 < line_count:
                                active_lines.append(i + j + 1)
                
                # Skip the continuation lines we've already processed
                i += continuation_lines + 1
                next_directive_line = next(directive_iter, None)
            else:
                # Regular line - include if we're in an active context
                if condition_stack[-1][0]:
                    active_lines.append(i)
                i += 1

        # Store in cache before returning
        _preprocessor_cache[cache_key] = active_lines

        return active_lines
    
    # Text-based processing removed - all processing now goes through process_structured()

    def _handle_directive_structured(self, directive, condition_stack, line_num):
        """Handle a specific preprocessor directive using structured data"""
        dtype = directive.directive_type
        
        if dtype == 'define':
            self._handle_define_structured(directive, condition_stack)
            return True
        elif dtype == 'undef':
            self._handle_undef_structured(directive, condition_stack)
            return True
        elif dtype == 'ifdef':
            self._handle_ifdef_structured(directive, condition_stack)
            return True
        elif dtype == 'ifndef':
            self._handle_ifndef_structured(directive, condition_stack)
            return True
        elif dtype == 'if':
            self._handle_if_structured(directive, condition_stack)
            return True
        elif dtype == 'elif':
            self._handle_elif_structured(directive, condition_stack)
            return True
        elif dtype == 'else':
            self._handle_else(condition_stack)
            return True
        elif dtype == 'endif':
            self._handle_endif(condition_stack)
            return True
        else:
            # Unknown directive - ignore but don't consume the line
            # This allows #include and other directives to be processed normally
            if self.verbose >= 8:
                print(f"SimplePreprocessor: Ignoring unknown directive #{dtype}")
            return False  # Indicate that this directive wasn't handled

    def _handle_else(self, condition_stack):
        """Handle #else directive"""
        if len(condition_stack) <= 1:
            return
            
        current_active, seen_else, any_condition_met = condition_stack.pop()
        if not seen_else:
            parent_active = condition_stack[-1][0] if condition_stack else True
            new_active = not any_condition_met and parent_active
            condition_stack.append((new_active, True, any_condition_met or new_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #else -> {new_active}")
        else:
            condition_stack.append((False, True, any_condition_met))
    
    def _handle_endif(self, condition_stack):
        """Handle #endif directive"""
        if len(condition_stack) > 1:
            condition_stack.pop()
            if self.verbose >= 9:
                print("SimplePreprocessor: #endif")
    
    def _handle_define_structured(self, directive, condition_stack):
        """Handle #define directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context
            
        if directive.macro_name:
            macro_value = directive.macro_value if directive.macro_value is not None else "1"
            self.macros[directive.macro_name] = macro_value
            if self.verbose >= 9:
                print(f"SimplePreprocessor: defined macro {directive.macro_name} = {macro_value}")
    
    def _handle_undef_structured(self, directive, condition_stack):
        """Handle #undef directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context
            
        if directive.macro_name and directive.macro_name in self.macros:
            del self.macros[directive.macro_name]
            if self.verbose >= 9:
                print(f"SimplePreprocessor: undefined macro {directive.macro_name}")
    
    def _handle_ifdef_structured(self, directive, condition_stack):
        """Handle #ifdef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = is_defined and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifdef {directive.macro_name} -> {is_defined}")
    
    def _handle_ifndef_structured(self, directive, condition_stack):
        """Handle #ifndef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = (not is_defined) and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifndef {directive.macro_name} -> {not is_defined}")
    
    def _handle_if_structured(self, directive, condition_stack):
        """Handle #if directive using structured data"""
        if directive.condition:
            try:
                # Strip comments before processing - work with StringZilla strings
                expr_sz = self._strip_comments_sz(directive.condition)
                result = self._evaluate_expression_sz(expr_sz)
                is_active = bool(result) and condition_stack[-1][0]
                condition_stack.append((is_active, False, is_active))
                if self.verbose >= 9:
                    print(f"SimplePreprocessor: #if {directive.condition} -> {result} ({is_active})")
            except Exception as e:
                # If evaluation fails, assume false
                if self.verbose >= 8:
                    print(f"SimplePreprocessor: #if evaluation failed for '{directive.condition}': {e}")
                condition_stack.append((False, False, False))
        else:
            # No condition provided
            condition_stack.append((False, False, False))
    
    def _handle_elif_structured(self, directive, condition_stack):
        """Handle #elif directive using structured data"""
        if len(condition_stack) <= 1:
            return
            
        current_active, seen_else, any_condition_met = condition_stack.pop()
        if not seen_else and not any_condition_met and directive.condition:
            parent_active = condition_stack[-1][0] if condition_stack else True
            try:
                # Strip comments before processing - work with StringZilla strings
                expr_sz = self._strip_comments_sz(directive.condition)
                result = self._evaluate_expression_sz(expr_sz)
                new_active = bool(result) and parent_active
                new_any_condition_met = any_condition_met or new_active
                condition_stack.append((new_active, False, new_any_condition_met))
                if self.verbose >= 9:
                    print(f"SimplePreprocessor: #elif {directive.condition} -> {result} ({new_active})")
            except Exception as e:
                if self.verbose >= 8:
                    print(f"SimplePreprocessor: #elif evaluation failed for '{directive.condition}': {e}")
                condition_stack.append((False, False, any_condition_met))
        else:
            # Either we already found a true condition or seen_else is True
            condition_stack.append((False, seen_else, any_condition_met))
    
    def _evaluate_expression(self, expr):
        """Evaluate a C preprocessor expression"""
        # This is a simplified expression evaluator
        # Handle common cases: defined(MACRO), numeric comparisons, logical operations
        
        expr = expr.strip()
        
        # Handle defined(MACRO) and defined MACRO
        expr = self._expand_defined(expr)
        
        # Replace macro names with their values (recursively)
        expr = self._recursive_expand_macros(expr)
        
        # Evaluate the expression safely
        return self._safe_eval(expr)
    
    def _expand_defined(self, expr):
        """Expand defined(MACRO) expressions"""
        import re

        # Handle defined(MACRO)
        def replace_defined_paren(match):
            macro_name = match.group(1)
            # Convert to StringZilla.Str for consistent lookup
            sz_macro_name = sz.Str(macro_name)
            return "1" if sz_macro_name in self.macros else "0"

        expr = re.sub(r'defined\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', replace_defined_paren, expr)

        # Handle defined MACRO (without parentheses)
        def replace_defined_space(match):
            macro_name = match.group(1)
            # Convert to StringZilla.Str for consistent lookup
            sz_macro_name = sz.Str(macro_name)
            return "1" if sz_macro_name in self.macros else "0"

        expr = re.sub(r'defined\s+([A-Za-z_][A-Za-z0-9_]*)', replace_defined_space, expr)

        return expr
    
    def _expand_macros(self, expr):
        """Replace macro names with their values.

        Avoid replacing logical word operators 'and', 'or', 'not' so our later
        operator translation still works even if users type them explicitly.
        """
        import re

        reserved = {"and", "or", "not"}

        def replace_macro(match):
            macro_name = match.group(0)
            if macro_name in reserved:
                return macro_name
            import stringzilla as sz
            sz_macro_name = sz.Str(macro_name)
            if sz_macro_name in self.macros:
                value = self.macros[sz_macro_name]
                # Try to convert to int if possible
                try:
                    return str(int(value))
                except ValueError:
                    return value
            else:
                # Undefined macro defaults to 0
                return "0"

        # Replace macro names (identifiers) with their values
        # Use word boundaries to avoid replacing parts of numbers or other tokens
        expr = re.sub(r'(?<![0-9])\b[A-Za-z_][A-Za-z0-9_]*\b(?![0-9])', replace_macro, expr)

        return expr
    
    def _recursive_expand_macros(self, expr, max_iterations=10):
        """Recursively expand macros until no more changes occur or max iterations reached"""
        # Convert to StringZilla and use the native implementation
        expr_sz = sz.Str(expr)
        result_sz = self._recursive_expand_macros_sz(expr_sz, max_iterations)
        return str(result_sz)
    
    def _safe_eval(self, expr):
        """Safely evaluate a numeric expression"""
        # Clean up the expression
        expr = expr.strip()
        
        # Remove trailing backslashes from multiline directives and normalize whitespace
        import re
        # Remove backslashes followed by whitespace (multiline continuations)
        expr = re.sub(r'\\\s*', ' ', expr)
        # Remove any remaining trailing backslashes
        expr = expr.rstrip('\\').strip()
        
        # First clean up any malformed expressions from macro replacement
        # Fix cases like "0(0)" which occur when macros expand to adjacent numbers
        expr = re.sub(r'(\d+)\s*\(\s*(\d+)\s*\)', r'\1 * \2', expr)
        
        # Remove C-style integer suffixes (L, UL, LL, ULL, etc.)
        expr = re.sub(r'(\d+)[LlUu]+\b', r'\1', expr)

        # Normalize C-style numeric literals to Python ints (hex, bin, octal)
        expr = self._normalize_numeric_literals(expr)
        
        # Convert C operators to Python equivalents
        # Handle comparison operators first (before replacing ! with not)
        # Use temporary placeholders to protect != from being affected by ! replacement
        expr = expr.replace('!=', '__NE__')  # Temporarily replace != with placeholder
        expr = expr.replace('>=', '__GE__')  # Also protect >= from > replacement
        expr = expr.replace('<=', '__LE__')  # Also protect <= from < replacement
        
        # Now handle logical operators (! is safe to replace now)
        expr = expr.replace('&&', ' and ')
        expr = expr.replace('||', ' or ')
        expr = expr.replace('!', ' not ')
        
        # Now restore comparison operators as Python equivalents
        expr = expr.replace('__NE__', '!=')
        expr = expr.replace('__GE__', '>=')
        expr = expr.replace('__LE__', '<=')
        # Note: ==, >, < are already correct for Python and need no conversion
        
        # Clean up any remaining whitespace issues
        expr = expr.strip()
        
        # Only allow safe characters and words
        # Allow bitwise ops (&, |, ^, ~), shifts (<<, >>) and letters for 'and', 'or', 'not'
        if not re.match(r'^[0-9\s\+\-\*\/\%\(\)\<\>\=\!&\|\^~andortnot ]+$', expr):
            raise ValueError(f"Unsafe expression: {expr}")
        
        try:
            # Use eval with a restricted environment
            allowed_names = {"__builtins__": {}}
            result = eval(expr, allowed_names, {})
            return int(result) if isinstance(result, (int, bool)) else 0
        except Exception as e:
            # If evaluation fails, return 0
            if self.verbose >= 8:
                print(f"SimplePreprocessor: Expression evaluation failed for '{expr}': {e}")
            return 0

    def _normalize_numeric_literals(self, expr):
        """Convert C-style numeric literals (hex, bin, oct) to decimal strings.

        - 0x... or 0X... -> decimal
        - 0b... or 0B... -> decimal
        - 0... (octal) -> decimal, but leave single '0' as is and ignore 0x/0b prefixes
        """
        import re

        def repl_hex(m):
            return str(int(m.group(0), 16))

        def repl_bin(m):
            return str(int(m.group(0), 2))

        def repl_oct(m):
            s = m.group(0)
            # avoid replacing just '0'
            if s == '0':
                return s
            return str(int(s, 8))

        # Replace hex first
        expr = re.sub(r'\b0[xX][0-9A-Fa-f]+\b', repl_hex, expr)
        # Replace binary
        expr = re.sub(r'\b0[bB][01]+\b', repl_bin, expr)
        # Replace octal: leading 0 followed by one or more octal digits, not 0x/0b already handled
        expr = re.sub(r'\b0[0-7]+\b', repl_oct, expr)
        return expr


def clear_preprocessor_cache():
    """Clear the global preprocessor cache (for testing)."""
    _preprocessor_cache.clear()


def print_preprocessor_stats():
    """Print statistics about preprocessor usage."""
    print("\n=== Preprocessor Statistics ===")
    print(f"Total process_structured calls: {_stats['call_count']}")
    print(f"Cache hits: {_stats['cache_hits']}")
    print(f"Cache misses: {_stats['cache_misses']}")
    if _stats['call_count'] > 0:
        hit_rate = (_stats['cache_hits'] / _stats['call_count']) * 100
        print(f"Cache hit rate: {hit_rate:.1f}%")
    print(f"\nTop 20 most processed files:")
    for filepath, count in _stats['files_processed'].most_common(20):
        print(f"  {count:6d}x  {filepath}")
    print(f"\nTop 20 call contexts:")
    for context, count in _stats['call_contexts'].most_common(20):
        print(f"  {count:6d}x  {context}")