import sys
import os
import re
from textwrap import dedent

# Add the parent directory to sys.path so we can import ct modules
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from compiletools.simple_preprocessor import SimplePreprocessor, compute_macro_hash
from compiletools.file_analyzer import FileAnalysisResult, PreprocessorDirective


class TestSimplePreprocessor:
    """Unit tests for the SimplePreprocessor class"""

    def setup_method(self):
        """Set up test fixtures before each test method."""
        import stringzilla as sz
        # Clear preprocessing cache before each test
        from compiletools.preprocessing_cache import clear_cache
        clear_cache()

        self.macros = {
            sz.Str('TEST_MACRO'): sz.Str('1'),
            sz.Str('FEATURE_A'): sz.Str('1'),
            sz.Str('VERSION'): sz.Str('3'),
            sz.Str('COUNT'): sz.Str('5')
        }
        self.processor = SimplePreprocessor(self.macros, verbose=0)
    
    def _create_file_analysis_result(self, text):
        """Helper to create FileAnalysisResult for testing"""
        lines = text.split('\n')
        
        # Create line_byte_offsets
        line_byte_offsets = []
        offset = 0
        for line in lines:
            line_byte_offsets.append(offset)
            offset += len(line.encode('utf-8')) + 1  # +1 for \n
        
        # Parse preprocessor directives
        directives = []
        directive_by_line = {}
        directive_positions = {}
        
        for line_num, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('#'):
                # Parse directive
                match = re.match(r'^\s*#\s*([a-zA-Z_]+)(?:\s+(.*))?', stripped)
                if match:
                    directive_type = match.group(1)
                    rest = match.group(2) or ""
                    
                    # Determine directive-specific fields
                    condition = None
                    macro_name = None
                    macro_value = None
                    
                    if directive_type in ['if', 'elif']:
                        import stringzilla as sz
                        condition = sz.Str(rest.strip())
                    elif directive_type in ['ifdef', 'ifndef']:
                        import stringzilla as sz
                        macro_name = sz.Str(rest.strip())
                    elif directive_type == 'define':
                        import stringzilla as sz
                        parts = rest.split(None, 1)
                        macro_name = sz.Str(parts[0]) if parts else sz.Str("")
                        macro_value = sz.Str(parts[1]) if len(parts) > 1 else sz.Str("1")
                        # Handle function-like macros
                        if '(' in str(macro_name):
                            macro_name = sz.Str(str(macro_name).split('(')[0])
                    elif directive_type == 'undef':
                        import stringzilla as sz
                        macro_name = sz.Str(rest.strip())
                    
                    directive = PreprocessorDirective(
                        line_num=line_num,
                        byte_pos=line_byte_offsets[line_num],
                        directive_type=directive_type,
                        continuation_lines=0,
                        condition=condition,
                        macro_name=macro_name,
                        macro_value=macro_value
                    )
                    
                    directives.append(directive)
                    directive_by_line[line_num] = directive
                    
                    # Track positions by type for compatibility
                    if directive_type not in directive_positions:
                        directive_positions[directive_type] = []
                    directive_positions[directive_type].append(line_byte_offsets[line_num])
        
        return FileAnalysisResult(
            line_count=len(lines),
            line_byte_offsets=line_byte_offsets,
            include_positions=[],
            magic_positions=[],
            directive_positions=directive_positions,
            directives=directives,
            directive_by_line=directive_by_line,
            bytes_analyzed=len(text.encode('utf-8')),
            was_truncated=False,
            includes=[],
            defines=[],
            magic_flags=[]
        )

    def test_expression_evaluation_basic(self):
        """Test basic expression evaluation"""
        # Test simple numeric expressions
        assert self.processor._evaluate_expression('1') == 1
        assert self.processor._evaluate_expression('0') == 0
        assert self.processor._evaluate_expression('1 + 1') == 2
        
    def test_expression_evaluation_comparisons(self):
        """Test comparison operators"""
        # Test == operator
        assert self.processor._evaluate_expression('1 == 1') == 1
        assert self.processor._evaluate_expression('1 == 0') == 0
        
        # Test != operator (this is the problematic one)
        assert self.processor._evaluate_expression('1 != 0') == 1
        assert self.processor._evaluate_expression('1 != 1') == 0
        
        # Test > operator
        assert self.processor._evaluate_expression('2 > 1') == 1
        assert self.processor._evaluate_expression('1 > 2') == 0
        
    def test_expression_evaluation_logical(self):
        """Test logical operators"""
        # Test && operator
        assert self.processor._evaluate_expression('1 && 1') == 1
        assert self.processor._evaluate_expression('1 && 0') == 0
        assert self.processor._evaluate_expression('0 && 1') == 0
        
        # Test || operator  
        assert self.processor._evaluate_expression('1 || 0') == 1
        assert self.processor._evaluate_expression('0 || 1') == 1
        assert self.processor._evaluate_expression('0 || 0') == 0
        
    def test_expression_evaluation_complex(self):
        """Test complex expressions combining operators"""
        # Test combinations
        assert self.processor._evaluate_expression('1 != 0 && 2 > 1') == 1
        assert self.processor._evaluate_expression('1 == 0 || 2 == 2') == 1
        assert self.processor._evaluate_expression('(1 + 1) == 2') == 1
        
    def test_macro_expansion(self):
        """Test macro expansion in expressions"""
        # Test simple macro expansion
        assert self.processor._evaluate_expression('TEST_MACRO') == 1
        assert self.processor._evaluate_expression('VERSION') == 3
        
        # Test macro in comparisons
        assert self.processor._evaluate_expression('VERSION == 3') == 1
        assert self.processor._evaluate_expression('VERSION != 2') == 1
        assert self.processor._evaluate_expression('COUNT > 3') == 1
        
    def test_defined_expressions(self):
        """Test defined() expressions"""
        # Test defined() function
        assert self.processor._evaluate_expression('defined(TEST_MACRO)') == 1
        assert self.processor._evaluate_expression('defined(UNDEFINED_MACRO)') == 0
        
        # Test defined() in complex expressions
        assert self.processor._evaluate_expression('defined(TEST_MACRO) && TEST_MACRO == 1') == 1
        assert self.processor._evaluate_expression('defined(VERSION) && VERSION > 2') == 1
        
    def test_conditional_compilation_ifdef(self):
        """Test #ifdef handling"""
        text = dedent('''
            #ifdef TEST_MACRO
            #include "test.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 contains '#include "test.h"'
        assert 1 in active_lines
        
    def test_conditional_compilation_ifndef(self):
        """Test #ifndef handling"""
        text = dedent('''
            #ifndef UNDEFINED_MACRO
            #include "test.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 contains '#include "test.h"'
        assert 1 in active_lines
        
    def test_conditional_compilation_if_simple(self):
        """Test simple #if handling"""
        text = dedent('''
            #if VERSION == 3
            #include "version3.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 contains '#include "version3.h"'
        assert 1 in active_lines
        
    def test_conditional_compilation_if_complex(self):
        """Test complex #if expressions"""
        text = dedent('''
            #if defined(VERSION) && VERSION > 2
            #include "advanced.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 contains '#include "advanced.h"'
        assert 1 in active_lines
        
    def test_conditional_compilation_if_with_not_equal(self):
        """Test #if with != operator (the problematic case)"""
        text = dedent('''
            #if COUNT != 0
            #include "nonzero.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 contains '#include "nonzero.h"'
        assert 1 in active_lines
        
    def test_conditional_compilation_nested(self):
        """Test nested conditional compilation"""
        text = dedent('''
            #ifdef TEST_MACRO
                #if VERSION >= 3
                    #include "test_v3.h"
                #endif
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 2 contains '#include "test_v3.h"'
        assert 2 in active_lines
        
    def test_conditional_compilation_else(self):
        """Test #else handling"""
        text = dedent('''
            #ifdef UNDEFINED_MACRO
            #include "undefined.h"
            #else
            #include "defined.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 3 contains '#include "defined.h"', line 1 should not be active
        assert 3 in active_lines
        assert 1 not in active_lines
        
    def test_conditional_compilation_elif(self):
        """Test #elif handling"""
        text = dedent('''
            #if VERSION == 1
            #include "version1.h"
            #elif VERSION == 2
            #include "version2.h" 
            #elif VERSION == 3
            #include "version3.h"
            #else
            #include "default.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 5 contains '#include "version3.h"', others should not be active
        assert 5 in active_lines
        assert 1 not in active_lines
        assert 3 not in active_lines
        assert 7 not in active_lines
        
    def test_macro_define_and_use(self):
        """Test #define and subsequent use"""
        text = dedent('''
            #define NEW_MACRO 42
            #if NEW_MACRO == 42
            #include "forty_two.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 0 contains #define, line 2 contains '#include "forty_two.h"'
        assert 0 in active_lines
        assert 2 in active_lines
        
    def test_macro_undef(self):
        """Test #undef functionality"""
        text = dedent('''
            #ifdef TEST_MACRO
            #include "before_undef.h"
            #endif
            #undef TEST_MACRO
            #ifdef TEST_MACRO
            #include "after_undef.h"
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        # Line 1 should be active, line 3 has #undef, line 5 should not be active
        assert 1 in active_lines
        assert 3 in active_lines  # #undef directive
        assert 5 not in active_lines


    def test_failing_scenario_use_epoll(self):
        """Test the exact scenario that's failing in the nested macros test"""
        import stringzilla as sz
        # Set up macros exactly as in the failing test
        failing_macros = {
            sz.Str('BUILD_CONFIG'): sz.Str('2'),
            sz.Str('__linux__'): sz.Str('1'),
            sz.Str('USE_EPOLL'): sz.Str('1'),
            sz.Str('ENABLE_THREADING'): sz.Str('1'),
            sz.Str('THREAD_COUNT'): sz.Str('4'),
            sz.Str('NUMA_SUPPORT'): sz.Str('1')
        }
        processor = SimplePreprocessor(failing_macros, verbose=0)
        
        # Test the exact problematic condition
        text = dedent('''
            #if defined(USE_EPOLL) && USE_EPOLL != 0
                #ifdef ENABLE_THREADING
                    #if defined(THREAD_COUNT) && THREAD_COUNT > 1
                        #include "linux_epoll_threading.hpp"
                        #ifdef NUMA_SUPPORT
                            #if NUMA_SUPPORT == 1
                                #include "numa_threading.hpp"
                            #endif
                        #endif
                    #endif
                #endif
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = processor.process_structured(file_result)
        
        # These should be included (lines 3 and 6)
        assert 3 in active_lines  # #include "linux_epoll_threading.hpp"
        assert 6 in active_lines  # #include "numa_threading.hpp"

    def test_recursive_macro_expansion(self):
        """Test recursive macro expansion functionality"""
        # Test simple case
        result = self.processor._recursive_expand_macros('VERSION')
        assert result == '3'
        
        # Test recursive expansion
        import stringzilla as sz
        processor_with_recursive = SimplePreprocessor({
            sz.Str('A'): sz.Str('B'),
            sz.Str('B'): sz.Str('C'),
            sz.Str('C'): sz.Str('42')
        }, verbose=0)

        result = processor_with_recursive._recursive_expand_macros('A')
        assert result == '42'

        # Test max iterations protection (prevent infinite loops)
        processor_with_loop = SimplePreprocessor({
            sz.Str('X'): sz.Str('Y'),
            sz.Str('Y'): sz.Str('X')
        }, verbose=0)
        
        result = processor_with_loop._recursive_expand_macros('X', max_iterations=5)
        # Should stop after max_iterations and return last value
        assert result in ['X', 'Y']  # Could be either depending on iteration count

    def test_comment_stripping(self):
        """Test C++ style comment stripping from expressions"""
        # Test basic comment stripping
        result = self.processor._strip_comments('1 + 1 // this is a comment')
        assert result == '1 + 1'
        
        # Test expression without comments
        result = self.processor._strip_comments('1 + 1')
        assert result == '1 + 1'
        
        # Test comment at beginning
        result = self.processor._strip_comments('// comment only')
        assert result == ''

    def test_platform_macros(self):
        """Test platform-specific macro initialization via compiler_macros"""
        import compiletools.compiler_macros
        
        # Since our simplified compiler_macros only queries the compiler,
        # and doesn't add platform macros without a compiler,
        # we'll test both with and without a compiler
        
        # Test 1: Without compiler (empty path)
        import stringzilla as sz
        macros_empty_raw = compiletools.compiler_macros.get_compiler_macros('', verbose=0)
        macros_empty = {sz.Str(k): sz.Str(v) for k, v in macros_empty_raw.items()}
        processor_empty = SimplePreprocessor(macros_empty, verbose=0)
        # Should work with empty macros
        assert processor_empty.macros == macros_empty
        
        # Test 2: With mocked compiler response
        from unittest.mock import patch, MagicMock
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "#define __linux__ 1\n#define __GNUC__ 11\n#define __x86_64__ 1"
        
        with patch('subprocess.run', return_value=mock_result):
            # Clear cache to ensure fresh call
            compiletools.compiler_macros.clear_cache()
            macros_raw = compiletools.compiler_macros.get_compiler_macros('gcc', verbose=0)
            macros = {sz.Str(k): sz.Str(v) for k, v in macros_raw.items()}
            processor = SimplePreprocessor(macros, verbose=0)
            
            # Verify the mocked macros are present
            import stringzilla as sz
            assert sz.Str('__linux__') in processor.macros
            assert processor.macros[sz.Str('__linux__')] == sz.Str('1')
            assert sz.Str('__GNUC__') in processor.macros
            assert processor.macros[sz.Str('__GNUC__')] == sz.Str('11')

    def test_if_with_comments(self):
        """Test #if directive with C++ style comments"""
        text = dedent('''
            #if 1 // this should be true
                included_line
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        assert 1 in active_lines

    def test_block_comment_stripping(self):
        """Test that block comments do not break expression parsing"""
        text = dedent('''
            #if /* block */ 1 /* more */
            ok
            #endif
        ''').strip()
        file_result = self._create_file_analysis_result(text)
        active_lines = self.processor.process_structured(file_result)
        assert 1 in active_lines

    def test_numeric_literal_parsing(self):
        """Test hex, binary, and octal numeric literals in expressions"""
        assert self.processor._evaluate_expression('0x10 == 16') == 1
        assert self.processor._evaluate_expression('0b1010 == 10') == 1
        assert self.processor._evaluate_expression('010 == 8') == 1  # octal
        assert self.processor._evaluate_expression('0 == 0') == 1

    def test_bitwise_operators(self):
        """Test bitwise and shift operators in expressions"""
        assert self.processor._evaluate_expression('1 & 1') == 1
        assert self.processor._evaluate_expression('1 | 0') == 1
        assert self.processor._evaluate_expression('1 ^ 1') == 0
        assert self.processor._evaluate_expression('~0 == -1') == 1
        assert self.processor._evaluate_expression('(1 << 3) == 8') == 1
        assert self.processor._evaluate_expression('(8 >> 2) == 2') == 1


class TestMacroHashConsistency:
    """Unit tests for macro hash computation consistency (Phase 0)"""

    def test_hash_determinism(self):
        """Verify same macro state always produces same hash."""
        import stringzilla as sz

        macros = {
            sz.Str("FOO"): sz.Str("1"),
            sz.Str("BAR"): sz.Str("value"),
            sz.Str("BAZ"): sz.Str("0x100")
        }

        hash1 = compute_macro_hash(macros)
        hash2 = compute_macro_hash(macros)

        assert hash1 == hash2, "Same macro state should produce same hash"
        assert len(hash1) == 16, f"Hash should be 16 characters, got {len(hash1)}"

    def test_hash_ordering_independence(self):
        """Verify hash is same regardless of insertion order."""
        import stringzilla as sz

        # Create dicts with different insertion orders
        macros1 = {
            sz.Str("A"): sz.Str("1"),
            sz.Str("B"): sz.Str("2"),
            sz.Str("C"): sz.Str("3")
        }

        macros2 = {
            sz.Str("C"): sz.Str("3"),
            sz.Str("A"): sz.Str("1"),
            sz.Str("B"): sz.Str("2")
        }

        hash1 = compute_macro_hash(macros1)
        hash2 = compute_macro_hash(macros2)

        assert hash1 == hash2, "Hash should be independent of insertion order"

    def test_hash_sensitivity_to_changes(self):
        """Verify different macro states produce different hashes."""
        import stringzilla as sz

        macros1 = {sz.Str("FOO"): sz.Str("1")}
        macros2 = {sz.Str("FOO"): sz.Str("2")}  # Different value
        macros3 = {sz.Str("BAR"): sz.Str("1")}  # Different key
        macros4 = {
            sz.Str("FOO"): sz.Str("1"),
            sz.Str("BAR"): sz.Str("2")
        }  # Additional key

        hash1 = compute_macro_hash(macros1)
        hash2 = compute_macro_hash(macros2)
        hash3 = compute_macro_hash(macros3)
        hash4 = compute_macro_hash(macros4)

        assert hash1 != hash2, "Different macro values should produce different hashes"
        assert hash1 != hash3, "Different macro keys should produce different hashes"
        assert hash1 != hash4, "Additional macros should produce different hash"
        assert hash2 != hash3, "All different states should have unique hashes"
        assert hash2 != hash4, "All different states should have unique hashes"
        assert hash3 != hash4, "All different states should have unique hashes"

    def test_hash_empty_macro_state(self):
        """Verify empty macro state has consistent hash."""

        empty1 = {}
        empty2 = {}

        hash1 = compute_macro_hash(empty1)
        hash2 = compute_macro_hash(empty2)

        assert hash1 == hash2, "Empty macro states should have same hash"
        assert len(hash1) == 16, f"Hash should be 16 characters, got {len(hash1)}"

    def test_hash_with_special_characters(self):
        """Verify hash handles special characters in macro values."""
        import stringzilla as sz

        macros1 = {
            sz.Str("PATH"): sz.Str("/usr/local/include"),
            sz.Str("FLAGS"): sz.Str("-O2 -g -Wall")
        }

        macros2 = {
            sz.Str("PATH"): sz.Str("/usr/local/include"),
            sz.Str("FLAGS"): sz.Str("-O3 -g -Wall")  # Different flag
        }

        hash1 = compute_macro_hash(macros1)
        hash2 = compute_macro_hash(macros2)

        assert len(hash1) == 16, "Hash should be 16 characters"
        assert hash1 != hash2, "Different values with special chars should have different hashes"

    def test_hash_cross_module_consistency(self):
        """Verify hash computation is consistent and accessible."""
        import stringzilla as sz
        from compiletools.simple_preprocessor import compute_macro_hash

        macros = {
            sz.Str("LINUX"): sz.Str("1"),
            sz.Str("DEBUG"): sz.Str("1"),
            sz.Str("VERSION"): sz.Str("100")
        }

        # Hash computation (used by preprocessing_cache for cache keys)
        hash_result = compute_macro_hash(macros)

        # Verify hash format
        assert len(hash_result) == 16, "Hash should be 16 characters (MD5 hex)"
        assert all(c in '0123456789abcdef' for c in hash_result), "Hash should be hex"

        # Verify it's deterministic
        hash_again = compute_macro_hash(macros)
        assert hash_result == hash_again, "Hash should be deterministic"


