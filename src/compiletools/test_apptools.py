import compiletools.apptools
import compiletools.testhelper as uth
import os
import argparse  # Used for the parse_args test
import configargparse


class FakeNamespace(object):
    def __init__(self):
        self.n1 = "v1_noquotes"
        self.n2 = '"v2_doublequotes"'
        self.n3 = "'v3_singlequotes'"
        self.n4 = '''"''v4_lotsofquotes''"'''

    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)


class TestFuncs:
    def test_strip_quotes(self):
        fns = FakeNamespace()
        compiletools.apptools._strip_quotes(fns)
        assert fns.n1 == "v1_noquotes"
        assert fns.n2 == "v2_doublequotes"
        assert fns.n3 == "v3_singlequotes"
        assert fns.n4 == "v4_lotsofquotes"

    def test_parse_args_strips_quotes(self):
        cmdline = [
            "--append-CPPFLAGS",
            '"-DNEWPROTOCOL -DV172"',
            "--append-CXXFLAGS",
            '"-DNEWPROTOCOL -DV172"',
        ]
        ap = argparse.ArgumentParser()
        ap.add_argument("--append-CPPFLAGS", action="append")
        ap.add_argument("--append-CXXFLAGS", action="append")
        args = ap.parse_args(cmdline)

        compiletools.apptools._strip_quotes(args)
        assert args.append_CPPFLAGS == ["-DNEWPROTOCOL -DV172"]
        assert args.append_CXXFLAGS == ["-DNEWPROTOCOL -DV172"]

    def test_extract_system_include_paths_with_quoted_spaces(self):
        """Test that extract_system_include_paths correctly parses quoted include paths with spaces.

        This test validates the shlex.split() fix that replaced the original regex-based
        approach. The regex approach would fail to handle shell quoting correctly.
        """

        # Create a mock args object with quoted include paths containing spaces
        class MockArgs:
            def __init__(self):
                # Test case that would break with regex but works with shlex
                self.CPPFLAGS = '-DSOME_MACRO -isystem "/path with spaces/include" -DANOTHER_MACRO'
                self.CXXFLAGS = '-I "/another path/headers" -std=c++17'

        mock_args = MockArgs()

        # Test the extract_system_include_paths function directly
        extracted_paths = compiletools.apptools.extract_system_include_paths(mock_args, verbose=9)

        print("\nShlex parsing test results:")
        print(f"CPPFLAGS: {mock_args.CPPFLAGS}")
        print(f"CXXFLAGS: {mock_args.CXXFLAGS}")
        print(f"Extracted include paths: {extracted_paths}")

        # Expected behavior: shlex should correctly parse quoted paths with spaces
        expected_paths = ["/path with spaces/include", "/another path/headers"]

        # Verify that both paths with spaces are correctly extracted
        for expected_path in expected_paths:
            assert expected_path in extracted_paths, \
                f"Failed to extract quoted path '{expected_path}'. Got: {extracted_paths}. " \
                f"This suggests shlex.split() fix didn't work or regressed to regex parsing."

        print("✓ Shlex parsing correctly handles quoted include paths with spaces!")

    def test_set_project_version_escaping(self):
        """Test that _set_project_version properly escapes version strings for C string literals.

        This test validates that:
        1. Version strings are wrapped in quotes to create C string literals
        2. Backslashes and double quotes within version strings are properly escaped
        3. Shell quoting is correctly applied for safe command-line passing
        """
        import shlex

        test_cases = [
            # (input_version, description)
            ('1.2.3', 'simple version'),
            ('simple-0.0.0-0', 'dash-separated'),
            ('my project-1.0.0', 'version with spaces'),
            ("it's-1.0", 'single quote in version'),
            ('v"special"-1.0', 'double quote in version'),
            ('path\\to\\version', 'backslash in version'),
            ('complex"name\\path-1.0', 'both quotes and backslashes'),
        ]

        for version, description in test_cases:
            print(f"\nTesting {description}: {version}")

            # Create mock args object
            class MockArgs:
                def __init__(self):
                    self.projectversion = version
                    self.CPPFLAGS = ''
                    self.CFLAGS = ''
                    self.CXXFLAGS = ''
                    self.verbose = 0

            mock_args = MockArgs()

            # Call the function
            compiletools.apptools._set_project_version(mock_args)

            print(f"  CPPFLAGS: {mock_args.CPPFLAGS}")

            # Extract the macro definition from CPPFLAGS
            assert '-DCAKE_PROJECT_VERSION=' in mock_args.CPPFLAGS

            # Verify the flag is in all three flag variables
            assert '-DCAKE_PROJECT_VERSION=' in mock_args.CFLAGS
            assert '-DCAKE_PROJECT_VERSION=' in mock_args.CXXFLAGS

            # Parse the flag value using shlex to simulate shell parsing
            # This extracts what the compiler will actually receive
            parts = shlex.split(mock_args.CPPFLAGS)
            version_flag = [p for p in parts if p.startswith('-DCAKE_PROJECT_VERSION=')][0]
            macro_value = version_flag.split('=', 1)[1]

            print(f"  Macro value (as seen by compiler): {macro_value}")

            # Verify it's a quoted string
            assert macro_value.startswith('"'), f"Macro value should start with quote: {macro_value}"
            assert macro_value.endswith('"'), f"Macro value should end with quote: {macro_value}"

            # Verify proper C escaping
            if '\\' in version:
                # Original backslash should be escaped as \\
                assert '\\\\' in macro_value, f"Backslashes should be escaped: {macro_value}"

            if '"' in version:
                # Original quote should be escaped as \"
                assert '\\"' in macro_value, f"Quotes should be escaped: {macro_value}"

            # Critical: verify this would work in actual C code
            # The macro should expand to a valid C string literal
            # For example: CAKE_PROJECT_VERSION expands to "1.2.3"
            # Not to: 1.2.3 (which would be invalid tokens)

            print(f"  ✓ {description} correctly formatted as C string literal")

    def test_set_project_version_integration(self):
        """Integration test: compile and run C code using CAKE_PROJECT_VERSION.

        This test verifies that the generated macro definition actually works
        in real C code compilation.
        """
        import subprocess
        import tempfile
        import shlex
        from textwrap import dedent

        test_versions = [
            '1.2.3',
            'test-0.0.0-0',
            'my project-1.0.0',  # Space
            "it's-1.0",  # Single quote
        ]

        # C source code that uses CAKE_PROJECT_VERSION
        c_source = dedent('''
            #include <stdio.h>
            #include <string.h>

            int main() {
                const char* version = CAKE_PROJECT_VERSION;
                printf("Version: %s\\n", version);
                return strlen(version) > 0 ? 0 : 1;
            }
        ''')

        for version in test_versions:
            print(f"\nIntegration test with version: {version}")

            # Create mock args and set the version
            class MockArgs:
                def __init__(self):
                    self.projectversion = version
                    self.CPPFLAGS = ''
                    self.CFLAGS = ''
                    self.CXXFLAGS = ''
                    self.verbose = 0

            mock_args = MockArgs()
            compiletools.apptools._set_project_version(mock_args)

            # Extract the macro flag
            parts = shlex.split(mock_args.CPPFLAGS)
            version_flag = [p for p in parts if p.startswith('-DCAKE_PROJECT_VERSION=')][0]

            print(f"  Compiler flag: {version_flag}")

            # Create temp files for compilation
            with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False) as f:
                f.write(c_source)
                c_file = f.name

            with tempfile.NamedTemporaryFile(suffix='.out', delete=False) as f:
                out_file = f.name

            try:
                # Compile with the version flag
                compile_cmd = ['gcc', version_flag, c_file, '-o', out_file]
                print(f"  Compile command: {' '.join(compile_cmd)}")

                result = subprocess.run(
                    compile_cmd,
                    capture_output=True,
                    text=True
                )

                if result.returncode != 0:
                    print(f"  COMPILATION FAILED:")
                    print(f"  stdout: {result.stdout}")
                    print(f"  stderr: {result.stderr}")
                    assert False, f"Failed to compile with version '{version}'"

                print(f"  ✓ Compilation successful")

                # Run the compiled program
                run_result = subprocess.run(
                    [out_file],
                    capture_output=True,
                    text=True
                )

                print(f"  Program output: {run_result.stdout.strip()}")

                assert run_result.returncode == 0, \
                    f"Program failed with version '{version}': {run_result.stderr}"

                assert version in run_result.stdout, \
                    f"Version '{version}' not found in output: {run_result.stdout}"

                print(f"  ✓ Program executed successfully with correct version")

            finally:
                # Cleanup
                import os
                try:
                    os.unlink(c_file)
                    os.unlink(out_file)
                except:
                    pass


class TestConfig:
    def setup_method(self):
        uth.reset()

    def _test_variable_handling_method(self, variable_handling_method):
        """If variable_handling_method is set to "override" (default as at 20240917) then
        command-line values override environment variables which override config file values which override defaults.
        If variable_handling_method is set to "append" then variables are appended.
        """
        with uth.TempDirContext(), uth.EnvironmentContext(
            {"CXXFLAGS": "-fdiagnostics-color=always -DVARFROMENV"}
        ):
            uth.create_temp_ct_conf(os.getcwd(), extralines=[f"variable-handling-method={variable_handling_method}"])
            cfgfile = "foo.dbg.conf"
            uth.create_temp_config(os.getcwd(), cfgfile, extralines=['CXXFLAGS="-DVARFROMFILE"'])
            with open(cfgfile, "r") as ff:
                print(ff.read())
            argv = ["--config=foo.dbg.conf", "-v"]
            variant = compiletools.configutils.extract_variant(argv=argv, gitroot=os.getcwd())
            assert variant == "foo.dbg"

            cap = configargparse.getArgumentParser(
                description="Test environment overrides config",
                formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
                auto_env_var_prefix="",
                default_config_files=["ct.conf"],
                args_for_setting_config_path=["-c", "--config"],
                ignore_unknown_config_file_keys=True,
            )
            compiletools.apptools.add_common_arguments(cap)
            compiletools.apptools.add_link_arguments(cap)
            # print(cap.format_help())
            args = compiletools.apptools.parseargs(cap, argv)
            # print(args)
            # Check that the environment variable overrode the config file
            assert variable_handling_method == args.variable_handling_method
            if variable_handling_method == "override":
                assert "-DVARFROMENV" in args.CXXFLAGS
                assert "-DVARFROMFILE" not in args.CXXFLAGS
            elif variable_handling_method == "append":
                assert "-DVARFROMENV" in args.CXXFLAGS
                assert "-DVARFROMFILE" in args.CXXFLAGS
            else:
                assert not "Unknown variable handling method.  Must be override or append."

    def test_environment_overrides_config(self):
        self._test_variable_handling_method(variable_handling_method="override")

    def test_environment_appends_config(self):
        self._test_variable_handling_method(variable_handling_method="append")

    def test_user_config_append_cxxflags(self):
        with uth.TempDirContext():
            uth.create_temp_ct_conf(os.getcwd())
            cfgfile = "foo.dbg.conf"
            uth.create_temp_config(os.getcwd(), cfgfile, extralines=['append-CXXFLAGS="-fdiagnostics-color=always"'])
            with open(cfgfile, "r") as ff:
                print(ff.read())
            argv = ["--config=" + cfgfile, "-v"]
            variant = compiletools.configutils.extract_variant(argv=argv, gitroot=os.getcwd())
            assert variant == "foo.dbg"

            cap = configargparse.getArgumentParser(
                description="Test reading and overriding configs",
                formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
                auto_env_var_prefix="",
                default_config_files=["ct.conf"],
                args_for_setting_config_path=["-c", "--config"],
                ignore_unknown_config_file_keys=True,
            )
            compiletools.apptools.add_common_arguments(cap)
            compiletools.apptools.add_link_arguments(cap)
            # print(cap.format_help())
            args = compiletools.apptools.parseargs(cap, argv)
            # print(args)
            # Check that the append-CXXFLAGS argument made its way into the CXXFLAGS
            assert "-fdiagnostics-color=always" in args.CXXFLAGS
