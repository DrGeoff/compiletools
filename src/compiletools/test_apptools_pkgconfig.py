"""Focused tests for package-spec handling in :mod:`apptools_pkgconfig`."""

import contextlib
import signal
import subprocess
import time
import warnings

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


def test_batch_fallback_uses_real_package_specs(monkeypatch):
    """The per-package fallback probes each spec, never the whole list joined.

    The package list is the post-tokenization shape
    ``build_inputs._merged_pkg_config_specs`` produces from the raw conf
    attrs. Handing this test the raw conf shape ``['present missing']``
    would only be testing that ``_batch_pkg_config`` re-tokenizes
    defensively, which is a property its callers do not need and should not
    have to keep.
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

    # catch_warnings rather than pytest.warns because both queries have to
    # run inside the same recording scope: the assertion is that they
    # produce ONE warning between them, so the existence memo cannot
    # re-report the same missing package once per flag option.
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        cflags = pkgconfig._batch_pkg_config(["present", "missing"], "--cflags")
        libs = pkgconfig._batch_pkg_config(["present", "missing"], "--libs")

    assert len(recorded) == 1
    assert str(recorded[0].message).startswith("pkg-config package 'missing' not found")
    assert "present missing" not in str(recorded[0].message)
    assert pkgconfig.filter_pkg_config_cflags(cflags["present"]) == "-isystem /present/include -DPRESENT"
    assert cflags["missing"] == ""
    assert libs["present"] == "-L/present/lib -lpresent"
    assert ["pkg-config", "--exists", "present", "missing"] in calls
    assert ["pkg-config", "--exists", "present missing"] not in calls


def test_batch_fast_path_keeps_constraint_as_one_spec(monkeypatch):
    """A constraint stays one argv element through the batched ``--exists``.

    As above, the package list carries the post-tokenization shape.
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

    out = pkgconfig._batch_pkg_config(["zlib >= 1.2", "other"], "--cflags")

    assert ["pkg-config", "--exists", "zlib >= 1.2", "other"] in calls
    assert ["pkg-config", "--cflags", "zlib >= 1.2"] in calls
    assert out["zlib >= 1.2"] == "-DCONSTRAINT_OK"


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

    with pytest.warns(UserWarning, match=r"pkg-config version requirement 'zlib >= 999' not satisfied"):
        assert pkgconfig.cached_pkg_config("zlib >= 999", "--cflags") == ""


def test_missing_constrained_package_warning_names_the_bare_package(monkeypatch):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="translated diagnostic")

    monkeypatch.setattr(pkgconfig.subprocess, "run", fake_run)

    with pytest.warns(UserWarning, match=r"pkg-config package 'ghost' not found"):
        assert pkgconfig.cached_pkg_config("ghost >= 1.0", "--cflags") == ""


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

    with pytest.raises(pkgconfig.PkgConfigError, match=category):
        pkgconfig._batch_pkg_config([spec], "--cflags")


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

    with pytest.warns(UserWarning, match=rf"pkg-config malformed package specification {spec!r}"):
        assert pkgconfig._batch_pkg_config([spec], "--cflags") == {spec: ""}


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

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = pkgconfig._batch_pkg_config(["zlib >= 1.2"], "--cflags")

    assert ["pkg-config", "--cflags", "zlib >= 1.2"] in probed, (
        f"the constraint was not queried as one intact spec: {probed!r}"
    )
    assert out["zlib >= 1.2"] == "-DZLIB_OK"


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


def test_a_repeated_batch_warning_is_forwarded_only_once(monkeypatch, capsys):
    """``_batch_pkg_config``'s all-exist fast path calls the uncached
    ``_run_pkg_config_query`` directly, so a package queried across two
    batch rounds in one process must not print the same stderr twice."""

    def succeeds_with_a_warning(cmd, **_kwargs):
        if "--exists" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="-I/opt/x", stderr="Variable 'prefix' not defined")

    monkeypatch.setattr(pkgconfig.subprocess, "run", succeeds_with_a_warning)

    assert pkgconfig._batch_pkg_config(["chatty"], "--cflags") == {"chatty": "-I/opt/x"}
    assert pkgconfig._batch_pkg_config(["chatty"], "--cflags") == {"chatty": "-I/opt/x"}

    err = capsys.readouterr().err
    assert err.count("Variable 'prefix' not defined") == 1


def test_clear_cache_resets_the_forwarded_stderr_dedup_set(monkeypatch, capsys):
    """``clear_cache`` must reopen the gate on an already-forwarded triple.

    The dedup set is process-global, so without an explicit reset here the
    only thing proving it works is incidental worker-seeding order under
    ``-n`` -- a renamed or reordered test can silently stop exercising the
    ``.clear()`` call in ``clear_cache`` without any test going red. Routed
    through ``_run_pkg_config_query`` itself (not a manual ``print``) so the
    assertion depends on the production gate, not on the test's own echo.
    """

    def succeeds_with_a_warning(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="-I/opt/x", stderr="Variable 'prefix' not defined")

    monkeypatch.setattr(pkgconfig.subprocess, "run", succeeds_with_a_warning)

    assert pkgconfig._run_pkg_config_query("chatty", "--cflags") == "-I/opt/x"
    pkgconfig.clear_cache()
    assert pkgconfig._run_pkg_config_query("chatty", "--cflags") == "-I/opt/x"

    err = capsys.readouterr().err
    assert err.count("Variable 'prefix' not defined") == 2


def test_a_different_stderr_for_the_same_package_and_option_is_not_suppressed(monkeypatch, capsys):
    """The dedup key is ``(package, option, stderr)``, not just ``(package,
    option)``: a second, different warning from the same query must still
    reach the user, while a literal repeat of either stays suppressed."""

    responses = iter(
        [
            "first warning",
            "first warning",
            "second warning",
            "first warning",
        ]
    )

    def succeeds_with_a_warning(cmd, **_kwargs):
        if "--exists" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="-I/opt/x", stderr=next(responses))

    monkeypatch.setattr(pkgconfig.subprocess, "run", succeeds_with_a_warning)

    for _ in range(4):
        assert pkgconfig._run_pkg_config_query("chatty", "--cflags") == "-I/opt/x"

    err = capsys.readouterr().err
    assert err.count("first warning") == 1
    assert err.count("second warning") == 1


# Wall-clock budget for the cycle-guard deadline. Shared so the anti-vacuity
# arm's own bound stays a fixed multiple of the alarm it validates.
_DEADLINE_SECONDS = 5


class _DidNotTerminate(Exception):
    """Raised by ``_fails_if_it_does_not_terminate`` when the alarm fires."""


@contextlib.contextmanager
def _fails_if_it_does_not_terminate(seconds=_DEADLINE_SECONDS):
    """Turn a hang into a test failure.

    A cycle guard's failure mode is a spin, not a wrong answer, so the only
    assertion that can catch its removal is a deadline. SIGALRM rather than a
    thread because the walk is synchronous and single-threaded; pytest-xdist
    runs each test in its worker's main thread, where SIGALRM is deliverable.
    """

    def _fire(_signum, _frame):
        raise _DidNotTerminate(f"did not terminate within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _write_pc(directory, name, body):
    path = directory / f"{name}.pc"
    path.write_text(body, encoding="utf-8")
    return path


class TestUndefinedPkgConfigVariables:
    """pkgconf expands a ``${var}`` no assignment defines to the empty string
    and exits 0, so a typo'd ``${includedir}`` silently truncates a flag.

    Measured on this platform: neither pkgconf 1.4.2 nor 2.3.0 reports it
    under ``--cflags``, ``--print-errors``, ``--errors-to-stdout``,
    ``--validate``, ``--simulate``, ``--log-file`` or ``PKG_CONFIG_DEBUG_SPEW``
    -- every one exits 0 in silence. freedesktop pkg-config 0.29.2 does
    report it (``parse.c`` ``parse_strict``), but is not what ships here.
    compiletools therefore scans the ``.pc`` text itself.
    """

    def test_the_premise_holds_pkgconf_exits_zero_with_a_truncated_flag(self, tmp_path, monkeypatch):
        """Pin the defect this detector exists for against the real
        pkg-config on the machine running the suite. If a future pkgconf
        starts erroring, this fails and the detector can be retired rather
        than silently duplicating the implementation's own check."""
        _write_pc(tmp_path, "undefvar", "Name: U\nDescription: d\nVersion: 1\nCflags: -I${includedir_typo}/u\n")
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        result = subprocess.run(
            ["pkg-config", "--print-errors", "--cflags", "undefvar"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "-I/u"
        assert "includedir_typo" not in result.stderr

    def test_a_query_names_the_variable_the_file_and_the_silent_expansion(self, tmp_path, monkeypatch):
        _write_pc(tmp_path, "undefvar", "Name: U\nDescription: d\nVersion: 1\nCflags: -I${includedir_typo}/u\n")
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        with pytest.warns(UserWarning, match="includedir_typo") as recorded:
            pkgconfig.cached_pkg_config("undefvar", "--cflags")

        messages = [str(w.message) for w in recorded]
        assert any("includedir_typo" in m for m in messages), messages
        assert any(str(tmp_path / "undefvar.pc") in m for m in messages), messages
        assert any("empty" in m for m in messages), messages

    def test_strict_mode_does_not_promote_it(self, tmp_path, monkeypatch):
        """Unlike a missing package, an undefined ``.pc`` variable is a
        cosmetic finding: pkg-config itself exits 0 with usable (if
        truncated) flags, and the closure-wide scan can trip on a package
        the user never declared and cannot fix (a transitive
        ``Requires``/``Requires.private`` dependency's own ``.pc``, possibly
        distro-owned). ``--pkg-config-errors=error`` governs *missing
        packages* and *failed queries* -- promoting this diagnostic too
        would hard-fail a build that pkg-config itself considered
        successful, with no way for the user to silence just this one
        finding short of abandoning strict mode entirely. Same carve-out as
        ``_warn_pkg_config_tokenize_degraded``: always warn, never raise."""
        _write_pc(tmp_path, "undefvar", "Name: U\nDescription: d\nVersion: 1\nCflags: -I${includedir_typo}/u\n")
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        pkgconfig.set_pkg_config_errors("error")

        with pytest.warns(UserWarning, match="includedir_typo"):
            result = pkgconfig.cached_pkg_config("undefvar", "--cflags")
        assert result == "-I/u"

    def test_a_well_formed_pc_file_is_silent(self, tmp_path, monkeypatch):
        """Anti-vacuity for every positive case above: the same code path
        over a file whose variables all resolve must emit nothing."""
        _write_pc(
            tmp_path,
            "wellformed",
            "prefix=/opt/w\nincludedir=${prefix}/include\nName: W\nDescription: d\nVersion: 1\n"
            "Cflags: -I${includedir}/w\n",
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert pkgconfig.cached_pkg_config("wellformed", "--cflags") == "-I/opt/w/include/w"

    @pytest.mark.parametrize("keyword", ["Requires", "Requires.private"])
    def test_the_scan_follows_the_requires_closure(self, tmp_path, monkeypatch, keyword):
        """A typo in a dependency truncates the consumer's flags just as
        surely as one in the package itself, and the consumer is the only
        name the user typed."""
        _write_pc(tmp_path, "dep", "Name: D\nDescription: d\nVersion: 1\nCflags: -I${prefix_typo}/dep\n")
        _write_pc(
            tmp_path,
            "top",
            f"Name: T\nDescription: d\nVersion: 1\n{keyword}: dep >= 1\nCflags: -I/opt/top\n",
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        with pytest.warns(UserWarning, match="prefix_typo") as recorded:
            pkgconfig.cached_pkg_config("top", "--cflags")

        messages = [str(w.message) for w in recorded]
        assert any("prefix_typo" in m and "dep.pc" in m for m in messages), messages

    def test_strict_mode_does_not_promote_a_transitive_dependency_typo(self, tmp_path, monkeypatch):
        """The closure-wide scan can trip on a package the user never
        declared and cannot fix -- a transitive ``Requires`` dependency's
        own (possibly distro-owned) ``.pc``. Strict mode must not turn that
        into a hard build failure: pkg-config itself resolved the actual
        ``--cflags`` query the caller asked for."""
        _write_pc(tmp_path, "dep", "Name: D\nDescription: d\nVersion: 1\nCflags: -I${prefix_typo}/dep\n")
        _write_pc(
            tmp_path,
            "top",
            "Name: T\nDescription: d\nVersion: 1\nRequires: dep >= 1\nCflags: -I/opt/top\n",
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))
        pkgconfig.set_pkg_config_errors("error")

        with pytest.warns(UserWarning, match="prefix_typo"):
            result = pkgconfig.cached_pkg_config("top", "--cflags")
        assert result == "-I/opt/top -I/dep"

    def test_a_definition_below_its_use_is_reported(self, tmp_path, monkeypatch):
        """The scan is order-sensitive because both implementations are.

        Measured on this box: pkgconf 1.4.2 expands ``-I${late}/b`` to
        ``-I/b`` and exits 0 when ``late=`` appears on a later line, and
        freedesktop 0.29.2 says ``Variable 'late' not defined`` and exits 1.
        An order-independent scan would call this a false positive and
        suppress a real silent truncation.
        """
        path = _write_pc(
            tmp_path,
            "backwards",
            "Name: B\nDescription: d\nVersion: 1\nCflags: -I${late}/b\nlate=/opt/late\n",
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        assert pkgconfig._undefined_pc_variables(str(path)) == ["late"]

        result = subprocess.run(["pkg-config", "--cflags", "backwards"], capture_output=True, text=True, check=False)
        assert (result.returncode, result.stdout.strip()) == (0, "-I/b")

        with pytest.warns(UserWarning, match="late"):
            pkgconfig.cached_pkg_config("backwards", "--cflags")

    def test_a_self_reference_resolves_against_the_previous_definition(self, tmp_path):
        """An assignment takes effect at the end of its own line, so the
        idiomatic ``prefix=${prefix}/suffix`` accumulation is not a finding
        when an earlier ``prefix=`` exists -- and is one when it does not."""
        satisfied = _write_pc(
            tmp_path,
            "selfref",
            "prefix=/opt\nprefix=${prefix}/x\nName: S\nDescription: d\nVersion: 1\nCflags: -I${prefix}\n",
        )
        assert pkgconfig._undefined_pc_variables(str(satisfied)) == []

        unsatisfied = _write_pc(
            tmp_path,
            "selfref-first",
            "prefix=${prefix}/x\nName: S\nDescription: d\nVersion: 1\nCflags: -I${prefix}\n",
        )
        assert pkgconfig._undefined_pc_variables(str(unsatisfied)) == ["prefix"]

    def test_builtin_variables_are_not_reported(self, tmp_path, monkeypatch):
        """``pcfiledir`` and friends are supplied by pkg-config itself, not
        by the file. Measured on pkgconf 1.4.2: pcfiledir, pc_sysrootdir and
        pc_top_builddir all resolve to a value for a file that defines none
        of them."""
        path = _write_pc(
            tmp_path,
            "builtins",
            "Name: B\nDescription: d\nVersion: 1\nCflags: -I${pcfiledir}/inc -I${pc_sysrootdir}usr/include\n",
        )
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        assert pkgconfig._undefined_pc_variables(str(path)) == []

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            cflags = pkgconfig.cached_pkg_config("builtins", "--cflags")
        assert f"-I{tmp_path}/inc" in cflags

    def test_the_shipped_example_corpus_is_clean(self, pkgconfig_env):
        """False-positive floor over every ``.pc`` compiletools ships. A
        detector that fires here would fire on real projects too."""
        import pathlib

        findings = {}
        for pc in sorted(pathlib.Path(pkgconfig_env).glob("*.pc")):
            undefined = pkgconfig._undefined_pc_variables(str(pc))
            if undefined:
                findings[pc.name] = undefined
        assert findings == {}

    def test_a_commented_out_reference_is_not_a_finding(self, tmp_path):
        """The measurement that produced this rule: three system icu ``.pc``
        files carry ``${pkgdatadir}`` on commented-out lines. Counting them
        put the false-positive rate at 3/236 instead of 0/236."""
        path = _write_pc(
            tmp_path,
            "commented",
            "Name: C\nDescription: d\nVersion: 1\n#datadir=${pkgdatadir}\nCflags: -I/opt/c\n",
        )
        assert pkgconfig._undefined_pc_variables(str(path)) == []


class TestBareDetachedPkgConfigFlags:
    """The visible symptom of the same defect at the other end: an
    ``-I${undefined}`` whose expansion consumed the whole path leaves a bare
    ``-I`` that eats the next argv token, and a bare ``-L`` makes the linker
    read ``-lfoo`` as a library *directory*."""

    def test_a_trailing_detached_flag_is_reported(self):
        assert pkgconfig._bare_detached_flags("-DFOO -I") == ["-I"]

    def test_a_detached_flag_followed_by_another_flag_is_reported(self):
        assert pkgconfig._bare_detached_flags("-L -lfoo") == ["-L"]

    def test_a_legitimately_detached_pair_is_silent(self):
        """Measured false positive this rule was narrowed to avoid:
        ``libbsd-overlay`` ships ``-isystem /usr/include/bsd``."""
        assert pkgconfig._bare_detached_flags("-isystem /usr/include/bsd") == []

    def test_attached_flags_are_silent(self):
        assert pkgconfig._bare_detached_flags("-I/usr/include -L/usr/lib -lfoo") == []

    def test_a_query_warns_naming_the_flag(self, monkeypatch):
        def truncating_query(cmd, **_kwargs):
            if "--exists" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="-L -lfoo", stderr="")

        monkeypatch.setattr(pkgconfig.subprocess, "run", truncating_query)

        with pytest.warns(UserWarning, match=r"detached '-L'"):
            pkgconfig.cached_pkg_config("truncated", "--libs")

    def test_strict_mode_does_not_promote_it(self, monkeypatch):
        """Same carve-out as the undefined-variable audit: pkg-config exited
        0 with the (truncated) flags it queried for, so this is a diagnostic
        about a third-party ``.pc``'s quality, not a query failure --
        ``--pkg-config-errors=error`` must not turn it into a build break."""

        def truncating_query(cmd, **_kwargs):
            if "--exists" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="-L -lfoo", stderr="")

        monkeypatch.setattr(pkgconfig.subprocess, "run", truncating_query)
        pkgconfig.set_pkg_config_errors("error")

        with pytest.warns(UserWarning, match=r"detached '-L'"):
            result = pkgconfig.cached_pkg_config("truncated", "--libs")
        assert result == "-L -lfoo"

    def test_a_narrow_subprocess_stub_cannot_break_the_query(self, monkeypatch):
        """The scanner adds one probe (``--variable pc_path pkg-config``) to
        the pkg-config call pattern. A caller stubbing ``subprocess.run`` to
        model only the calls the query itself makes must still get its flags:
        a best-effort diagnostic may not turn a working query into a crash.
        This is the regression that ``test_add_flags_fallback_uses_real_package_specs``
        caught -- it raised ``KeyError: '--variable'`` from inside its own stub.
        """

        def models_only_exists_and_cflags(cmd, **_kwargs):
            if "--exists" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout={"--cflags": "-I/opt/x"}[cmd[1]], stderr="")

        monkeypatch.setenv("PKG_CONFIG_LIBDIR", "")
        monkeypatch.delenv("PKG_CONFIG_LIBDIR")
        monkeypatch.setattr(pkgconfig.subprocess, "run", models_only_exists_and_cflags)

        assert pkgconfig.cached_pkg_config("narrow", "--cflags") == "-I/opt/x"


class TestTheRequiresClosureTerminatesOnCycles:
    """A ``Requires`` cycle must terminate rather than spin.

    The shipped ``cycle-alpha``/``cycle-beta`` example pair does NOT exercise
    this: both halves are Cflags/Libs only, so each closure is a single node.
    That pair is cyclic in link order, which is what it was shipped for. The
    cycle the seen set exists to stop has to be constructed, and it is
    constructed here -- a cycle guard whose only evidence is an artefact that
    cannot reach it survives the refactor that deletes it.
    """

    @staticmethod
    def _write_cycle(tmp_path):
        _write_pc(tmp_path, "cyc-a", "Name: A\nDescription: d\nVersion: 1\nRequires: cyc-b\nCflags: -I/opt/a\n")
        _write_pc(tmp_path, "cyc-b", "Name: B\nDescription: d\nVersion: 1\nRequires: cyc-a\nCflags: -I/opt/b\n")

    @pytest.mark.parametrize(
        "entry,expected",
        [("cyc-a", ["cyc-a", "cyc-b"]), ("cyc-b", ["cyc-b", "cyc-a"])],
    )
    def test_a_two_cycle_terminates_and_returns_both_packages(self, tmp_path, monkeypatch, entry, expected):
        """Entered from either end: both nodes come back, once each, in
        breadth-first order from the entry point."""
        self._write_cycle(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        with _fails_if_it_does_not_terminate():
            closure = pkgconfig._pc_requires_closure(entry)

        assert [name for name, _path in closure] == expected

    def test_a_self_require_terminates_and_returns_one_package(self, tmp_path, monkeypatch):
        """The degenerate cycle: the seen set is seeded with the entry point,
        so a package requiring itself must not enqueue itself."""
        _write_pc(tmp_path, "selfy", "Name: S\nDescription: d\nVersion: 1\nRequires: selfy\nCflags: -I/opt/s\n")
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        with _fails_if_it_does_not_terminate():
            closure = pkgconfig._pc_requires_closure("selfy")

        assert [name for name, _path in closure] == ["selfy"]

    def test_the_shipped_cycle_pair_carries_no_requires_edge(self, pkgconfig_env):
        """Pins the premise of this class rather than asserting it in prose.

        If someone later adds a ``Requires`` line to the shipped pair, this
        fails and the docstring above stops being true -- which is the moment
        to reconsider whether the constructed fixtures are still needed.

        ``pkgconfig_env`` points PKG_CONFIG_PATH at the in-repo pkgs/
        directory so the lookup cannot come up empty on a machine with no
        ambient PKG_CONFIG_PATH; a skip here would make the assertion
        unreachable in the default environment, which is the state this test
        exists to detect.
        """
        for package in ("cycle-alpha", "cycle-beta"):
            path = pkgconfig._locate_pc_file(package)
            assert path is not None, f"{package}.pc missing from {pkgconfig_env}"
            requires = [line for line in pkgconfig._read_pc_file(path) if line.lstrip().startswith("Requires")]
            assert requires == [], (package, path, requires)

    def test_the_seen_set_is_what_stops_the_cycle(self, tmp_path, monkeypatch):
        """Anti-vacuity: the guard above only means something if the timeout
        it relies on can actually fire. Re-run the same two-cycle through a
        copy of the walk with the seen check removed and assert it does NOT
        terminate, so a future timeout that silently stopped working cannot
        make the three tests above pass for free.

        The walk carries its OWN wall-clock bound because this arm would
        otherwise test the deadline using the deadline: with SIGALRM
        neutered, the four ordinary cells stay green in ~0.26s while this
        one spins to whatever external timeout the runner has (measured at
        exit 124 against a 25s bound), so a dead alarm shows up as a hung
        xdist worker instead of a red test. Exceeding 3x the alarm makes it
        red at the cost of one extra monotonic() per iteration -- the spin
        itself is far more expensive, at ~287k _locate_pc_file plus ~287k
        _read_pc_file calls (real filesystem I/O) per suite run.
        """
        self._write_cycle(tmp_path)
        monkeypatch.setenv("PKG_CONFIG_PATH", str(tmp_path))

        def unguarded_closure(package):
            started = time.monotonic()
            queue = [package]
            while queue:
                assert time.monotonic() - started < _DEADLINE_SECONDS * 3, (
                    f"the cycle spun past {_DEADLINE_SECONDS * 3}s without SIGALRM firing: "
                    "the deadline this arm exists to validate is not being delivered"
                )
                current = queue.pop(0)
                path = pkgconfig._locate_pc_file(current)
                if path is None:
                    continue
                for line in pkgconfig._read_pc_file(path):
                    keyword = pkgconfig._PC_KEYWORD_RE.match(line)
                    if keyword is None or keyword.group(1) not in ("Requires", "Requires.private"):
                        continue
                    queue.extend(pkgconfig._pc_required_packages(keyword.group(2)))

        with pytest.raises(_DidNotTerminate):
            with _fails_if_it_does_not_terminate():
                unguarded_closure("cyc-a")


class TestDefaultSearchDirsSurvivesTransientFailure:
    """``_pkg_config_default_search_dirs`` degrades any failure to ``()`` so
    a caller stubbing ``subprocess.run`` for an unrelated test never breaks.
    But a real transient failure (pkg-config momentarily unavailable, a test
    stub active for one call) must not permanently poison the process-wide
    memo -- only a genuinely successful probe should be cached, since that
    is the only case the "compiled-in list never changes" rationale for
    skipping ``clear_cache()`` actually applies to.
    """

    @pytest.fixture(autouse=True)
    def _reset_default_search_dirs_cache(self, monkeypatch):
        monkeypatch.setattr(pkgconfig, "_pkg_config_default_search_dirs_cache", None)
        yield
        monkeypatch.setattr(pkgconfig, "_pkg_config_default_search_dirs_cache", None)

    def test_a_failed_probe_is_retried_on_the_next_call(self, monkeypatch):
        calls = []

        def failing_then_succeeding(cmd, **_kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pkg-config: not found")
            return subprocess.CompletedProcess(cmd, 0, stdout="/opt/pkgconfig/default\n", stderr="")

        monkeypatch.setattr(pkgconfig.subprocess, "run", failing_then_succeeding)

        first = pkgconfig._pkg_config_default_search_dirs()
        assert first == ()

        second = pkgconfig._pkg_config_default_search_dirs()
        assert second == ("/opt/pkgconfig/default",)
        assert len(calls) == 2, "a failed probe must not be cached -- the second call must re-probe"

    def test_an_exception_during_the_probe_is_retried_on_the_next_call(self, monkeypatch):
        calls = []

        def raising_then_succeeding(cmd, **_kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                raise OSError("pkg-config executable vanished mid-probe")
            return subprocess.CompletedProcess(cmd, 0, stdout="/opt/pkgconfig/default\n", stderr="")

        monkeypatch.setattr(pkgconfig.subprocess, "run", raising_then_succeeding)

        first = pkgconfig._pkg_config_default_search_dirs()
        assert first == ()

        second = pkgconfig._pkg_config_default_search_dirs()
        assert second == ("/opt/pkgconfig/default",)
        assert len(calls) == 2, "an exception during the probe must not be cached"

    def test_a_successful_probe_is_still_cached(self, monkeypatch):
        """The no-regression half: a genuinely successful probe (even one
        resolving to an empty list) must be memoised, not re-run every call
        -- that is the whole point of the process-wide cache."""
        calls = []

        def always_succeeding(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="/opt/pkgconfig/default\n", stderr="")

        monkeypatch.setattr(pkgconfig.subprocess, "run", always_succeeding)

        first = pkgconfig._pkg_config_default_search_dirs()
        second = pkgconfig._pkg_config_default_search_dirs()
        assert first == second == ("/opt/pkgconfig/default",)
        assert len(calls) == 1, "a successful probe must be cached across calls"


class TestRequiresValueParsing:
    """``_pc_required_packages`` must return only package names for every
    constraint spelling pkgconf itself accepts. The sharp corner is the
    operator attached to the name with the operand spaced off
    (``zlib>= 1.2``): the name consumes one token, so a parser that does not
    also consume the detached operand reads ``1.2`` as a package. Today the
    phantom only widens the closure walk (``_locate_pc_file`` returns None),
    but a real ``1.2.pc`` on the search path -- or any future consumer of
    the closure -- turns it into a bogus dependency.
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("zlib", ["zlib"]),
            ("zlib libpng", ["zlib", "libpng"]),
            ("zlib >= 1.2", ["zlib"]),
            ("zlib >=1.2", ["zlib"]),
            ("zlib>=1.2", ["zlib"]),
            ("zlib>= 1.2", ["zlib"]),
            ("zlib= 1.2", ["zlib"]),
            ("zlib!= 1.2", ["zlib"]),
            ("zlib>= 1.2 libpng", ["zlib", "libpng"]),
            ("zlib >= 1.2, libpng <= 3", ["zlib", "libpng"]),
            ("zlib>=", ["zlib"]),
        ],
    )
    def test_version_operands_never_become_package_names(self, value, expected):
        assert pkgconfig._pc_required_packages(value) == expected
