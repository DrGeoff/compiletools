import os
import subprocess
from unittest.mock import patch

import configargparse
import pytest

import compiletools.apptools
import compiletools.configutils
import compiletools.file_analyzer
import compiletools.findtargets
import compiletools.global_hash_registry
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe global configargparse parser cache around every test in this
    module so the FindTargets / apptools parsers don't leak across tests."""
    uth.reset()
    yield
    uth.reset()


def _make_findtargets(description, *extra_argv, exedir=None):
    """Build a FindTargets bound to a parsed-args namespace with --shorten."""
    exedir = exedir or uth.cakedir()
    config_files = compiletools.configutils.config_files_from_variant(exedir=exedir, argv=[])
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        description=description,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        default_config_files=config_files,
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.findtargets.add_arguments(cap)
    argv = ["--shorten", *extra_argv]
    args = compiletools.apptools.parseargs(cap, argv=argv, context=BuildContext())
    findtargets = compiletools.findtargets.FindTargets(args, exedir=exedir, context=BuildContext())
    return args, findtargets


class TestFindTargetsModule:
    def _find_samples_targets(self, disable_tests, disable_exes=False):
        relativeexpectedexes = {
            "appinfo/main.cpp",
            "basename_collision/appalpha/main.cpp",
            "basename_collision/appbeta/main.cpp",
            "cache_scoping/no_ref.cpp",
            "cache_scoping/tu_via_header.cpp",
            "cache_scoping/with_ref.cpp",
            "calculator/main.cpp",
            "cli_features/alpha.cpp",
            "cli_features/beta.cpp",
            "cli_features/gamma.cpp",
            "computed_include/main.cpp",
            "conditional_includes/main.cpp",
            "conf_dir_relative_pkgconfig/main.cpp",
            "cppflags_macros/advanced_preprocessor_test.cpp",
            "cppflags_macros/compiler_builtin_test.cpp",
            "cppflags_macros/elif_test.cpp",
            "cppflags_macros/main.cpp",
            "cppflags_macros/multi_flag_test.cpp",
            "cppflags_macros/nested_macros_test.cpp",
            "cxx_modules/main.cpp",
            "cxx_modules_header_units/main.cpp",
            "cxx_modules_header_unit_isystem/main.cpp",
            "cxx_modules_header_unit_pkg_config/main.cpp",
            "cxx_modules_import_std/main.cpp",
            "cxx_modules_partitions/main.cpp",
            "cxx_modules_split/main.cpp",
            "cxx_modules_transitive_header_unit/main.cpp",
            "dottypaths/dottypaths.cpp",
            "duplicate_flags/main.cpp",
            "dynamic_library/main.cpp",
            "empty_macro_bug/libs/main.cpp",
            "export_template_cmd_hash/form1/app0.cpp",
            "export_template_cmd_hash/form1/app1.cpp",
            "export_template_cmd_hash/form1/app2.cpp",
            "export_template_cmd_hash/form2/app0.cpp",
            "export_template_cmd_hash/form2/app1.cpp",
            "export_template_cmd_hash/form2/app2.cpp",
            "feature_headers/main.cpp",
            "ffile_prefix_map/path_probe.cpp",
            "has_include/main.cpp",
            "header_guard_bug/main.cpp",
            "hunter_macro_propagation/app.cpp",
            "isystem_include_bug/main.cpp",
            "ldflags/conditional_ldflags_test.cpp",
            "ldflags/macro_expanded_ldflags.cpp",
            "ldflags/version_dependent_ldflags.cpp",
            "library/main.cpp",
            "lotsofmagic/lotsofmagic.cpp",
            "macro_deps/main.cpp",
            "macro_state_dependency/clean_main.cpp",
            "macro_state_dependency/main.cpp",
            "macro_state_dependency/sample.cpp",
            "magic_processing_order/complex_test.cpp",
            "magicinclude/main.cpp",
            "magicpkgconfig/main.cpp",
            "magicpkgconfig_fake/main.cpp",
            "magicsourceinheader/main.cpp",
            "movingheaders/main.cpp",
            "multi_axis_variant/axis_probe.cpp",
            "nestedconfig/nc.cpp",
            "nestedconfig/subdir/nc.cpp",
            "parse_order_macro_bug/libs/entry_point_1.cpp",
            "parse_order_macro_bug/libs/entry_point_2.cpp",
            "pch/pch_user.cpp",
            "pch_bypass_bug/consumer.cpp",
            "pkgconfig/main.cpp",
            "pkgconfig_cycle/main.cpp",
            "platform_has_include/platform_main.cpp",
            "postbuild_script/env_printer.cpp",
            "prebuild_script/version_banner.cpp",
            "project_pkgconfig_override/main.cpp",
            "project_version/version_banner.cpp",
            "relative_cas_dir_bug/widget.cpp",
            "separate_cpp_cxx/main.cpp",
            "simple/helloworld_c.c",
            "simple/helloworld_cpp.cpp",
            "static_link_order/main.cpp",
            "sudoku_tui/sudoku_tui.cpp",
            "terminal_games/aquarium/aquarium.cpp",
            "terminal_games/breakout/breakout.cpp",
            "terminal_games/invaders/invaders.cpp",
            "terminal_games/moonlander/moonlander.cpp",
            "terminal_games/snake/snake.cpp",
            "transitive_cache_bug/engine/a-game.cpp",
            "transitive_cache_bug/engine/b-game.cpp",
            "undef_bug/main.cpp",
            "unit_test_marker/main.cpp",
        }
        relativeexpectedtests = {
            "cross_platform/test_source.cpp",
            "factory/test_factory.cpp",
            "magic_processing_order/test_macro_transform.cpp",
            "numbers/test_direct_include.cpp",
            "numbers/test_library.cpp",
            "pkg_config_header_deps/src/test.cpp",
            "serialise_tests/test_flock_1.cpp",
            "serialise_tests/test_flock_2.cpp",
            "sudoku_tui/test_stepper.cpp",
            "terminal_games/common/test_frontend.cpp",
            "terminal_games/aquarium/test_bubbles.cpp",
            "terminal_games/aquarium/test_fish.cpp",
            "terminal_games/aquarium/test_seaweed.cpp",
            "terminal_games/aquarium/test_tank.cpp",
            "terminal_games/breakout/test_arena.cpp",
            "terminal_games/breakout/test_bricks.cpp",
            "terminal_games/invaders/test_bullet.cpp",
            "terminal_games/invaders/test_field.cpp",
            "terminal_games/invaders/test_formation.cpp",
            "terminal_games/moonlander/test_physics.cpp",
            "terminal_games/snake/test_rng.cpp",
            "terminal_games/snake/test_snake.cpp",
            "simple/test_cflags.c",
            "test_xml_output/test_stub_catch2.cpp",
            "test_xml_output/test_stub_doctest.cpp",
            "test_xml_output/test_stub_gtest.cpp",
            "test_xml_output/test_unknown_framework.cpp",
            "testprefix/test_quick.cpp",
            "unit_test_marker/test_widget.cpp",
            "version_dependent_api/test_main.cpp",
            "version_dependent_api/test_main_new.cpp",
        }

        expectedexes = set()
        if not disable_exes:
            expectedexes = {os.path.realpath(uth.example_file(exe)) for exe in relativeexpectedexes}
        expectedtests = set()
        if not disable_tests:
            expectedtests = {os.path.realpath(uth.example_file(tt)) for tt in relativeexpectedtests}

        extra_argv = []
        if disable_tests:
            extra_argv.append("--disable-tests")
        if disable_exes:
            extra_argv.append("--disable-exes")
        _args, findtargets = _make_findtargets("TestFindTargetsModule", *extra_argv)
        executabletargets, testtargets = findtargets(path=uth.cakedir())
        assert expectedexes == set(executabletargets)
        assert expectedtests == set(testtargets)

    def test_samples(self):
        self._find_samples_targets(disable_tests=False)

    def test_disable_tests(self):
        self._find_samples_targets(disable_tests=True)

    def test_tests_only(self):
        self._find_samples_targets(disable_tests=False, disable_exes=True)


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

    def test_process_populates_args(self):
        """Test that process() adds targets to args.filename and args.tests."""
        args, findtargets = _make_findtargets("TestFindTargetsProcess")
        args.filename = []
        args.tests = None
        findtargets.process(args, path=uth.cakedir())
        # Should have found some executables and tests
        assert len(args.filename) > 0
        assert args.tests is not None
        assert len(args.tests) > 0

    def test_process_verbose(self):
        """Test that process() with verbose >= 2 prints style output."""
        args, findtargets = _make_findtargets("TestFindTargetsProcessVerbose")
        args.verbose = 2
        args.filename = []
        args.tests = None
        # Should not raise
        findtargets.process(args, path=uth.cakedir())


class TestFindTargetsNoExemarkers:
    """Test FindTargets behavior when exemarkers is None."""

    def test_no_exemarkers_exits(self):
        """Test that None exemarkers causes sys.exit(1)."""
        args, findtargets = _make_findtargets("TestNoExemarkers")
        args.exemarkers = None  # Force None
        try:
            findtargets()
            assert False, "Should have called sys.exit"
        except SystemExit as e:
            assert e.code == 1


class TestFindTargetsOsWalkFallback:
    """Test FindTargets os.walk fallback for non-git directories."""

    def test_walk_fallback(self, tmp_path):
        """Test the os.walk fallback when get_tracked_files returns empty."""
        # Create a source file with main()
        (tmp_path / "hello.cpp").write_text("#include <iostream>\nint main() { return 0; }\n")

        _args, findtargets = _make_findtargets("TestWalkFallback")

        # Mock get_tracked_files to return empty dict (non-git)
        with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
            exes, _tests = findtargets(path=str(tmp_path))
            # Should find our file as an executable
            assert any("hello.cpp" in e for e in exes)


class TestFindTargetsTopbindirFilter:
    """The os.walk fallback skips roots containing topbindir() as a
    substring. A dot-prefixed relative bindir must never yield ``./``
    there — that substring matches every walked root and would silently
    skip all sources."""

    def test_dot_prefixed_bindir_never_yields_dot_slash_topbindir(self, tmp_path):
        (tmp_path / "hello.cpp").write_text("#include <iostream>\nint main() { return 0; }\n")

        _args, findtargets = _make_findtargets("TestTopbindirFilter", "--bindir=./bin/x")

        topbindir = findtargets.namer.topbindir()
        assert topbindir != "." + os.sep, "normalized bindir must not degrade topbindir() to './'"
        assert topbindir == "bin" + os.sep

        with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
            exes, _tests = findtargets(path=str(tmp_path))
            assert any("hello.cpp" in e for e in exes)


class TestFindTargetsMain:
    """Test findtargets.main() entry point."""

    def test_main_runs(self):
        """Test main() runs without error."""
        compiletools.findtargets.main(argv=["--style=flat", "--shorten"])


def _bare_parser(description):
    return configargparse.ArgumentParser(
        conflict_handler="resolve",
        description=description,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        default_config_files=[],
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )


class TestDiscoveryArgumentSplit:
    """``--style`` belongs to ct-findtargets' own output, not to the
    discovery surface every ``--auto`` consumer registers. ct-filelist has
    its own incompatible ``--style`` and registers only the discovery half.
    """

    def test_discovery_arguments_omit_style(self):
        cap = _bare_parser("discovery half")
        compiletools.findtargets.add_discovery_arguments(cap)
        assert compiletools.apptools._parser_has_option(cap, "--auto")
        assert compiletools.apptools._parser_has_option(cap, "--exemarkers")
        assert not compiletools.apptools._parser_has_option(cap, "--style")

    def test_add_arguments_layers_style_on_top(self):
        cap = _bare_parser("full findtargets surface")
        compiletools.findtargets.add_arguments(cap)
        assert compiletools.apptools._parser_has_option(cap, "--auto")
        assert compiletools.apptools._parser_has_option(cap, "--style")

    def test_discovery_half_keeps_auto_on_by_default(self):
        cap = _bare_parser("auto default")
        compiletools.findtargets.add_discovery_arguments(cap)
        assert cap.parse_args([]).auto is True
        assert cap.parse_args(["--no-auto"]).auto is False

    def test_auto_exclude_registers_both_the_bare_and_accumulating_spellings(self):
        """The bare key is last-writer-wins across the conf hierarchy, so
        the accumulating append-/prepend- pair is what a subproject conf
        uses to ADD an exclusion. ``apptools.parseargs`` merges the pair
        into ``auto_exclude`` via ``_do_xxpend_list``."""
        cap = _bare_parser("auto-exclude spellings")
        compiletools.findtargets.add_discovery_arguments(cap)
        assert compiletools.apptools._parser_has_option(cap, "--auto-exclude")
        assert compiletools.apptools._parser_has_option(cap, "--append-AUTO-EXCLUDE")
        assert compiletools.apptools._parser_has_option(cap, "--prepend-AUTO-EXCLUDE")
        args = cap.parse_args(["--append-AUTO-EXCLUDE=vendor", "--prepend-AUTO-EXCLUDE=legacy"])
        assert args.append_auto_exclude == ["vendor"]
        assert args.prepend_auto_exclude == ["legacy"]

    def test_registering_both_halves_is_idempotent(self):
        """Every registrar in this codebase is safe to call twice; the
        style layer must not raise on a re-registration either."""
        cap = _bare_parser("double registration")
        compiletools.findtargets.add_arguments(cap)
        compiletools.findtargets.add_arguments(cap)
        assert cap.parse_args([]).style == "indent"


class TestAutoExcludeMatching:
    """A pattern with a path separator fnmatches the gitroot-relative and
    absolute paths; a pattern without one fnmatches each individual path
    component."""

    def _excluded(self, tmp_path, pattern, relpath):
        return compiletools.findtargets.is_auto_excluded(
            os.path.join(str(tmp_path), relpath), (pattern,), anchor_root=str(tmp_path)
        )

    def test_bare_name_excludes_that_directorys_whole_subtree(self, tmp_path):
        assert self._excluded(tmp_path, "vendor", "vendor/deep/main.cpp")
        assert not self._excluded(tmp_path, "vendor", "main.cpp")

    def test_bare_name_matches_whole_components_not_substrings(self, tmp_path):
        """Excluding ``vendor`` must not also exclude ``vendorlib``."""
        assert not self._excluded(tmp_path, "vendor", "vendorlib/main.cpp")
        assert not self._excluded(tmp_path, "main", "main.cpp")

    def test_bare_glob_matches_a_basename_at_any_depth(self, tmp_path):
        assert self._excluded(tmp_path, "test_*.cpp", "a/b/test_x.cpp")
        assert not self._excluded(tmp_path, "test_*.cpp", "a/b/main.cpp")

    def test_slashed_pattern_matches_the_anchor_relative_path(self, tmp_path):
        assert self._excluded(tmp_path, "src/legacy/*", "src/legacy/old.cpp")
        assert self._excluded(tmp_path, "src/legacy/*", "src/legacy/deep/old.cpp")
        assert not self._excluded(tmp_path, "src/legacy/*", "src/current/new.cpp")

    def test_slashed_directory_name_excludes_its_subtree(self, tmp_path):
        """A directory path is the obvious thing to type; it must not need
        a trailing ``/*`` to mean "everything under here"."""
        assert self._excluded(tmp_path, "src/legacy", "src/legacy/deep/old.cpp")
        assert not self._excluded(tmp_path, "src/legacy", "src/legacyish/old.cpp")

    def test_globbed_directory_name_also_excludes_its_subtree(self, tmp_path):
        """The subtree rule is fnmatch on both halves, so it survives a glob
        in the pattern: ``*/legacy`` must behave like ``src/legacy`` does."""
        assert self._excluded(tmp_path, "*/legacy", "src/legacy/deep/old.cpp")
        assert not self._excluded(tmp_path, "*/legacy", "src/legacyish/old.cpp")

    def test_slashed_pattern_matches_the_absolute_path(self, tmp_path):
        """A conf file writes ``${CONF_DIR}/legacy/*``, which expands to an
        absolute pattern that must still match."""
        assert self._excluded(tmp_path, os.path.realpath(str(tmp_path)) + "/legacy/*", "legacy/old.cpp")

    def test_outside_the_anchor_falls_back_to_absolute_components(self, tmp_path):
        """A target outside the gitroot has no anchor-relative form; the
        bare-name rule still applies to its absolute components."""
        assert compiletools.findtargets.is_auto_excluded(
            str(tmp_path / "vendor" / "main.cpp"), ("vendor",), anchor_root=str(tmp_path / "elsewhere")
        )

    def test_no_patterns_excludes_nothing(self, tmp_path):
        assert not compiletools.findtargets.is_auto_excluded(str(tmp_path / "main.cpp"), ())


class TestAutoExcludeInDiscovery:
    """The exclusion applies inside FindTargets.__call__, so both the
    tracked-files generator and the os.walk fallback honour it."""

    def _tree(self, tmp_path):
        (tmp_path / "keep").mkdir()
        (tmp_path / "keep" / "main.cpp").write_text("int main() { return 0; }\n")
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "main.cpp").write_text("// vendor\nint main() { return 0; }\n")
        (tmp_path / "keep" / "test_thing.cpp").write_text("// t\nint main() { return 0; }\n")

    def test_walk_fallback_drops_excluded_sources(self, tmp_path):
        self._tree(tmp_path)
        _args, findtargets = _make_findtargets("TestAutoExcludeWalk", f"--auto-exclude={tmp_path / 'vendor'}")
        with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
            exes, tests = findtargets(path=str(tmp_path))
        assert any(e.endswith(os.path.join("keep", "main.cpp")) for e in exes)
        assert not any("vendor" in e for e in exes)
        assert any("test_thing.cpp" in t for t in tests)

    def test_tracked_files_generator_drops_excluded_sources(self, tmp_path):
        """The git-tracked path is the production one; the assertion that
        the registry is non-empty stops this silently degrading into a
        second os.walk-fallback test."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        self._tree(tmp_path)
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        with uth.DirectoryContext(str(tmp_path)):
            _args, findtargets = _make_findtargets("TestAutoExcludeTracked", "--auto-exclude=vendor")
            assert compiletools.global_hash_registry.get_tracked_files(findtargets.context)
            exes, _tests = findtargets(path=str(tmp_path))
        assert any(e.endswith(os.path.join("keep", "main.cpp")) for e in exes)
        assert not any("vendor" in e for e in exes)

    def test_glob_exclusion_drops_tests(self, tmp_path):
        self._tree(tmp_path)
        _args, findtargets = _make_findtargets("TestAutoExcludeGlob", "--auto-exclude=*test_*.cpp")
        with uth.DirectoryContext(str(tmp_path)):
            with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
                exes, tests = findtargets(path=str(tmp_path))
        assert tests == []
        assert len(exes) == 2

    def test_excluded_sources_are_never_analysed(self, tmp_path):
        """Exclusion happens before analyze_file, so an excluded subtree
        costs nothing to skip."""
        self._tree(tmp_path)
        _args, findtargets = _make_findtargets("TestAutoExcludeNoAnalyse", f"--auto-exclude={tmp_path / 'vendor'}")
        vendor_hash = compiletools.global_hash_registry.get_file_hash(
            os.path.realpath(str(tmp_path / "vendor" / "main.cpp")), findtargets.context
        )
        real_analyze = compiletools.file_analyzer.analyze_file
        analysed = []

        def _spy(content_hash, context):
            analysed.append(content_hash)
            return real_analyze(content_hash, context)

        with patch("compiletools.file_analyzer.analyze_file", side_effect=_spy):
            with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
                exes, _tests = findtargets(path=str(tmp_path))
        assert not any("vendor" in e for e in exes)
        assert vendor_hash not in analysed


def test_discovery_reanchor_cap_is_the_shared_apptools_cap():
    """One defensive cap in one place: the discovery re-anchor loop must
    reuse apptools._MAX_TARGET_CONF_ROUNDS, not carry its own copy."""
    assert compiletools.findtargets._MAX_DISCOVERY_REANCHOR_ROUNDS == compiletools.apptools._MAX_TARGET_CONF_ROUNDS
