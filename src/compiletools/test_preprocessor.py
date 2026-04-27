"""Tests for compiletools.preprocessor module."""

import subprocess
import types
from unittest import mock

import pytest

from compiletools.preprocessor import PreProcessor


def _make_args(cpp="cpp", cppflags="", verbose=0):
    return types.SimpleNamespace(CPP=cpp, CPPFLAGS=cppflags, verbose=verbose)


class TestPreProcessorProcess:
    """Tests for PreProcessor.process()."""

    def test_source_file(self):
        """Non-header file is passed directly to the command."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            result = pp.process("/tmp/foo.cpp", "-DFOO")
            cmd = m.call_args[0][0]
            assert cmd[-1] == "/tmp/foo.cpp"
            assert "-DFOO" in cmd
            assert result == "output"

    def test_header_file(self):
        """Header file uses -include with /dev/null."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            pp.process("/tmp/foo.hpp", "")
            cmd = m.call_args[0][0]
            assert "-include" in cmd
            assert "/tmp/foo.hpp" in cmd
            assert "-x" in cmd
            assert "c++" in cmd
            assert cmd[-1] == "/dev/null"

    def test_header_file_h_extension(self):
        """A .h file is also treated as a header."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            pp.process("/tmp/foo.h", "")
            cmd = m.call_args[0][0]
            assert "-include" in cmd

    def test_redirect_stderr_to_stdout(self):
        """redirect_stderr_to_stdout passes stderr=STDOUT."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            pp.process("/tmp/foo.cpp", "", redirect_stderr_to_stdout=True)
            kwargs = m.call_args[1]
            assert kwargs["stderr"] == subprocess.STDOUT

    def test_no_redirect_stderr(self):
        """Without redirect, stderr defaults to inherit (None)."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            pp.process("/tmp/foo.cpp", "")
            kwargs = m.call_args[1]
            assert kwargs.get("stderr") is None

    def test_verbose_3_prints_cmd(self, capsys):
        """verbose >= 3 prints the command."""
        args = _make_args(verbose=3)
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output"):
            pp.process("/tmp/foo.cpp", "")
        captured = capsys.readouterr()
        assert "cpp" in captured.out
        assert "foo.cpp" in captured.out

    def test_verbose_5_prints_output(self, capsys):
        """verbose >= 5 prints the output."""
        args = _make_args(verbose=5)
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="preprocessed stuff"):
            pp.process("/tmp/foo.cpp", "")
        captured = capsys.readouterr()
        assert "preprocessed stuff" in captured.out

    def test_oserror_raised(self, capsys):
        """OSError is printed to stderr and re-raised."""
        args = _make_args()
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", side_effect=OSError("no such file")):
            with pytest.raises(OSError):
                pp.process("/tmp/foo.cpp", "")
        captured = capsys.readouterr()
        assert "Failed to preprocess" in captured.err

    def test_called_process_error_raised(self, capsys):
        """CalledProcessError is printed to stderr and re-raised."""
        args = _make_args()
        pp = PreProcessor(args)
        err = subprocess.CalledProcessError(1, "cpp", output="bad")
        with mock.patch("subprocess.check_output", side_effect=err):
            with pytest.raises(subprocess.CalledProcessError):
                pp.process("/tmp/foo.cpp", "")
        captured = capsys.readouterr()
        assert "Preprocessing failed" in captured.err

    def test_cppflags_split_into_cmd(self):
        """CPPFLAGS from args are split and included in the command."""
        args = _make_args(cppflags="-I/usr/include -DBAR")
        pp = PreProcessor(args)
        with mock.patch("subprocess.check_output", return_value="output") as m:
            pp.process("/tmp/foo.cpp", "")
            cmd = m.call_args[0][0]
            assert "-I/usr/include" in cmd
            assert "-DBAR" in cmd
