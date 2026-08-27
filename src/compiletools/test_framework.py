"""Detect the unit-test framework a test source uses, and emit the
right argv tokens to make that framework write a JUnit-shaped XML report.

Single source of truth for the gtest / doctest / Catch2 detection table.
Consumed by ``build_backend._test_command_for`` (when ``--test-xml-dir`` is
set) to build each test rule's argv.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TestFramework:
    """A unit-test framework recognisable by its include text or packages.

    ``header_substrings`` match against header *texts*: raw ``#include``
    operand tokens and resolved transitive header paths (both spellings
    of the same source-text property). ``pkgconfig_substrings`` match
    against ``//#PKG-CONFIG`` package names, catching site-local wrapper
    packages (e.g. a ``cpp_doctest`` package that vendors doctest).

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
    pkgconfig_substrings: tuple[str, ...] = ()

    def xml_argv(self, path: str) -> list[str]:
        return [token.format(path=path) for token in self.xml_argv_template]

    def matches(self, header_texts: list[str], pkgconfig_packages: list[str]) -> bool:
        return any(sub in text for sub in self.header_substrings for text in header_texts) or any(
            sub in pkg for sub in self.pkgconfig_substrings for pkg in pkgconfig_packages
        )


KNOWN_FRAMEWORKS: tuple[TestFramework, ...] = (
    TestFramework(
        id="gtest",
        header_substrings=("gtest/gtest.h",),
        xml_argv_template=("--gtest_output=xml:{path}",),
        pkgconfig_substrings=("gtest",),
    ),
    TestFramework(
        id="doctest",
        header_substrings=("doctest/doctest.h", "doctest.h"),
        xml_argv_template=("--reporters=junit", "--out={path}"),
        pkgconfig_substrings=("doctest",),
    ),
    TestFramework(
        id="catch2",
        header_substrings=("catch2/catch_all.hpp", "catch2/catch.hpp", "catch.hpp"),
        xml_argv_template=("--reporter", "junit", "--out", "{path}"),
        pkgconfig_substrings=("catch2",),
    ),
)


def detect_framework(
    header_texts: Iterable[str],
    test_source: str,
    pkgconfig_packages: Iterable[str] = (),
) -> TestFramework | None:
    """Return the unique ``TestFramework`` this test's source text names.

    A framework matches when any of its ``header_substrings`` appears as
    a substring of any string in ``header_texts`` (raw ``#include``
    operand tokens and/or resolved header paths -- which framework a
    test uses is a property of the source text, so callers should feed
    the raw tokens rather than only the resolved subset), or when any of
    its ``pkgconfig_substrings`` appears in any ``pkgconfig_packages``
    name.

    Returns ``None`` when no framework matches; raises ``ValueError``
    on multi-match (the signals name two frameworks at once -- silently
    picking one would lose data, so callers must disambiguate, typically
    by fixing include paths or magic flags).
    """
    texts = list(header_texts)
    packages = list(pkgconfig_packages)
    matched = [fw for fw in KNOWN_FRAMEWORKS if fw.matches(texts, packages)]
    if len(matched) > 1:
        ids = ", ".join(fw.id for fw in matched)
        raise ValueError(f"{test_source}: multiple test frameworks detected ({ids}); disambiguate include paths")
    return matched[0] if matched else None
