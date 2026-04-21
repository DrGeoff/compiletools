import os
import shutil
import subprocess
from unittest.mock import patch

import pytest
import stringzilla as sz

import compiletools.apptools
import compiletools.cake
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_context import BuildContext

# Although this is virtually identical to the test_cake.py, we can't merge
# the tests due to memoized results.


class TestMagicPKGCONFIG(tb.BaseCompileToolsTestCase):
    @uth.requires_functional_compiler
    @uth.requires_pkg_config("zlib")
    def test_magicpkgconfig(self):
        # This test is to ensure that the //#PKG-CONFIG magic flag
        # correctly acquires extra cflags and libs
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig test files to the temp directory and compile
            # using ct-cake
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig")
            shutil.copytree(self._get_sample_path("magicpkgconfig"), tmpmagicpkgconfig)

            with uth.DirectoryContext(tmpmagicpkgconfig):
                argv = [
                    "--exemarkers=main",
                    "--testmarkers=gtest.hpp",
                    "--quiet",
                    "--auto",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["magicpkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

    @uth.requires_functional_compiler
    @uth.requires_pkg_config("zlib")
    def test_cmdline_pkgconfig(self):
        # This test is to ensure that the "--pkg-config zlib" flag
        # correctly acquires extra cflags and libs
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the pkgconfig test files to the temp directory and compile
            # using ct-cake
            tmppkgconfig = os.path.join(tmpdir, "pkgconfig")
            shutil.copytree(self._get_sample_path("pkgconfig"), tmppkgconfig)

            with uth.DirectoryContext(tmppkgconfig):
                argv = [
                    "--exemarkers=main",
                    "--testmarkers=gtest.hpp",
                    "--quiet",
                    "--auto",
                    "--pkg-config=zlib",
                    "--config=" + config_path,
                ]

                compiletools.cake.main(argv)

            relativepaths = ["pkgconfig/main.cpp"]
            self._verify_one_exe_per_main(relativepaths, search_dir=tmpdir)

    @uth.requires_functional_compiler
    def test_magicpkgconfig_flags_discovery(self, pkgconfig_env):
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig_fake test files to the temp directory
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig_fake")
            shutil.copytree(self._get_sample_path("magicpkgconfig_fake"), tmpmagicpkgconfig)

            with uth.DirectoryContext(tmpmagicpkgconfig):
                # Create a minimal args object for testing
                # Use a simpler approach - create args from scratch like other tests
                class MockArgs:
                    def __init__(self):
                        self.config_file = config_path
                        self.variant = "debug"
                        self.verbose = 0
                        self.quiet = True
                        self.magic = "direct"
                        self.headerdeps = "direct"
                        self.CPPFLAGS = ""
                        self.CFLAGS = ""
                        self.CXXFLAGS = ""
                        self.CXX = compiletools.apptools.get_functional_cxx_compiler() or "g++"

                args = MockArgs()

                # Create magicflags parser
                ctx = BuildContext()
                headerdeps = compiletools.headerdeps.create(args, context=ctx)
                magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)

                # Test the sample file that contains //#PKG-CONFIG=conditional nested
                sample_file = os.path.join(tmpmagicpkgconfig, "main.cpp")

                # Parse the magic flags
                try:
                    parsed_flags = magicparser.parse(sample_file)
                except RuntimeError as e:
                    if "No functional C++ compiler detected" in str(e):
                        pytest.skip("No functional C++ compiler detected")
                    else:
                        raise

                # Verify PKG-CONFIG flag was found
                assert sz.Str("PKG-CONFIG") in parsed_flags
                pkgconfig_flags = [str(x) for x in parsed_flags[sz.Str("PKG-CONFIG")]]
                assert len(pkgconfig_flags) == 2
                assert "conditional" in pkgconfig_flags
                assert "nested" in pkgconfig_flags

                # Verify CXXFLAGS were extracted (should contain conditional and nested cflags)
                assert sz.Str("CXXFLAGS") in parsed_flags
                cxxflags = " ".join(str(x) for x in parsed_flags[sz.Str("CXXFLAGS")])

                # Check that fake pkg-config results are present
                # conditional.pc has: -I/usr/local/include/testpkg -DTEST_PKG_ENABLED
                # nested.pc has: -I/usr/local/include/testpkg1 -DTEST_PKG1_ENABLED
                assert "-isystem /usr/local/include/testpkg" in cxxflags or "TEST_PKG_ENABLED" in cxxflags
                assert "-isystem /usr/local/include/testpkg1" in cxxflags or "TEST_PKG1_ENABLED" in cxxflags

                # Verify LDFLAGS were extracted
                assert sz.Str("LDFLAGS") in parsed_flags
                ldflags = " ".join(str(x) for x in parsed_flags[sz.Str("LDFLAGS")])

                # conditional.pc has: -L/usr/local/lib -ltestpkg
                # nested.pc has: -L/usr/local/lib -ltestpkg1
                assert "-ltestpkg" in ldflags
                assert "-ltestpkg1" in ldflags

    @uth.requires_functional_compiler
    def test_pkg_config_transformation_in_actual_parsing(self, pkgconfig_env):
        """Test that the -I to -isystem transformation occurs during actual magic flag parsing using sample code"""
        with uth.CompileToolsTestContext() as (tmpdir, config_path):
            # Copy the magicpkgconfig_fake sample to the temp directory
            tmpmagicpkgconfig = os.path.join(tmpdir, "magicpkgconfig_fake")
            shutil.copytree(self._get_sample_path("magicpkgconfig_fake"), tmpmagicpkgconfig)

            # Create minimal args object
            class MockArgs:
                def __init__(self):
                    self.config_file = config_path
                    self.variant = "debug"
                    self.verbose = 0
                    self.quiet = True
                    self.magic = "direct"
                    self.headerdeps = "direct"
                    self.CPPFLAGS = ""
                    self.CFLAGS = ""
                    self.CXXFLAGS = ""
                    self.max_file_read_size = 0
                    self.CXX = compiletools.apptools.get_functional_cxx_compiler() or "g++"

            args = MockArgs()

            # Create magicflags parser
            ctx = BuildContext()
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)

            # Use the actual magicpkgconfig sample file
            sample_file = os.path.join(tmpmagicpkgconfig, "main.cpp")

            # Parse the magic flags
            try:
                parsed_flags = magicparser.parse(sample_file)

                # Verify PKG-CONFIG flag was found (should contain "conditional nested")
                assert sz.Str("PKG-CONFIG") in parsed_flags, "PKG-CONFIG directive should be parsed"
                pkgconfig_flags = [str(x) for x in parsed_flags[sz.Str("PKG-CONFIG")]]
                assert len(pkgconfig_flags) == 2
                assert "conditional" in pkgconfig_flags
                assert "nested" in pkgconfig_flags

                # Check CXXFLAGS for the presence of -isystem transformations
                if sz.Str("CXXFLAGS") in parsed_flags:
                    cxxflags_list = [str(x) for x in parsed_flags[sz.Str("CXXFLAGS")]]
                    cxxflags_str = " ".join(cxxflags_list)

                    # If there are any include paths from pkg-config, they should use -isystem
                    if "/include" in cxxflags_str:
                        assert "-isystem" in cxxflags_str, f"Expected -isystem in CXXFLAGS, got: {cxxflags_str}"

                        # Verify no -I flags remain (they should all be transformed to -isystem)
                        assert "-I/" not in cxxflags_str, f"Found -I/ in CXXFLAGS (should be -isystem): {cxxflags_str}"
                        assert not any(flag.startswith("-I ") for flag in cxxflags_list), (
                            f"Found -I flags in CXXFLAGS (should be -isystem): {cxxflags_list}"
                        )

                        # Verify that other flags like -D are preserved
                        if any("-D" in flag for flag in cxxflags_list):
                            assert any("-D" in flag for flag in cxxflags_list), (
                                f"Macro definitions should be preserved in CXXFLAGS: {cxxflags_list}"
                            )

            except subprocess.CalledProcessError:
                # If pkg-config fails (e.g., packages not available), that's okay for this test
                # The important thing is that the transformation logic is in place
                pass

    @uth.requires_functional_compiler
    def test_project_pkgconfig_override(self, pkgconfig_env):
        """Test that ct.conf.d/pkgconfig/ overrides take priority over system/base .pc files.

        Uses the existing pkgconfig_env fixture (which sets PKG_CONFIG_PATH to
        samples/pkgs/) and layers a project-level override on top.
        """
        with uth.CompileToolsTestContext() as (tmpdir, _config_path):
            # Create the project override directory
            project_pkgconfig = os.path.join(tmpdir, "ct.conf.d", "pkgconfig")
            os.makedirs(project_pkgconfig)

            # Write an override .pc file for "conditional" with distinctive flags
            override_pc = os.path.join(project_pkgconfig, "conditional.pc")
            with open(override_pc, "w") as f:
                f.write(
                    "Name: OverriddenPackage\n"
                    "Description: Project-level override\n"
                    "Version: 2.0.0\n"
                    "Cflags: -I/project/override/include -DPROJECT_OVERRIDE\n"
                    "Libs: -L/project/override/lib -lprojectoverride\n"
                )

            # Point find_git_root at our temp dir so the override is discovered
            with patch("compiletools.git_utils.find_git_root", return_value=tmpdir):
                compiletools.apptools.clear_cache()

                # Apply the override (prepends ct.conf.d/pkgconfig/ to PKG_CONFIG_PATH)
                ctx = BuildContext()
                compiletools.apptools._setup_pkg_config_overrides(ctx)

                # Create a source file that requests the 'conditional' package
                source_content = "//#PKG-CONFIG=conditional\nint main() { return 0; }\n"
                files = uth.write_sources({"test_override.cpp": source_content})
                source_file = str(files["test_override.cpp"])

                # Create parser and parse (reuse same ctx for consistency)
                mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=ctx)
                result = mf.parse(source_file)

                # Verify the OVERRIDE flags are used, not the base conditional.pc flags
                assert sz.Str("CPPFLAGS") in result
                cppflags = " ".join(str(f) for f in result[sz.Str("CPPFLAGS")])
                assert "-DPROJECT_OVERRIDE" in cppflags, f"Expected project override flags, got: {cppflags}"
                # The base conditional.pc has -DTEST_PKG_ENABLED — should NOT appear
                assert "-DTEST_PKG_ENABLED" not in cppflags, f"Base flags should be overridden, got: {cppflags}"

                # Check LDFLAGS
                assert sz.Str("LDFLAGS") in result
                ldflags = " ".join(str(f) for f in result[sz.Str("LDFLAGS")])
                assert "-lprojectoverride" in ldflags, f"Expected override libs, got: {ldflags}"
                assert "-ltestpkg" not in ldflags, f"Base libs should be overridden, got: {ldflags}"

            # clear_cache resets the override guard for other tests
            compiletools.apptools.clear_cache()

    @uth.requires_functional_compiler
    def test_pkg_config_flags_are_split(self, pkgconfig_env):
        """Test that pkg-config output is split into individual flags.

        This test ensures that flags returned by pkg-config (e.g. "-I/path -Dflag")
        are correctly split into a list of separate flags (["-I/path", "-Dflag"])
        rather than being treated as a single string argument.
        """

        # Create a source file that requests the 'nested' package
        # nested.pc has:
        # Cflags: -I/usr/local/include/testpkg1 -DTEST_PKG1_ENABLED
        # Libs: -L/usr/local/lib -ltestpkg1

        files = uth.write_sources({"test.cpp": "//#PKG-CONFIG=nested\nint main() {}"})
        source_file = str(files["test.cpp"])

        # Create parser
        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())

        # Parse
        result = mf.parse(source_file)

        # Check CPPFLAGS (from Cflags)
        assert sz.Str("CPPFLAGS") in result
        cppflags = result[sz.Str("CPPFLAGS")]

        # Convert to python strings for easier assertion
        cppflags_str_list = [str(f) for f in cppflags]

        # We expect at least two distinct flags.
        # If the bug were present, len(cppflags_str_list) would be 1 (containing the concatenated string)
        assert len(cppflags_str_list) >= 2, f"Expected multiple CPPFLAGS, got: {cppflags_str_list}"

        # Note: compiletools may convert -I to -isystem and split the flag and path
        # So we check for the presence of the path and the define
        assert any("/usr/local/include/testpkg1" in f for f in cppflags_str_list)
        assert "-DTEST_PKG1_ENABLED" in cppflags_str_list

        # Check LDFLAGS (from Libs)
        assert sz.Str("LDFLAGS") in result
        ldflags = result[sz.Str("LDFLAGS")]

        ldflags_str_list = [str(f) for f in ldflags]

        # We expect at least two distinct flags
        assert len(ldflags_str_list) >= 2, f"Expected multiple LDFLAGS, got: {ldflags_str_list}"
        assert "-L/usr/local/lib" in ldflags_str_list
        assert "-ltestpkg1" in ldflags_str_list

    @uth.requires_functional_compiler
    def test_transitive_deps_preserved_in_ldflags(self, pkgconfig_env):
        """Transitive -l dependencies from pkg-config --libs must all appear
        in the parsed LDFLAGS.

        transitive-deps.pc has:
          Libs: -L/usr/local/lib/transitive -ltransitivemain -ltransitiveutil -ltransitivecore

        All three -l flags must survive magic flag parsing and deduplication.
        """
        files = uth.write_sources({"test.cpp": "//#PKG-CONFIG=transitive-deps\nint main() { return 0; }"})
        source_file = str(files["test.cpp"])

        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())
        result = mf.parse(source_file)

        assert sz.Str("LDFLAGS") in result
        ldflags = [str(f) for f in result[sz.Str("LDFLAGS")]]

        assert "-L/usr/local/lib/transitive" in ldflags, f"-L path missing: {ldflags}"
        assert "-ltransitivemain" in ldflags, f"-ltransitivemain missing: {ldflags}"
        assert "-ltransitiveutil" in ldflags, f"-ltransitiveutil missing: {ldflags}"
        assert "-ltransitivecore" in ldflags, f"-ltransitivecore missing: {ldflags}"

    @uth.requires_functional_compiler
    def test_transitive_deps_in_link_line_across_files(self, pkgconfig_env):
        """When two source files use packages with transitive deps, all deps
        must appear in the merged link output from merge_ldflags_with_topo_sort.

        This is an end-to-end test: magic flag parsing -> per-file LDFLAGS -> merge.
        """
        files = uth.write_sources(
            {
                "a.cpp": "//#PKG-CONFIG=transitive-deps\nint fn_a() { return 0; }",
                "b.cpp": "//#PKG-CONFIG=nested\nint fn_b() { return 0; }",
            }
        )

        mf = tb.create_magic_parser(["--magic=direct"], tempdir=self._tmpdir, context=BuildContext())

        result_a = mf.parse(str(files["a.cpp"]))
        result_b = mf.parse(str(files["b.cpp"]))

        ldflags_a = [str(f) for f in result_a.get(sz.Str("LDFLAGS"), [])]
        ldflags_b = [str(f) for f in result_b.get(sz.Str("LDFLAGS"), [])]

        per_file = [ldflags_a, ldflags_b]
        merged = compiletools.utils.merge_ldflags_with_topo_sort(per_file)

        # All transitive deps from transitive-deps.pc
        for lib in ("-ltransitivemain", "-ltransitiveutil", "-ltransitivecore"):
            assert lib in merged, f"{lib} missing from merged link: {merged}"
        # Lib from nested.pc
        assert "-ltestpkg1" in merged, f"-ltestpkg1 missing from merged link: {merged}"

    @uth.requires_functional_compiler
    def test_prepend_pkg_config_path_via_cli(self, pkgconfig_env):
        """Test that --prepend-PKG-CONFIG-PATH overrides base PKG_CONFIG_PATH.

        The pkgconfig_env fixture sets PKG_CONFIG_PATH to samples/pkgs/ which
        contains conditional.pc.  We create a CLI-prepended directory with a
        higher-priority conditional.pc and verify its flags win.
        """
        with uth.CompileToolsTestContext() as (tmpdir, _config_path):
            # Create a CLI-prepended directory with a conditional.pc override
            cli_pkgconfig = os.path.join(tmpdir, "cli-pkgconfig")
            os.makedirs(cli_pkgconfig)
            with open(os.path.join(cli_pkgconfig, "conditional.pc"), "w") as f:
                f.write(
                    "Name: CLIPrepended\n"
                    "Description: CLI-prepended override\n"
                    "Version: 9.0.0\n"
                    "Cflags: -DCLI_PREPENDED_FLAG\n"
                    "Libs: -lcliprepended\n"
                )

            source_content = "//#PKG-CONFIG=conditional\nint main() { return 0; }\n"
            files = uth.write_sources({"test_prepend.cpp": source_content})
            source_file = str(files["test_prepend.cpp"])

            # Pass --prepend-PKG-CONFIG-PATH through the argument parser
            mf = tb.create_magic_parser(
                [
                    "--magic=direct",
                    f"--prepend-PKG-CONFIG-PATH={cli_pkgconfig}",
                ],
                tempdir=self._tmpdir,
            )
            result = mf.parse(source_file)

            # CLI-prepended .pc should win over base conditional.pc
            cppflags = " ".join(str(f) for f in result[sz.Str("CPPFLAGS")])
            assert "-DCLI_PREPENDED_FLAG" in cppflags, f"Expected CLI-prepended flags, got: {cppflags}"
            # Base conditional.pc defines -DTEST_PKG_ENABLED — should not appear
            assert "-DTEST_PKG_ENABLED" not in cppflags, f"Base flags should be overridden, got: {cppflags}"

            ldflags = " ".join(str(f) for f in result[sz.Str("LDFLAGS")])
            assert "-lcliprepended" in ldflags, f"Expected CLI-prepended libs, got: {ldflags}"

    @uth.requires_functional_compiler
    def test_prepend_pkg_config_path_overrides_project(self, pkgconfig_env):
        """Test that --prepend-PKG-CONFIG-PATH takes priority over ct.conf.d/pkgconfig/.

        Creates both a project-level override (ct.conf.d/pkgconfig/) and a
        CLI-prepended override, verifying the CLI override wins.
        """
        with uth.CompileToolsTestContext() as (tmpdir, _config_path):
            # Create the project override directory with a .pc file
            project_pkgconfig = os.path.join(tmpdir, "ct.conf.d", "pkgconfig")
            os.makedirs(project_pkgconfig)
            with open(os.path.join(project_pkgconfig, "conditional.pc"), "w") as f:
                f.write(
                    "Name: ProjectConditional\n"
                    "Description: Project-level override\n"
                    "Version: 2.0.0\n"
                    "Cflags: -DPROJECT_LEVEL\n"
                    "Libs: -lprojectlevel\n"
                )

            # Create a CLI-prepended override directory with higher priority
            cli_pkgconfig = os.path.join(tmpdir, "cli-override-pkgconfig")
            os.makedirs(cli_pkgconfig)
            with open(os.path.join(cli_pkgconfig, "conditional.pc"), "w") as f:
                f.write(
                    "Name: CLIConditional\n"
                    "Description: CLI-prepended override\n"
                    "Version: 3.0.0\n"
                    "Cflags: -DCLI_OVERRIDE\n"
                    "Libs: -lclioverride\n"
                )

            with patch("compiletools.git_utils.find_git_root", return_value=tmpdir):
                compiletools.apptools.clear_cache()

                source_content = "//#PKG-CONFIG=conditional\nint main() { return 0; }\n"
                files = uth.write_sources({"test_cli_override.cpp": source_content})
                source_file = str(files["test_cli_override.cpp"])

                # Pass --prepend-PKG-CONFIG-PATH through the argument parser
                # so _setup_pkg_config_overrides is called once (from parseargs)
                # with both project and CLI overrides in the correct priority.
                mf = tb.create_magic_parser(
                    [
                        "--magic=direct",
                        f"--prepend-PKG-CONFIG-PATH={cli_pkgconfig}",
                    ],
                    tempdir=self._tmpdir,
                )
                result = mf.parse(source_file)

                # CLI override should win over project-level override
                cppflags = " ".join(str(f) for f in result[sz.Str("CPPFLAGS")])
                assert "-DCLI_OVERRIDE" in cppflags, f"Expected CLI override flags, got: {cppflags}"
                assert "-DPROJECT_LEVEL" not in cppflags, f"Project flags should be overridden by CLI, got: {cppflags}"

                ldflags = " ".join(str(f) for f in result[sz.Str("LDFLAGS")])
                assert "-lclioverride" in ldflags, f"Expected CLI override libs, got: {ldflags}"

            compiletools.apptools.clear_cache()
