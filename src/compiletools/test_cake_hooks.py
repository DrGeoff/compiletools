"""Tests for ct-cake's --prebuild-script / --postbuild-script hooks.

These hooks run user-supplied shell command strings around the build:
* prebuild runs before backend.build_graph() so generated headers are
  visible to headerdeps.
* postbuild runs after a successful backend.execute("build") but before
  executables are copied to the top-level bindir.

Both abort the build on non-zero exit. Neither fires on --clean /
--realclean.
"""

import contextlib
import os
from unittest.mock import MagicMock, patch

import pytest

import compiletools.apptools
import compiletools.cake
import compiletools.testhelper as uth
from compiletools.build_backend import ensure_backends_registered, get_backend_class
from compiletools.build_context import BuildContext
from compiletools.testhelper import CakeTestContext

ensure_backends_registered()


@pytest.fixture
def ninja_cake(monkeypatch):
    """Build a ninja-backed CakeTestContext, chdir into its tmpdir, and
    yield (cake, tmpdir). Cleans up via the context manager on teardown."""
    with CakeTestContext("ninja") as ctx:
        monkeypatch.chdir(ctx[1])
        yield ctx


def _write_marker_script(tmpdir, name, marker_filename, *, exit_code=0):
    """Create an executable shell script in *tmpdir* that touches the
    marker file (path relative to its own cwd at run time) and exits
    with *exit_code*. Returns the absolute script path."""
    script_path = os.path.join(tmpdir, name)
    with open(script_path, "w") as f:
        f.write(f"#!/bin/sh\necho run >> {marker_filename}\nexit {exit_code}\n")
    os.chmod(script_path, 0o755)
    return script_path


class TestPrebuildPostbuildHooks:
    def test_prebuild_runs_before_backend_execute(self, ninja_cake):
        """The prebuild script's side effects must be visible by the time
        backend.execute("build") runs."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "prebuild.marker")
        script = _write_marker_script(tmpdir, "pre.sh", marker)
        cake.args.prebuild_scripts = [script]

        expected = get_backend_class("ninja")

        def _assert_marker_present(*_a, **_k):
            assert os.path.exists(marker), "prebuild marker must exist by the time backend.execute() runs"

        with (
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute", side_effect=_assert_marker_present),
        ):
            cake.process()

    def test_postbuild_runs_after_backend_execute(self, ninja_cake):
        """The postbuild marker must NOT exist when backend.execute fires,
        but must exist by the time process() returns."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "postbuild.marker")
        script = _write_marker_script(tmpdir, "post.sh", marker)
        cake.args.postbuild_scripts = [script]

        expected = get_backend_class("ninja")

        def _assert_marker_absent(*_a, **_k):
            assert not os.path.exists(marker), "postbuild marker must NOT exist when backend.execute() runs"

        with (
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute", side_effect=_assert_marker_absent),
        ):
            cake.process()

        assert os.path.exists(marker), "postbuild marker must exist after process() returns"

    def test_multiple_prebuild_scripts_run_in_declaration_order(self, ninja_cake):
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "order.marker")
        # Each script appends a unique line; we then check the order.
        scripts = []
        for tag in ("first", "second", "third"):
            path = os.path.join(tmpdir, f"{tag}.sh")
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\necho {tag} >> {marker}\n")
            os.chmod(path, 0o755)
            scripts.append(path)
        cake.args.prebuild_scripts = scripts

        expected = get_backend_class("ninja")
        with (
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute"),
        ):
            cake.process()

        with open(marker) as f:
            lines = [line.strip() for line in f]
        assert lines == ["first", "second", "third"]

    def test_failing_prebuild_aborts_before_backend_execute(self, ninja_cake):
        """Non-zero exit from a prebuild script raises SystemExit before
        backend.execute is reached."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "pre.marker")
        failing = _write_marker_script(tmpdir, "fail.sh", marker, exit_code=7)
        cake.args.prebuild_scripts = [failing]

        expected = get_backend_class("ninja")
        with (
            patch.object(expected, "build_graph", return_value=MagicMock()) as mock_graph,
            patch.object(expected, "generate") as mock_generate,
            patch.object(expected, "execute") as mock_execute,
        ):
            with pytest.raises(SystemExit) as excinfo:
                cake.process()

            assert "prebuild script failed" in str(excinfo.value)
            assert "exit 7" in str(excinfo.value)
            mock_graph.assert_not_called()
            mock_generate.assert_not_called()
            mock_execute.assert_not_called()

    def test_failing_postbuild_aborts_before_copyexes(self, ninja_cake):
        """Non-zero exit from a postbuild script raises SystemExit after
        backend.execute but before _copyexes."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "post.marker")
        failing = _write_marker_script(tmpdir, "fail.sh", marker, exit_code=3)
        cake.args.postbuild_scripts = [failing]

        expected = get_backend_class("ninja")
        with (
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute") as mock_execute,
        ):
            with pytest.raises(SystemExit) as excinfo:
                cake.process()

            assert "postbuild script failed" in str(excinfo.value)
            assert "exit 3" in str(excinfo.value)
            mock_execute.assert_called_once()
            # _copyexes is MagicMock'd by CakeTestContext
            cake._copyexes.assert_not_called()  # type: ignore[attr-defined]

    def test_subsequent_prebuild_scripts_skipped_after_failure(self, ninja_cake):
        """If script 1 of N fails, script 2 must not run."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "order.marker")
        failing = _write_marker_script(tmpdir, "fail.sh", marker, exit_code=1)
        after = _write_marker_script(tmpdir, "after.sh", marker)
        cake.args.prebuild_scripts = [failing, after]

        expected = get_backend_class("ninja")
        with (
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute"),
        ):
            with pytest.raises(SystemExit):
                cake.process()

        # Only the first (failing) script wrote to the marker.
        with open(marker) as f:
            lines = [line.strip() for line in f]
        assert lines == ["run"], "second prebuild script must not run after the first fails"

    @pytest.mark.parametrize("flag,backend_method", [("clean", "clean"), ("realclean", "realclean")])
    def test_clean_modes_skip_both_hooks(self, tmp_path, monkeypatch, flag, backend_method):
        with CakeTestContext("ninja", **{flag: True}) as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
            cake.args.output = os.path.join(tmpdir, "out")
            pre_marker = os.path.join(tmpdir, "pre.marker")
            post_marker = os.path.join(tmpdir, "post.marker")
            cake.args.prebuild_scripts = [_write_marker_script(tmpdir, "pre.sh", pre_marker)]
            cake.args.postbuild_scripts = [_write_marker_script(tmpdir, "post.sh", post_marker)]

            expected = get_backend_class("ninja")
            mock_graph = MagicMock()
            mock_graph.outputs = {"build", "all"}
            with (
                patch.object(expected, "build_graph", return_value=mock_graph),
                patch.object(expected, "generate"),
                patch.object(expected, backend_method) as mock_clean_method,
                patch.object(expected, "execute") as mock_execute,
            ):
                cake.process()
                mock_clean_method.assert_called_once()
                mock_execute.assert_not_called()

            assert not os.path.exists(pre_marker), f"prebuild must not fire on --{flag}"
            assert not os.path.exists(post_marker), f"postbuild must not fire on --{flag}"

    def test_prebuild_runs_before_build_graph(self, ninja_cake):
        """The prebuild script must run BEFORE build_graph() — otherwise
        headerdeps would miss generated headers."""
        cake, tmpdir = ninja_cake
        marker = os.path.join(tmpdir, "pre.marker")
        cake.args.prebuild_scripts = [_write_marker_script(tmpdir, "pre.sh", marker)]

        expected = get_backend_class("ninja")

        def _assert_marker_present(*_a, **_k):
            assert os.path.exists(marker), (
                "prebuild marker must exist before build_graph() so generated headers are visible to headerdeps"
            )
            return MagicMock()

        with (
            patch.object(expected, "build_graph", side_effect=_assert_marker_present),
            patch.object(expected, "generate"),
            patch.object(expected, "execute"),
        ):
            cake.process()

    def test_empty_script_lists_are_no_op(self, ninja_cake):
        """Default empty lists must short-circuit before subprocess.run.

        Patching the runner method itself (not subprocess.run, which the
        rest of ct-cake's startup invokes for git_root resolution) keeps
        this test focused on the empty-list contract.
        """
        cake, _tmpdir = ninja_cake
        assert cake.args.prebuild_scripts == []
        assert cake.args.postbuild_scripts == []

        expected = get_backend_class("ninja")
        with (
            patch("subprocess.run") as mock_run,
            patch.object(expected, "build_graph", return_value=MagicMock()),
            patch.object(expected, "generate"),
            patch.object(expected, "execute"),
        ):
            cake.process()
            # The hook runner uses shell=True; ct-cake startup uses lists/argv.
            # Filter to only shell=True invocations to isolate hook runs.
            shell_calls = [c for c in mock_run.call_args_list if c.kwargs.get("shell") is True]
            assert shell_calls == [], f"empty script lists must not invoke a shell, got: {shell_calls}"


@contextlib.contextmanager
def _hook_conf_repo(ct_conf_lines, variant_conf_lines, variant="hookvariant"):
    """Two-layer conf fixture for hook-key layering tests.

    Writes a project ``ct.conf`` (lower priority) and a
    ``ct.conf.d/<variant>.conf`` (higher priority, selected via
    ``--variant``) inside a TempDirContextNoChange. Yields the repo root.
    Mirrors ``test_apptools._temp_repo_with_ct_conf`` but keeps both
    layers hook-focused.
    """
    with uth.TempDirContextNoChange() as repo_root:
        conf_d = os.path.join(repo_root, "ct.conf.d")
        os.makedirs(conf_d, exist_ok=True)
        with open(os.path.join(repo_root, "ct.conf"), "w") as fh:
            fh.write(f"variant = {variant}\n")
            fh.write(f"variant-canonical-order = {variant}\n")
            fh.write("exemarkers = [main]\n")
            fh.write("testmarkers = unit_test.hpp\n")
            for line in ct_conf_lines:
                fh.write(line + "\n")
        with open(os.path.join(conf_d, f"{variant}.conf"), "w") as fh:
            fh.write("\n".join(variant_conf_lines) + "\n")
        yield repo_root


def _parse_cake_args(repo_root, argv):
    """create_parser + Cake.add_arguments + registercallback + parseargs,
    isolated via DirectoryContext + ParserContext (same plumbing as
    ``cake.main`` / ``test_cake.TestCake._make_cake_args``)."""
    argv = list(argv)
    with uth.DirectoryContext(repo_root):
        with uth.ParserContext():
            cap = compiletools.apptools.create_parser("hook layering test", argv=argv)
            compiletools.cake.Cake.add_arguments(cap)
            compiletools.cake.Cake.registercallback()
            return compiletools.apptools.parseargs(cap, argv, context=BuildContext())


class TestHookConfLayering:
    """Conf-layer semantics for prebuild-script / postbuild-script.

    Bare keys are last-writer-wins across conf layers (like every other
    non-``append-``/``prepend-`` key); accumulation is spelled
    ``append-PREBUILD-SCRIPT`` / ``prepend-PREBUILD-SCRIPT`` (key case
    matters in conf files — lowercase forms are silently ignored).
    """

    _ARGV = ("--variant=hookvariant", "--no-git-root")

    @pytest.fixture(autouse=True)
    def _needs_compiler(self):
        # parseargs resolves CXX via get_functional_cxx_compiler when the
        # conf doesn't pin one; skip cleanly on compiler-less machines.
        if compiletools.apptools.get_functional_cxx_compiler() is None:
            pytest.skip("No functional C++ compiler detected")

    def test_bare_key_higher_layer_replaces_lower(self):
        """Pin: a bare ``prebuild-script`` in the higher-priority conf
        replaces the lower layer's value — it does NOT accumulate."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["prebuild-script = ./project_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./project_hook.sh"]

    def test_bare_key_empty_list_suppresses_lower_layer(self):
        """Pin: ``prebuild-script = []`` in the higher layer suppresses the
        lower layer's hook entirely."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["prebuild-script = []"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == []

    def test_bare_key_json_list_yields_all_entries(self):
        """Pin: a single-layer JSON list value expands to multiple scripts
        in declaration order."""
        with _hook_conf_repo(
            [],
            ['prebuild-script = ["./gen_a.sh", "./gen_b.sh"]'],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./gen_a.sh", "./gen_b.sh"]

    def test_append_prebuild_script_accumulates_across_layers(self):
        """``append-PREBUILD-SCRIPT`` in two conf layers must yield both
        scripts, lower layer first."""
        with _hook_conf_repo(
            ["append-PREBUILD-SCRIPT = ./global_hook.sh"],
            ["append-PREBUILD-SCRIPT = ./project_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./global_hook.sh", "./project_hook.sh"]

    def test_append_extends_bare_base_across_layers(self):
        """A bare ``prebuild-script`` base in the lower layer plus an
        ``append-PREBUILD-SCRIPT`` in the higher layer runs both, base
        first."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["append-PREBUILD-SCRIPT = ./project_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./global_hook.sh", "./project_hook.sh"]

    def test_prepend_prebuild_script_lands_leftmost(self):
        """``prepend-PREBUILD-SCRIPT`` places its script before the bare
        base value."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["prepend-PREBUILD-SCRIPT = ./early_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./early_hook.sh", "./global_hook.sh"]

    def test_append_postbuild_script_accumulates_across_layers(self):
        """postbuild parity: ``append-POSTBUILD-SCRIPT`` accumulates the
        same way."""
        with _hook_conf_repo(
            ["append-POSTBUILD-SCRIPT = ./global_post.sh"],
            ["append-POSTBUILD-SCRIPT = ./project_post.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.postbuild_scripts == ["./global_post.sh", "./project_post.sh"]

    def test_empty_list_does_not_suppress_append_contributions(self):
        """Pin the asymmetry documented in README.ct-cake.rst: a higher
        layer's ``prebuild-script = []`` clears only the bare-key base;
        an ``append-PREBUILD-SCRIPT`` contribution from a lower layer
        survives and cannot be suppressed downstream."""
        with _hook_conf_repo(
            ["append-PREBUILD-SCRIPT = ./global_hook.sh"],
            ["prebuild-script = []"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./global_hook.sh"]

    def test_same_script_appended_in_two_layers_runs_twice(self):
        """Pin the dedup scope documented in README.ct-cake.rst: xxpend
        never dedups append entries against each other, so the same
        script appended in two layers appears twice."""
        with _hook_conf_repo(
            ["append-PREBUILD-SCRIPT = ./same_hook.sh"],
            ["append-PREBUILD-SCRIPT = ./same_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./same_hook.sh", "./same_hook.sh"]

    def test_append_entry_matching_bare_base_is_not_duplicated(self):
        """Pin the base-dedup half of the same doc claim: an appended
        script already present in the bare-key base is not added again."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["append-PREBUILD-SCRIPT = ./global_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./global_hook.sh"]

    def test_append_entry_matching_prepend_contribution_is_not_duplicated(self):
        """Pin the cross-group dedup half of the doc claim: prepend
        contributions merge into the base before append is processed, so
        an appended script matching a prepend entry is not added again —
        even with no bare key set at all."""
        with _hook_conf_repo(
            ["prepend-PREBUILD-SCRIPT = ./same_hook.sh"],
            ["append-PREBUILD-SCRIPT = ./same_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./same_hook.sh"]

    def test_lowercase_append_key_is_silently_ignored(self):
        """Pin the case-sensitivity warning stated in both READMEs and the
        --prebuild-script help text: conf keys are case-sensitive, so
        ``append-prebuild-script`` (lowercase) is an unknown key that
        configargparse silently drops — no scripts, no error."""
        with _hook_conf_repo(
            ["append-prebuild-script = ./global_hook.sh"],
            [],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == []

    def test_shadowed_bare_key_notes_at_verbose(self, capsys):
        """At ``-v`` a discarded lower-layer bare-key value gets a stderr
        note naming its conf file; silent at the default verbosity."""
        with _hook_conf_repo(
            ["prebuild-script = ./global_hook.sh"],
            ["prebuild-script = ./project_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./project_hook.sh"]
            assert "discarded" not in capsys.readouterr().err

            args = _parse_cake_args(repo_root, [*self._ARGV, "-v"])
            err = capsys.readouterr().err
            assert "prebuild-script = ./global_hook.sh" in err
            assert err.count("discarded") == 1
            assert "ct.conf:" in err
            assert "append-PREBUILD-SCRIPT" in err

    def test_shadow_note_fires_for_same_file_duplicate(self, capsys):
        """A bare key set twice in the SAME conf file is also
        last-writer-wins; the discarded first value gets the note too."""
        with _hook_conf_repo(
            [
                "prebuild-script = ./first.sh",
                "prebuild-script = ./second.sh",
            ],
            [],
        ) as repo_root:
            args = _parse_cake_args(repo_root, [*self._ARGV, "-v"])
            assert args.prebuild_scripts == ["./second.sh"]
            err = capsys.readouterr().err
            assert "prebuild-script = ./first.sh" in err
            assert "discarded" in err

    def test_shadow_note_not_fooled_by_quoted_values(self, capsys):
        """A quoted conf value survives post-_strip_quotes under a
        different spelling; the note must normalise both sides and stay
        silent for it."""
        with _hook_conf_repo(
            ['prebuild-script = "./my hook.sh"'],
            [],
        ) as repo_root:
            args = _parse_cake_args(repo_root, [*self._ARGV, "-v"])
            assert args.prebuild_scripts == ["./my hook.sh"]
            assert "discarded" not in capsys.readouterr().err

    def test_shadow_note_not_emitted_for_surviving_or_appended_values(self, capsys):
        """The note must not fire for the winning value, nor for a value
        that lost the bare-key contest but re-entered via append-."""
        with _hook_conf_repo(
            [
                "prebuild-script = ./global_hook.sh",
                "append-PREBUILD-SCRIPT = ./global_hook.sh",
            ],
            ["prebuild-script = ./project_hook.sh"],
        ) as repo_root:
            args = _parse_cake_args(repo_root, [*self._ARGV, "-v"])
            # ./global_hook.sh lost the bare-key contest to ./project_hook.sh
            # but re-entered via append- — it runs, so no note for it.
            assert args.prebuild_scripts == ["./project_hook.sh", "./global_hook.sh"]
            assert "discarded" not in capsys.readouterr().err

    def test_bare_cli_combines_with_bare_conf_value(self):
        """Pin the doc's opening claim for the bare key: a bare
        ``--prebuild-script`` on the CLI combines with (does not replace)
        the conf-file bare-key value, CLI value last."""
        with _hook_conf_repo(
            ["prebuild-script = ./from_conf.sh"],
            [],
        ) as repo_root:
            argv = [*self._ARGV, "--prebuild-script=./from_cli.sh"]
            args = _parse_cake_args(repo_root, argv)
            assert args.prebuild_scripts == ["./from_conf.sh", "./from_cli.sh"]

    def test_cli_append_combines_with_conf_append(self):
        """A CLI ``--append-PREBUILD-SCRIPT`` combines with conf-file
        ``append-PREBUILD-SCRIPT`` values rather than suppressing them."""
        with _hook_conf_repo(
            ["append-PREBUILD-SCRIPT = ./global_hook.sh"],
            [],
        ) as repo_root:
            argv = [*self._ARGV, "--append-PREBUILD-SCRIPT=./cli_hook.sh"]
            args = _parse_cake_args(repo_root, argv)
            assert args.prebuild_scripts == ["./global_hook.sh", "./cli_hook.sh"]

    def test_env_var_replaces_conf_and_bare_cli_combines_on_top(self, monkeypatch):
        """Pin the doc's override chain for the bare key: the
        ``PREBUILD_SCRIPT`` environment variable replaces the conf-file
        value (env > conf), while a bare ``--prebuild-script`` on the CLI
        combines with the env winner rather than replacing it."""
        monkeypatch.setenv("PREBUILD_SCRIPT", "./from_env.sh")
        with _hook_conf_repo(
            ["prebuild-script = ./from_conf.sh"],
            [],
        ) as repo_root:
            args = _parse_cake_args(repo_root, self._ARGV)
            assert args.prebuild_scripts == ["./from_env.sh"]
        with _hook_conf_repo(
            ["prebuild-script = ./from_conf.sh"],
            [],
        ) as repo_root:
            argv = [*self._ARGV, "--prebuild-script=./from_cli.sh"]
            args = _parse_cake_args(repo_root, argv)
            assert args.prebuild_scripts == ["./from_env.sh", "./from_cli.sh"]

    def test_shadow_note_fires_for_env_var_winner(self, capsys, monkeypatch):
        """An environment-variable winner also triggers the ``-v`` note for
        the discarded conf value. Env winners don't appear in conf
        provenance, so the note names only the loser's conf file — and it
        fires exactly once per parse."""
        monkeypatch.setenv("PREBUILD_SCRIPT", "./from_env.sh")
        with _hook_conf_repo(
            ["prebuild-script = ./from_conf.sh"],
            [],
        ) as repo_root:
            args = _parse_cake_args(repo_root, [*self._ARGV, "-v"])
            assert args.prebuild_scripts == ["./from_env.sh"]
            err = capsys.readouterr().err
            assert "prebuild-script = ./from_conf.sh" in err
            assert err.count("discarded") == 1
            assert "ct.conf:" in err
