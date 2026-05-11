"""Detect the unit-test framework a test source uses, and emit the
right argv tokens to make that framework write a JUnit-shaped XML report.

Single source of truth for the gtest / doctest / Catch2 detection table.
Consumed by ``build_backend._run_tests`` when ``--test-xml-dir`` is set.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TestFramework:
    """A unit-test framework recognisable by its transitive headers.

    ``xml_argv_template`` tokens may contain the literal substring
    ``{path}``; ``xml_argv()`` formats it with the per-test XML output
    path. Stored as a tuple so instances stay hashable / immutable.
    """

    # Opt out of pytest's "collect anything named Test*" rule -- this
    # is a domain dataclass, not a test class. Without this, pytest
    # warns at collection time when test_test_framework.py imports it.
    __test__ = False

    id: str
    header_substrings: tuple[str, ...]
    xml_argv_template: tuple[str, ...]

    def xml_argv(self, path: str) -> list[str]:
        return [token.format(path=path) for token in self.xml_argv_template]


KNOWN_FRAMEWORKS: tuple[TestFramework, ...] = (
    TestFramework(
        id="gtest",
        header_substrings=("gtest/gtest.h",),
        xml_argv_template=("--gtest_output=xml:{path}",),
    ),
    TestFramework(
        id="doctest",
        header_substrings=("doctest/doctest.h", "doctest.h"),
        xml_argv_template=("--reporters=junit", "--out={path}"),
    ),
    TestFramework(
        id="catch2",
        header_substrings=("catch2/catch_all.hpp", "catch2/catch.hpp", "catch.hpp"),
        xml_argv_template=("--reporter", "junit", "--out", "{path}"),
    ),
)


def detect_framework(
    transitive_headers: Iterable[str],
    test_source: str,
) -> TestFramework | None:
    """Return the unique ``TestFramework`` matching this test's headers.

    A framework matches when *any* of its ``header_substrings`` appears
    as a substring of *any* path in ``transitive_headers``.

    Returns ``None`` when no framework matches; raises ``ValueError``
    on multi-match (the test transitively pulls in two frameworks at
    once -- silently picking one would lose data, so callers must
    disambiguate, typically by fixing include paths).
    """
    headers = list(transitive_headers)
    matched = [fw for fw in KNOWN_FRAMEWORKS if any(any(sub in h for sub in fw.header_substrings) for h in headers)]
    if len(matched) > 1:
        ids = ", ".join(fw.id for fw in matched)
        raise ValueError(f"{test_source}: multiple test frameworks detected ({ids}); disambiguate include paths")
    return matched[0] if matched else None
