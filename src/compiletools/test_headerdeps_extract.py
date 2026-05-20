"""Tests for headerdeps flag-path extraction helpers."""

import pytest

from compiletools.headerdeps import HeaderDepsBase

# Shorthand aliases — these are static methods so call directly.
_extract_isystem = HeaderDepsBase._extract_isystem_paths_from_flags
_extract_include = HeaderDepsBase._extract_include_paths_from_flags


@pytest.mark.parametrize(
    ("extractor", "flags", "expected"),
    [
        pytest.param(_extract_isystem, ["-isystem", "/opt/inc"], ["/opt/inc"], id="list-isystem-regression"),
        pytest.param(_extract_include, ["-I", "/opt/inc"], ["/opt/inc"], id="list-include"),
        pytest.param(_extract_isystem, "-isystem /opt/inc", ["/opt/inc"], id="separate-isystem"),
        pytest.param(_extract_include, "-I /opt/inc", ["/opt/inc"], id="separate-include"),
        pytest.param(_extract_isystem, "-isystem/opt/inc", ["/opt/inc"], id="attached-isystem"),
        pytest.param(_extract_include, "-I/opt/inc", ["/opt/inc"], id="attached-include"),
        pytest.param(_extract_include, "-I /a -I /b", ["/a", "/b"], id="multiple-include"),
        pytest.param(_extract_isystem, "-isystem /a -isystem /b", ["/a", "/b"], id="multiple-isystem"),
        pytest.param(_extract_include, "-I/a -I /b -I/c", ["/a", "/b", "/c"], id="mixed-include"),
        pytest.param(_extract_include, '-I "/path with spaces"', ["/path with spaces"], id="quoted-include"),
        pytest.param(
            _extract_isystem,
            '-isystem "/path with spaces"',
            ["/path with spaces"],
            id="quoted-isystem",
        ),
        pytest.param(_extract_isystem, "", [], id="empty-isystem"),
        pytest.param(_extract_isystem, None, [], id="none-isystem"),
        pytest.param(_extract_include, "", [], id="empty-include"),
        pytest.param(_extract_include, None, [], id="none-include"),
        pytest.param(_extract_include, "-I", [], id="prefix-only-include"),
        pytest.param(_extract_isystem, "-isystem", [], id="prefix-only-isystem"),
    ],
)
def test_extract_paths_from_flags(extractor, flags, expected):
    assert extractor(flags) == expected
