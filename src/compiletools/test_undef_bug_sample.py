"""Integration test for #undef bug using the undef_bug sample code.

This test validates that the preprocessing cache correctly handles #undef directives
when processing multiple files in sequence, ensuring that macros cleaned up via #undef
do not "resurrect" and affect subsequent conditional compilation.
"""

import pytest
from pathlib import Path

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
from compiletools.test_base import BaseCompileToolsTestCase
from compiletools.testhelper import samplesdir


class TestUndefBugSample(BaseCompileToolsTestCase):
    """Test the undef_bug sample to validate #undef handling."""

    def test_undef_bug_sample_finds_all_headers(self, pkgconfig_env):
        """Test that #undef in cleans_up.hpp allows should_not_see_macro.hpp to be included.

        Dependency chain:
        main.cpp
          -> uses_conditional.hpp
               -> cleans_up.hpp
                    -> defines_macro.hpp (defines TEMP_BUFFER_SIZE)
                    -> #undef TEMP_BUFFER_SIZE
               -> should_be_included.hpp (via #ifndef TEMP_BUFFER_SIZE)

        BUG: If #undef is ignored, TEMP_BUFFER_SIZE persists and #ifndef fails,
        so should_be_included.hpp is NOT included.

        Expected: 4 headers (uses_conditional, cleans_up, defines_macro, should_not_see_macro)
        Buggy: 3 headers (missing should_not_see_macro)
        """
        sample_dir = Path(samplesdir()) / "undef_bug"
        main_cpp = sample_dir / "main.cpp"

        argv = ['-vvv', f'--INCLUDE={sample_dir}', str(main_cpp)]

        cap = compiletools.apptools.create_parser("test_undef_bug", argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.hunter.add_arguments(cap)
        cap.add('filename', nargs='+')

        args = compiletools.apptools.parseargs(cap, argv)

        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        # Get header dependencies
        headers = hunter.header_dependencies(str(main_cpp))
        header_names = [Path(h).name for h in headers]

        print(f"\nHeaders found: {header_names}")

        # Validate all expected headers are present
        assert 'uses_conditional.hpp' in header_names, "uses_conditional.hpp should be found (direct include)"
        assert 'cleans_up.hpp' in header_names, "cleans_up.hpp should be found (included by uses_conditional)"
        assert 'defines_macro.hpp' in header_names, "defines_macro.hpp should be found (included by cleans_up)"

        # This is the critical assertion - tests that #undef was correctly processed
        assert 'should_be_included.hpp' in header_names, \
            "BUG: should_be_included.hpp NOT found!\n" \
            "     This means #undef TEMP_BUFFER_SIZE was ignored.\n" \
            "     The macro persisted after cleans_up.hpp, so #ifndef TEMP_BUFFER_SIZE failed.\n" \
            "     Root cause: preprocessing_cache.py with_updates() merges instead of replacing."

        # Validate PKG-CONFIG extraction from conditionally included header
        magic_flags = hunter.magicflags(str(main_cpp))
        import stringzilla as sz
        pkg_config_key = sz.Str('PKG-CONFIG')
        pkg_configs = [str(f) for f in magic_flags.get(pkg_config_key, [])]

        print(f"PKG-CONFIG flags: {pkg_configs}")

        assert 'leaked-macro-pkg' in pkg_configs, \
            "BUG: PKG-CONFIG=leaked-macro-pkg NOT found!\n" \
            "     This flag is in should_not_see_macro.hpp which was not discovered.\n" \
            "     The #undef bug prevented the header from being included."


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
