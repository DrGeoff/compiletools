"""Tests for build_timer module."""

from __future__ import annotations

import threading
import time

import pytest

from compiletools.build_timer import BuildTimer, TimingEvent, _classify_output


# ------------------------------------------------------------------ TimingEvent


class TestTimingEvent:
    def test_elapsed_with_end(self):
        ev = TimingEvent(name="x", category="compile", start_s=1.0, end_s=3.5)
        assert ev.elapsed_s == pytest.approx(2.5)

    def test_elapsed_without_end_uses_monotonic(self):
        ev = TimingEvent(name="x", category="compile", start_s=time.monotonic() - 1.0)
        assert ev.elapsed_s >= 1.0

    def test_to_dict_phase(self):
        ev = TimingEvent(name="build_graph", category="phase", start_s=0.0, end_s=1.5)
        d = ev.to_dict()
        assert d["name"] == "build_graph"
        assert d["elapsed_s"] == pytest.approx(1.5)
        assert "rule_type" not in d

    def test_to_dict_rule(self):
        ev = TimingEvent(
            name="foo.o",
            category="compile",
            start_s=0.0,
            end_s=2.0,
            target="obj/foo.o",
            source="src/foo.cpp",
        )
        d = ev.to_dict()
        assert d["rule_type"] == "compile"
        assert d["target"] == "obj/foo.o"
        assert d["source"] == "src/foo.cpp"
        assert d["start_s"] == pytest.approx(0.0)
        assert d["end_s"] == pytest.approx(2.0)

    def test_to_dict_with_children(self):
        child = TimingEvent(name="child", category="compile", start_s=0.0, end_s=1.0, target="a.o", source="a.cpp")
        parent = TimingEvent(name="build_execution", category="phase", start_s=0.0, end_s=2.0, children=[child])
        d = parent.to_dict()
        assert "rules" in d
        assert len(d["rules"]) == 1
        assert d["rules"][0]["name"] == "child"

    def test_roundtrip(self):
        ev = TimingEvent(
            name="foo.o",
            category="compile",
            start_s=1.0,
            end_s=3.0,
            target="obj/foo.o",
            source="src/foo.cpp",
            metadata={"weight": 5},
        )
        d = ev.to_dict()
        restored = TimingEvent.from_dict(d)
        assert restored.name == "foo.o"
        assert restored.category == "compile"
        assert restored.elapsed_s == pytest.approx(2.0)
        assert restored.target == "obj/foo.o"
        assert restored.source == "src/foo.cpp"


# ---------------------------------------------------------- BuildTimer disabled


class TestBuildTimerDisabled:
    def test_phase_is_noop(self):
        timer = BuildTimer(enabled=False)
        with timer.phase("test"):
            pass
        # Root should have no children
        assert len(timer._root.children) == 0

    def test_record_rule_is_noop(self):
        timer = BuildTimer(enabled=False)
        timer.record_rule("compile", "a.o", "a.cpp", 1.0)
        assert len(timer._root.children) == 0

    def test_to_dict_empty(self):
        timer = BuildTimer(enabled=False)
        d = timer.to_dict()
        assert d["version"] == 1
        assert d["phases"] == []

    def test_record_rules_from_ninja_log_noop(self, tmp_path):
        log = tmp_path / ".ninja_log"
        log.write_text("# ninja log v5\n0\t1000\t0\ta.o\thash\n")
        timer = BuildTimer(enabled=False)
        timer.record_rules_from_ninja_log(str(log))
        assert len(timer._root.children) == 0

    def test_record_rules_from_make_timing_noop(self, tmp_path):
        log = tmp_path / ".ct-make-timing.jsonl"
        log.write_text('{"target":"a.o","start_ns":0,"end_ns":1000000000}\n')
        timer = BuildTimer(enabled=False)
        timer.record_rules_from_make_timing(str(log))
        assert len(timer._root.children) == 0


# ----------------------------------------------------------- BuildTimer enabled


class TestBuildTimerEnabled:
    def test_phase_records_elapsed(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("test_phase"):
            time.sleep(0.01)
        assert len(timer._root.children) == 1
        phase = timer._root.children[0]
        assert phase.name == "test_phase"
        assert phase.category == "phase"
        assert phase.elapsed_s >= 0.01

    def test_nested_phases(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("outer"):
            with timer.phase("inner"):
                time.sleep(0.01)
        assert len(timer._root.children) == 1
        outer = timer._root.children[0]
        assert outer.name == "outer"
        assert len(outer.children) == 1
        inner = outer.children[0]
        assert inner.name == "inner"
        assert inner.elapsed_s >= 0.01

    def test_record_rule_adds_to_current_phase(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 1.5)
        phase = timer._root.children[0]
        assert len(phase.children) == 1
        rule = phase.children[0]
        assert rule.category == "compile"
        assert rule.target == "a.o"
        assert rule.source == "a.cpp"

    def test_record_rule_thread_safety(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            threads = []
            for i in range(20):
                t = threading.Thread(
                    target=timer.record_rule,
                    args=("compile", f"obj/{i}.o", f"src/{i}.cpp", 0.1 * i),
                )
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
        phase = timer._root.children[0]
        assert len(phase.children) == 20

    def test_to_dict_structure(self):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
        with timer.phase("build_graph"):
            time.sleep(0.01)
        d = timer.to_dict()
        assert d["version"] == 1
        assert d["variant"] == "gcc.debug"
        assert d["backend"] == "ninja"
        assert d["total_elapsed_s"] > 0
        assert len(d["phases"]) == 1
        assert d["phases"][0]["name"] == "build_graph"

    def test_to_json_roundtrip(self, tmp_path):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 2.5, start_s=0.0, end_s=2.5)
            timer.record_rule("link", "app", "", 1.0, start_s=2.5, end_s=3.5)
        path = str(tmp_path / "timing.json")
        timer.to_json(path)

        loaded = BuildTimer.from_json(path)
        assert loaded.variant == "gcc.debug"
        assert loaded.backend == "make"
        assert len(loaded._root.children) == 1
        phase = loaded._root.children[0]
        assert phase.name == "build_execution"
        assert len(phase.children) == 2

    def test_from_dict_roundtrip(self):
        timer = BuildTimer(enabled=True, variant="clang.release", backend="ninja")
        with timer.phase("generate"):
            pass
        with timer.phase("build_execution"):
            timer.record_rule("compile", "x.o", "x.cpp", 3.0)

        d = timer.to_dict()
        restored = BuildTimer.from_dict(d)
        assert restored.variant == "clang.release"
        assert restored.backend == "ninja"
        d2 = restored.to_dict()
        assert len(d2["phases"]) == len(d["phases"])

    def test_finish_sets_end(self):
        timer = BuildTimer(enabled=True)
        assert timer._root.end_s is None
        timer.finish()
        assert timer._root.end_s is not None

    def test_finish_idempotent(self):
        timer = BuildTimer(enabled=True)
        timer.finish()
        end1 = timer._root.end_s
        timer.finish()
        assert timer._root.end_s == end1


# -------------------------------------------------------- ninja log parsing


class TestNinjaLogParsing:
    def _write_log(self, tmp_path, lines, header=True):
        log = tmp_path / ".ninja_log"
        content = ""
        if header:
            content = "# ninja log v5\n"
        content += "\n".join(lines) + "\n"
        log.write_text(content)
        return str(log)

    def test_parse_valid_log(self, tmp_path):
        log = self._write_log(tmp_path, [
            "0\t1500\t12345\tobj/foo.o\tabc123",
            "100\t2000\t12346\tobj/bar.o\tdef456",
        ])
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(log)
        phase = timer._root.children[0]
        assert len(phase.children) == 2
        # Check first rule
        rules_by_target = {r.target: r for r in phase.children}
        foo = rules_by_target["obj/foo.o"]
        assert foo.elapsed_s == pytest.approx(1.5)
        assert foo.category == "compile"
        bar = rules_by_target["obj/bar.o"]
        assert bar.elapsed_s == pytest.approx(1.9)

    def test_parse_log_with_comments(self, tmp_path):
        log = self._write_log(tmp_path, [
            "# some comment",
            "0\t1000\t0\ta.o\thash",
        ])
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(log)
        assert len(timer._root.children[0].children) == 1

    def test_parse_log_with_offset(self, tmp_path):
        log_path = tmp_path / ".ninja_log"
        old_content = "# ninja log v5\n0\t1000\t0\told.o\thash\n"
        new_content = "0\t2000\t0\tnew.o\thash\n"
        log_path.write_text(old_content + new_content)
        offset = len(old_content)

        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(str(log_path), offset=offset)
        phase = timer._root.children[0]
        assert len(phase.children) == 1
        assert phase.children[0].target == "new.o"

    def test_parse_empty_log(self, tmp_path):
        log = self._write_log(tmp_path, [])
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(log)
        assert len(timer._root.children[0].children) == 0

    def test_parse_duplicate_outputs_keeps_last(self, tmp_path):
        log = self._write_log(tmp_path, [
            "0\t1000\t0\tobj/foo.o\thash1",
            "1000\t3000\t0\tobj/foo.o\thash2",
        ])
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(log)
        phase = timer._root.children[0]
        assert len(phase.children) == 1
        assert phase.children[0].elapsed_s == pytest.approx(2.0)

    def test_parse_nonexistent_log(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log("/nonexistent/.ninja_log")
        assert len(timer._root.children[0].children) == 0

    def test_parse_with_graph_lookup(self, tmp_path):
        from compiletools.build_graph import BuildGraph, BuildRule

        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="obj/foo.o",
            inputs=["src/foo.cpp", "src/foo.h"],
            command=["g++", "-c", "src/foo.cpp", "-o", "obj/foo.o"],
            rule_type="compile",
        ))
        graph.add_rule(BuildRule(
            output="bin/app",
            inputs=["obj/foo.o"],
            command=["g++", "obj/foo.o", "-o", "bin/app"],
            rule_type="link",
        ))

        log = self._write_log(tmp_path, [
            "0\t5000\t0\tobj/foo.o\thash1",
            "5000\t6000\t0\tbin/app\thash2",
        ])
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_ninja_log(log, graph=graph)
        phase = timer._root.children[0]
        rules = {r.target: r for r in phase.children}
        assert rules["obj/foo.o"].source == "src/foo.cpp"
        assert rules["obj/foo.o"].category == "compile"
        assert rules["bin/app"].category == "link"


# ------------------------------------------------------- make timing parsing


class TestMakeTimingParsing:
    def test_parse_valid_jsonl(self, tmp_path):
        log = tmp_path / ".ct-make-timing.jsonl"
        log.write_text(
            '{"target":"obj/foo.o","start_ns":1000000000,"end_ns":3500000000}\n'
            '{"target":"bin/app","start_ns":3500000000,"end_ns":4000000000}\n'
        )
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_make_timing(str(log))
        phase = timer._root.children[0]
        assert len(phase.children) == 2
        rules = {r.target: r for r in phase.children}
        assert rules["obj/foo.o"].elapsed_s == pytest.approx(2.5)
        assert rules["bin/app"].elapsed_s == pytest.approx(0.5)

    def test_parse_with_graph(self, tmp_path):
        from compiletools.build_graph import BuildGraph, BuildRule

        graph = BuildGraph()
        graph.add_rule(BuildRule(
            output="obj/foo.o",
            inputs=["src/foo.cpp"],
            command=["g++", "-c", "src/foo.cpp"],
            rule_type="compile",
        ))

        log = tmp_path / ".ct-make-timing.jsonl"
        log.write_text('{"target":"obj/foo.o","start_ns":0,"end_ns":2000000000}\n')
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_make_timing(str(log), graph=graph)
        rule = timer._root.children[0].children[0]
        assert rule.source == "src/foo.cpp"
        assert rule.category == "compile"

    def test_parse_invalid_json_skipped(self, tmp_path):
        log = tmp_path / ".ct-make-timing.jsonl"
        log.write_text(
            'not json\n'
            '{"target":"a.o","start_ns":0,"end_ns":1000000000}\n'
        )
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_make_timing(str(log))
        assert len(timer._root.children[0].children) == 1

    def test_parse_nonexistent_log(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rules_from_make_timing("/nonexistent/log.jsonl")
        assert len(timer._root.children[0].children) == 0


# --------------------------------------------------------- chrome trace


class TestChromeTrace:
    def test_format(self):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="ninja")
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 2.0, start_s=0.0, end_s=2.0)
        events = timer.to_chrome_trace()
        assert len(events) >= 2  # root + phase + rule
        # All events should have required Chrome trace fields
        for ev in events:
            assert "name" in ev
            assert "ph" in ev
            assert ev["ph"] == "X"
            assert "ts" in ev
            assert "dur" in ev
            assert "pid" in ev
            assert "tid" in ev

    def test_rule_has_args(self):
        timer = BuildTimer(enabled=True)
        with timer.phase("build_execution"):
            timer.record_rule("compile", "obj/foo.o", "src/foo.cpp", 1.0)
        events = timer.to_chrome_trace()
        compile_events = [e for e in events if e["name"] == "foo.cpp"]
        assert len(compile_events) == 1
        assert compile_events[0]["args"]["target"] == "obj/foo.o"
        assert compile_events[0]["args"]["source"] == "src/foo.cpp"


# ----------------------------------------------------------- summary table


class TestSummaryTable:
    def test_returns_rich_table(self):
        timer = BuildTimer(enabled=True, variant="gcc.debug", backend="make")
        with timer.phase("build_graph"):
            pass
        with timer.phase("build_execution"):
            timer.record_rule("compile", "a.o", "a.cpp", 2.0)
            timer.record_rule("link", "app", "", 1.0)
        table = timer.summary_table()
        assert table is not None
        assert "Build Timing Report" in table.title

    def test_disabled_timer_summary(self):
        timer = BuildTimer(enabled=False)
        table = timer.summary_table()
        # Should still produce a table (just empty phases)
        assert table is not None


# ------------------------------------------------------------- classify


class TestClassifyOutput:
    def test_object_file(self):
        assert _classify_output("obj/foo.o") == "compile"
        assert _classify_output("foo.obj") == "compile"

    def test_static_library(self):
        assert _classify_output("lib/libfoo.a") == "static_library"

    def test_shared_library(self):
        assert _classify_output("lib/libfoo.so") == "shared_library"
        assert _classify_output("lib/libfoo.dylib") == "shared_library"
        assert _classify_output("lib/foo.dll") == "shared_library"

    def test_executable(self):
        assert _classify_output("bin/myapp") == "link"
