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
from compiletools.examples_registry import example_path


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

    def test_main_runs(self, capsys):
        """Smoke test from a directory whose discovered targets carry no
        conflicting subproject confs. The repo root's do: --auto discovery
        there raises ConfContradictionError for every tool on the shared
        re-anchoring driver, ct-filelist and ct-cake included."""
        with uth.DirectoryContext(example_path("simple")):
            with uth.ParserContext():
                assert compiletools.findtargets.main(argv=["--style=flat", "--shorten"]) == 0
        assert "helloworld_cpp.cpp" in capsys.readouterr().out


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
    """A pattern with a path separator fnmatches the gitroot-relative path
    (and, when the pattern is itself absolute, the absolute path); a
    pattern without one fnmatches each individual path component."""

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

    def test_a_relative_pattern_never_reaches_above_the_gitroot(self, tmp_path):
        """``*/legacy`` is the spelling the docs recommend for matching at
        any depth, and ``*`` spans separators -- so if the absolute path
        were a candidate for a relative pattern, a checkout under
        ``/tmp/...`` would be wholly excluded by ``*/tmp/*``, discovering
        nothing, over a path component the project never chose. Only an
        absolute pattern gets the absolute path."""
        anchor = os.path.realpath(str(tmp_path))
        above = (os.path.basename(anchor), os.path.basename(os.path.dirname(anchor)))
        for ancestor in above:
            for pattern in (f"*/{ancestor}/*", f"*/{ancestor}", f"{ancestor}/*"):
                assert not self._excluded(tmp_path, pattern, "src/main.cpp"), pattern
        # The in-tree spellings the ancestor rule must not cost us.
        assert self._excluded(tmp_path, "*/src/*", "src/main.cpp")
        assert self._excluded(tmp_path, "*/src", "src/main.cpp")

    def test_an_absolute_pattern_only_counts_when_it_reaches_into_the_tree(self, tmp_path):
        """An absolute pattern naming an ANCESTOR of the gitroot must not
        get the absolute path. ``/tmp`` is the spelling a gitignore-trained
        user with a project-level ``tmp`` directory writes, and if the
        checkout happens to sit under ``/tmp`` that would exclude the whole
        project. Neither a leading ``/`` nor a glob-free first component
        distinguishes the two readings; only a literal head that lands
        inside the tree does."""
        anchor = os.path.realpath(str(tmp_path))
        components = anchor.split(os.sep)
        ancestors = (os.sep + components[1], os.sep + os.sep.join(components[1:3]))
        for ancestor in ancestors:
            leaf = os.path.basename(ancestor)
            for pattern in (ancestor, f"{ancestor}/*", f"/*/{leaf}/*", f"/*/{leaf}"):
                assert not self._excluded(tmp_path, pattern, "src/main.cpp"), pattern
        # Each of those spellings still resolves, as the gitroot-anchored
        # form it looks like: an in-project directory of the same name is
        # excluded, which is what the user meant.
        assert self._excluded(tmp_path, os.sep + components[1] + "/*", components[1] + "/old.cpp")
        assert self._excluded(tmp_path, "/*/src/*", "sub/src/main.cpp")
        assert self._excluded(tmp_path, "/sr*/main.cpp", "src/main.cpp")
        # The in-tree absolute spelling the candidate exists for.
        assert self._excluded(tmp_path, anchor + "/legacy/*", "legacy/old.cpp")

    def test_leading_separator_anchors_at_the_gitroot(self, tmp_path):
        """``/src/legacy`` is the spelling a gitignore-trained user reaches
        for. It must mean the anchored pattern it looks like, not nothing."""
        assert self._excluded(tmp_path, "/src/legacy", "src/legacy/old.cpp")
        assert self._excluded(tmp_path, "/src/legacy/*", "src/legacy/old.cpp")
        assert not self._excluded(tmp_path, "/src/legacy", "other/src/legacy/old.cpp")

    def test_a_root_anchor_still_yields_relative_candidates(self):
        """A repo rooted at ``/`` (or non-git discovery from ``/``) must not
        lose the relative spellings: ``/`` already ends in the separator, so
        appending one tests for ``//`` and nothing looks anchored."""
        assert compiletools.findtargets.is_auto_excluded("/src/vendor/main.cpp", ("vendor",), anchor_root="/")
        assert compiletools.findtargets.is_auto_excluded("/src/legacy/old.cpp", ("src/legacy",), anchor_root="/")
        assert compiletools.findtargets.is_auto_excluded("/src/legacy/old.cpp", ("/src/legacy",), anchor_root="/")
        assert not compiletools.findtargets.is_auto_excluded("/src/legacyish/old.cpp", ("src/legacy",), anchor_root="/")

    def test_outside_the_anchor_offers_only_the_basename_to_bare_patterns(self, tmp_path):
        """A file whose realpath escapes the gitroot (an in-tree symlink) must
        not have its ancestor directories scanned: a bare pattern would then
        match a ``tmp`` or username component the project never chose."""
        outside = str(tmp_path / "vendor" / "main.cpp")
        elsewhere = str(tmp_path / "elsewhere")
        assert not compiletools.findtargets.is_auto_excluded(outside, ("vendor",), anchor_root=elsewhere)
        assert compiletools.findtargets.is_auto_excluded(outside, ("main.cpp",), anchor_root=elsewhere)
        assert compiletools.findtargets.is_auto_excluded(outside, ("*/vendor/*",), anchor_root=elsewhere)

    def test_no_patterns_excludes_nothing(self, tmp_path):
        assert not compiletools.findtargets.is_auto_excluded(str(tmp_path / "main.cpp"), ())


# The file grid the normalisation differential runs over. ``<ANCHOR>`` in a
# pattern stands for the realpath of the anchor root, which only exists at
# test time.
_NORMALISATION_FILES = (
    "main.cpp",
    "vendor/main.cpp",
    "src/vendor/main.cpp",
    "src/vendor/deep/x.cpp",
    "sub/src/vendor/x.cpp",
    "vendorlib/main.cpp",
    "src/legacy/old.cpp",
    "src/legacy/deep/old.cpp",
    "src/legacyish/old.cpp",
    "src/current/new.cpp",
    "a/b/test_x.cpp",
    "legacy/old.cpp",
)

# Every cell that flips False -> True once the pattern is normalised, taken
# from a differential of the pre-fix and post-fix matchers over the grid
# above (33 flips, zero cells narrowing). The existing suite is blind to all
# of them, so they are pinned here by hand. The last two rows are the
# doubled-leading-separator spellings, which normalisation alone leaves
# unchanged and which the explicit strip covers.
_NEWLY_EXCLUDED_CELLS = tuple(
    (pattern, relpath)
    for pattern, relpaths in (
        ("vendor/", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp", "sub/src/vendor/x.cpp")),
        ("vendor//", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp", "sub/src/vendor/x.cpp")),
        ("./vendor", ("vendor/main.cpp", "src/vendor/main.cpp", "src/vendor/deep/x.cpp", "sub/src/vendor/x.cpp")),
        (".//vendor", ("vendor/main.cpp", "src/vendor/main.cpp", "src/vendor/deep/x.cpp", "sub/src/vendor/x.cpp")),
        ("a/../vendor", ("vendor/main.cpp", "src/vendor/main.cpp", "src/vendor/deep/x.cpp", "sub/src/vendor/x.cpp")),
        ("src//vendor", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp")),
        ("src/./vendor", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp")),
        ("./src/vendor", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp")),
        ("./src/legacy", ("src/legacy/old.cpp", "src/legacy/deep/old.cpp")),
        ("src/legacy//*", ("src/legacy/old.cpp", "src/legacy/deep/old.cpp")),
        ("./test_*.cpp", ("a/b/test_x.cpp",)),
        ("<ANCHOR>//legacy/*", ("legacy/old.cpp",)),
        ("//vendor", ("vendor/main.cpp",)),
        ("//src/vendor", ("src/vendor/main.cpp", "src/vendor/deep/x.cpp")),
    )
    for relpath in relpaths
)

# A redundant spelling and the plain spelling it is a synonym for. The pair
# must agree on every file in the grid -- which is the "nothing narrows"
# half of the differential, and stops a future change from making both
# spellings equally wrong.
_EQUIVALENT_SPELLINGS = (
    ("vendor/", "vendor"),
    ("vendor//", "vendor"),
    ("./vendor", "vendor"),
    (".//vendor", "vendor"),
    ("a/../vendor", "vendor"),
    ("./test_*.cpp", "test_*.cpp"),
    ("src/vendor/", "src/vendor"),
    ("src//vendor", "src/vendor"),
    ("src/./vendor", "src/vendor"),
    ("./src/vendor", "src/vendor"),
    ("./src/legacy", "src/legacy"),
    ("src/legacy/", "src/legacy"),
    ("src/legacy//*", "src/legacy/*"),
    ("*/legacy/", "*/legacy"),
    ("/vendor/", "/vendor"),
    ("/src/vendor/", "/src/vendor"),
    ("<ANCHOR>/legacy/", "<ANCHOR>/legacy"),
    ("<ANCHOR>//legacy/*", "<ANCHOR>/legacy/*"),
    ("//vendor", "/vendor"),
    ("//src/vendor", "/src/vendor"),
    # The largest single behaviour swing the doubled-separator strip makes:
    # "//*" goes from matching nothing to excluding every file, agreeing with
    # the "/*" it is a spelling of. Pinned so a reader who finds a whole tree
    # excluded by two characters can see it is the intended reading.
    ("//*", "/*"),
)


class TestAutoExcludePatternNormalisation:
    """Redundant path syntax in a pattern must not change what it matches.

    The subject path is realpath-normalised but the pattern was not, so how
    a pattern was spelled decided which matching branch it took: ``vendor/``
    was anchored at the gitroot instead of keeping gitignore's any-depth
    reading, and ``./vendor`` / ``src//vendor`` landed in the anchored
    branch, whose candidates can never match them, so they excluded nothing.
    """

    def _excluded(self, tmp_path, pattern, relpath):
        anchor = os.path.realpath(str(tmp_path))
        return compiletools.findtargets.is_auto_excluded(
            os.path.join(str(tmp_path), relpath),
            (pattern.replace("<ANCHOR>", anchor),),
            anchor_root=str(tmp_path),
        )

    @pytest.mark.parametrize("pattern,relpath", _NEWLY_EXCLUDED_CELLS)
    def test_redundant_syntax_no_longer_silently_matches_nothing(self, tmp_path, pattern, relpath):
        assert self._excluded(tmp_path, pattern, relpath)

    @pytest.mark.parametrize("redundant,plain", _EQUIVALENT_SPELLINGS)
    def test_a_redundant_spelling_matches_exactly_what_its_plain_form_matches(self, tmp_path, redundant, plain):
        redundant_matches = {f for f in _NORMALISATION_FILES if self._excluded(tmp_path, redundant, f)}
        plain_matches = {f for f in _NORMALISATION_FILES if self._excluded(tmp_path, plain, f)}
        assert redundant_matches == plain_matches
        assert plain_matches, f"{plain!r} matches nothing in the grid, so the comparison is vacuous"

    def test_normalisation_preserves_gitroot_anchoring(self, tmp_path):
        """A leading ``/`` survives normalisation, so the anchored spelling
        keeps meaning "at the gitroot only" -- normalisation widens the
        redundant spellings without widening the anchored one."""
        assert self._excluded(tmp_path, "/vendor/", "vendor/main.cpp")
        assert not self._excluded(tmp_path, "/vendor/", "src/vendor/main.cpp")
        assert not self._excluded(tmp_path, "/src/legacy/", "other/src/legacy/old.cpp")

    def test_normalisation_preserves_whole_component_matching(self, tmp_path):
        assert not self._excluded(tmp_path, "vendor/", "vendorlib/main.cpp")
        assert not self._excluded(tmp_path, "./vendor", "vendorlib/main.cpp")
        assert not self._excluded(tmp_path, "src/legacy/", "src/legacyish/old.cpp")
        assert self._excluded(tmp_path, "//vendorlib", "vendorlib/main.cpp")
        assert not self._excluded(tmp_path, "//vendor", "vendorlib/main.cpp")

    def test_normalisation_does_not_reach_above_the_anchor(self, tmp_path):
        """The ancestor-reach rule survives: a redundant spelling of an
        ancestor component must still not exclude the whole checkout."""
        anchor = os.path.realpath(str(tmp_path))
        parent_leaf = os.path.basename(os.path.dirname(anchor))
        assert not self._excluded(tmp_path, f"*/{parent_leaf}/", "src/main.cpp")
        assert not self._excluded(tmp_path, f"./*/{parent_leaf}/*", "src/main.cpp")

    def test_an_interior_dotdot_resolves_to_the_path_it_names(self, tmp_path):
        """``a/../vendor`` becomes ``vendor``, which is a WIDENING, not a
        syntax cleanup: gitignore would read it as a literal path matching
        nothing, and here it moves from the anchored branch to the any-depth
        component branch. The chosen semantics are "the pattern means the
        path it names", because that is what the user wrote and the
        alternative -- silently matching nothing -- is the whole defect class
        this fix exists to close."""
        assert self._excluded(tmp_path, "a/../vendor", "src/vendor/main.cpp")
        assert self._excluded(tmp_path, "src/legacy/../legacy", "src/legacy/old.cpp")

    def test_a_leading_dotdot_stays_anchored_and_matches_nothing(self, tmp_path):
        """The asymmetry with the interior ``..`` is deliberate. Normalising
        ``../vendor`` cannot remove its separator -- it names a path ABOVE
        the anchor, and ``--auto`` never walks there -- so it keeps the
        anchored reading and matches nothing, rather than collapsing into
        ``vendor`` and becoming an accidental escape hatch out of the
        no-reach-above-the-gitroot rule."""
        assert not self._excluded(tmp_path, "../vendor", "vendor/main.cpp")
        assert not self._excluded(tmp_path, "../vendor", "src/vendor/main.cpp")
        assert not self._excluded(tmp_path, "../../vendor", "vendor/main.cpp")

    def test_an_empty_pattern_still_excludes_nothing(self, tmp_path):
        """``normpath("")`` is ``"."``; the separator gate keeps both the
        empty and the bare-dot pattern out of normalisation entirely, so
        neither can become a match-all. ``"//"`` is the one that gets past
        the gate: it normalises to itself, the doubled-separator strip makes
        it ``"/"``, and it must stay the no-op ``"/"`` already is rather than
        excluding the whole tree."""
        assert not self._excluded(tmp_path, "", "src/main.cpp")
        assert not self._excluded(tmp_path, ".", "src/main.cpp")
        assert not self._excluded(tmp_path, "/", "src/main.cpp")
        assert not self._excluded(tmp_path, "//", "src/main.cpp")
        assert not self._excluded(tmp_path, "///", "src/main.cpp")


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

    def test_trailing_slash_pattern_drops_a_nested_vendor_subtree(self, tmp_path):
        """End to end for the gitignore spelling: ``--auto-exclude=vendor/``
        must drop ``src/vendor``, not just a gitroot-top-level ``vendor``."""
        self._tree(tmp_path)
        nested = tmp_path / "src" / "vendor"
        nested.mkdir(parents=True)
        (nested / "main.cpp").write_text("// nested vendor\nint main() { return 0; }\n")
        _args, findtargets = _make_findtargets("TestAutoExcludeTrailingSlash", "--auto-exclude=vendor/")
        with uth.DirectoryContext(str(tmp_path)):
            with patch("compiletools.global_hash_registry.get_tracked_files", return_value={}):
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


@pytest.fixture
def reanchor_repo(tmp_path):
    """A repo whose round-one discovery loads a conf that changes round-two
    discovery: appbeta/main.cpp is an exe under the gitroot's
    ``exemarkers = [main]``, and appbeta/ct.conf -- reachable only once that
    target is discovered -- excludes its own directory.

    One discovery pass reports appbeta/main.cpp; the re-anchoring fixpoint
    does not. ct-cake --auto and ct-filelist --auto already run the
    fixpoint, so the difference is also the difference between what
    ct-findtargets reports and what those two act on."""
    root = tmp_path / "reanchorrepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ct.conf").write_text("exemarkers = [main]\ntestmarkers = unit_test.hpp\n")

    appalpha = root / "appalpha"
    appalpha.mkdir()
    (appalpha / "main.cpp").write_text("int main() { return 0; }\n")

    appbeta = root / "appbeta"
    appbeta.mkdir()
    (appbeta / "ct.conf").write_text("append-AUTO-EXCLUDE = ${CONF_DIR}\n")
    # Distinct bodies: the global hash registry refuses to reverse-look-up a
    # content hash shared by two tracked files.
    (appbeta / "main.cpp").write_text("int main() { return 1; }\n")
    return root


def test_main_reports_the_target_set_the_reanchor_fixpoint_settles_on(reanchor_repo, capsys):
    """ct-findtargets must go through discover_targets_and_reanchor rather
    than run its own single discovery pass, or it reports targets ct-cake
    --auto would not build."""
    with uth.DirectoryContext(str(reanchor_repo)):
        with uth.ParserContext():
            assert compiletools.findtargets.main([]) == 0
    out = capsys.readouterr().out
    assert os.path.join("appalpha", "main.cpp") in out
    assert os.path.join("appbeta", "main.cpp") not in out


def test_no_auto_reports_nothing(reanchor_repo, capsys):
    """--no-auto means "do not walk", so with nothing named the target set is
    empty. Same contract as ct-filelist's
    test_no_auto_keeps_a_bare_invocation_silent."""
    with uth.DirectoryContext(str(reanchor_repo)):
        with uth.ParserContext():
            assert compiletools.findtargets.main(["--style=args", "--no-auto"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_an_explicit_target_suppresses_discovery(reanchor_repo, capsys):
    """A named target is the whole target set: it must not merge with the
    discovered one. scripts/ct-build feeds this string to ct-create-makefile,
    so a merged list hands it the named target twice -- once in the caller's
    spelling and once in the discovered absolute one, which ordered_unique
    cannot collapse."""
    named = os.path.join("appalpha", "main.cpp")
    with uth.DirectoryContext(str(reanchor_repo)):
        with uth.ParserContext():
            assert compiletools.findtargets.main(["--style=args", "--filename", named]) == 0
    out = capsys.readouterr().out
    assert out.split() == [named]


@pytest.fixture
def library_repo(tmp_path):
    """A repo with one executable and one library source, so ``--static``
    can name a real file that parseargs will resolve and hash."""
    root = tmp_path / "libraryrepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "ct.conf").write_text("exemarkers = [main]\ntestmarkers = unit_test.hpp\n")

    app = root / "app"
    app.mkdir()
    (app / "main.cpp").write_text("int main() { return 0; }\n")

    lib = root / "lib"
    lib.mkdir()
    (lib / "widget.cpp").write_text("int widget() { return 3; }\n")
    return root


class TestLibrarySlotsAreRejected:
    """ct-findtargets reports two buckets, executables and tests, and the
    style classes take exactly those two. ``--static`` / ``--dynamic`` reach
    the parser only because the re-anchoring driver reparses through this
    same cap and needs the slots registered, so a named library is accepted
    and then never printed.

    Base 168b1076 rejected both flags outright -- main() did not register
    the target arguments, so argparse exited 2 on an unrecognized argument.
    Restoring that rejection with a diagnostic is the contract these tests
    pin; the silent-swallow surface is one release old and is the accident.
    """

    @staticmethod
    def _run(repo, argv):
        with uth.DirectoryContext(str(repo)):
            with uth.ParserContext():
                return compiletools.findtargets.main(argv)

    def test_a_named_executable_still_reports(self, library_repo, capsys):
        """Control: the rejection must be specific to the library slots, not
        a blanket refusal of every explicitly named target."""
        named = os.path.join("app", "main.cpp")
        assert self._run(library_repo, ["--style=args", "--filename", named]) == 0
        assert capsys.readouterr().out.split() == [named]

    @pytest.mark.parametrize("flag", ["--static", "--dynamic"])
    def test_a_library_slot_alone_is_rejected(self, library_repo, flag):
        with pytest.raises(SystemExit) as excinfo:
            self._run(library_repo, ["--style=args", flag, os.path.join("lib", "widget.cpp")])
        assert excinfo.value.code == 2

    @pytest.mark.parametrize("flag", ["--static", "--dynamic"])
    def test_a_library_slot_combined_with_filename_is_rejected(self, library_repo, flag, capsys):
        """The dangerous form. Combined with a named executable the tool
        used to exit 0 printing only the executable -- a plausible answer
        that silently drops the library, which survives far longer
        downstream than the empty output the flag produces on its own."""
        with pytest.raises(SystemExit) as excinfo:
            self._run(
                library_repo,
                [
                    "--style=args",
                    flag,
                    os.path.join("lib", "widget.cpp"),
                    "--filename",
                    os.path.join("app", "main.cpp"),
                ],
            )
        assert excinfo.value.code == 2
        assert "main.cpp" not in capsys.readouterr().out

    def test_the_diagnostic_names_the_flag_the_slots_and_the_other_tool(self, library_repo, capsys):
        """A rejection a user cannot act on is worse than the silence it
        replaces, so pin the three things the message has to carry: which
        flag was refused, which slots this tool does report, and where a
        library build actually goes."""
        with pytest.raises(SystemExit):
            self._run(library_repo, ["--static", os.path.join("lib", "widget.cpp")])
        err = capsys.readouterr().err
        assert "--static" in err
        assert "positional" in err
        assert "--tests" in err
        assert "ct-create-makefile" in err
