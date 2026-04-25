"""Tests for timing_report module."""

from __future__ import annotations

import json

from compiletools.build_timer import BuildTimer
from compiletools.timing_report import _find_timing_file, _format_pct, _styled, main


class TestFindTimingFile:
    def test_explicit_path(self):
        assert _find_timing_file("/some/path.json") == "/some/path.json"

    def test_auto_detect_cwd(self, tmp_path, monkeypatch):
        timing = tmp_path / ".ct-timing.json"
        timing.write_text("{}")
        monkeypatch.chdir(tmp_path)
        assert _find_timing_file(None) == ".ct-timing.json"

    def test_auto_detect_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _find_timing_file(None) is None

    def test_auto_detect_objdir(self, tmp_path, monkeypatch):
        """Fix 6: users with ``--objdir=shared-objdir/...`` previously got
        false 'no timing file found'.  ``_find_timing_file`` must accept
        an ``objdir`` argument and search there too."""
        monkeypatch.chdir(tmp_path)
        objdir = tmp_path / "shared-objdir" / "myproject"
        objdir.mkdir(parents=True)
        timing = objdir / ".ct-timing.json"
        timing.write_text("{}")
        # cwd has no .ct-timing.json, but objdir does
        result = _find_timing_file(None, objdir=str(objdir))
        assert result == str(timing)

    def test_auto_detect_objdir_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        objdir = tmp_path / "shared-objdir"
        objdir.mkdir()
        assert _find_timing_file(None, objdir=str(objdir)) is None


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
