"""Tests for ct-cake's --prebuild-script / --postbuild-script hooks.

These hooks run user-supplied shell command strings around the build:
* prebuild runs before backend.build_graph() so generated headers are
  visible to headerdeps.
* postbuild runs after a successful backend.execute("build") but before
  executables are copied to the top-level bindir.

Both abort the build on non-zero exit. Neither fires on --clean /
--realclean.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from compiletools.build_backend import ensure_backends_registered, get_backend_class
from compiletools.testhelper import CakeTestContext

ensure_backends_registered()


def _write_marker_script(tmpdir, name, marker_filename, *, exit_code=0):
    """Create an executable shell script in *tmpdir* that touches the
    marker file (path relative to its own cwd at run time) and exits
    with *exit_code*. Returns the absolute script path."""
    script_path = os.path.join(tmpdir, name)
    with open(script_path, "w") as f:
        f.write(
            f"#!/bin/sh\n"
            f"echo run >> {marker_filename}\n"
            f"exit {exit_code}\n"
        )
    os.chmod(script_path, 0o755)
    return script_path


class TestPrebuildPostbuildHooks:
    def test_prebuild_runs_before_backend_execute(self, tmp_path, monkeypatch):
        """The prebuild script's side effects must be visible by the time
        backend.execute("build") runs."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
            marker = os.path.join(tmpdir, "prebuild.marker")
            script = _write_marker_script(tmpdir, "pre.sh", marker)
            cake.args.prebuild_scripts = [script]

            expected = get_backend_class("ninja")

            def _assert_marker_present(*_a, **_k):
                assert os.path.exists(marker), (
                    "prebuild marker must exist by the time backend.execute() runs"
                )

            with (
                patch.object(expected, "build_graph", return_value=MagicMock()),
                patch.object(expected, "generate"),
                patch.object(expected, "execute", side_effect=_assert_marker_present),
            ):
                cake.process()

    def test_postbuild_runs_after_backend_execute(self, tmp_path, monkeypatch):
        """The postbuild marker must NOT exist when backend.execute fires,
        but must exist by the time process() returns."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
            marker = os.path.join(tmpdir, "postbuild.marker")
            script = _write_marker_script(tmpdir, "post.sh", marker)
            cake.args.postbuild_scripts = [script]

            expected = get_backend_class("ninja")

            def _assert_marker_absent(*_a, **_k):
                assert not os.path.exists(marker), (
                    "postbuild marker must NOT exist when backend.execute() runs"
                )

            with (
                patch.object(expected, "build_graph", return_value=MagicMock()),
                patch.object(expected, "generate"),
                patch.object(expected, "execute", side_effect=_assert_marker_absent),
            ):
                cake.process()

            assert os.path.exists(marker), (
                "postbuild marker must exist after process() returns"
            )

    def test_multiple_prebuild_scripts_run_in_declaration_order(self, tmp_path, monkeypatch):
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
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

    def test_failing_prebuild_aborts_before_backend_execute(self, tmp_path, monkeypatch):
        """Non-zero exit from a prebuild script raises SystemExit before
        backend.execute is reached."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
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

    def test_failing_postbuild_aborts_before_copyexes(self, tmp_path, monkeypatch):
        """Non-zero exit from a postbuild script raises SystemExit after
        backend.execute but before _copyexes."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
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

    def test_subsequent_prebuild_scripts_skipped_after_failure(self, tmp_path, monkeypatch):
        """If script 1 of N fails, script 2 must not run."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
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
            assert lines == ["run"], (
                "second prebuild script must not run after the first fails"
            )

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

    def test_prebuild_runs_before_build_graph(self, tmp_path, monkeypatch):
        """The prebuild script must run BEFORE build_graph() — otherwise
        headerdeps would miss generated headers."""
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
            marker = os.path.join(tmpdir, "pre.marker")
            cake.args.prebuild_scripts = [_write_marker_script(tmpdir, "pre.sh", marker)]

            expected = get_backend_class("ninja")

            def _assert_marker_present(*_a, **_k):
                assert os.path.exists(marker), (
                    "prebuild marker must exist before build_graph() so generated "
                    "headers are visible to headerdeps"
                )
                return MagicMock()

            with (
                patch.object(expected, "build_graph", side_effect=_assert_marker_present),
                patch.object(expected, "generate"),
                patch.object(expected, "execute"),
            ):
                cake.process()

    def test_empty_script_lists_are_no_op(self, tmp_path, monkeypatch):
        """Default empty lists must short-circuit before subprocess.run.

        Patching the runner method itself (not subprocess.run, which the
        rest of ct-cake's startup invokes for git_root resolution) keeps
        this test focused on the empty-list contract.
        """
        with CakeTestContext("ninja") as (cake, tmpdir):
            monkeypatch.chdir(tmpdir)
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
                shell_calls = [
                    c for c in mock_run.call_args_list if c.kwargs.get("shell") is True
                ]
                assert shell_calls == [], (
                    f"empty script lists must not invoke a shell, got: {shell_calls}"
                )
