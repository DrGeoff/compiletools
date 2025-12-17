import os
import io
from contextlib import redirect_stdout

import compiletools.cppdeps
import compiletools.testhelper as uth


@uth.requires_functional_compiler
def test_cppdeps():
    uth.reset()
    
    with uth.CPPDepsTestContext(
        variant_configs=['blank.conf'],
        reload_modules=[compiletools.headerdeps, compiletools.cppdeps]
    ):
        output_buffer = io.StringIO()
        with redirect_stdout(output_buffer):
            compiletools.cppdeps.main(
                [os.path.join(uth.samplesdir(), "numbers/test_direct_include.cpp")]
            )
        
        output = output_buffer.getvalue().strip().split()
        expected_output = [
            os.path.join(uth.samplesdir(), "numbers/get_double.hpp"),
            os.path.join(uth.samplesdir(), "numbers/get_int.hpp"),
            os.path.join(uth.samplesdir(), "numbers/get_numbers.hpp"),
        ]
        assert sorted(expected_output) == sorted(output)


