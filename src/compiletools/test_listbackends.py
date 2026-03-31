from unittest.mock import Mock, patch

import compiletools.listbackends

ALL_BACKENDS = {"bazel", "cmake", "make", "ninja", "shake", "tup"}


def _args(style="pretty", show_all=False):
    args = Mock()
    args.style = style
    args.show_all = show_all
    return args


def test_list_backends_default_shows_available_only():
    with patch("compiletools.listbackends.is_backend_available", side_effect=lambda n: n == "make"):
        output = compiletools.listbackends.list_backends()
    assert "make" in output
    assert "bazel" not in output


def test_list_backends_pretty_all():
    output = compiletools.listbackends.list_backends(args=_args(show_all=True))
    for name in ALL_BACKENDS:
        assert name in output


def test_list_backends_flat_all():
    output = compiletools.listbackends.list_backends(args=_args(style="flat", show_all=True))
    tokens = set(output.split())
    assert tokens == ALL_BACKENDS


def test_list_backends_filelist_all():
    output = compiletools.listbackends.list_backends(args=_args(style="filelist", show_all=True))
    lines = set(output.strip().splitlines())
    assert lines == ALL_BACKENDS


def test_list_backends_flat_available_only():
    with patch("compiletools.listbackends.is_backend_available", side_effect=lambda n: n == "make"):
        output = compiletools.listbackends.list_backends(args=_args(style="flat"))
    assert output.strip() == "make"


def test_main_returns_zero(capsys):
    rc = compiletools.listbackends.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out  # non-empty output
