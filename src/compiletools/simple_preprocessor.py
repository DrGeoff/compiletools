"""Simple C preprocessor for handling conditional compilation directives."""

from typing import List, Dict, Tuple, Any, TYPE_CHECKING
import stringzilla as sz
from compiletools.stringzilla_utils import is_alpha_or_underscore_sz
from collections import Counter

if TYPE_CHECKING:
    from compiletools.file_analyzer import PreprocessorDirective, FileAnalysisResult

# Global statistics for profiling
_stats: Dict[str, Any] = {
    'call_count': 0,
    'files_processed': Counter(),
    'call_contexts': Counter(),
}


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

    def __init__(self, defined_macros: Dict[sz.Str, sz.Str], verbose: int = 0) -> None:
        # Caller must provide dict with sz.Str keys and values - no type conversion needed
        self.macros = defined_macros.copy()
        self.verbose = verbose

    def _strip_comments_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Strip C/C++ style comments from StringZilla expressions."""
        from compiletools.stringzilla_utils import strip_sz

        # Strip C++ style line comments
        comment_pos = expr_sz.find('//')
        if comment_pos >= 0:
            expr_sz = expr_sz[:comment_pos]
            expr_sz = strip_sz(expr_sz)

        # Strip C-style block comments using StringZilla operations
        start_pos = expr_sz.find('/*')
        if start_pos >= 0:
            # Build list of non-comment regions
            regions = []
            pos = 0

            while True:
                start_pos = expr_sz.find('/*', pos)
                if start_pos < 0:
                    # No more comments, add remaining text
                    if pos < len(expr_sz):
                        regions.append(expr_sz[pos:])
                    break

                # Add text before comment
                if start_pos > pos:
                    regions.append(expr_sz[pos:start_pos])

                # Find end of comment
                end_pos = expr_sz.find('*/', start_pos + 2)
                if end_pos < 0:
                    # Unclosed comment - skip rest
                    break

                # Add space where comment was
                regions.append(sz.Str(' '))
                pos = end_pos + 2

            # Join regions efficiently using concat_sz
            from compiletools.stringzilla_utils import concat_sz
            expr_sz = concat_sz(*regions) if regions else sz.Str('')

            # Normalize whitespace: convert to str, normalize, convert back
            # (for tiny expressions this is acceptable and simpler than vectorization)
            if len(expr_sz) > 0:
                parts = str(expr_sz).split()
                if parts:
                    expr_sz = sz.Str(' '.join(parts))
                else:
                    expr_sz = sz.Str('')

        return expr_sz

    def _evaluate_expression_sz(self, expr_sz: sz.Str) -> int:
        """Evaluate a StringZilla expression using native StringZilla operations"""
        # Use StringZilla-native RECURSIVE macro expansion for better performance
        expanded_sz = self._recursive_expand_macros_sz(expr_sz)
        # Strip comments AFTER macro expansion to handle cases where comments were preserved through expansion
        final_sz = self._strip_comments_sz(expanded_sz)
        # For now, convert final expression to str for safe_eval, but this could be optimized
        expr_str = str(final_sz)
        result = self._safe_eval(expr_str)
        return result

    def _expand_defined_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Expand defined(MACRO) expressions using StringZilla operations"""
        from compiletools.stringzilla_utils import is_alpha_or_underscore_sz

        result_parts = []
        i = 0

        while i < len(expr_sz):
            # Look for 'defined'
            defined_pos = expr_sz.find('defined', i)
            if defined_pos == -1:
                # No more 'defined' occurrences
                result_parts.append(expr_sz[i:])
                break

            # Add text before 'defined'
            if defined_pos > i:
                result_parts.append(expr_sz[i:defined_pos])

            # Check if this is actually 'defined' keyword (not part of identifier)
            if defined_pos > 0 and is_alpha_or_underscore_sz(expr_sz, defined_pos - 1):
                # Part of another identifier
                result_parts.append(expr_sz[defined_pos:defined_pos + 7])
                i = defined_pos + 7
                continue

            after_defined = defined_pos + 7  # len('defined')
            if after_defined < len(expr_sz) and is_alpha_or_underscore_sz(expr_sz, after_defined):
                # Part of longer identifier
                result_parts.append(expr_sz[defined_pos:after_defined])
                i = after_defined
                continue

            # Skip whitespace after 'defined' - vectorized
            j = expr_sz.find_first_not_of(' \t', after_defined)
            if j == -1:
                j = len(expr_sz)

            if j >= len(expr_sz):
                result_parts.append(expr_sz[defined_pos:])
                break

            # Check for parenthesized form: defined(MACRO)
            macro_name = None
            end_pos = j

            ch = expr_sz[j:j+1]
            if len(ch) > 0 and ch[0] == '(':
                # Find macro name inside parens
                j += 1
                # Skip whitespace - vectorized
                j = expr_sz.find_first_not_of(' \t', j)
                if j == -1:
                    j = len(expr_sz)

                # Extract macro name - vectorized
                if j < len(expr_sz) and is_alpha_or_underscore_sz(expr_sz, j):
                    macro_start = j
                    # Find end of identifier (alphanumeric + underscore)
                    identifier_end = expr_sz.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', macro_start)
                    j = identifier_end if identifier_end != -1 else len(expr_sz)
                    macro_name = expr_sz[macro_start:j]

                    # Skip whitespace before closing paren - vectorized
                    next_non_ws = expr_sz.find_first_not_of(' \t', j)
                    j = next_non_ws if next_non_ws != -1 else len(expr_sz)

                    # Check for closing paren
                    if j < len(expr_sz):
                        ch = expr_sz[j:j+1]
                        if len(ch) > 0 and ch[0] == ')':
                            end_pos = j + 1
            else:
                # Space form: defined MACRO - vectorized
                if is_alpha_or_underscore_sz(expr_sz, j):
                    macro_start = j
                    # Find end of identifier (alphanumeric + underscore)
                    identifier_end = expr_sz.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', macro_start)
                    j = identifier_end if identifier_end != -1 else len(expr_sz)
                    macro_name = expr_sz[macro_start:j]
                    end_pos = j

            # Replace with 1 or 0
            if macro_name:
                result_parts.append(sz.Str("1") if macro_name in self.macros else sz.Str("0"))
                i = end_pos
            else:
                # Couldn't parse, keep as is
                result_parts.append(expr_sz[defined_pos:after_defined])
                i = after_defined

        from compiletools.stringzilla_utils import concat_sz
        return concat_sz(*result_parts) if result_parts else sz.Str('')

    def _expand_macros_sz(self, expr_sz: sz.Str) -> sz.Str:
        """Replace macro names with their values using StringZilla operations"""
        # First handle defined() expressions to avoid expanding macros inside them
        result = self._expand_defined_sz(expr_sz)

        reserved = {sz.Str("and"), sz.Str("or"), sz.Str("not")}

        # Start from the beginning and find identifier patterns
        i = 0

        while i < len(result):
            # Skip non-identifier characters
            if not is_alpha_or_underscore_sz(result, i):
                i += 1
                continue

            # Find the end of the identifier - vectorized
            identifier_start = i
            identifier_end = result.find_first_not_of('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_', identifier_start)
            i = identifier_end if identifier_end != -1 else len(result)

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

    def _recursive_expand_macros_sz(self, expr_sz: sz.Str, max_iterations: int = 10) -> sz.Str:
        """Recursively expand macros using StringZilla operations until no more changes occur"""
        previous_expr = sz.Str("")  # Initialize with empty StringZilla.Str instead of None
        iteration = 0

        while expr_sz != previous_expr and iteration < max_iterations:
            previous_expr = expr_sz
            expr_sz = self._expand_macros_sz(expr_sz)
            iteration += 1

        return expr_sz

    def process_structured(self, file_result: 'FileAnalysisResult') -> List[int]:
        """Process FileAnalysisResult and return active line numbers using structured directive data.

        Args:
            file_result: FileAnalysisResult with structured directive information

        Returns:
            List of line numbers (0-based) that are active after conditional compilation
        """
        # Lookup filepath from content hash for logging
        from compiletools.global_hash_registry import get_filepath_by_hash
        filepath = get_filepath_by_hash(file_result.content_hash)

        # Track statistics
        _stats['call_count'] += 1
        _stats['files_processed'][filepath] += 1

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

        return active_lines
    
    # Text-based processing removed - all processing now goes through process_structured()

    def _handle_directive_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]], line_num: int) -> bool:
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

    def _handle_else(self, condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #else directive"""
        if len(condition_stack) <= 1:
            return

        _, seen_else, any_condition_met = condition_stack.pop()
        if not seen_else:
            parent_active = condition_stack[-1][0] if condition_stack else True
            new_active = not any_condition_met and parent_active
            condition_stack.append((new_active, True, any_condition_met or new_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #else -> {new_active}")
        else:
            condition_stack.append((False, True, any_condition_met))
    
    def _handle_endif(self, condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #endif directive"""
        if len(condition_stack) > 1:
            condition_stack.pop()
            if self.verbose >= 9:
                print("SimplePreprocessor: #endif")
    
    def _handle_define_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #define directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context
            
        if directive.macro_name:
            macro_value = directive.macro_value if directive.macro_value is not None else "1"
            self.macros[directive.macro_name] = macro_value
            if self.verbose >= 9:
                print(f"SimplePreprocessor: defined macro {directive.macro_name} = {macro_value}")
    
    def _handle_undef_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #undef directive using structured data"""
        if not condition_stack[-1][0]:
            return  # Not in active context
            
        if directive.macro_name and directive.macro_name in self.macros:
            del self.macros[directive.macro_name]
            if self.verbose >= 9:
                print(f"SimplePreprocessor: undefined macro {directive.macro_name}")
    
    def _handle_ifdef_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #ifdef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = is_defined and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifdef {directive.macro_name} -> {is_defined}")
    
    def _handle_ifndef_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #ifndef directive using structured data"""
        if directive.macro_name:
            is_defined = directive.macro_name in self.macros
            is_active = (not is_defined) and condition_stack[-1][0]
            condition_stack.append((is_active, False, is_active))
            if self.verbose >= 9:
                print(f"SimplePreprocessor: #ifndef {directive.macro_name} -> {not is_defined}")
    
    def _handle_if_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
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
    
    def _handle_elif_structured(self, directive: 'PreprocessorDirective', condition_stack: List[Tuple[bool, bool, bool]]) -> None:
        """Handle #elif directive using structured data"""
        if len(condition_stack) <= 1:
            return
            
        _, seen_else, any_condition_met = condition_stack.pop()
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
    
    def _safe_eval(self, expr: str) -> int:
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

    def _normalize_numeric_literals(self, expr: str) -> str:
        """Convert C-style numeric literals (hex, bin, oct) to decimal strings.

        - 0x... or 0X... -> decimal
        - 0b... or 0B... -> decimal
        - 0... (octal) -> decimal, but leave single '0' as is and ignore 0x/0b prefixes
        """
        import re

        def repl_hex(m: re.Match[str]) -> str:
            return str(int(m.group(0), 16))

        def repl_bin(m: re.Match[str]) -> str:
            return str(int(m.group(0), 2))

        def repl_oct(m: re.Match[str]) -> str:
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


def print_preprocessor_stats() -> None:
    """Print SimplePreprocessor call statistics only."""
    print("\n=== SimplePreprocessor Call Statistics ===")
    print(f"Total process_structured calls: {_stats['call_count']}")
    print("\nTop 20 most processed files:")
    for filepath, count in _stats['files_processed'].most_common(20):
        print(f"  {count:6d}x  {filepath}")
    print("\nTop 20 call contexts:")
    for context, count in _stats['call_contexts'].most_common(20):
        print(f"  {count:6d}x  {context}")