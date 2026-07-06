"""Unit tests for the rule cost model + critical-time pass (no compiler)."""

from __future__ import annotations

from compiletools import rule_cost
from compiletools.build_graph import BuildGraph, BuildRule


def _r(output, inputs, rtype):
    return BuildRule(output=output, inputs=inputs, command=["true"], rule_type=rtype)


# --------------------------------------------------------------- cost_key


def test_cost_key_stable_across_output_path():
    a = _r("out/a_HASH1.o", ["src/a.cpp"], "compile")
    b = _r("out/a_HASH2.o", ["src/a.cpp"], "compile")
    assert rule_cost.cost_key(a) == rule_cost.cost_key(b)


def test_cost_key_distinguishes_type_and_input():
    a = _r("a.o", ["src/a.cpp"], "compile")
    b = _r("b.o", ["src/b.cpp"], "compile")
    assert rule_cost.cost_key(a) != rule_cost.cost_key(b)


def test_cost_key_strips_cas_hashes_from_link_first_input():
    """A LINK rule's first input is a cas-objdir path embedding three content
    hashes; the key must survive a content change of that TU (different
    hashes, same basename)."""
    obj_v1 = "cas/ab/main_0123456789ab_0123456789abcd_0123456789abcdef.o"
    obj_v2 = "cas/cd/main_ba9876543210_dcba9876543210_fedcba9876543210.o"
    a = _r("exe/app_k1", [obj_v1, "x.o"], "link")
    b = _r("exe/app_k2", [obj_v2, "x.o"], "link")
    assert rule_cost.cost_key(a) == rule_cost.cost_key(b)
    # And the stripped key retains the discriminating basename.
    assert "main" in rule_cost.cost_key(a)


def test_cost_key_leaves_source_paths_untouched():
    src = "src/dir_0123456789ab/a.cpp"  # hash-ish dir segment, not a CAS .o
    a = _r("a.o", [src], "compile")
    assert rule_cost.cost_key(a).endswith(src)


# ------------------------------------------------------------ persistence


def test_history_round_trip(tmp_path):
    p = str(tmp_path / rule_cost.COST_FILE)
    rule_cost.save_cost_history(p, {"compile\x1fsrc/a.cpp": 3.5})
    assert rule_cost.load_cost_history(p) == {"compile\x1fsrc/a.cpp": 3.5}


def test_corrupt_history_tolerated(tmp_path):
    p = tmp_path / rule_cost.COST_FILE
    p.write_text("{not json")
    assert rule_cost.load_cost_history(str(p)) == {}


def test_missing_history_tolerated(tmp_path):
    assert rule_cost.load_cost_history(str(tmp_path / "nope.json")) == {}


def test_non_numeric_values_dropped(tmp_path):
    p = tmp_path / rule_cost.COST_FILE
    p.write_text('{"a": 1.5, "b": "oops", "c": true}')
    assert rule_cost.load_cost_history(str(p)) == {"a": 1.5}


# --------------------------------------------------------------- estimate


def test_cold_start_ordering():
    hu = _r("h.pcm", ["h.hpp"], "header_unit")
    ln = _r("app", ["a.o"], "link")
    co = _r("a.o", ["a.cpp"], "compile")

    def cost(r):
        return rule_cost.estimate_cost(r, {}, sizeof=lambda _p: 1000)

    assert cost(hu) > cost(ln) > cost(co)


def test_precompile_outputs_get_long_pole_cost():
    """PCH / BMI precompiles are emitted as rule_type='compile' with
    .gch/.pcm/.gcm outputs; the cold heuristic must classify them as the
    header_unit long pole, not as a source-size-scaled compile."""
    plain = _r("a.o", ["a.cpp"], "compile")
    for ext in (".gch", ".pcm", ".gcm"):
        pre = _r(f"cache/deadbeef/wrap.h{ext}", ["wrap.h"], "compile")
        cost = rule_cost.estimate_cost(pre, {}, sizeof=lambda _p: 1000)
        assert cost == rule_cost._COLD_BASE["header_unit"], ext
        assert cost > rule_cost.estimate_cost(plain, {}, sizeof=lambda _p: 1000)


def test_precompile_exts_match_build_backend():
    """Drift guard: the suffix set here must mirror the artefact-ext set the
    rule emitters in build_backend use."""
    from compiletools.build_backend import _BMI_PCH_ARTEFACT_EXTS

    assert set(rule_cost._PRECOMPILE_OUTPUT_EXTS) == set(_BMI_PCH_ARTEFACT_EXTS)


def test_learned_history_still_overrides_precompile_heuristic():
    pre = _r("cache/deadbeef/wrap.h.gch", ["wrap.h"], "compile")
    key = rule_cost.cost_key(pre)
    assert rule_cost.estimate_cost(pre, {key: 12.5}, sizeof=lambda _p: 1) == 12.5


def test_save_caps_entries_preferring_current_build(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_cost, "_MAX_COST_ENTRIES", 5)
    p = str(tmp_path / rule_cost.COST_FILE)
    hist = {f"compile\x1fsrc/f{i}.cpp": float(i) for i in range(10)}
    current = {"compile\x1fsrc/f7.cpp", "compile\x1fsrc/f9.cpp"}
    rule_cost.save_cost_history(p, hist, prefer=current)
    loaded = rule_cost.load_cost_history(p)
    assert len(loaded) == 5
    assert current <= set(loaded)  # this build's keys survive the trim


def test_history_overrides_cold_start():
    co = _r("a.o", ["a.cpp"], "compile")
    key = rule_cost.cost_key(co)
    assert rule_cost.estimate_cost(co, {key: 99.0}, sizeof=lambda p: 1) == 99.0


def test_compile_cost_scales_with_source_size():
    small = _r("s.o", ["s.cpp"], "compile")
    big = _r("b.o", ["b.cpp"], "compile")
    cost_small = rule_cost.estimate_cost(small, {}, sizeof=lambda p: 1_000)
    cost_big = rule_cost.estimate_cost(big, {}, sizeof=lambda p: 5_000_000)
    assert cost_big > cost_small


def test_estimate_tolerates_missing_source():
    co = _r("a.o", ["a.cpp"], "compile")

    def boom(_p):
        raise OSError("gone")

    # Falls back to the base compile cost rather than raising.
    assert rule_cost.estimate_cost(co, {}, sizeof=boom) == 2.0


# ----------------------------------------------------------- dependents map


def test_dependents_map_from_inputs():
    g = BuildGraph()
    g.add_rule(_r("a.o", ["a.cpp"], "compile"))
    g.add_rule(_r("app", ["a.o"], "link"))
    dm = rule_cost.build_dependents_map(g)
    assert dm.get("a.o") == ["app"]
    assert "a.cpp" not in dm  # leaf, not a rule output


# ------------------------------------------------------------ critical time


def test_critical_time_chain():
    # a.cpp -> a.o -> app ; crit(a.o) = cost(a.o) + cost(app)
    g = BuildGraph()
    g.add_rule(_r("a.o", ["a.cpp"], "compile"))
    g.add_rule(_r("app", ["a.o"], "link"))
    crit = rule_cost.compute_critical_times(g, lambda r: 1.0)
    assert crit["app"] == 1.0
    assert crit["a.o"] == 2.0


def test_critical_time_diamond():
    # top feeds a.o and b.o; both feed app. Longest path is top->{a|b}->app.
    g = BuildGraph()
    g.add_rule(_r("top.pcm", ["top.hpp"], "header_unit"))
    g.add_rule(_r("a.o", ["top.pcm"], "compile"))
    g.add_rule(_r("b.o", ["top.pcm"], "compile"))
    g.add_rule(_r("app", ["a.o", "b.o"], "link"))
    crit = rule_cost.compute_critical_times(g, lambda r: 1.0)
    assert crit["app"] == 1.0
    assert crit["a.o"] == 2.0
    assert crit["top.pcm"] == 3.0  # top + one compile + link


def test_critical_time_long_pole_wins():
    # Wide fan-out of cheap compiles + one long pole (header_unit) all feeding
    # the final link. The long pole must have the highest critical time.
    g = BuildGraph()
    g.add_rule(_r("app", ["pole.pcm", "c0.o", "c1.o", "c2.o"], "link"))
    g.add_rule(_r("pole.pcm", ["pole.hpp"], "header_unit"))
    for i in range(3):
        g.add_rule(_r(f"c{i}.o", [f"c{i}.cpp"], "compile"))

    def cost(r):
        return rule_cost.estimate_cost(r, {}, sizeof=lambda _p: 1000)

    crit = rule_cost.compute_critical_times(g, cost)
    assert crit["pole.pcm"] == max(crit.values())
    assert crit["pole.pcm"] > crit["c0.o"]


def test_critical_time_cycle_guard():
    g = BuildGraph()
    g.add_rule(_r("a", ["b"], "compile"))
    g.add_rule(_r("b", ["a"], "compile"))
    crit = rule_cost.compute_critical_times(g, lambda r: 1.0)  # must not recurse forever
    assert set(crit) == {"a", "b"}


# ---------------------------------------------------------------------------
# Scheduling micro-benchmark (in-repo, IP-free): a synthetic DAG demonstration
# that under a constrained PriorityGate ordered by critical time, the planted
# long pole is dispatched ahead of the cheap fan-out. Pure ordering/correctness
# demonstration -- no compiler, no real files, no measured numbers.
# ---------------------------------------------------------------------------


def test_scheduling_micro_benchmark_long_pole_first():
    import asyncio

    from compiletools.priority_gate import PriorityGate

    g = BuildGraph()
    inputs = ["pole.pcm"] + [f"c{i}.o" for i in range(8)]
    g.add_rule(_r("app", inputs, "link"))
    g.add_rule(_r("pole.pcm", ["pole.hpp"], "header_unit"))
    for i in range(8):
        g.add_rule(_r(f"c{i}.o", [f"c{i}.cpp"], "compile"))

    crit = rule_cost.compute_critical_times(g, lambda r: rule_cost.estimate_cost(r, {}, sizeof=lambda p: 1000))

    async def main():
        gate = PriorityGate(1)
        dispatched: list[str] = []
        # Discovery order deliberately puts a cheap compile FIRST and the long
        # pole second, then the rest. The first coroutine grabs the single free
        # slot immediately (no priority involved); every later rule parks on the
        # gate. When the first slot frees, the gate must hand it to the highest
        # critical-time waiter -- the long pole -- not the FIFO-next compile.
        ready = [g.get_rule("c0.o"), g.get_rule("pole.pcm")]
        ready += [g.get_rule(f"c{i}.o") for i in range(1, 8)]

        async def run(rule):
            await gate.acquire(crit[rule.output])
            dispatched.append(rule.output)
            await asyncio.sleep(0)  # yield so ordering reflects priority, not code order
            gate.release()

        await asyncio.gather(*(run(r) for r in ready))
        return dispatched

    dispatched = asyncio.run(asyncio.wait_for(main(), 5.0))
    assert dispatched[0] == "c0.o", dispatched  # grabbed the immediate free slot
    assert dispatched[1] == "pole.pcm", dispatched  # long pole wins the next slot by priority
