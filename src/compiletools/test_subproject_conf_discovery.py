"""Target-anchored config discovery: a target's nearest-ancestor ct.conf /
ct.conf.d layer loads regardless of cwd; same-tier contradictions hard-error.
Subproject flags apply invocation-globally (no per-TU scoping).
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

    def test_no_git_root_walk_picks_up_conf_above_gitroot(self, tmp_path):
        # A conf layer above the gitroot is invisible to the git-bounded walk
        # but becomes eligible under --no-git-root (git_bounded=False), where
        # the walk is bounded only by the filesystem root.
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / "ct.conf").write_text("append-CPPFLAGS = -DOUTER\n")
        repo = outer / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "main.cpp").write_text("int main() { return 0; }\n")

        assert compiletools.configutils.walk_target_conf_layers([str(repo / "main.cpp")]) == ()

        layers = compiletools.configutils.walk_target_conf_layers([str(repo / "main.cpp")], git_bounded=False)
        assert len(layers) == 1
        assert os.path.realpath(layers[0].subproject_dir) == os.path.realpath(str(outer))

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

    def test_cwd_layer_variant_pin_overridden_by_cli_is_not_a_contradiction(self, tmp_path):
        # Critical 1: the cwd conf pins one variant; the CLI overrides it to a
        # different resolved variant. The cwd layer's variant is the cwd-tier
        # value, so it must NOT be compared against the invocation variant --
        # only TARGET layers get that comparison. A harmless target layer that
        # shares no key must parse cleanly.
        cwd_conf = tmp_path / "cwdproj" / "ct.conf"
        cwd_conf.parent.mkdir()
        cwd_conf.write_text("variant = pinnedvariant\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        validate_no_conf_contradictions([beta], [str(cwd_conf)], "othervariant", ["a", "b"])


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

    def test_begintests_synonym_is_partitioned(self, tmp_path):
        # --begintests is a dest=tests synonym; it must be popped alongside
        # --tests, and a flag left with no surviving value must be dropped.
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha_main = str(tmp_path / "appalpha" / "main.cpp")
        beta_test = str(tmp_path / "appbeta" / "test_foo.cpp")
        for p in (alpha_main, beta_test):
            with open(p, "w") as f:
                f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake", [alpha_main, "--begintests", beta_test], [alpha, beta], [alpha_main, beta_test]
        )
        assert beta_test not in commands[0]
        assert "--begintests" not in commands[0]  # dangling flag dropped
        assert "--begintests" in commands[1] and beta_test in commands[1]

    def test_nested_subprojects_use_deepest_prefix(self, tmp_path):
        # A child subproject nested under a parent: the child's target must be
        # owned by the child (deepest matching prefix), not lost to the parent.
        parent = _layer(tmp_path, "app", "x = 1\n")
        child = _layer(tmp_path, "app/sub", "x = 2\n")
        parent_main = str(tmp_path / "app" / "other.cpp")
        child_main = str(tmp_path / "app" / "sub" / "main.cpp")
        for p in (parent_main, child_main):
            with open(p, "w") as f:
                f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake", [parent_main, child_main], [parent, child], [parent_main, child_main]
        )
        assert len(commands) == 2
        # The command that excludes the parent-only target is the child's; it
        # must still build the child target (RED under first-prefix ownership).
        child_only = [c for c in commands if parent_main not in c]
        assert len(child_only) == 1
        assert child_main in child_only[0]

    def test_auto_form_emits_distinct_cd_commands(self, tmp_path):
        # --auto path: discovered targets are not in argv; each remedy must cd
        # into its own subproject and rediscover via --auto. Two remedies must
        # differ and each must build only its subproject.
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha_main = str(tmp_path / "appalpha" / "main.cpp")
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        for p in (alpha_main, beta_main):
            with open(p, "w") as f:
                f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--variant=monovariant", "--auto"],
            [alpha, beta],
            [alpha_main, beta_main],
            auto=True,
        )
        assert len(commands) == 2
        assert commands[0] != commands[1]
        assert all(c.startswith("cd ") and "--auto" in c for c in commands)
        assert str(tmp_path / "appalpha") in commands[0]
        assert str(tmp_path / "appbeta") in commands[1]

    def test_shared_target_kept_in_every_command(self, tmp_path):
        # A target owned by NO participant layer (shared source, e.g.
        # libcore/util.cpp) must stay in every remedy command; only targets
        # owned by a DIFFERENT participant are dropped.
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha_main = str(tmp_path / "appalpha" / "main.cpp")
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        libcore = tmp_path / "libcore"
        libcore.mkdir()
        shared = str(libcore / "util.cpp")
        for p in (alpha_main, beta_main, shared):
            with open(p, "w") as f:
                f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--variant=monovariant", alpha_main, beta_main, shared],
            [alpha, beta],
            [alpha_main, beta_main, shared],
        )
        assert len(commands) == 2
        assert shared in commands[0] and shared in commands[1]
        assert beta_main not in commands[0]
        assert alpha_main not in commands[1]

    def test_shared_target_kept_in_cwd_participant_form(self, tmp_path):
        # cd form: the shared target rides along with the participant that
        # owns an explicit target, but the --auto fallback (participant owning
        # no target) must carry NO targets at all -- cake ignores --auto
        # whenever any target list is non-empty, so a shared target appended
        # there would make the remedy command build nothing.
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        cwd_dir = str(tmp_path / "appalpha")
        os.makedirs(cwd_dir, exist_ok=True)
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        libcore = tmp_path / "libcore"
        libcore.mkdir()
        shared = str(libcore / "util.cpp")
        for p in (beta_main, shared):
            with open(p, "w") as f:
                f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--variant=monovariant", beta_main, shared],
            [beta],
            [beta_main, shared],
            cwd_layer_dir=cwd_dir,
        )
        assert len(commands) == 2
        beta_cmd = [c for c in commands if "appbeta" in c.split("&&")[0]]
        assert len(beta_cmd) == 1
        assert "util.cpp" in beta_cmd[0] and "main.cpp" in beta_cmd[0]
        auto_cmd = [c for c in commands if c is not beta_cmd[0]]
        assert auto_cmd[0].rstrip().endswith("--auto")
        assert "util.cpp" not in auto_cmd[0] and "main.cpp" not in auto_cmd[0]

    def test_library_flag_synonyms_are_partitioned(self, tmp_path):
        # --static-library / --dynamic-library are add_target_arguments
        # synonyms of --static / --dynamic; their values must partition the
        # same way (RED with the pre-fix _TARGET_VALUE_FLAGS tuple, which
        # would drop the flag but keep the value as a stray positional).
        alpha = _layer(tmp_path, "appalpha", "x = 1\n")
        beta = _layer(tmp_path, "appbeta", "x = 2\n")
        alpha_lib = str(tmp_path / "appalpha" / "lib.cpp")
        beta_lib = str(tmp_path / "appbeta" / "lib.cpp")
        for p in (alpha_lib, beta_lib):
            with open(p, "w") as f:
                f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--static-library", alpha_lib, "--dynamic-library", beta_lib],
            [alpha, beta],
            [alpha_lib, beta_lib],
        )
        assert len(commands) == 2
        assert "--static-library" in commands[0] and alpha_lib in commands[0]
        assert "--dynamic-library" not in commands[0] and beta_lib not in commands[0]
        assert "--dynamic-library" in commands[1] and beta_lib in commands[1]
        assert "--static-library" not in commands[1] and alpha_lib not in commands[1]

    def test_cwd_vs_target_form_emits_distinct_actionable_pair(self, tmp_path):
        # cwd layer participates in the conflict: the target subproject gets an
        # explicit cd + relative target; the cwd subproject (owns no target)
        # falls back to --auto. The two remedies must differ and be actionable.
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        cwd_dir = str(tmp_path / "appalpha")
        os.makedirs(cwd_dir, exist_ok=True)
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        with open(beta_main, "w") as f:
            f.write("int main() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake", ["--variant=monovariant", beta_main], [beta], [beta_main], cwd_layer_dir=cwd_dir
        )
        assert len(commands) == 2
        assert commands[0] != commands[1]
        assert all(c.startswith("cd ") for c in commands)
        beta_cmd = [c for c in commands if "appbeta" in c]
        assert len(beta_cmd) == 1 and "main.cpp" in beta_cmd[0]
        alpha_cmd = [c for c in commands if c.rstrip().endswith("--auto")]
        assert len(alpha_cmd) == 1 and "appalpha" in alpha_cmd[0]


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
        # -v -v: at verbose >= 2 the raw ConfContradictionError propagates
        # (at lower verbosity it is rendered to stderr and exits SystemExit(1),
        # covered by TestCakeMainErrorRendering).
        with pytest.raises(compiletools.configutils.ConfContradictionError) as excinfo:
            _parse_cake_args(
                monorepo,
                [*_ARGV_BASE, "-v", "-v", os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
            )
        message = str(excinfo.value)
        assert "-DAPPALPHA_EXTRA" in message and "-DAPPBETA_EXTRA" in message
        assert "Build separately" in message
        assert "identical" in message

    def test_conflict_at_default_verbosity_renders_and_exits(self, monorepo, capsys):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        with pytest.raises(SystemExit) as excinfo:
            _parse_cake_args(
                monorepo,
                [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
            )
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "Build separately" in err and "identical" in err

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
                [*_ARGV_BASE, "-v", "-v", os.path.join("..", "appbeta", "main.cpp")],
            )

    def test_variant_contradiction_errors(self, monorepo):
        appdelta = monorepo / "appdelta"
        appdelta.mkdir()
        (appdelta / "ct.conf").write_text("variant = othervariant\n")
        (appdelta / "main.cpp").write_text("int main() { return 0; }\n")
        with pytest.raises(compiletools.configutils.ConfContradictionError):
            _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", "-v", os.path.join("appdelta", "main.cpp")])


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

    def test_compilation_database_auto_reanchors_subproject_conf(self, monorepo):
        """ct-compilation-database --auto must re-anchor after discovery like
        cake.process() does: appbeta/main.cpp's CDB entry must carry
        -DAPPBETA_EXTRA from appbeta/ct.conf."""
        import json

        import compiletools.compilation_database

        uth.reset()
        output = str(monorepo / "cdb.json")
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                result = compiletools.compilation_database.main(
                    [*_ARGV_BASE, "--auto", f"--compilation-database-output={output}"]
                )
        assert result == 0
        with open(output) as f:
            entries = json.load(f)
        beta_entries = [e for e in entries if e["file"].endswith(os.path.join("appbeta", "main.cpp"))]
        assert beta_entries, f"appbeta/main.cpp missing from CDB: {[e['file'] for e in entries]}"
        assert "-DAPPBETA_EXTRA" in beta_entries[0]["arguments"]

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
                # -v -v forces verbose >= 2 so _apply_target_conf_layers
                # propagates the raw error instead of rendering to stderr and
                # raising SystemExit(1).
                with pytest.raises(compiletools.configutils.ConfContradictionError):
                    compiletools.cake.main([*_ARGV_BASE, "--auto", "-v", "-v"])


class TestCakeMainErrorRendering:
    def test_explicit_conflict_renders_cleanly_without_traceback(self, monorepo, capsys):
        """An explicit-target contradiction is rendered to stderr inside
        parseargs (message + remedies, no raw traceback) and exits via
        SystemExit(1) -- the argparse convention every ct-* entry point
        inherits without a per-tool handler."""
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("// appalpha\nint main() { return 0; }\n")
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                with pytest.raises(SystemExit) as excinfo:
                    compiletools.cake.main(
                        [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")]
                    )
        assert excinfo.value.code == 1
        combined = "".join(capsys.readouterr())
        assert "Build separately" in combined
        assert "identical" in combined  # remedy 2
        assert "Traceback" not in combined

    def test_explicit_conflict_reraises_at_high_verbose(self, monorepo):
        """verbose >= 2 re-raises so the traceback is available for debugging,
        matching the in-build handler's convention."""
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("// appalpha\nint main() { return 0; }\n")
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                with pytest.raises(compiletools.configutils.ConfContradictionError):
                    compiletools.cake.main(
                        [
                            *_ARGV_BASE,
                            "-v",
                            "-v",
                            os.path.join("appalpha", "main.cpp"),
                            os.path.join("appbeta", "main.cpp"),
                        ]
                    )
