"""Tests for headerdeps flag-path extraction helpers."""
from compiletools.headerdeps import HeaderDepsBase


class _Stub(HeaderDepsBase):
    """Minimal subclass to access protected helpers."""
    def __init__(self):
        pass

    def _process_impl(self, *a, **kw):
        raise NotImplementedError


def test_extract_isystem_handles_list_input():
    """List input (from configargparse multi-value) should work like string."""
    stub = _Stub()
    result = stub._extract_isystem_paths_from_flags(["-isystem", "/opt/inc"])
    assert result == ["/opt/inc"]


def test_extract_isystem_handles_string_input():
    stub = _Stub()
    result = stub._extract_isystem_paths_from_flags("-isystem /opt/inc")
    assert result == ["/opt/inc"]


def test_extract_isystem_attached_form():
    stub = _Stub()
    result = stub._extract_isystem_paths_from_flags("-isystem/opt/inc")
    assert result == ["/opt/inc"]


def test_extract_include_handles_list_input():
    stub = _Stub()
    result = stub._extract_include_paths_from_flags(["-I", "/opt/inc"])
    assert result == ["/opt/inc"]


def test_extract_include_handles_string_input():
    stub = _Stub()
    result = stub._extract_include_paths_from_flags("-I /opt/inc")
    assert result == ["/opt/inc"]


def test_extract_include_attached_form():
    stub = _Stub()
    result = stub._extract_include_paths_from_flags("-I/opt/inc")
    assert result == ["/opt/inc"]


def test_extract_isystem_empty():
    stub = _Stub()
    assert stub._extract_isystem_paths_from_flags("") == []
    assert stub._extract_isystem_paths_from_flags(None) == []


def test_extract_include_empty():
    stub = _Stub()
    assert stub._extract_include_paths_from_flags("") == []
    assert stub._extract_include_paths_from_flags(None) == []
