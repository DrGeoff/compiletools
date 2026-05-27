"""Tests for config module."""

import subprocess
from types import SimpleNamespace
from unittest import mock

import pytest

import compiletools.config as config
import compiletools.git_utils


@pytest.fixture
def config_mocks():
    """Patch the 3 apptools entry points exercised by ``config.main``.

    Common to 5 unit tests in this module. Yields a SimpleNamespace so
    tests bind only the mock they assert on; ``create.return_value`` is
    pre-set to a fresh MagicMock so tests that need the ``cap`` value
    (e.g. asserting ``parseargs(cap, ...)``) can reach it without
    duplicating the setter, while tests that need a specific cap can
    overwrite it.
    """
    with (
        mock.patch("compiletools.apptools.create_parser") as create,
        mock.patch("compiletools.cake.Cake.add_arguments") as add,
        mock.patch("compiletools.apptools.parseargs") as parse,
    ):
        create.return_value = mock.MagicMock()
        yield SimpleNamespace(create=create, add=add, parse=parse)


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


def test_main_returns_zero(config_mocks, capsys):
    """Test that main() returns 0 on success."""
    result = config.main(argv=[])
    assert result == 0


def test_main_calls_create_parser_with_correct_args(config_mocks):
    """Test that main passes expected arguments to create_parser."""
    config.main(argv=["--variant=gcc.debug"])
    config_mocks.create.assert_called_once_with(
        "Configuration examination tool. Write the config to file with -w.",
        argv=["--variant=gcc.debug"],
        include_config=True,
        include_write_config=True,
    )


def test_main_adds_cake_arguments(config_mocks):
    """Test that Cake.add_arguments is called with the parser."""
    cap = mock.MagicMock()
    config_mocks.create.return_value = cap
    config.main(argv=[])
    config_mocks.add.assert_called_once_with(cap)


def test_main_calls_parseargs(config_mocks):
    """Test that parseargs is called with parser and argv."""
    cap = mock.MagicMock()
    config_mocks.create.return_value = cap
    argv = ["--verbose"]
    config.main(argv=argv)
    config_mocks.parse.assert_called_once_with(cap, argv, context=mock.ANY)


def test_main_prints_newline(config_mocks, capsys):
    """Test that main prints a trailing newline."""
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
