"""Focused tests for package-spec handling in :mod:`apptools_pkgconfig`."""

import subprocess
import warnings
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("zlib >=, libxml-2.0", ["zlib >=", "libxml-2.0"]),
        ("zlib, >= 1.2", ["zlib", ">= 1.2"]),
    ],
)
def test_tokenize_pkg_config_specs_treats_comma_as_a_hard_boundary(raw, expected):
    """A comma ends a spec as firmly as a list-element boundary does.

    The docstring calls commas package separators, but replacing them with
    spaces before splitting erases the boundary, so a dangling operator
    reaches across the comma and swallows the next package as its version
    operand. The result is not even a warning: pkg-config compares the
    installed version against the literal string it was handed and can
    return success, so the swallowed package's cflags and libs vanish
    silently.
    """
    assert pkgconfig.tokenize_pkg_config_specs([raw]) == expected


def test_add_flags_fallback_uses_real_package_specs(monkeypatch):
    """The per-package fallback probes each spec, never the whole list joined.

    ``args.pkg_config`` is built the way callers must build it — already
    tokenized, the shape ``build_inputs._merged_pkg_config_specs`` produces
    from the raw conf attrs. Handing this test the raw conf shape
    ``['present missing']`` would only be testing that
    ``_add_flags_from_pkg_config`` re-tokenizes defensively, which is a
    property its callers do not need and should not have to keep.
    """
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
        pkg_config=["present", "missing"],
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
    """A constraint stays one argv element through the batched ``--exists``.

    As above, ``args.pkg_config`` carries the post-tokenization shape.
    ``'zlib >= 1.2'`` is one element and ``'other'`` is another; what this
    pins is that the batch probe does not re-split the constraint into
    ``zlib``, ``>=`` and ``1.2`` on its way to argv.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        output = "-DCONSTRAINT_OK" if cmd[1] == "--cflags" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=output)

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["zlib >= 1.2", "other"],
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


@pytest.mark.parametrize(
    ("spec", "category"),
    [
        ("zlib >=", "malformed package specification"),
        ("missing", "not found"),
        ("zlib >= 999", "version requirement"),
        ("ghost >= 1.0", "while evaluating"),
    ],
)
def test_pkg_config_error_mode_promotes_every_failure_category(monkeypatch, spec, category):
    """Strict mode uses the same four stable diagnostics as warn mode."""

    def fake_run(cmd, **_kwargs):
        if cmd[-1] == "zlib":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pkg-config failure")

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    pkgconfig.set_pkg_config_errors("error")
    args = SimpleNamespace(
        pkg_config=[spec],
        pkg_config_errors="error",
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    with pytest.raises(pkgconfig.PkgConfigError, match=category):
        pkgconfig._add_flags_from_pkg_config(args)


def test_clear_cache_preserves_pkg_config_error_mode(monkeypatch):
    """A cache clear must not disarm an enforcement policy.

    ``clear_cache`` used to reset the policy to ``warn``. The shipped
    fan-out ``Hunter.clear_cache`` -> ``MagicFlagsBase.clear_cache`` ->
    ``apptools.clear_cache`` -> here reaches it mid-process, so a build that
    asked for ``--pkg-config-errors=error`` could silently continue in warn
    mode after any cache clear.
    """
    monkeypatch.setattr(
        pkgconfig.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing"),
    )
    pkgconfig.set_pkg_config_errors("error")
    pkgconfig.clear_cache()

    assert pkgconfig.get_pkg_config_errors() == "error"
    with pytest.raises(pkgconfig.PkgConfigError, match=r"pkg-config package 'missing' not found"):
        pkgconfig.cached_pkg_config("missing", "--cflags")


def test_clear_cache_still_clears_the_memos(monkeypatch):
    """The policy carve-out must not stop the caches being cleared."""
    monkeypatch.setattr(
        pkgconfig.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing"),
    )
    with pytest.warns(UserWarning, match=r"pkg-config package 'missing' not found"):
        assert pkgconfig.cached_pkg_config("missing", "--cflags") == ""
    assert pkgconfig.cached_pkg_config.cache_info().currsize > 0

    pkgconfig.clear_cache()

    assert pkgconfig.cached_pkg_config.cache_info().currsize == 0
    assert pkgconfig._cached_pkg_config_exists.cache_info().currsize == 0


def test_switching_to_error_mode_reprobes_a_warm_warn_failure(monkeypatch):
    """A cached warn-mode failure cannot bypass a later strict-mode query."""
    calls = []

    def missing(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")

    monkeypatch.setattr(pkgconfig.subprocess, "run", missing)

    with pytest.warns(UserWarning, match=r"pkg-config package 'missing' not found"):
        assert pkgconfig.cached_pkg_config("missing", "--cflags") == ""
    assert len(calls) == 1

    pkgconfig.set_pkg_config_errors("error")

    with pytest.raises(pkgconfig.PkgConfigError, match=r"pkg-config package 'missing' not found"):
        pkgconfig.cached_pkg_config("missing", "--cflags")
    assert len(calls) == 2


@pytest.mark.parametrize("spec", ["zlib >=", ">= 1.2", ">=1.2", "zlib>= 1.2", "zlib >=1.2"])
def test_malformed_comparison_gets_an_explicit_diagnostic_without_a_probe(monkeypatch, spec):
    """Specs pkg-config would answer by making something up.

    The two half-spaced forms belong here with the operand-less ones even
    though each names a real package. ``zlib>= 1.2`` is read as two
    packages, ``zlib>=`` and ``1.2``, neither of which the user wrote.
    ``zlib >=1.2`` is worse: pkg-config swallows the version's first
    character into the operator token and enforces ``>= .2``, so the floor
    the user asked for silently disappears and the probe exits 0. See
    test_apptools.py::TestPkgConfigConfValueSplitting for the measured
    end-to-end consequence.
    """

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


def test_fully_spaced_constraint_is_the_form_that_reaches_a_probe(monkeypatch):
    """``zlib >= 1.2`` is the only spelling that survives to pkg-config intact.

    Counterpart to the malformed parametrize above: three of its four
    neighbours differ from this one by a single space, so the classifier
    has to keep letting the correct spelling through as one spec rather
    than rejecting the whole family.
    """
    probed: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        probed.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="-DZLIB_OK", stderr="")

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)
    args = SimpleNamespace(
        pkg_config=["zlib >= 1.2"],
        verbose=0,
        CPPFLAGS="",
        CFLAGS="",
        CXXFLAGS="",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pkgconfig._add_flags_from_pkg_config(args)

    assert ["pkg-config", "--cflags", "zlib >= 1.2"] in probed, (
        f"the constraint was not queried as one intact spec: {probed!r}"
    )
    assert "-DZLIB_OK" in args.CPPFLAGS


def _exists_ok_query_fails(cmd, **_kwargs):
    """``--exists`` succeeds, the flag query fails.

    Measured against pkgconf 1.4.2: a ``.pc`` with an unresolvable
    ``Requires.private`` exits 0 on ``--exists`` and 1 on ``--cflags``, so
    ``--exists`` is not a proxy for the flag queries. A bare ``--libs`` on that
    same ``.pc`` exits 0, so the ``--libs`` test below pins the general
    contract rather than that specific ``.pc``.
    """
    if "--exists" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Package 'ghost' not found")


def test_failed_query_after_a_passing_exists_warns(monkeypatch):
    """An empty flag string must not be the only trace of a failed query.

    Without the returncode check, a package whose ``--cflags`` fails is
    indistinguishable from one that legitimately contributes no flags, and
    the build compiles without the include paths it asked for.
    """
    monkeypatch.setattr(pkgconfig.subprocess, "run", _exists_ok_query_fails)

    with pytest.warns(UserWarning, match=r"pkg-config --cflags 'broken' failed"):
        assert pkgconfig.cached_pkg_config("broken", "--cflags") == ""


def test_failed_query_is_fatal_in_error_mode(monkeypatch):
    monkeypatch.setattr(pkgconfig.subprocess, "run", _exists_ok_query_fails)
    pkgconfig.set_pkg_config_errors("error")

    with pytest.raises(pkgconfig.PkgConfigError, match=r"pkg-config --libs 'broken' failed"):
        pkgconfig.cached_pkg_config("broken", "--libs")


def test_failed_query_on_the_batch_fast_path_is_fatal_in_error_mode(monkeypatch):
    """The batch fast path skips the per-package ``--exists``, so it owns
    its own returncode check rather than inheriting one."""
    monkeypatch.setattr(pkgconfig.subprocess, "run", _exists_ok_query_fails)
    pkgconfig.set_pkg_config_errors("error")

    with pytest.raises(pkgconfig.PkgConfigError, match=r"pkg-config --cflags 'broken' failed"):
        pkgconfig._batch_pkg_config(["broken"], "--cflags")


def test_failed_query_diagnostic_carries_the_pkg_config_stderr(monkeypatch):
    monkeypatch.setattr(pkgconfig.subprocess, "run", _exists_ok_query_fails)

    with pytest.warns(UserWarning, match=r"Package 'ghost' not found"):
        pkgconfig.cached_pkg_config("broken", "--cflags")


def test_a_warning_from_a_succeeding_query_still_reaches_the_user(monkeypatch, capsys):
    """Capturing stderr to build the failure diagnostic must not silence the
    success path.

    A query that exits 0 can still write a diagnostic to stderr, and before
    stderr was captured it went straight to the terminal, so capturing it
    without re-emitting would delete that signal. The stub is deliberate:
    pkgconf 1.4.2 does not reach this path. Eight malformed-``.pc`` shapes
    were probed and none wrote to stderr on a successful query, including an
    undefined variable -- which exits 0 with empty stderr and quietly
    truncates ``-I${nope}/include`` to ``-I/include``, so it is a
    silent-wrong-flags case rather than the noisy one this test pins. The
    contract is kept for the implementations that do warn.
    """

    def succeeds_with_a_warning(cmd, **_kwargs):
        if "--exists" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="-I/opt/x", stderr="Variable 'prefix' not defined")

    monkeypatch.setattr(pkgconfig.subprocess, "run", succeeds_with_a_warning)

    assert pkgconfig.cached_pkg_config("chatty", "--cflags") == "-I/opt/x"
    assert "Variable 'prefix' not defined" in capsys.readouterr().err


def test_a_succeeding_query_is_not_promoted_to_a_failure_in_error_mode(monkeypatch):
    """A warning on stderr is not a failed query: pkg-config exited 0 and
    returned usable flags, so strict mode must not turn it into a fatal."""

    def succeeds_with_a_warning(cmd, **_kwargs):
        if "--exists" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="-I/opt/x", stderr="Variable 'prefix' not defined")

    monkeypatch.setattr(pkgconfig.subprocess, "run", succeeds_with_a_warning)
    pkgconfig.set_pkg_config_errors("error")

    assert pkgconfig.cached_pkg_config("chatty", "--cflags") == "-I/opt/x"
