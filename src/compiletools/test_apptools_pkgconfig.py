"""Focused tests for package-spec handling in :mod:`apptools_pkgconfig`."""

import subprocess
from types import SimpleNamespace

import pytest

import compiletools.apptools_pkgconfig as pkgconfig


@pytest.fixture(autouse=True)
def _clear_pkg_config_cache():
    pkgconfig.clear_cache()
    yield
    pkgconfig.clear_cache()


def test_tokenize_pkg_config_specs_preserves_version_constraints():
    assert pkgconfig.tokenize_pkg_config_specs(
        [
            "alpha beta",
            "zlib >= 1.2, openssl<3",
            "glib-2.0 != 2.0",
            "gamma == 2",
            "unfinished >=",
        ]
    ) == [
        "alpha",
        "beta",
        "zlib >= 1.2",
        "openssl<3",
        "glib-2.0 != 2.0",
        "gamma == 2",
        "unfinished >=",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zlib,libxml-2.0", ["zlib", "libxml-2.0"]),
        ("zlib>=1.2", ["zlib>=1.2"]),
        ("zlib >=1.2", ["zlib >=1.2"]),
        ("zlib>= 1.2", ["zlib>= 1.2"]),
        ("zlib >=", ["zlib >="]),
    ],
)
def test_tokenize_pkg_config_specs_is_idempotent(raw, expected):
    first = pkgconfig.tokenize_pkg_config_specs([raw])

    assert first == expected
    assert pkgconfig.tokenize_pkg_config_specs(first) == expected


def test_tokenize_pkg_config_specs_never_joins_across_list_elements():
    raw = ["zlib >=", "libxml-2.0"]

    assert pkgconfig.tokenize_pkg_config_specs(raw) == raw


def test_add_flags_fallback_uses_real_package_specs(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if "--exists" in cmd:
            specs = cmd[cmd.index("--exists") + 1 :]
            available = all(spec == "present" for spec in specs)
            missing = next((spec for spec in specs if spec != "present"), "")
            stderr = "" if available else f"Package {missing} was not found in the pkg-config search path."
            return subprocess.CompletedProcess(cmd, 0 if available else 1, stdout="", stderr=stderr)
        output = {
            "--cflags": "-I/present/include -DPRESENT",
            "--libs": "-L/present/lib -lpresent",
        }[cmd[1]]
        return subprocess.CompletedProcess(cmd, 0, stdout=output)

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["present missing"],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
        LDFLAGS="",
    )

    with pytest.warns(UserWarning, match=r"pkg-config package 'missing' not found") as recorded:
        pkgconfig._add_flags_from_pkg_config(args)

    assert len(recorded) == 1
    assert str(recorded[0].message).startswith("pkg-config package 'missing' not found")
    assert "present missing" not in str(recorded[0].message)
    assert "-isystem /present/include" in args.CPPFLAGS
    assert "-DPRESENT" in args.CFLAGS
    assert "-DPRESENT" in args.CXXFLAGS
    assert "-lpresent" in args.LDFLAGS
    assert ["pkg-config", "--exists", "present", "missing"] in calls
    assert ["pkg-config", "--exists", "present missing"] not in calls


def test_batch_fast_path_keeps_constraint_as_one_spec(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        output = "-DCONSTRAINT_OK" if cmd[1] == "--cflags" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=output)

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["zlib >= 1.2 other"],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    pkgconfig._add_flags_from_pkg_config(args)

    assert ["pkg-config", "--exists", "zlib >= 1.2", "other"] in calls
    assert ["pkg-config", "--cflags", "zlib >= 1.2"] in calls
    assert "-DCONSTRAINT_OK" in args.CPPFLAGS


def test_unsatisfied_version_floor_warning_names_the_full_spec(monkeypatch):
    def fake_run(cmd, **_kwargs):
        if cmd[-1] == "zlib":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(
                "Package dependency requirement 'zlib >= 999' could not be satisfied.\n"
                "Package 'zlib' has version '1.2', required version is '>= 999'"
            ),
        )

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["zlib >= 999"],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    with pytest.warns(UserWarning, match=r"pkg-config version requirement 'zlib >= 999' not satisfied"):
        pkgconfig._add_flags_from_pkg_config(args)


def test_missing_constrained_package_warning_names_the_bare_package(monkeypatch):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="translated diagnostic")

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["ghost >= 1.0"],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    with pytest.warns(UserWarning, match=r"pkg-config package 'ghost' not found"):
        pkgconfig._add_flags_from_pkg_config(args)


@pytest.mark.parametrize("spec", ["zlib >=", ">= 1.2", ">=1.2"])
def test_malformed_comparison_gets_an_explicit_diagnostic_without_a_probe(monkeypatch, spec):
    def fail_if_called(*_args, **_kwargs):
        pytest.fail("malformed specs must not invoke pkg-config")

    monkeypatch.setattr(pkgconfig.subprocess, "run", fail_if_called)
    args = SimpleNamespace(
        pkg_config=[spec],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    with pytest.warns(UserWarning, match=rf"pkg-config malformed package specification {spec!r}"):
        pkgconfig._add_flags_from_pkg_config(args)
