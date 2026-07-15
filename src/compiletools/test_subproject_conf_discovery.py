"""Target-anchored config discovery: a target's nearest-ancestor ct.conf /
ct.conf.d layer loads regardless of cwd; same-tier contradictions hard-error.

Spec: docs/superpowers/specs/2026-07-15-subproject-conf-discovery-design.md
"""

import os
import subprocess

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.configutils
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.configutils import (
    ConfContradictionError,
    TargetConfLayer,
    build_separate_build_commands,
    validate_no_conf_contradictions,
)


def _make_repo(tmp_path):
    root = tmp_path / "monorepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


class TestWalkTargetConfLayers:
    def test_finds_bare_ct_conf_in_target_dir(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        appbeta.mkdir()
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(appbeta / "main.cpp")])
        assert len(layers) == 1
        assert os.path.realpath(layers[0].subproject_dir) == os.path.realpath(str(appbeta))
        assert layers[0].conf_paths == (os.path.join(os.path.realpath(str(appbeta)), "ct.conf"),)

    def test_nearest_ancestor_wins_for_nested_target(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        deep = appbeta / "src" / "deep"
        deep.mkdir(parents=True)
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (deep / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(deep / "main.cpp")])
        assert len(layers) == 1
        assert os.path.realpath(layers[0].subproject_dir) == os.path.realpath(str(appbeta))

    def test_ct_conf_d_only_subproject_is_found(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        conf_d = appbeta / "ct.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(appbeta / "main.cpp")])
        assert len(layers) == 1
        assert layers[0].conf_paths == (os.path.join(os.path.realpath(str(appbeta)), "ct.conf.d", "ct.conf"),)

    def test_gitroot_conf_is_excluded(self, tmp_path):
        # gitroot ct.conf is the project layer, never a subproject layer
        root = _make_repo(tmp_path)
        (root / "ct.conf").write_text("exemarkers = [main]\n")
        (root / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(root / "main.cpp")])
        assert layers == ()

    def test_target_without_ancestor_conf_contributes_nothing(self, tmp_path):
        root = _make_repo(tmp_path)
        libcore = root / "libcore"
        libcore.mkdir()
        (libcore / "util.cpp").write_text("int util() { return 42; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(libcore / "util.cpp")])
        assert layers == ()

    def test_two_targets_same_subproject_yield_one_layer(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        appbeta.mkdir()
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")
        (appbeta / "other.cpp").write_text("int other() { return 1; }\n")

        layers = compiletools.configutils.walk_target_conf_layers(
            [str(appbeta / "main.cpp"), str(appbeta / "other.cpp")]
        )
        assert len(layers) == 1

    def test_non_git_target_walks_to_filesystem_root(self, tmp_path):
        # No git init: the walk is bounded only by the filesystem root and
        # includes the target's own directory; nearest ancestor layer wins.
        appbeta = tmp_path / "plaintree" / "appbeta"
        deep = appbeta / "src" / "deep"
        deep.mkdir(parents=True)
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (deep / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(deep / "main.cpp")])
        assert len(layers) == 1
        assert os.path.realpath(layers[0].subproject_dir) == os.path.realpath(str(appbeta))

    def test_nonexistent_target_is_skipped(self, tmp_path):
        root = _make_repo(tmp_path)
        layers = compiletools.configutils.walk_target_conf_layers([str(root / "nope" / "missing.cpp")])
        assert layers == ()

    def test_axis_conf_filenames_are_collected_in_order(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        conf_d = appbeta / "ct.conf.d"
        conf_d.mkdir(parents=True)
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DBETA\n")
        (conf_d / "monovariant.conf").write_text("append-CXXFLAGS = -DBETA_VARIANT\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers(
            [str(appbeta / "main.cpp")], conf_filenames=("ct.conf", "monovariant.conf")
        )
        beta = os.path.realpath(str(appbeta))
        # per filename: ct.conf.d entry (lower) before bare-dir entry (higher),
        # mirroring get_existing_config_files ordering for the cwd layer
        assert layers[0].conf_paths == (
            os.path.join(beta, "ct.conf"),
            os.path.join(beta, "ct.conf.d", "monovariant.conf"),
        )


def _layer(tmp_path, name, content):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    conf = d / "ct.conf"
    conf.write_text(content)
    return TargetConfLayer(subproject_dir=str(d), conf_paths=(str(conf),))


class TestValidateNoConfContradictions:
    def test_disjoint_keys_pass(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CXXFLAGS = -DBETA\n")
        validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["cmd-a", "cmd-b"])

    def test_identical_values_merge_silently(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DSHARED\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DSHARED\n")
        validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["cmd-a", "cmd-b"])

    def test_same_key_different_value_raises_with_remedies(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions(
                [alpha, beta], [], "monovariant", ["ct-cake appalpha/main.cpp", "ct-cake appbeta/main.cpp"]
            )
        message = str(excinfo.value)
        assert "append-CPPFLAGS" in message
        assert "-DALPHA" in message and "-DBETA" in message
        assert str(tmp_path / "appalpha" / "ct.conf") in message
        assert str(tmp_path / "appbeta" / "ct.conf") in message
        assert "ct-cake appalpha/main.cpp" in message
        assert "ct-cake appbeta/main.cpp" in message
        assert "identical" in message  # remedy 2

    def test_cwd_layer_participates_as_a_tier_peer(self, tmp_path):
        cwd_conf = tmp_path / "cwdproj" / "ct.conf"
        cwd_conf.parent.mkdir()
        cwd_conf.write_text("append-CPPFLAGS = -DCWD\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions([beta], [str(cwd_conf)], "monovariant", ["a", "b"])

    def test_intra_layer_override_is_not_a_contradiction(self, tmp_path):
        d = tmp_path / "appbeta"
        conf_d = d / "ct.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "ct.conf").write_text("append-CPPFLAGS = -DLOW\n")
        (d / "ct.conf").write_text("append-CPPFLAGS = -DHIGH\n")
        layer = TargetConfLayer(
            subproject_dir=str(d),
            conf_paths=(str(conf_d / "ct.conf"), str(d / "ct.conf")),
        )
        validate_no_conf_contradictions([layer], [], "monovariant", ["cmd"])

    def test_variant_mismatch_is_a_contradiction(self, tmp_path):
        beta = _layer(tmp_path, "appbeta", "variant = othervariant\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions([beta], [], "monovariant", ["cmd"])
        assert "variant" in str(excinfo.value)

    def test_variant_match_passes(self, tmp_path):
        beta = _layer(tmp_path, "appbeta", "variant = monovariant\n")
        validate_no_conf_contradictions([beta], [], "monovariant", ["cmd"])


class TestBuildSeparateBuildCommands:
    def test_partitions_targets_by_subproject(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha_main = str(tmp_path / "appalpha" / "main.cpp")
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        for p in (alpha_main, beta_main):
            with open(p, "w") as f:
                f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake", ["--variant=monovariant", alpha_main, beta_main], [alpha, beta], [alpha_main, beta_main]
        )
        assert len(commands) == 2
        assert alpha_main in commands[0] and beta_main not in commands[0]
        assert beta_main in commands[1] and alpha_main not in commands[1]
        assert all("--variant=monovariant" in c for c in commands)

    def test_flag_value_form_is_partitioned(self, tmp_path):
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        alpha_main = str(tmp_path / "appalpha" / "main.cpp")
        beta_test = str(tmp_path / "appbeta" / "test_foo.cpp")
        for p in (alpha_main, beta_test):
            with open(p, "w") as f:
                f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake", [alpha_main, "--tests", beta_test], [alpha, beta], [alpha_main, beta_test]
        )
        assert beta_test not in commands[0]
        assert "--tests" in commands[1] and beta_test in commands[1]


_VARIANT = "monovariant"
_ARGV_BASE = (f"--variant={_VARIANT}",)


@pytest.fixture(autouse=True)
def _needs_compiler():
    if compiletools.apptools.get_functional_cxx_compiler() is None:
        pytest.skip("No functional C++ compiler detected")


@pytest.fixture
def monorepo(tmp_path):
    """gitroot ct.conf + ct.conf.d/<variant>.conf, two subprojects, shared libcore."""
    root = _make_repo(tmp_path)
    (root / "ct.conf").write_text(
        f"variant = {_VARIANT}\n"
        f"variant-canonical-order = {_VARIANT}\n"
        "exemarkers = [main]\n"
        "testmarkers = unit_test.hpp\n"
    )
    conf_d = root / "ct.conf.d"
    conf_d.mkdir()
    (conf_d / f"{_VARIANT}.conf").write_text("# variant conf\n")

    appbeta = root / "appbeta"
    appbeta.mkdir()
    (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
    (appbeta / "main.cpp").write_text("int main() { return 0; }\n")

    libcore = root / "libcore"
    libcore.mkdir()
    (libcore / "util.cpp").write_text("int util() { return 42; }\n")
    return root


def _parse_cake_args(cwd, argv):
    argv = list(argv)
    uth.reset()
    with uth.DirectoryContext(str(cwd)):
        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("subproject conf discovery test", argv=argv)
            compiletools.cake.Cake.add_arguments(cap)
            compiletools.cake.Cake.registercallback()
            return compiletools.apptools.parseargs(cap, argv, context=BuildContext())


class TestParseargsTargetAnchoring:
    def test_explicit_target_outside_cwd_project_picks_up_its_ct_conf(self, monorepo):
        """The bug: invoking from gitroot with a target inside appbeta/ must
        load appbeta/ct.conf."""
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appbeta", "main.cpp")])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS

    def test_control_cwd_inside_subproject_picks_up_its_ct_conf(self, monorepo):
        args = _parse_cake_args(monorepo / "appbeta", [*_ARGV_BASE, "main.cpp"])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS

    def test_control_subproject_flags_apply_invocation_globally(self, monorepo):
        """Pins invocation-global application (spec: no per-TU scoping)."""
        args = _parse_cake_args(monorepo / "appbeta", [*_ARGV_BASE, os.path.join("..", "libcore", "util.cpp")])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS

    def test_no_subproject_conf_means_no_extra_parse_effects(self, monorepo):
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("libcore", "util.cpp")])
        assert "-DAPPBETA_EXTRA" not in args.CPPFLAGS

    def test_deeply_nested_target_finds_subproject_conf(self, monorepo):
        deep = monorepo / "appbeta" / "src" / "deep"
        deep.mkdir(parents=True)
        (deep / "prog.cpp").write_text("int main() { return 0; }\n")
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appbeta", "src", "deep", "prog.cpp")])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS

    def test_ct_conf_d_only_subproject_is_loaded(self, monorepo):
        appgamma = monorepo / "appgamma"
        conf_d = appgamma / "ct.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "ct.conf").write_text("append-CPPFLAGS = -DAPPGAMMA_EXTRA\n")
        (appgamma / "main.cpp").write_text("int main() { return 0; }\n")
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appgamma", "main.cpp")])
        assert "-DAPPGAMMA_EXTRA" in args.CPPFLAGS

    def test_two_conflicting_subprojects_error_with_remedies(self, monorepo):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        with pytest.raises(compiletools.configutils.ConfContradictionError) as excinfo:
            _parse_cake_args(
                monorepo,
                [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
            )
        message = str(excinfo.value)
        assert "-DAPPALPHA_EXTRA" in message and "-DAPPBETA_EXTRA" in message
        assert "Build separately" in message
        assert "identical" in message

    def test_two_harmonious_subprojects_merge(self, monorepo):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")  # identical value
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        args = _parse_cake_args(
            monorepo,
            [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
        )
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS

    def test_cwd_conf_vs_target_conf_conflict_errors(self, monorepo):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        # cwd inside appalpha, target in appbeta: both are cwd-tier peers
        with pytest.raises(compiletools.configutils.ConfContradictionError):
            _parse_cake_args(
                monorepo / "appalpha",
                [*_ARGV_BASE, os.path.join("..", "appbeta", "main.cpp")],
            )

    def test_variant_contradiction_errors(self, monorepo):
        appdelta = monorepo / "appdelta"
        appdelta.mkdir()
        (appdelta / "ct.conf").write_text("variant = othervariant\n")
        (appdelta / "main.cpp").write_text("int main() { return 0; }\n")
        with pytest.raises(compiletools.configutils.ConfContradictionError):
            _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appdelta", "main.cpp")])


class TestAutoDiscoveryReanchor:
    def test_reanchor_helper_loads_discovered_targets_conf(self, monorepo):
        """Simulates the cake --auto flow: parse with no targets, then
        assign discovered targets and re-anchor."""
        args = _parse_cake_args(monorepo, [*_ARGV_BASE])
        assert "-DAPPBETA_EXTRA" not in args.CPPFLAGS
        args.filename = [str(monorepo / "appbeta" / "main.cpp")]
        with uth.DirectoryContext(str(monorepo)):
            reanchored = compiletools.apptools.reanchor_config_for_discovered_targets(args)
        assert reanchored is not None
        assert "-DAPPBETA_EXTRA" in reanchored.CPPFLAGS
        assert reanchored.filename == [str(monorepo / "appbeta" / "main.cpp")]

    def test_reanchor_helper_returns_none_when_nothing_new(self, monorepo):
        args = _parse_cake_args(monorepo, [*_ARGV_BASE])
        args.filename = [str(monorepo / "libcore" / "util.cpp")]
        with uth.DirectoryContext(str(monorepo)):
            assert compiletools.apptools.reanchor_config_for_discovered_targets(args) is None

    def test_cake_auto_builds_with_subproject_define(self, monorepo, tmp_path):
        """End-to-end: ct-cake --auto from the gitroot must compile
        appbeta/main.cpp with -DAPPBETA_EXTRA (the source #errors without it)."""
        (monorepo / "appbeta" / "main.cpp").write_text(
            "#ifndef APPBETA_EXTRA\n"
            "#error APPBETA_EXTRA missing - subproject ct.conf was not loaded\n"
            "#endif\n"
            "int main() { return 0; }\n"
        )
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                result = compiletools.cake.main([*_ARGV_BASE, "--auto"])
        # A non-zero result means either main() swallowed an exception or the
        # compiler rejected the #error -- both mean the subproject ct.conf
        # was not loaded.
        assert result == 0

    def test_cake_auto_conflicting_subprojects_error(self, monorepo):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        # Content must differ from appbeta/main.cpp: the global hash registry
        # refuses to disambiguate two tracked files with identical content.
        (appalpha / "main.cpp").write_text("// appalpha\nint main() { return 0; }\n")
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                # -v -v forces verbose >= 2 so cake.main re-raises instead of
                # swallowing the error into a return code (see main()'s
                # _FATAL_ERROR_RENDERERS dispatch).
                with pytest.raises(compiletools.configutils.ConfContradictionError):
                    compiletools.cake.main([*_ARGV_BASE, "--auto", "-v", "-v"])
