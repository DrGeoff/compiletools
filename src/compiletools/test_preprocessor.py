"""Tests for compiletools.preprocessor module."""

import subprocess
import types
from unittest import mock

import pytest

import compiletools.testhelper as uth
from compiletools.preprocessor import PreProcessor


def _make_args(cpp="cpp", cppflags="", verbose=0):
    args = types.SimpleNamespace(
        CPP=cpp,
        CPPFLAGS=cppflags,
        verbose=verbose,
    )
    uth.finalize_flag_state(args)
    return args


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
        result = make_pp().process("/tmp/foo.cpp", ["-DFOO"])
        cmd = mock_check_output.call_args[0][0]
        assert cmd[-1] == "/tmp/foo.cpp"
        assert "-DFOO" in cmd
        assert result == "output"

    @pytest.mark.parametrize("path", ["/tmp/foo.hpp", "/tmp/foo.h"], ids=["hpp", "h"])
    def test_header_file(self, make_pp, mock_check_output, path):
        """Header files use -include with /dev/null and -x c++."""
        make_pp().process(path, [])
        cmd = mock_check_output.call_args[0][0]
        assert "-include" in cmd
        assert path in cmd
        assert "-x" in cmd
        assert "c++" in cmd
        assert cmd[-1] == "/dev/null"

    def test_redirect_stderr_to_stdout(self, make_pp, mock_check_output):
        """redirect_stderr_to_stdout passes stderr=STDOUT."""
        make_pp().process("/tmp/foo.cpp", [], redirect_stderr_to_stdout=True)
        assert mock_check_output.call_args[1]["stderr"] == subprocess.STDOUT

    def test_no_redirect_stderr(self, make_pp, mock_check_output):
        """Without redirect, stderr defaults to inherit (None)."""
        make_pp().process("/tmp/foo.cpp", [])
        assert mock_check_output.call_args[1].get("stderr") is None

    @pytest.mark.usefixtures("mock_check_output")
    def test_verbose_3_prints_cmd(self, make_pp, capsys):
        """verbose >= 3 prints the command."""
        make_pp(verbose=3).process("/tmp/foo.cpp", [])
        out = capsys.readouterr().out
        assert "cpp" in out
        assert "foo.cpp" in out

    def test_verbose_5_prints_output(self, make_pp, mock_check_output, capsys):
        """verbose >= 5 prints the output."""
        mock_check_output.return_value = "preprocessed stuff"
        make_pp(verbose=5).process("/tmp/foo.cpp", [])
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
                make_pp().process("/tmp/foo.cpp", [])
        assert expected_stderr in capsys.readouterr().err

    def test_cppflags_split_into_cmd(self, make_pp, mock_check_output):
        """CPPFLAGS from args are split and included in the command."""
        make_pp(cppflags="-I/usr/include -DBAR").process("/tmp/foo.cpp", [])
        cmd = mock_check_output.call_args[0][0]
        assert "-I/usr/include" in cmd
        assert "-DBAR" in cmd

    def test_flags_cpp_tokens_used_verbatim(self, mock_check_output):
        """The argv takes the build state's cpp tokens and extraargs as-is:
        a token with an embedded space (a shlex.join'd raw string would
        carry it quoted) arrives as ONE argv element, and CPP is
        shlex-split so quoted segments in it survive as a single element."""
        args = _make_args(cpp="ccache 'my cpp'", cppflags="-DMSG='hello world'")
        PreProcessor(args).process("/tmp/foo.cpp", ["-include", "weird dir/pre.h"])
        cmd = mock_check_output.call_args[0][0]
        assert cmd[:2] == ["ccache", "my cpp"]
        assert "-DMSG=hello world" in cmd
        assert "weird dir/pre.h" in cmd

    def test_process_takes_pre_split_token_list(self, make_pp, mock_check_output):
        """extraargs is a token sequence end-to-end; a '-I/has space/inc'
        element must reach the argv as ONE token."""
        make_pp().process("/tmp/foo.cpp", ["-MM", "-I/has space/inc"])
        cmd = mock_check_output.call_args[0][0]
        assert "-I/has space/inc" in cmd

    def test_unbalanced_quote_in_cpp_raises_systemexit_not_a_traceback(self, make_pp, capsys):
        """coverage-gaps Task 9 review finding Q2: this is the single
        choke point every --headerdeps=cpp / --magic=cpp caller
        (headerdeps.py, magicflags.py x2) reaches, so converting a
        malformed --CPP's FlagTokenizeError to a clean SystemExit(1) here
        covers all three call sites at once -- and, since SystemExit is
        not an Exception subclass, survives Hunter's broad
        ``except Exception`` in its source-expansion walk (which would
        otherwise downgrade this to a per-source warning and let the
        build continue with missing preprocessor output) and reaches
        standalone callers with no exception handling of their own
        (ct-headertree, ct-magicflags, ct-filelist mains) as a clean,
        traceback-free message instead of a raw exception."""
        pp = make_pp(cpp='ccache "cpp')
        with pytest.raises(SystemExit) as excinfo:
            pp.process("/tmp/foo.cpp", [])
        assert excinfo.value.code == 1
        error_output = capsys.readouterr().err
        assert "CPP" in error_output
        assert "Traceback" not in error_output
