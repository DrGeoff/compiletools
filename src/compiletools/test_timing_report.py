"""Tests for timing_report module."""

from __future__ import annotations

import json
import os
import sys
import types

import compiletools.timing_report as tr
from compiletools.build_timer import BuildTimer
from compiletools.timing_report import _find_timing_file, _format_pct, _resolve_and_load, _styled, main


class TestFindTimingFile:
    def test_explicit_path(self):
        assert _find_timing_file("/some/path.json") == "/some/path.json"

    def test_auto_detect_cwd(self, tmp_path, monkeypatch):
        timing = tmp_path / "timing.json"
        timing.write_text("{}")
        monkeypatch.chdir(tmp_path)
        assert _find_timing_file(None) == "timing.json"

    def test_auto_detect_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _find_timing_file(None) is None

    def test_auto_detect_bindir_diagnostics_newest(self, tmp_path, monkeypatch):
        """With the new diagnostics-dir layout, look up the newest invocation
        subdir under <bindir>/diagnostics/ by lex sort and return its
        timing.json."""
        monkeypatch.chdir(tmp_path)
        bindir = tmp_path / "bin"
        diag = bindir / "diagnostics"
        older = diag / "20260506T120000-100"
        newer = diag / "20260506T143022-200"
        for d in (older, newer):
            d.mkdir(parents=True)
            (d / "timing.json").write_text("{}")
        result = _find_timing_file(None, bindir=str(bindir))
        assert result == str(newer / "timing.json")

    def test_auto_detect_sort_is_pid_numeric_within_same_second(self, tmp_path, monkeypatch):
        """Within the same wall-clock second, PIDs of different string widths
        must sort numerically. '20260506T120000-1000' is newer than
        '20260506T120000-999' but lex sort would invert that."""
        monkeypatch.chdir(tmp_path)
        bindir = tmp_path / "bin"
        diag = bindir / "diagnostics"
        older = diag / "20260506T120000-999"
        newer = diag / "20260506T120000-1000"
        for d in (older, newer):
            d.mkdir(parents=True)
            (d / "timing.json").write_text("{}")
        result = _find_timing_file(None, bindir=str(bindir))
        assert result == str(newer / "timing.json")

    def test_auto_detect_diagnostics_dir_overrides_bindir(self, tmp_path, monkeypatch):
        """An explicit ``diagnostics_dir`` is used directly even if it lives
        outside any ``bindir``."""
        monkeypatch.chdir(tmp_path)
        bindir = tmp_path / "bin"
        bindir.mkdir()  # exists but contains no diagnostics
        diag = tmp_path / "elsewhere" / "diag"
        invocation = diag / "20260506T120000-100"
        invocation.mkdir(parents=True)
        timing = invocation / "timing.json"
        timing.write_text("{}")
        result = _find_timing_file(None, bindir=str(bindir), diagnostics_dir=str(diag))
        assert result == str(timing)

    def test_auto_detect_nonexistent_diagnostics_dir_returns_none(self, tmp_path, monkeypatch):
        """If diagnostics-dir doesn't exist and no other fallback hits,
        return None rather than raising."""
        monkeypatch.chdir(tmp_path)
        assert _find_timing_file(None, diagnostics_dir=str(tmp_path / "does-not-exist")) is None

    def test_auto_detect_ignores_non_invocation_entries(self, tmp_path, monkeypatch):
        """Stray files or non-matching directories under diagnostics-dir
        must not confuse the lex sort; only entries matching the
        ``YYYYMMDDTHHMMSS-PID`` pattern count."""
        monkeypatch.chdir(tmp_path)
        bindir = tmp_path / "bin"
        diag = bindir / "diagnostics"
        diag.mkdir(parents=True)
        # Stray entries: a plain directory, a non-matching name, a file
        (diag / "tmp").mkdir()
        (diag / "some-other-thing").mkdir()
        (diag / "README.txt").write_text("")
        # The real invocation
        real = diag / "20260506T120000-100"
        real.mkdir()
        (real / "timing.json").write_text("{}")
        result = _find_timing_file(None, bindir=str(bindir))
        assert result == str(real / "timing.json")

    def test_auto_detect_empty_diagnostics_dir_returns_none(self, tmp_path, monkeypatch):
        """An existing-but-empty diagnostics-dir returns None rather than
        a bogus path."""
        monkeypatch.chdir(tmp_path)
        bindir = tmp_path / "bin"
        diag = bindir / "diagnostics"
        diag.mkdir(parents=True)  # empty
        assert _find_timing_file(None, bindir=str(bindir)) is None


class TestMainCLI:
    def test_diagnostics_dir_accepted_on_argv(self, tmp_path, monkeypatch):
        """ct-timing-report's parser must accept --diagnostics-dir on the
        CLI so users / orchestrators can direct the auto-discovery."""
        monkeypatch.chdir(tmp_path)
        # No timing file anywhere -> exits 1, but the parser must accept
        # the flag without erroring out at parse time.
        rc = main(["--summary", "--diagnostics-dir", str(tmp_path / "nonexistent")])
        assert rc == 1

    def test_bindir_accepted_on_argv(self, tmp_path, monkeypatch):
        """ct-timing-report's parser must accept --bindir on the CLI."""
        monkeypatch.chdir(tmp_path)
        rc = main(["--summary", "--bindir", str(tmp_path / "bin")])
        assert rc == 1

    def test_diagnostics_dir_routes_to_correct_file(self, tmp_path, monkeypatch):
        """End-to-end: --diagnostics-dir on the CLI should make ct-timing-report
        find the timing.json under <diag>/<invocation-id>/."""
        monkeypatch.chdir(tmp_path)
        diag = tmp_path / "diag"
        invocation = diag / "20260506T143022-200"
        invocation.mkdir(parents=True)
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.0)
        timer.to_json(str(invocation / "timing.json"))
        rc = main(["--summary", "--diagnostics-dir", str(diag)])
        assert rc == 0


class TestMainSummary:
    def test_summary_with_valid_file(self, tmp_path):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_graph"):
            pass
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 2.0)
        path = str(tmp_path / "timing.json")
        timer.to_json(path)

        rc = main(["--summary", path])
        assert rc == 0

    def test_summary_missing_file(self):
        rc = main(["--summary", "/nonexistent/timing.json"])
        assert rc == 1

    def test_summary_no_file_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        rc = main(["--summary"])
        assert rc == 1


class TestChromeTraceExport:
    def test_export_valid(self, tmp_path):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.5, start_s=0.0, end_s=1.5)
        input_path = str(tmp_path / "timing.json")
        output_path = str(tmp_path / "trace.json")
        timer.to_json(input_path)

        rc = main(["--chrome-trace", output_path, input_path])
        assert rc == 0

        with open(output_path) as f:
            data = json.load(f)
        assert "traceEvents" in data
        assert len(data["traceEvents"]) >= 1

    def test_export_missing_input(self, tmp_path):
        output_path = str(tmp_path / "trace.json")
        rc = main(["--chrome-trace", output_path, "/nonexistent.json"])
        assert rc == 1


class TestComparison:
    def _write_timer(self, tmp_path, name, phases):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        for phase_name, rules in phases.items():
            with timer.phase(phase_name):
                for rule_type, target, source, elapsed in rules:
                    timer.record_rule(rule_type, target, source, elapsed)
        path = str(tmp_path / name)
        timer.to_json(path)
        return path

    def test_comparison_shows_deltas(self, tmp_path):
        before = self._write_timer(
            tmp_path,
            "before.json",
            {
                "build_execution": [
                    ("compile", "a.o", "a.cpp", 5.0),
                    ("link", "app", "", 2.0),
                ],
            },
        )
        after = self._write_timer(
            tmp_path,
            "after.json",
            {
                "build_execution": [
                    ("compile", "a.o", "a.cpp", 3.0),
                    ("link", "app", "", 2.5),
                ],
            },
        )
        rc = main(["--compare", before, after])
        assert rc == 0

    def test_comparison_missing_file(self, tmp_path):
        before = self._write_timer(tmp_path, "before.json", {"build_execution": []})
        rc = main(["--compare", before, "/nonexistent.json"])
        assert rc == 1

    @staticmethod
    def _write_raw(path, total_elapsed_s, phases):
        with open(path, "w") as f:
            json.dump(
                {
                    "version": 1,
                    "timestamp": "2026-04-25T00:00:00+00:00",
                    "total_elapsed_s": total_elapsed_s,
                    "variant": "gcc.debug",
                    "backend": "make",
                    "phases": phases,
                },
                f,
            )

    def test_comparison_new_phase_shows_meaningful_pct(self, tmp_path, capfd):
        # Phase exists in `after` only; previous code silently rendered "+0.0%"
        # because it short-circuited on b_time == 0, hiding the regression.
        before = str(tmp_path / "before.json")
        after = str(tmp_path / "after.json")
        self._write_raw(before, 0.0, [])
        self._write_raw(after, 5.0, [{"name": "build_execution", "elapsed_s": 5.0, "children": []}])
        assert main(["--compare", before, after]) == 0
        rendered = capfd.readouterr().err
        assert "(new)" in rendered, rendered
        assert "+0.0%" not in rendered, rendered

    def test_comparison_zero_delta_phase(self, tmp_path):
        # Phase elapsed of exactly 0 → empty colour style; must not emit "[]…[/]" markup.
        path = str(tmp_path / "same.json")
        self._write_raw(path, 0.0, [{"name": "build_execution", "elapsed_s": 0.0, "children": []}])
        assert main(["--compare", path, path]) == 0


class TestResolveAndLoad:
    def test_returns_timer_and_loaded_path(self, tmp_path):
        """``_resolve_and_load`` must return both the timer and the path it
        loaded from so callers (notably ``_run_tui``) don't have to re-call
        ``_find_timing_file`` and risk a TOCTOU window where a peer
        ct-cake invocation writes a newer diagnostics subdir between the
        two lookups."""
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.0)
        path = str(tmp_path / "timing.json")
        timer.to_json(path)

        class _Args:
            timing_file = path
            objdir = None
            bindir = None
            diagnostics_dir = None

        loaded_timer, loaded_path = _resolve_and_load(_Args())
        assert loaded_timer is not None
        assert loaded_path == path

    def test_failure_returns_none_path(self, tmp_path, monkeypatch):
        """On failure both elements of the tuple are ``None`` so callers can
        unpack unconditionally."""
        monkeypatch.chdir(tmp_path)

        class _Args:
            timing_file = None
            objdir = None
            bindir = None
            diagnostics_dir = None

        loaded_timer, loaded_path = _resolve_and_load(_Args())
        assert loaded_timer is None
        assert loaded_path is None

    def test_tui_uses_path_from_resolve_and_load(self, tmp_path, monkeypatch):
        """Regression: ``_run_tui`` previously called ``_find_timing_file`` a
        second time to recover the path for the TUI title. A peer ct-cake
        could write a newer ``<diagnostics-dir>/<invocation-id>/timing.json``
        between the two lookups, leaving the loaded timer and the displayed
        path pointing at different invocations.

        Simulate that race by monkeypatching ``_find_timing_file`` to return
        DIFFERENT values on consecutive calls and assert the TUI is
        instantiated with the path the timer was actually loaded from
        (i.e. the FIRST value, not a second lookup).
        """
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.0)
        first_path = str(tmp_path / "first" / "timing.json")
        second_path = str(tmp_path / "second" / "timing.json")
        for p in (first_path, second_path):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            timer.to_json(p)

        # Each call to _find_timing_file returns the next value in the queue.
        # If _run_tui calls it twice, the TUI gets the second_path and the
        # timer was loaded from first_path -> mismatch detected by the assert.
        responses = [first_path, second_path]

        def fake_find(*args, **kwargs):
            return responses.pop(0) if responses else None

        monkeypatch.setattr(tr, "_find_timing_file", fake_find)

        captured = {}

        class FakeApp:
            def __init__(self, timer, path):
                captured["timer"] = timer
                captured["path"] = path

            def run(self):
                pass

        # Inject a fake timing_tui module so _run_tui's import succeeds.
        fake_module = types.ModuleType("compiletools.timing_tui")
        fake_module.TimingReportApp = FakeApp  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "compiletools.timing_tui", fake_module)

        class _Args:
            timing_file = None
            objdir = None
            bindir = None
            diagnostics_dir = None

        rc = tr._run_tui(_Args())
        assert rc == 0
        # The TUI must receive the SAME path the timer was loaded from
        # (first_path), proving _find_timing_file was called exactly once.
        assert captured["path"] == first_path
        # And exactly one queue entry should have been consumed.
        assert responses == [second_path]


class TestTUIFallback:
    def test_tui_fallback_when_textual_missing(self, tmp_path, monkeypatch):
        """When textual is not installed, should fall back to summary."""
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.0)
        path = str(tmp_path / "timing.json")
        timer.to_json(path)

        # Mock the import to fail
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "compiletools.timing_tui":
                raise ImportError("textual not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        rc = main([path])
        assert rc == 0


class TestStyled:
    def test_empty_style_returns_plain_text(self):
        # Empty style would produce "[]text[/]" — invalid Rich markup.
        assert _styled("hello", "") == "hello"

    def test_nonempty_style_wraps_in_markup(self):
        assert _styled("+0.50", "red") == "[red]+0.50[/]"


class TestFormatPct:
    def test_normal_positive(self):
        assert _format_pct(b_time=10.0, delta=5.0) == "+50.0%"

    def test_normal_negative(self):
        assert _format_pct(b_time=10.0, delta=-2.5) == "-25.0%"

    def test_new_phase_when_before_is_zero(self):
        # Was: silently "+0.0%" — hides a regression where a new phase was added.
        assert _format_pct(b_time=0.0, delta=5.0) == "(new)"

    def test_zero_before_zero_delta_renders_dash(self):
        assert _format_pct(b_time=0.0, delta=0.0) == "—"

    def test_negative_before_treated_as_no_baseline(self):
        # Defensive: negative b_time shouldn't crash or yield a bogus pct.
        assert _format_pct(b_time=-1.0, delta=5.0) == "(new)"
