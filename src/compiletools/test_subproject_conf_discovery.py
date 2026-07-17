"""Target-anchored config discovery: a target's nearest-ancestor ct.conf /
ct.conf.d layer loads regardless of cwd; same-tier contradictions hard-error.
Subproject flags apply invocation-globally (no per-TU scoping).
"""

import os
import shlex
import subprocess
import sys

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.configutils
import compiletools.findtargets
import compiletools.testhelper as uth
import compiletools.utils
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

    def test_unbounded_walk_note_prints_at_verbose_one_and_is_silent_at_zero(self, tmp_path, capsys):
        # An unbounded-walk layer can be a stray conf far above the target,
        # so the note is a load-bearing verbose >= 1 advisory (not a >= 2
        # diagnostic). Silent at verbose 0 for layers below $HOME's parent.
        # Emission is the caller's job (emit_unbounded_walk_notices); the
        # walk itself only records the hazard on the layer.
        appbeta = tmp_path / "plaintree" / "appbeta"
        appbeta.mkdir(parents=True)
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")
        target = str(appbeta / "main.cpp")

        layers = compiletools.configutils.walk_target_conf_layers([target], verbose=0)
        assert layers[0].git_bounded is False
        assert layers[0].above_home is False
        assert capsys.readouterr().err == ""

        compiletools.configutils.emit_unbounded_walk_notices(layers, verbose=0)
        assert "not bounded by a git root" not in capsys.readouterr().err

        compiletools.configutils.emit_unbounded_walk_notices(layers, verbose=1)
        err = capsys.readouterr().err
        assert "not bounded by a git root" in err
        assert "apply to the whole build" in err

    def test_home_ancestor_layer_warns_unconditionally(self, tmp_path, monkeypatch, capsys):
        # A conf at $HOME (or above) reached by an unbounded walk is almost
        # certainly a stray file; the warning prints even at verbose 0.
        fakehome = tmp_path / "fakehome"
        proj = fakehome / "proj"
        proj.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fakehome))
        conf = fakehome / "ct.conf"
        conf.write_text("append-CPPFLAGS = -DSTRAY\n")
        (proj / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(proj / "main.cpp")], verbose=0)
        assert layers[0].above_home is True
        compiletools.configutils.emit_unbounded_walk_notices(layers, verbose=0)
        err = capsys.readouterr().err
        assert "ct: warning:" in err
        assert "home directory or above" in err
        assert str(conf.resolve()) in err

    def test_git_bounded_layer_emits_no_note(self, tmp_path, capsys):
        # The common git-repo subproject case stays silent: the walk was
        # bounded by the gitroot, so no unbounded-walk note or warning.
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        appbeta.mkdir()
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")

        layers = compiletools.configutils.walk_target_conf_layers([str(appbeta / "main.cpp")], verbose=1)
        compiletools.configutils.emit_unbounded_walk_notices(layers, verbose=1)
        err = capsys.readouterr().err
        assert "not bounded by a git root" not in err
        assert "ct: warning:" not in err
        assert layers[0].git_bounded is True
        assert layers[0].above_home is False

    def test_layer_records_anchor_targets(self, tmp_path):
        root = _make_repo(tmp_path)
        appbeta = root / "appbeta"
        appbeta.mkdir()
        (appbeta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appbeta / "main.cpp").write_text("int main() { return 0; }\n")
        (appbeta / "other.cpp").write_text("int other() { return 1; }\n")
        targets = [str(appbeta / "main.cpp"), str(appbeta / "other.cpp")]

        layers = compiletools.configutils.walk_target_conf_layers(targets)
        assert layers[0].anchor_targets == tuple(sorted(targets))

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


# Explicit tuple for direct unit-test calls; production callers derive this
# from the live parser via apptools._target_value_flags_from_parser.
_TARGET_FLAGS = (
    "--tests",
    "--begintests",
    "--static",
    "--static-library",
    "--dynamic",
    "--dynamic-library",
)


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

    def test_intra_layer_scalar_override_is_not_a_contradiction(self, tmp_path):
        d = tmp_path / "appbeta"
        conf_d = d / "ct.conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "ct.conf").write_text("CXX = g++\n")
        (d / "ct.conf").write_text("CXX = clang++\n")
        layer = TargetConfLayer(
            subproject_dir=str(d),
            conf_paths=(str(conf_d / "ct.conf"), str(d / "ct.conf")),
        )
        validate_no_conf_contradictions([layer], [], "monovariant", ["cmd"])

    def test_intra_layer_append_masking_is_still_a_contradiction(self, tmp_path):
        """append-* accumulates across a layer's files, so alpha's effective
        value is "-DALPHA_ONLY -DCOMMON", not the last file's "-DCOMMON".
        Raw last-writer-wins comparison would see both layers as -DCOMMON and
        let -DALPHA_ONLY leak onto appbeta's translation units."""
        alpha_dir = tmp_path / "appalpha"
        alpha_conf_d = alpha_dir / "ct.conf.d"
        alpha_conf_d.mkdir(parents=True)
        (alpha_conf_d / "ct.conf").write_text("append-CPPFLAGS = -DALPHA_ONLY\n")
        (alpha_dir / "ct.conf").write_text("append-CPPFLAGS = -DCOMMON\n")
        alpha = TargetConfLayer(
            subproject_dir=str(alpha_dir),
            conf_paths=(str(alpha_conf_d / "ct.conf"), str(alpha_dir / "ct.conf")),
        )
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DCOMMON\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["a", "b"])
        message = str(excinfo.value)
        assert "-DALPHA_ONLY -DCOMMON" in message

    def test_identical_conf_dir_placeholders_expand_differently_and_contradict(self, tmp_path):
        """Two layers with the textually identical value
        ``-I${CONF_DIR}/include`` expand to different absolute dirs; comparing
        raw text would pass validation and apply both include dirs to every
        translation unit."""
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -I${CONF_DIR}/include\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -I${CONF_DIR}/include\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["a", "b"])
        message = str(excinfo.value)
        assert str(tmp_path / "appalpha" / "include") in message
        assert str(tmp_path / "appbeta" / "include") in message

    def test_env_vars_expanding_to_same_value_are_not_a_contradiction(self, monkeypatch, tmp_path):
        """Two different spellings that expand identically must merge silently
        -- raw-text comparison would raise a spurious contradiction."""
        monkeypatch.setenv("CT_TEST_SUBPROJ_FLAG", "-DFROMENV")
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = $CT_TEST_SUBPROJ_FLAG\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DFROMENV\n")
        validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["a", "b"])

    def test_three_layer_conflict_renders_each_value_once(self, tmp_path):
        """A/B/B across three layers renders one line per distinct value, not
        one line pair per clashing layer pair."""
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        gamma = _layer(tmp_path, "appgamma", "append-CPPFLAGS = -DBETA\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions([alpha, beta, gamma], [], "monovariant", ["a", "b", "c"])
        message = str(excinfo.value)
        assert message.count("-DALPHA") == 1
        assert message.count("-DBETA") == 1

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

    def test_error_message_notes_conf_level_strictness(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        with pytest.raises(ConfContradictionError) as excinfo:
            validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["a", "b"])
        assert "CLI" in str(excinfo.value)


class TestParserAwareContradictionValidation:
    def test_unregistered_key_is_skipped_when_registered_keys_given(self, tmp_path):
        # Another ct-* tool's key differing across subprojects must not block
        # a tool that never registered it.
        alpha = _layer(tmp_path, "appalpha", "some-other-tools-key = red\n")
        beta = _layer(tmp_path, "appbeta", "some-other-tools-key = blue\n")
        validate_no_conf_contradictions(
            [alpha, beta],
            [],
            "monovariant",
            ["a", "b"],
            registered_keys={"append_CPPFLAGS", "variant"},
        )

    def test_unregistered_key_still_raises_without_registered_keys(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "some-other-tools-key = red\n")
        beta = _layer(tmp_path, "appbeta", "some-other-tools-key = blue\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions([alpha, beta], [], "monovariant", ["a", "b"])

    def test_registered_key_conflict_still_raises(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions(
                [alpha, beta],
                [],
                "monovariant",
                ["a", "b"],
                registered_keys={"append_CPPFLAGS", "variant"},
            )

    def test_scalar_conflict_still_raises_with_registered_keys(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "CXX = g++\n")
        beta = _layer(tmp_path, "appbeta", "CXX = clang++\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions(
                [alpha, beta],
                [],
                "monovariant",
                ["a", "b"],
                registered_keys={"CXX", "variant"},
            )

    def test_boolean_synonyms_merge_via_canonicalizer(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "use-mtime = true\n")
        beta = _layer(tmp_path, "appbeta", "use-mtime = 1\n")
        validate_no_conf_contradictions(
            [alpha, beta],
            [],
            "monovariant",
            ["a", "b"],
            registered_keys={"use_mtime"},
            value_canonicalizers={"use_mtime": compiletools.utils.to_bool},
        )

    def test_boolean_true_vs_false_still_raises(self, tmp_path):
        alpha = _layer(tmp_path, "appalpha", "use-mtime = true\n")
        beta = _layer(tmp_path, "appbeta", "use-mtime = false\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions(
                [alpha, beta],
                [],
                "monovariant",
                ["a", "b"],
                registered_keys={"use_mtime"},
                value_canonicalizers={"use_mtime": compiletools.utils.to_bool},
            )

    def test_canonicalizer_rejection_falls_back_to_raw_string(self, tmp_path):
        # A value the type rejects compares as its raw string; argparse
        # produces the real error later. Identical raw strings merge.
        alpha = _layer(tmp_path, "appalpha", "use-mtime = maybe\n")
        beta = _layer(tmp_path, "appbeta", "use-mtime = maybe\n")
        validate_no_conf_contradictions(
            [alpha, beta],
            [],
            "monovariant",
            ["a", "b"],
            registered_keys={"use_mtime"},
            value_canonicalizers={"use_mtime": compiletools.utils.to_bool},
        )

    def test_variant_carve_out_survives_registered_keys_filter(self, tmp_path):
        beta = _layer(tmp_path, "appbeta", "variant = othervariant\n")
        with pytest.raises(ConfContradictionError):
            validate_no_conf_contradictions(
                [beta],
                [],
                "monovariant",
                ["cmd"],
                registered_keys={"variant"},
            )


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
            "ct-cake",
            ["--variant=monovariant", alpha_main, beta_main],
            [alpha, beta],
            [alpha_main, beta_main],
            target_value_flags=_TARGET_FLAGS,
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
            "ct-cake",
            [alpha_main, "--tests", beta_test],
            [alpha, beta],
            [alpha_main, beta_test],
            target_value_flags=_TARGET_FLAGS,
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
            "ct-cake",
            [alpha_main, "--begintests", beta_test],
            [alpha, beta],
            [alpha_main, beta_test],
            target_value_flags=_TARGET_FLAGS,
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
            "ct-cake",
            [parent_main, child_main],
            [parent, child],
            [parent_main, child_main],
            target_value_flags=_TARGET_FLAGS,
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
            target_value_flags=_TARGET_FLAGS,
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
            target_value_flags=_TARGET_FLAGS,
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
            target_value_flags=_TARGET_FLAGS,
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
        # same way as the short forms.
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
            target_value_flags=_TARGET_FLAGS,
        )
        assert len(commands) == 2
        assert "--static-library" in commands[0] and alpha_lib in commands[0]
        assert "--dynamic-library" not in commands[0] and beta_lib not in commands[0]
        assert "--dynamic-library" in commands[1] and beta_lib in commands[1]
        assert "--static-library" not in commands[1] and alpha_lib not in commands[1]

    def test_dash_prefixed_target_survives_cwd_form_as_path_token(self, tmp_path):
        # A legal-but-pathological source name beginning with '-' must not be
        # emitted as a bare '-weird.cpp' in the cd remedy form: relpath strips
        # the './' the user needed to make argparse treat it as a positional.
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        cwd_dir = str(tmp_path / "appalpha")
        os.makedirs(cwd_dir, exist_ok=True)
        weird = str(tmp_path / "appbeta" / "-weird.cpp")
        with open(weird, "w") as f:
            f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--variant=monovariant", weird],
            [beta],
            [weird],
            cwd_layer_dir=cwd_dir,
            target_value_flags=_TARGET_FLAGS,
        )
        beta_cmd = next(c for c in commands if "appbeta" in c.split("&&")[0])
        rebuilt_targets = [t for t in shlex.split(beta_cmd.split("&&", 1)[1]) if t.endswith("-weird.cpp")]
        assert rebuilt_targets, f"target missing from remedy: {beta_cmd}"
        assert all(not t.startswith("-") for t in rebuilt_targets), (
            f"dash-prefixed target emitted as a flag token: {beta_cmd}"
        )

    def test_cwd_form_keeps_flag_target_association(self, tmp_path):
        # A7 regression: in the cwd-participant form, a kept target must stay
        # attached to its flag. Stripping all targets and re-appending kept
        # ones as bare positionals silently turned `--tests test.cpp` into a
        # plain executable target.
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        alpha_test = str(tmp_path / "appalpha" / "test.cpp")
        beta_lib = str(tmp_path / "appbeta" / "lib.cpp")
        for p in (alpha_test, beta_lib):
            with open(p, "w") as f:
                f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            ["--tests", alpha_test, "--dynamic", beta_lib],
            [alpha, beta],
            [alpha_test, beta_lib],
            cwd_layer_dir=str(tmp_path / "appalpha"),
            target_value_flags=_TARGET_FLAGS,
        )
        alpha_cmd = next(c for c in commands if "test.cpp" in c)
        beta_cmd = next(c for c in commands if "lib.cpp" in c)
        assert alpha_cmd != beta_cmd
        alpha_tokens = shlex.split(alpha_cmd.split("&&", 1)[1])
        beta_tokens = shlex.split(beta_cmd.split("&&", 1)[1])
        assert alpha_tokens[alpha_tokens.index("--tests") + 1] == "test.cpp"
        assert "--dynamic" not in alpha_tokens and "lib.cpp" not in alpha_tokens
        assert beta_tokens[beta_tokens.index("--dynamic") + 1] == "lib.cpp"
        assert "--tests" not in beta_tokens and "test.cpp" not in beta_tokens

    def test_cwd_form_reassembles_flag_equals_value(self, tmp_path):
        # `--tests=path` form: the kept target is rewritten in place and the
        # `--flag=value` token reassembled relative to the subproject.
        alpha = _layer(tmp_path, "appalpha", "append-CPPFLAGS = -DALPHA\n")
        beta = _layer(tmp_path, "appbeta", "append-CPPFLAGS = -DBETA\n")
        alpha_test = str(tmp_path / "appalpha" / "test.cpp")
        beta_main = str(tmp_path / "appbeta" / "main.cpp")
        for p in (alpha_test, beta_main):
            with open(p, "w") as f:
                f.write("int f() { return 0; }\n")

        commands = build_separate_build_commands(
            "ct-cake",
            [f"--tests={alpha_test}", beta_main],
            [alpha, beta],
            [alpha_test, beta_main],
            cwd_layer_dir=str(tmp_path / "appalpha"),
            target_value_flags=_TARGET_FLAGS,
        )
        alpha_cmd = next(c for c in commands if "test.cpp" in c)
        alpha_tokens = shlex.split(alpha_cmd.split("&&", 1)[1])
        assert "--tests=test.cpp" in alpha_tokens
        assert "main.cpp" not in alpha_cmd

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
            "ct-cake",
            ["--variant=monovariant", beta_main],
            [beta],
            [beta_main],
            cwd_layer_dir=cwd_dir,
            target_value_flags=_TARGET_FLAGS,
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

    def test_unregistered_key_conflict_does_not_block_ct_cake(self, monorepo):
        # A key ct-cake never registered (another ct-* tool's key) differing
        # across two subprojects must not raise for ct-cake.
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("some-other-tools-key = red\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text("some-other-tools-key = blue\n")
        args = _parse_cake_args(
            monorepo,
            [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
        )
        assert args is not None

    def test_boolean_synonym_values_do_not_conflict_for_ct_cake(self, monorepo):
        # `use-mtime = true` vs `use-mtime = 1` canonicalize identically via
        # the parser's bool coercion, so no contradiction is raised.
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("use-mtime = true\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text("use-mtime = 1\n")
        args = _parse_cake_args(
            monorepo,
            [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")],
        )
        assert args.use_mtime is True

    def test_boolean_true_vs_false_conflicts_for_ct_cake(self, monorepo):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("use-mtime = true\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text("use-mtime = false\n")
        with pytest.raises(compiletools.configutils.ConfContradictionError):
            _parse_cake_args(
                monorepo,
                [
                    *_ARGV_BASE,
                    "-v",
                    "-v",
                    os.path.join("appalpha", "main.cpp"),
                    os.path.join("appbeta", "main.cpp"),
                ],
            )

    def test_remedy_keeps_tests_flag_attached_to_target(self, monorepo):
        # A7 e2e: cwd-layer conflict with `--tests appalpha/test.cpp
        # --dynamic appbeta/lib.cpp` must produce remedies that keep each
        # target attached to its flag.
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "test.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "lib.cpp").write_text("int f() { return 0; }\n")
        appgamma = monorepo / "appgamma"
        appgamma.mkdir()
        (appgamma / "ct.conf").write_text("append-CPPFLAGS = -DAPPGAMMA_EXTRA\n")
        with pytest.raises(compiletools.configutils.ConfContradictionError) as excinfo:
            _parse_cake_args(
                appgamma,
                [
                    *_ARGV_BASE,
                    "-v",
                    "-v",
                    "--tests",
                    os.path.join("..", "appalpha", "test.cpp"),
                    "--dynamic",
                    os.path.join("..", "appbeta", "lib.cpp"),
                ],
            )
        message = str(excinfo.value)
        remedies = [line.strip() for line in message.splitlines() if line.strip().startswith("cd ")]
        alpha_cmd = next(c for c in remedies if "test.cpp" in c)
        alpha_tokens = shlex.split(alpha_cmd.split("&&", 1)[1])
        assert alpha_tokens[alpha_tokens.index("--tests") + 1] == "test.cpp"
        assert "lib.cpp" not in alpha_tokens
        beta_cmd = next(c for c in remedies if "lib.cpp" in c)
        beta_tokens = shlex.split(beta_cmd.split("&&", 1)[1])
        assert beta_tokens[beta_tokens.index("--dynamic") + 1] == "lib.cpp"
        assert "test.cpp" not in beta_tokens

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

    def test_case_mismatched_key_in_target_layer_gets_did_you_mean_note(self, monorepo, capsys):
        """Conf keys are case-sensitive; configargparse silently ignores
        unknown ones. A target-layer key differing from a registered key only
        in case must produce a stderr note naming the file and the intended
        spelling, so the resulting no-op is self-diagnosing."""
        (monorepo / "appbeta" / "ct.conf").write_text("append-cppflags = -DAPPBETA_EXTRA\n")
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", os.path.join("appbeta", "main.cpp")])
        assert "-DAPPBETA_EXTRA" not in args.CPPFLAGS  # still ignored -- the note is diagnostic only
        err = capsys.readouterr().err
        assert "append-cppflags" in err
        assert "append-CPPFLAGS" in err
        assert str(monorepo / "appbeta" / "ct.conf") in err

    def test_correctly_cased_target_layer_keys_emit_no_note(self, monorepo, capsys):
        _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", os.path.join("appbeta", "main.cpp")])
        assert "did you mean" not in capsys.readouterr().err

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

    def test_identical_append_values_across_layers_apply_once(self, monorepo):
        """Two layers appending the same value pass validation, and the
        accumulating parser applies the token once, not once per layer."""
        appdelta = monorepo / "appdelta"
        appdelta.mkdir()
        (appdelta / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\n")
        (appdelta / "main.cpp").write_text("int main() { return 0; }\n")
        args = _parse_cake_args(
            monorepo,
            [*_ARGV_BASE, os.path.join("appbeta", "main.cpp"), os.path.join("appdelta", "main.cpp")],
        )
        assert args.CPPFLAGS.count("-DAPPBETA_EXTRA") == 1

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

    def test_layer_loaded_note_names_target_and_scope(self, monorepo, capsys):
        """The verbose>=1 note must say WHICH target anchored the layer,
        WHAT was loaded, and that the settings apply invocation-wide --
        otherwise a changed build is not self-diagnosing."""
        target = os.path.join("appbeta", "main.cpp")
        _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", target])
        err = capsys.readouterr().err
        assert "ct: note: target" in err
        assert target in err
        assert str(monorepo / "appbeta" / "ct.conf") in err
        assert "whole invocation" in err
        # appbeta's layer sets only append-CPPFLAGS: no scalar-keys suffix.
        assert "scalar keys" not in err

    def test_layer_note_names_scalar_keys(self, monorepo, capsys):
        """The verbose>=1 anchoring note names the scalar keys the layer
        sets, so an invocation-global scalar (e.g. disable-tests) is visible
        at the point it sneaks in."""
        (monorepo / "appbeta" / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\ndisable-tests = true\n")
        _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", os.path.join("appbeta", "main.cpp")])
        err = capsys.readouterr().err
        assert "(scalar keys: disable-tests)" in err

    def test_layer_loaded_note_silent_at_verbose_zero(self, monorepo, capsys):
        _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appbeta", "main.cpp")])
        assert "ct: note: target" not in capsys.readouterr().err

    def test_cwd_tier_participation_note_at_verbose_two(self, monorepo, capsys):
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        # Different key from appbeta's layer: participates in same-tier
        # validation without contradicting.
        (appalpha / "ct.conf").write_text("append-CXXFLAGS = -DALPHA_CXX\n")
        _parse_cake_args(
            monorepo / "appalpha",
            [*_ARGV_BASE, "-v", "-v", os.path.join("..", "appbeta", "main.cpp")],
        )
        err = capsys.readouterr().err
        assert "cwd config layer" in err
        assert "contradiction validation" in err

    def test_conf_injected_test_target_gets_its_layer_walked(self, monorepo):
        """Fixpoint: appbeta's conf injects a test target inside appgamma;
        appgamma's own layer must then be walked and loaded too. Before the
        fixpoint the injected target's layer was silently skipped."""
        appgamma = monorepo / "appgamma"
        appgamma.mkdir()
        (appgamma / "ct.conf").write_text("append-CXXFLAGS = -DAPPGAMMA_EXTRA\n")
        (appgamma / "test_gamma.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text(
            "append-CPPFLAGS = -DAPPBETA_EXTRA\ntests = [${CONF_DIR}/../appgamma/test_gamma.cpp]\n"
        )
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appbeta", "main.cpp")])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS
        assert "-DAPPGAMMA_EXTRA" in args.CXXFLAGS
        assert any(t.endswith("test_gamma.cpp") for t in args.tests)

    def test_conf_injected_targets_terminate_on_mutual_injection(self, monorepo):
        """Mutual injection (beta's conf points into gamma, gamma's conf
        points back into beta) converges: round two finds beta's layer
        already loaded, so the fixpoint stops instead of looping."""
        appgamma = monorepo / "appgamma"
        appgamma.mkdir()
        (appgamma / "test_gamma.cpp").write_text("int main() { return 0; }\n")
        (appgamma / "ct.conf").write_text(
            "append-CXXFLAGS = -DAPPGAMMA_EXTRA\ndynamic = [${CONF_DIR}/../appbeta/libbeta.cpp]\n"
        )
        (monorepo / "appbeta" / "libbeta.cpp").write_text("int beta() { return 1; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text(
            "append-CPPFLAGS = -DAPPBETA_EXTRA\ntests = [${CONF_DIR}/../appgamma/test_gamma.cpp]\n"
        )
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, os.path.join("appbeta", "main.cpp")])
        assert "-DAPPBETA_EXTRA" in args.CPPFLAGS
        assert "-DAPPGAMMA_EXTRA" in args.CXXFLAGS
        assert any(t.endswith("libbeta.cpp") for t in args.dynamic)

    def test_rejected_layers_do_not_persist_on_parser(self, monorepo):
        """A caught contradiction must leave no accumulated layers on the
        parser: a caller that catches the SystemExit and re-runs parseargs
        on the same parser would otherwise re-validate rejected layers."""
        appalpha = monorepo / "appalpha"
        appalpha.mkdir()
        (appalpha / "ct.conf").write_text("append-CPPFLAGS = -DAPPALPHA_EXTRA\n")
        (appalpha / "main.cpp").write_text("int main() { return 0; }\n")
        argv = [*_ARGV_BASE, os.path.join("appalpha", "main.cpp"), os.path.join("appbeta", "main.cpp")]
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                cap = compiletools.apptools.create_parser("subproject conf discovery test", argv=argv)
                compiletools.cake.Cake.add_arguments(cap)
                compiletools.cake.Cake.registercallback()
                with pytest.raises(SystemExit):
                    compiletools.apptools.parseargs(cap, argv, context=BuildContext())
        assert getattr(cap, "_ct_loaded_target_layers", []) == []

    def test_eleven_deep_nested_subproject_chain_converges(self, tmp_path):
        """An 11-deep nested-subproject layout with harmonious confs is
        legal and must converge without hitting the fixpoint cap. Conf
        discovery only -- no compilation."""
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
        current = root
        targets = []
        conf_paths = []
        for level in range(1, 12):
            current = current / f"proj{level}"
            current.mkdir()
            conf = current / "ct.conf"
            conf.write_text("append-CPPFLAGS = -DCHAIN\n")
            conf_paths.append(str(conf))
            src = current / f"main{level}.cpp"
            src.write_text(f"// level {level}\nint main() {{ return 0; }}\n")
            targets.append(os.path.relpath(str(src), str(root)))
        args = _parse_cake_args(root, [*_ARGV_BASE, *targets])
        assert args.CPPFLAGS.count("-DCHAIN") == 1
        loaded = {os.path.realpath(p) for p in args._parser._default_config_files}
        for conf in conf_paths:
            assert os.path.realpath(conf) in loaded
        assert len(args._parser._ct_loaded_target_layers) == 11

    def test_home_warning_fires_once_across_multi_round_fixpoint(self, tmp_path, monkeypatch, capsys):
        """The stray-$HOME-conf warning must print once for the layer's
        loading round, not once per fixpoint round: round two (triggered by
        the home conf injecting a test target) walks the home layer again
        but finds it already loaded."""
        fakehome = tmp_path / "fakehome"
        work = fakehome / "work"
        src = fakehome / "src"
        sub = fakehome / "sub"
        for d in (work, src, sub):
            d.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fakehome))
        (work / "ct.conf").write_text(
            f"variant = {_VARIANT}\n"
            f"variant-canonical-order = {_VARIANT}\n"
            "exemarkers = [main]\n"
            "testmarkers = unit_test.hpp\n"
        )
        (work / "ct.conf.d").mkdir()
        (work / "ct.conf.d" / f"{_VARIANT}.conf").write_text("# variant conf\n")
        (sub / "ct.conf").write_text("append-CXXFLAGS = -DSUB_EXTRA\n")
        (sub / "t2.cpp").write_text("// sub test\nint main() { return 0; }\n")
        (fakehome / "ct.conf").write_text(f"append-CPPFLAGS = -DSTRAY\ntests = [{sub / 't2.cpp'}]\n")
        (src / "main.cpp").write_text("int main() { return 0; }\n")
        args = _parse_cake_args(work, [*_ARGV_BASE, os.path.join("..", "src", "main.cpp")])
        assert "-DSTRAY" in args.CPPFLAGS
        assert "-DSUB_EXTRA" in args.CXXFLAGS  # round two loaded sub's layer
        err = capsys.readouterr().err
        assert err.count("home directory or above") == 1

    def test_round_two_layer_contradicting_round_one_layer_errors(self, monorepo):
        """A layer loaded in a later fixpoint round must be validated against
        the layers from earlier rounds, not only its own round's peers."""
        appgamma = monorepo / "appgamma"
        appgamma.mkdir()
        (appgamma / "ct.conf").write_text("append-CPPFLAGS = -DAPPGAMMA_DIFFERENT\n")
        (appgamma / "test_gamma.cpp").write_text("int main() { return 0; }\n")
        (monorepo / "appbeta" / "ct.conf").write_text(
            "append-CPPFLAGS = -DAPPBETA_EXTRA\ntests = [${CONF_DIR}/../appgamma/test_gamma.cpp]\n"
        )
        with pytest.raises(compiletools.configutils.ConfContradictionError) as excinfo:
            _parse_cake_args(monorepo, [*_ARGV_BASE, "-v", "-v", os.path.join("appbeta", "main.cpp")])
        message = str(excinfo.value)
        assert "-DAPPBETA_EXTRA" in message and "-DAPPGAMMA_DIFFERENT" in message


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
        # Pure config re-anchor: discovered targets are NOT re-applied; the
        # discover_targets_and_reanchor driver re-discovers them under the
        # widened config so freshly loaded markers can shape the set.
        assert reanchored.filename == []

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

    def test_cake_main_argv_none_explicit_target(self, monorepo, monkeypatch):
        """Console entry points call main(argv=None); the target-layer walk
        must resolve sys.argv itself. Regression: list(None) TypeError."""
        (monorepo / "appbeta" / "main.cpp").write_text(
            "#ifndef APPBETA_EXTRA\n"
            "#error APPBETA_EXTRA missing - subproject ct.conf was not loaded\n"
            "#endif\n"
            "int main() { return 0; }\n"
        )
        monkeypatch.setattr(sys, "argv", ["ct-cake", *_ARGV_BASE, os.path.join("appbeta", "main.cpp")])
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                assert compiletools.cake.main(None) == 0

    def test_cake_main_argv_none_auto_reanchors(self, monorepo, monkeypatch):
        """main(argv=None) with --auto must still re-anchor after discovery.
        Regression: _argv stashed as None made the re-anchor a silent no-op."""
        (monorepo / "appbeta" / "main.cpp").write_text(
            "#ifndef APPBETA_EXTRA\n"
            "#error APPBETA_EXTRA missing - subproject ct.conf was not loaded\n"
            "#endif\n"
            "int main() { return 0; }\n"
        )
        monkeypatch.setattr(sys, "argv", ["ct-cake", *_ARGV_BASE, "--auto"])
        uth.reset()
        with uth.DirectoryContext(str(monorepo)):
            with uth.ParserContext():
                assert compiletools.cake.main(None) == 0

    def test_reanchor_reapplies_subproject_pkg_config_path(self, monorepo, monkeypatch):
        """The first parseargs latches context.pkg_config_overrides_applied;
        the re-anchor's parseargs re-run must reset it so a target layer's
        prepend-PKG-CONFIG-PATH reaches os.environ."""
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        (monorepo / "appbeta" / "ct.conf").write_text(
            "append-CPPFLAGS = -DAPPBETA_EXTRA\nprepend-PKG-CONFIG-PATH = ${CONF_DIR}/pkgconfig\n"
        )
        (monorepo / "appbeta" / "pkgconfig").mkdir()
        args = _parse_cake_args(monorepo, [*_ARGV_BASE])
        try:
            args.filename = [str(monorepo / "appbeta" / "main.cpp")]
            with uth.DirectoryContext(str(monorepo)):
                reanchored = compiletools.apptools.reanchor_config_for_discovered_targets(args)
            assert reanchored is not None
            expected = str(monorepo / "appbeta" / "pkgconfig")
            assert expected in os.environ.get("PKG_CONFIG_PATH", "").split(os.pathsep)
        finally:
            args._context.restore_pkg_config_path()

    def test_auto_rediscovery_honors_subproject_disable_tests(self, monorepo):
        """The marker leak: appbeta's own conf sets disable-tests, which
        arrives only after round-one discovery already classified
        test_beta.cpp as a test. The driver's round two must re-discover
        under the new config and drop it. Fails without the analyze-file
        cache clear between rounds (marker_type is baked into the cached
        result keyed only by content hash)."""
        (monorepo / "appbeta" / "ct.conf").write_text("append-CPPFLAGS = -DAPPBETA_EXTRA\ndisable-tests = true\n")
        # Content must differ from appbeta/main.cpp: the global hash registry
        # refuses to disambiguate two tracked files with identical content.
        (monorepo / "appbeta" / "test_beta.cpp").write_text("// test_beta\nint main() { return 0; }\n")
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, "--auto"])
        with uth.DirectoryContext(str(monorepo)):
            final = compiletools.findtargets.discover_targets_and_reanchor(args, args._context)
        assert "-DAPPBETA_EXTRA" in final.CPPFLAGS
        assert not final.tests
        assert any(f.endswith(os.path.join("appbeta", "main.cpp")) for f in final.filename)

    def test_auto_rediscovery_honors_subproject_exemarkers(self, monorepo):
        """A subproject layer's own exemarkers must shape discovery. The
        layer's value REPLACES the project-tier one (highest-priority conf
        wins), so appbeta/main.cpp -- discovered in round one under
        `exemarkers = [main]` -- is no longer an executable in round two.
        This is the tripwire for the analyze-file cache clear between
        rounds: without it, round two replays the cached
        marker_type == EXE classification and main.cpp survives."""
        (monorepo / "appbeta" / "ct.conf").write_text(
            "append-CPPFLAGS = -DAPPBETA_EXTRA\nexemarkers = [SPECIAL_ENTRY]\n"
        )
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, "--auto"])
        with uth.DirectoryContext(str(monorepo)):
            final = compiletools.findtargets.discover_targets_and_reanchor(args, args._context)
        assert "-DAPPBETA_EXTRA" in final.CPPFLAGS  # the layer WAS loaded
        assert not any(f.endswith(os.path.join("appbeta", "main.cpp")) for f in final.filename)

    def test_round_two_discovered_layer_contradicting_round_one_layer_errors(self, monorepo):
        """A layer loaded in an earlier driver round must stay visible to a
        later round's contradiction validation even when its own target is
        no longer discovered. appbeta's exemarkers swap drops
        appbeta/main.cpp from round-two discovery, which instead finds
        appgamma; without the parser-persisted layer accumulation,
        appgamma's conflicting append-CPPFLAGS would merge silently with
        appbeta's."""
        (monorepo / "appbeta" / "ct.conf").write_text("append-CPPFLAGS = -DFROM_BETA\nexemarkers = [SPECIAL_ENTRY]\n")
        appgamma = monorepo / "appgamma"
        appgamma.mkdir()
        (appgamma / "ct.conf").write_text("append-CPPFLAGS = -DFROM_GAMMA\n")
        # Invisible to round one (no `main` marker), discovered in round two
        # once appbeta's layer swaps exemarkers to SPECIAL_ENTRY. The marker
        # must be code, not a comment -- comment-only markers don't classify.
        (appgamma / "tool.cpp").write_text("int SPECIAL_ENTRY() { return 0; }\n")
        # -v -v: propagate the raw ConfContradictionError instead of the
        # rendered-stderr SystemExit(1) path.
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, "--auto", "-v", "-v"])
        with uth.DirectoryContext(str(monorepo)):
            with pytest.raises(compiletools.configutils.ConfContradictionError):
                compiletools.findtargets.discover_targets_and_reanchor(args, args._context)

    def test_rediscovery_does_not_duplicate_targets(self, monorepo):
        """A config-widening round triggers a fresh discovery pass;
        FindTargets.process appends, so the driver must start each round
        from a namespace without the previous round's discoveries."""
        args = _parse_cake_args(monorepo, [*_ARGV_BASE, "--auto"])
        with uth.DirectoryContext(str(monorepo)):
            final = compiletools.findtargets.discover_targets_and_reanchor(args, args._context)
        assert "-DAPPBETA_EXTRA" in final.CPPFLAGS  # the widening round ran
        beta_main = [f for f in final.filename if f.endswith(os.path.join("appbeta", "main.cpp"))]
        assert len(beta_main) == 1
        assert len(final.filename) == len(set(final.filename))

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
