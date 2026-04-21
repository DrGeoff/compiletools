"""Tests for config module."""

import subprocess
from unittest import mock

import compiletools.config as config
import compiletools.git_utils


def test_main_help():
    """ct-config --help works."""
    result = subprocess.run(
        ["ct-config", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=compiletools.git_utils.find_git_root(),
    )
    assert result.returncode == 0
    assert "Configuration examination tool" in result.stdout or "usage:" in result.stdout


def test_main_returns_zero(capsys):
    """Test that main() returns 0 on success."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments"),
        mock.patch("compiletools.apptools.parseargs"),
    ):
        mock_create.return_value = mock.MagicMock()
        result = config.main(argv=[])
    assert result == 0


def test_main_calls_create_parser_with_correct_args():
    """Test that main passes expected arguments to create_parser."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments"),
        mock.patch("compiletools.apptools.parseargs"),
    ):
        mock_create.return_value = mock.MagicMock()
        config.main(argv=["--variant=gcc.debug"])
        mock_create.assert_called_once_with(
            "Configuration examination tool. Write the config to file with -w.",
            argv=["--variant=gcc.debug"],
            include_config=True,
            include_write_config=True,
        )


def test_main_adds_cake_arguments():
    """Test that Cake.add_arguments is called with the parser."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments") as mock_add,
        mock.patch("compiletools.apptools.parseargs"),
    ):
        cap = mock.MagicMock()
        mock_create.return_value = cap
        config.main(argv=[])
        mock_add.assert_called_once_with(cap)


def test_main_calls_parseargs():
    """Test that parseargs is called with parser and argv."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments"),
        mock.patch("compiletools.apptools.parseargs") as mock_parse,
    ):
        cap = mock.MagicMock()
        mock_create.return_value = cap
        argv = ["--verbose"]
        config.main(argv=argv)
        mock_parse.assert_called_once_with(cap, argv, context=mock.ANY)


def test_main_prints_newline(capsys):
    """Test that main prints a trailing newline."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments"),
        mock.patch("compiletools.apptools.parseargs"),
    ):
        mock_create.return_value = mock.MagicMock()
        config.main(argv=[])
    captured = capsys.readouterr()
    assert captured.out == "\n"


def test_main_none_argv_appends_verbose():
    """Test that when argv is None, -vvv is appended to sys.argv."""
    with (
        mock.patch("compiletools.apptools.create_parser") as mock_create,
        mock.patch("compiletools.cake.Cake.add_arguments"),
        mock.patch("compiletools.apptools.parseargs"),
        mock.patch("compiletools.config.sys") as mock_sys,
    ):
        mock_sys.argv = ["ct-config"]
        mock_create.return_value = mock.MagicMock()
        config.main(argv=None)
        assert "-vvv" in mock_sys.argv
