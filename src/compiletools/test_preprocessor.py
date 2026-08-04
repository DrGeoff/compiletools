"""Tests for compiletools.preprocessor module."""

import subprocess
import types
from unittest import mock

import pytest

from compiletools.flags import Flags
from compiletools.preprocessor import PreProcessor
from compiletools.utils import split_command_cached


def _make_args(cpp="cpp", cppflags="", verbose=0):
    return types.SimpleNamespace(
        CPP=cpp,
        CPPFLAGS=cppflags,
        flags=Flags(cpp=tuple(split_command_cached(cppflags))),
        verbose=verbose,
    )


@pytest.fixture
def mock_check_output():
    """Patch subprocess.check_output with a default return value."""
    with mock.patch("subprocess.check_output", return_value="output") as m:
        yield m


@pytest.fixture
def make_pp():
    """Factory yielding a PreProcessor with optional _make_args overrides."""

    def _factory(**kwargs):
        return PreProcessor(_make_args(**kwargs))

    return _factory


class TestPreProcessorProcess:
    """Tests for PreProcessor.process()."""

    def test_source_file(self, make_pp, mock_check_output):
        """Non-header file is passed directly to the command."""
        result = make_pp().process("/tmp/foo.cpp", "-DFOO")
        cmd = mock_check_output.call_args[0][0]
        assert cmd[-1] == "/tmp/foo.cpp"
        assert "-DFOO" in cmd
        assert result == "output"

    @pytest.mark.parametrize("path", ["/tmp/foo.hpp", "/tmp/foo.h"], ids=["hpp", "h"])
    def test_header_file(self, make_pp, mock_check_output, path):
        """Header files use -include with /dev/null and -x c++."""
        make_pp().process(path, "")
        cmd = mock_check_output.call_args[0][0]
        assert "-include" in cmd
        assert path in cmd
        assert "-x" in cmd
        assert "c++" in cmd
        assert cmd[-1] == "/dev/null"

    def test_redirect_stderr_to_stdout(self, make_pp, mock_check_output):
        """redirect_stderr_to_stdout passes stderr=STDOUT."""
        make_pp().process("/tmp/foo.cpp", "", redirect_stderr_to_stdout=True)
        assert mock_check_output.call_args[1]["stderr"] == subprocess.STDOUT

    def test_no_redirect_stderr(self, make_pp, mock_check_output):
        """Without redirect, stderr defaults to inherit (None)."""
        make_pp().process("/tmp/foo.cpp", "")
        assert mock_check_output.call_args[1].get("stderr") is None

    @pytest.mark.usefixtures("mock_check_output")
    def test_verbose_3_prints_cmd(self, make_pp, capsys):
        """verbose >= 3 prints the command."""
        make_pp(verbose=3).process("/tmp/foo.cpp", "")
        out = capsys.readouterr().out
        assert "cpp" in out
        assert "foo.cpp" in out

    def test_verbose_5_prints_output(self, make_pp, mock_check_output, capsys):
        """verbose >= 5 prints the output."""
        mock_check_output.return_value = "preprocessed stuff"
        make_pp(verbose=5).process("/tmp/foo.cpp", "")
        assert "preprocessed stuff" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "exc,raises_type,expected_stderr",
        [
            (OSError("no such file"), OSError, "Failed to preprocess"),
            (
                subprocess.CalledProcessError(1, "cpp", output="bad"),
                subprocess.CalledProcessError,
                "Preprocessing failed",
            ),
        ],
        ids=["oserror", "called_process_error"],
    )
    def test_subprocess_errors_printed_and_reraised(self, make_pp, exc, raises_type, expected_stderr, capsys):
        """Subprocess errors are printed to stderr and re-raised."""
        with mock.patch("subprocess.check_output", side_effect=exc):
            with pytest.raises(raises_type):
                make_pp().process("/tmp/foo.cpp", "")
        assert expected_stderr in capsys.readouterr().err

    def test_cppflags_split_into_cmd(self, make_pp, mock_check_output):
        """CPPFLAGS from args are split and included in the command."""
        make_pp(cppflags="-I/usr/include -DBAR").process("/tmp/foo.cpp", "")
        cmd = mock_check_output.call_args[0][0]
        assert "-I/usr/include" in cmd
        assert "-DBAR" in cmd

    def test_flags_cpp_tokens_used_verbatim(self, mock_check_output):
        """The argv takes args.flags.cpp tokens as-is: a token with an
        embedded space (a shlex.join'd raw string would carry it quoted)
        arrives as ONE argv element, and CPP/extraargs are shlex-split so
        quoted segments in either survive as single elements."""
        args = types.SimpleNamespace(
            CPP="ccache 'my cpp'",
            CPPFLAGS="-DMSG='hello world'",
            flags=Flags(cpp=("-DMSG=hello world",)),
            verbose=0,
        )
        PreProcessor(args).process("/tmp/foo.cpp", "-include 'weird dir/pre.h'")
        cmd = mock_check_output.call_args[0][0]
        assert cmd[:2] == ["ccache", "my cpp"]
        assert "-DMSG=hello world" in cmd
        assert "weird dir/pre.h" in cmd
