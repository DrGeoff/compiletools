from types import SimpleNamespace
from unittest.mock import patch

import pytest

import compiletools.listbackends

EXPECTED_BACKENDS = {"bazel", "cmake", "make", "ninja", "shake", "slurm"}


def _args(style="pretty", show_all=False):
    return SimpleNamespace(style=style, show_all=show_all)


def _backend_names_from_output(style, output):
    if style == "pretty":
        return {name for name in EXPECTED_BACKENDS if name in output}
    if style == "flat":
        return set(output.split())
    return set(output.strip().splitlines())


def test_list_backends_default_shows_available_only():
    with patch("compiletools.listbackends.is_backend_available", side_effect=lambda n: n == "make"):
        output = compiletools.listbackends.list_backends()
    assert "make" in output
    assert "bazel" not in output


@pytest.mark.parametrize(
    "style",
    [
        pytest.param("pretty", id="pretty"),
        pytest.param("flat", id="flat"),
        pytest.param("filelist", id="filelist"),
    ],
)
def test_list_backends_all(style):
    output = compiletools.listbackends.list_backends(args=_args(style=style, show_all=True))
    assert _backend_names_from_output(style, output) >= EXPECTED_BACKENDS


def test_list_backends_flat_available_only():
    with patch("compiletools.listbackends.is_backend_available", side_effect=lambda n: n == "make"):
        output = compiletools.listbackends.list_backends(args=_args(style="flat"))
    assert output.strip() == "make"


def test_main_returns_zero(capsys):
    rc = compiletools.listbackends.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out  # non-empty output
