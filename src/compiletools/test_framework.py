"""Detect the unit-test framework a test source uses, and emit the
right argv tokens to make that framework write a JUnit-shaped XML report.

Single source of truth for the gtest / doctest / Catch2 detection table.
Consumed by ``build_backend._test_command_for`` (when ``--test-xml-dir`` is
set) to build each test rule's argv.
"""

from __future__ import annotations

import sys
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

    def matches_headers(self, header_texts: list[str]) -> bool:
        return any(sub in text for sub in self.header_substrings for text in header_texts)

    def matches_pkgconfig(self, pkgconfig_packages: list[str]) -> bool:
        return any(sub in pkg for sub in self.pkgconfig_substrings for pkg in pkgconfig_packages)


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


def _warn_ambiguous(test_source: str, matched: list[TestFramework]) -> None:
    ids = ", ".join(fw.id for fw in matched)
    print(
        f"{test_source}: multiple test frameworks detected ({ids}); "
        "disambiguate include paths or //#PKG-CONFIG; skipping XML output for this test",
        file=sys.stderr,
    )


def detect_framework(
    header_texts: Iterable[str],
    test_source: str,
    pkgconfig_packages: Iterable[str] = (),
) -> TestFramework | None:
    """Return the unique ``TestFramework`` this test's source text names.

    Two evidence tiers, consulted in order:

    1. ``header_substrings`` against ``header_texts`` (raw ``#include``
       operand tokens and/or resolved header paths -- which framework a
       test uses is a property of the source text, so callers should
       feed the raw tokens rather than only the resolved subset).
    2. ``pkgconfig_substrings`` against ``pkgconfig_packages``, consulted
       ONLY when no header matched. This is a fallback tier, not a peer
       signal: a test linking a library annotated with a
       ``//#PKG-CONFIG`` package for one framework while its headers
       name a different one is extremely common (e.g. a doctest test
       pulling in a ``gtest`` pkg-config package transitively) and must
       not be treated as ambiguous. The fallback exists to catch a
       site-local wrapper package (e.g. ``cpp_doctest``) that never
       textually names the framework it vendors.

    Returns ``None`` when no framework matches at either tier. Also
    returns ``None`` -- after printing a warning naming the offending
    source and every matched framework -- when more than one framework
    matches WITHIN a single tier (e.g. two conditional-blind raw header
    matches, such as a ``#if``/``#else`` selecting between gtest and
    catch2 headers behind a shared macro). A same-tier ambiguity cannot
    be resolved by picking a fixed order: the frameworks' argv are
    mutually incompatible, so handing one binary another's XML flag
    converts a graph-time diagnostic into a runtime test failure. Losing
    one test's XML report is preferable to failing the whole build over
    optional reporting, so this is a warning, not a raised exception.
    """
    texts = list(header_texts)
    packages = list(pkgconfig_packages)

    header_matched = [fw for fw in KNOWN_FRAMEWORKS if fw.matches_headers(texts)]
    if len(header_matched) > 1:
        _warn_ambiguous(test_source, header_matched)
        return None
    if header_matched:
        return header_matched[0]

    pkg_matched = [fw for fw in KNOWN_FRAMEWORKS if fw.matches_pkgconfig(packages)]
    if len(pkg_matched) > 1:
        _warn_ambiguous(test_source, pkg_matched)
        return None
    return pkg_matched[0] if pkg_matched else None
