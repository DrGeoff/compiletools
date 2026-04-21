"""Tests for headerdeps flag-path extraction helpers."""
from compiletools.headerdeps import HeaderDepsBase


# Shorthand aliases — these are static methods so call directly.
_extract_isystem = HeaderDepsBase._extract_isystem_paths_from_flags
_extract_include = HeaderDepsBase._extract_include_paths_from_flags


# -- list input (regression: -isystem variant used to crash) --

def test_extract_isystem_handles_list_input():
    assert _extract_isystem(["-isystem", "/opt/inc"]) == ["/opt/inc"]


def test_extract_include_handles_list_input():
    assert _extract_include(["-I", "/opt/inc"]) == ["/opt/inc"]


# -- separate-token form --

def test_extract_isystem_handles_string_input():
    assert _extract_isystem("-isystem /opt/inc") == ["/opt/inc"]


def test_extract_include_handles_string_input():
    assert _extract_include("-I /opt/inc") == ["/opt/inc"]


# -- attached form --

def test_extract_isystem_attached_form():
    assert _extract_isystem("-isystem/opt/inc") == ["/opt/inc"]


def test_extract_include_attached_form():
    assert _extract_include("-I/opt/inc") == ["/opt/inc"]


# -- multiple paths in one flag string --

def test_extract_include_multiple_paths():
    assert _extract_include("-I /a -I /b") == ["/a", "/b"]


def test_extract_isystem_multiple_paths():
    assert _extract_isystem("-isystem /a -isystem /b") == ["/a", "/b"]


def test_extract_include_mixed_forms():
    assert _extract_include("-I/a -I /b -I/c") == ["/a", "/b", "/c"]


# -- quoted paths with spaces (shell parsing) --

def test_extract_include_quoted_path_with_spaces():
    assert _extract_include('-I "/path with spaces"') == ["/path with spaces"]


def test_extract_isystem_quoted_path_with_spaces():
    assert _extract_isystem('-isystem "/path with spaces"') == ["/path with spaces"]


# -- empty / None --

def test_extract_isystem_empty():
    assert _extract_isystem("") == []
    assert _extract_isystem(None) == []


def test_extract_include_empty():
    assert _extract_include("") == []
    assert _extract_include(None) == []


# -- prefix-only token (no following path) --

def test_extract_include_prefix_only_no_path():
    """A trailing '-I' with no following path should produce no paths."""
    assert _extract_include("-I") == []


def test_extract_isystem_prefix_only_no_path():
    assert _extract_isystem("-isystem") == []
