"""Registry of example projects shipped with compiletools.

Examples live in two role-keyed sibling directories under the
package root:

* ``examples-end-to-end/`` — vanilla buildable via ``ct-cake`` across
  every registered backend. Walked by the cross-backend matrix in
  ``test_examples_end_to_end_cross_backend.py``.
* ``examples-features/`` — fixtures consumed by per-feature unit
  tests in ``src/compiletools/test_*.py``. Not buildable end-to-end
  via ``ct-cake`` (fictional libraries, headers-only, multi-step
  ``build.sh``-driven, framework-mixing, etc.).

Adding a new example to disk without registering it here trips the
drift guard in ``test_examples_registry.py`` with a diagnostic that
names this file.
"""

from __future__ import annotations

import os

_PACKAGE_ROOT = os.path.dirname(os.path.realpath(__file__))
_E2E_SUBDIR = "examples-end-to-end"
_FEATURES_SUBDIR = "examples-features"


EXAMPLES_E2E: frozenset[str] = frozenset(
    {
        "appinfo",
        "calculator",
        "cache_scoping",
        "cli_features",
        "computed_include",
        "conditional_includes",
        "cppflags_macros",
        "cross_platform",
        "cxx_modules",
        "cxx_modules_header_units",
        "cxx_modules_header_unit_isystem",
        "cxx_modules_import_std",
        "cxx_modules_transitive_header_unit",
        "cxx_modules_partitions",
        "cxx_modules_split",
        "dottypaths",
        "factory",
        "feature_headers",
        "ffile_prefix_map",
        "has_include",
        "hunter_macro_propagation",
        "macro_state_dependency",
        "magicinclude",
        "magicsourceinheader",
        "movingheaders",
        "multi_axis_variant",
        "nestedconfig",
        "numbers",
        "pch",
        "pkgconfig_cycle",
        "platform_has_include",
        "postbuild_script",
        "prebuild_script",
        "project_version",
        "separate_cpp_cxx",
        "simple",
        "terminal_games",
        "testprefix",
        "unit_test_marker",
        "version_dependent_api",
    }
)


EXAMPLES_FEATURES: frozenset[str] = frozenset(
    {
        "conf_dir_relative_pkgconfig",
        "cycle",
        "duplicate_flags",
        "dynamic_library",
        "empty_macro_bug",
        "header_guard_bug",
        "isystem_include_bug",
        "ldflags",
        "library",
        "lotsofmagic",
        "macro_deps",
        "magic_processing_order",
        "magicpkgconfig",
        "magicpkgconfig_fake",
        "parse_order_macro_bug",
        "pch_bypass_bug",
        "pkg_config_header_deps",
        "pkgconfig",
        "pkgs",
        "project_pkgconfig_override",
        "relative_cas_dir_bug",
        "serialise_tests",
        "static_link_order",
        "test_xml_output",
        "transitive_cache_bug",
        "undef_bug",
    }
)


def e2e_dir() -> str:
    """Absolute path to the examples-end-to-end/ directory."""
    return os.path.join(_PACKAGE_ROOT, _E2E_SUBDIR)


def features_dir() -> str:
    """Absolute path to the examples-features/ directory."""
    return os.path.join(_PACKAGE_ROOT, _FEATURES_SUBDIR)


def example_path(name: str) -> str:
    """Resolve an example name to its absolute on-disk directory.

    Raises:
        KeyError: ``name`` is not in either registry. The error
            message names ``examples_registry.py`` so the caller
            knows where to register the new example.
    """
    if name in EXAMPLES_E2E:
        return os.path.join(e2e_dir(), name)
    if name in EXAMPLES_FEATURES:
        return os.path.join(features_dir(), name)
    raise KeyError(
        f"unknown example {name!r}; register it in "
        f"compiletools/examples_registry.py "
        f"(EXAMPLES_E2E or EXAMPLES_FEATURES)"
    )


def example_file(relpath: str) -> str:
    """Resolve a ``<example>/<rel>`` path string to absolute.

    ``example_file('simple/helloworld.cpp')`` returns
    ``<e2e_dir>/simple/helloworld.cpp``. A bare name with no slash
    (``example_file('pkgs')``) resolves to the example's own
    directory, equivalent to ``example_path(name)``.
    """
    name, _, rest = relpath.partition("/")
    base = example_path(name)
    return os.path.join(base, rest) if rest else base
