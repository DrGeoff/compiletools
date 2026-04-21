import os
import tempfile
from unittest.mock import patch

import configargparse

import compiletools.apptools
import compiletools.configutils
import compiletools.findtargets
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_context import BuildContext


class TestFindTargetsModule:
    def setup_method(self):
        uth.reset()

    def _find_samples_targets(self, disable_tests, disable_exes=False):
        relativeexpectedexes = {
            "macro_state_dependency/sample.cpp",
            "macro_state_dependency/main.cpp",
            "macro_state_dependency/clean_main.cpp",
            "conditional_includes/main.cpp",
            "cppflags_macros/main.cpp",
            "cppflags_macros/multi_flag_test.cpp",
            "cppflags_macros/compiler_builtin_test.cpp",
            "cppflags_macros/elif_test.cpp",
            "cppflags_macros/advanced_preprocessor_test.cpp",
            "cppflags_macros/nested_macros_test.cpp",
            "dottypaths/dottypaths.cpp",
            "feature_headers/main.cpp",
            "has_include/main.cpp",
            "platform_has_include/platform_main.cpp",
            "hunter_macro_propagation/app.cpp",
            "isystem_include_bug/main.cpp",
            "ldflags/conditional_ldflags_test.cpp",
            "ldflags/macro_expanded_ldflags.cpp",
            "ldflags/version_dependent_ldflags.cpp",
            "library/main.cpp",
            "lotsofmagic/lotsofmagic.cpp",
            "macro_deps/main.cpp",
            "magic_processing_order/complex_test.cpp",
            "magicinclude/main.cpp",
            "magicpkgconfig/main.cpp",
            "magicpkgconfig_fake/main.cpp",
            "magicsourceinheader/main.cpp",
            "movingheaders/main.cpp",
            "nestedconfig/nc.cpp",
            "nestedconfig/subdir/nc.cpp",
            "pkgconfig/main.cpp",
            "project_pkgconfig_override/main.cpp",
            "simple/helloworld_c.c",
            "simple/helloworld_cpp.cpp",
            "calculator/main.cpp",
            "duplicate_flags/main.cpp",
            "empty_macro_bug/libs/main.cpp",
            "parse_order_macro_bug/libs/entry_point_1.cpp",
            "parse_order_macro_bug/libs/entry_point_2.cpp",
            "transitive_cache_bug/engine/a-game.cpp",
            "transitive_cache_bug/engine/b-game.cpp",
            "header_guard_bug/main.cpp",
            "undef_bug/main.cpp",
            "computed_include/main.cpp",
            "static_link_order/main.cpp",
            "pch/pch_user.cpp",
        }
        relativeexpectedtests = {
            "cross_platform/test_source.cpp",
            "factory/test_factory.cpp",
            "magic_processing_order/test_macro_transform.cpp",
            "numbers/test_direct_include.cpp",
            "numbers/test_library.cpp",
            "simple/test_cflags.c",
            "serialise_tests/test_flock_1.cpp",
            "serialise_tests/test_flock_2.cpp",
            "version_dependent_api/test_main.cpp",
            "version_dependent_api/test_main_new.cpp",
            "pkg_config_header_deps/src/test.cpp",
        }

        expectedexes = set()
        if not disable_exes:
            expectedexes = {os.path.realpath(os.path.join(uth.samplesdir(), exe)) for exe in relativeexpectedexes}
        expectedtests = set()
        if not disable_tests:
            expectedtests = {os.path.realpath(os.path.join(uth.samplesdir(), tt)) for tt in relativeexpectedtests}

        config_files = compiletools.configutils.config_files_from_variant(exedir=uth.cakedir(), argv=[])
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="TestFindTargetsModule",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.findtargets.add_arguments(cap)
        argv = ["--shorten"]
        if disable_tests:
            argv.append("--disable-tests")
        if disable_exes:
            argv.append("--disable-exes")
        args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
        findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir(), context=BuildContext())
        executabletargets, testtargets = findtargets(path=uth.cakedir())
        assert expectedexes == set(executabletargets)
        assert expectedtests == set(testtargets)

    def test_samples(self):
        self._find_samples_targets(disable_tests=False)

    def test_disable_tests(self):
        self._find_samples_targets(disable_tests=True)

    def test_tests_only(self):
        self._find_samples_targets(disable_tests=False, disable_exes=True)

    def teardown_method(self):
        uth.reset()


class TestFindTargetsStyles:
    """Test output formatting styles."""

    def test_flat_style(self, capsys):
        style = compiletools.findtargets.FlatStyle()
        style(["a.cpp", "b.cpp"], ["t.cpp"])
        assert capsys.readouterr().out == "a.cpp b.cpp t.cpp\n"

    def test_indent_style(self, capsys):
        style = compiletools.findtargets.IndentStyle()
        style(["main.cpp"], [])
        out = capsys.readouterr().out
        assert "Executable Targets:" in out
        assert "\tmain.cpp" in out
        assert "None found" in out  # no tests

    def test_indent_style_no_exes(self, capsys):
        style = compiletools.findtargets.IndentStyle()
        style([], ["test.cpp"])
        out = capsys.readouterr().out
        assert "None found" in out  # no exes
        assert "\ttest.cpp" in out

    def test_args_style(self, capsys):
        style = compiletools.findtargets.ArgsStyle()
        style(["main.cpp"], ["test.cpp"])
        out = capsys.readouterr().out
        assert " main.cpp" in out
        assert " --tests" in out
        assert " test.cpp" in out

    def test_args_style_no_tests(self, capsys):
        """Test ArgsStyle with no test targets."""
        style = compiletools.findtargets.ArgsStyle()
        style(["main.cpp"], [])
        out = capsys.readouterr().out
        assert " main.cpp" in out
        assert "--tests" not in out

    def test_args_style_no_exes(self, capsys):
        """Test ArgsStyle with no executable targets."""
        style = compiletools.findtargets.ArgsStyle()
        style([], ["test.cpp"])
        out = capsys.readouterr().out
        assert "--tests" in out
        assert " test.cpp" in out

    def test_null_style(self, capsys):
        """Test NullStyle output."""
        style = compiletools.findtargets.NullStyle()
        style(["a.cpp"], ["b.cpp"])
        out = capsys.readouterr().out
        assert "a.cpp" in out
        assert "b.cpp" in out


class TestFindTargetsProcess:
    """Test FindTargets.process method."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    def test_process_populates_args(self):
        """Test that process() adds targets to args.filename and args.tests."""
        config_files = compiletools.configutils.config_files_from_variant(exedir=uth.cakedir(), argv=[])
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="TestFindTargetsProcess",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.findtargets.add_arguments(cap)
        argv = ["--shorten"]
        args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
        findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir(), context=BuildContext())

        # Set up args for process()
        args.filename = []
        args.tests = None
        findtargets.process(args, path=uth.cakedir())
        # Should have found some executables and tests
        assert len(args.filename) > 0
        assert args.tests is not None
        assert len(args.tests) > 0

    def test_process_verbose(self):
        """Test that process() with verbose >= 2 prints style output."""
        config_files = compiletools.configutils.config_files_from_variant(exedir=uth.cakedir(), argv=[])
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="TestFindTargetsProcessVerbose",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.findtargets.add_arguments(cap)
        argv = ["--shorten"]
        args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
        args.verbose = 2
        findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir(), context=BuildContext())

        args.filename = []
        args.tests = None
        # Should not raise
        findtargets.process(args, path=uth.cakedir())


class TestFindTargetsNoExemarkers:
    """Test FindTargets behavior when exemarkers is None."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    def test_no_exemarkers_exits(self):
        """Test that None exemarkers causes sys.exit(1)."""
        config_files = compiletools.configutils.config_files_from_variant(exedir=uth.cakedir(), argv=[])
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            description="TestNoExemarkers",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.findtargets.add_arguments(cap)
        argv = ["--shorten"]
        args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
        args.exemarkers = None  # Force None
        findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir(), context=BuildContext())
        try:
            findtargets()
            assert False, "Should have called sys.exit"
        except SystemExit as e:
            assert e.code == 1


class TestFindTargetsOsWalkFallback:
    """Test FindTargets os.walk fallback for non-git directories."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    def test_walk_fallback(self):
        """Test the os.walk fallback when get_tracked_files returns empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a source file with main(
            src = os.path.join(tmpdir, "hello.cpp")
            with open(src, "w") as f:
                f.write("#include <iostream>\nint main() { return 0; }\n")

            config_files = compiletools.configutils.config_files_from_variant(exedir=uth.cakedir(), argv=[])
            cap = configargparse.ArgumentParser(
                conflict_handler="resolve",
                description="TestWalkFallback",
                formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
                default_config_files=config_files,
                args_for_setting_config_path=["-c", "--config"],
                ignore_unknown_config_file_keys=True,
            )
            compiletools.findtargets.add_arguments(cap)
            argv = ["--shorten"]
            args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
            findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir(), context=BuildContext())

            # Mock get_tracked_files to return empty dict (non-git)
            with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
                exes, _tests = findtargets(path=tmpdir)
                # Should find our file as an executable
                assert any("hello.cpp" in e for e in exes)


class TestFindTargetsMain:
    """Test findtargets.main() entry point."""

    def setup_method(self):
        uth.reset()

    def teardown_method(self):
        uth.reset()

    def test_main_runs(self):
        """Test main() runs without error."""
        compiletools.findtargets.main(argv=["--style=flat", "--shorten"])
