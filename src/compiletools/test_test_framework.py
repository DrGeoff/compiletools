"""Unit tests for compiletools.test_framework: per-framework header
detection and XML-emit argv formatting."""

from __future__ import annotations

import pytest

from compiletools.test_framework import (
    KNOWN_FRAMEWORKS,
    TestFramework,
    detect_framework,
)

# --- detect_framework: positive matches -----------------------------------


@pytest.mark.parametrize(
    "header_path, expected_id",
    [
        # gtest, vendor-namespaced (the only spelling the spec lists)
        ("/usr/include/gtest/gtest.h", "gtest"),
        ("third_party/googletest/include/gtest/gtest.h", "gtest"),
        # doctest, both bare and vendor-namespaced
        ("/usr/include/doctest/doctest.h", "doctest"),
        ("vendor/doctest.h", "doctest"),
        # Catch2, all three documented forms
        ("/usr/include/catch2/catch_all.hpp", "catch2"),
        ("third_party/catch2/catch.hpp", "catch2"),
        ("vendor/catch.hpp", "catch2"),
    ],
)
def test_detect_framework_recognises_known_headers(header_path, expected_id):
    framework = detect_framework([header_path], test_source="t.cpp")
    assert framework is not None
    assert framework.id == expected_id


def test_detect_framework_finds_match_among_unrelated_headers():
    """Detection must scan the *whole* transitive set, not just the first
    header — real test sources pull in dozens of stdlib headers before
    the test framework's own header appears."""
    headers = [
        "/usr/include/string.h",
        "/usr/include/stdio.h",
        "/some/other/random.h",
        "/usr/include/gtest/gtest.h",
        "/usr/include/iostream",
    ]
    framework = detect_framework(headers, test_source="t.cpp")
    assert framework is not None and framework.id == "gtest"


# --- detect_framework: negative cases -------------------------------------


def test_detect_framework_returns_none_for_unknown():
    headers = ["/usr/include/string.h", "vendor/my_test_helper.h"]
    assert detect_framework(headers, test_source="t.cpp") is None


def test_detect_framework_returns_none_on_empty_header_set():
    assert detect_framework([], test_source="t.cpp") is None


# --- detect_framework: multi-match must hard-error ------------------------


def test_detect_framework_multi_match_raises_naming_both_ids():
    headers = ["/inc/gtest/gtest.h", "/inc/doctest/doctest.h"]
    with pytest.raises(ValueError, match="multiple test frameworks") as excinfo:
        detect_framework(headers, test_source="my_test.cpp")
    msg = str(excinfo.value)
    # The error message must name the source so users can locate the
    # offending test, and name *every* matched framework so they know
    # what to disambiguate.
    assert "my_test.cpp" in msg
    assert "gtest" in msg
    assert "doctest" in msg


def test_detect_framework_three_way_multi_match_lists_all():
    headers = [
        "/inc/gtest/gtest.h",
        "/inc/doctest/doctest.h",
        "/inc/catch2/catch.hpp",
    ]
    with pytest.raises(ValueError) as excinfo:
        detect_framework(headers, test_source="t.cpp")
    msg = str(excinfo.value)
    for fw_id in ("gtest", "doctest", "catch2"):
        assert fw_id in msg


# --- xml_argv: per-framework formatting -----------------------------------


def _by_id(fw_id: str) -> TestFramework:
    return next(fw for fw in KNOWN_FRAMEWORKS if fw.id == fw_id)


def test_xml_argv_gtest_uses_colon_separator():
    """gtest's spelling is one token: ``--gtest_output=xml:PATH``. Splitting
    it into two argv elements would make gtest reject it."""
    argv = _by_id("gtest").xml_argv("/tmp/out.xml")
    assert argv == ["--gtest_output=xml:/tmp/out.xml"]


def test_xml_argv_doctest_two_tokens_with_equals():
    argv = _by_id("doctest").xml_argv("/tmp/out.xml")
    assert argv == ["--reporters=junit", "--out=/tmp/out.xml"]


def test_xml_argv_catch2_four_tokens_space_separated():
    """Catch2 uses space-separated argv (``--reporter junit --out PATH``),
    not equals-form. That's *four* tokens, not two."""
    argv = _by_id("catch2").xml_argv("/tmp/out.xml")
    assert argv == ["--reporter", "junit", "--out", "/tmp/out.xml"]


def test_xml_argv_path_with_spaces_is_passed_through_intact():
    """argv-style invocation hands paths to subprocess.run as a list, so
    spaces in the path don't need shell-escaping. Verify {path} is
    substituted verbatim."""
    weird = "/tmp/dir with spaces/out file.xml"
    for fw in KNOWN_FRAMEWORKS:
        argv = fw.xml_argv(weird)
        # The path appears in exactly one of the formatted tokens.
        assert any(weird in token for token in argv), (fw.id, argv)


# --- TestFramework dataclass invariants -----------------------------------


def test_test_framework_is_frozen_and_hashable():
    """Frozen dataclass: must be hashable so a backend can stash it in
    a dict without surprises, and immutable so accidental mutation in
    one worker thread can't corrupt detection state in another."""
    fw = _by_id("gtest")
    with pytest.raises((AttributeError, Exception)):
        fw.id = "mutated"  # type: ignore[misc]
    # Hashable: round-trips through a set without raising.
    assert {fw, fw} == {fw}
