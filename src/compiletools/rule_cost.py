"""Best-effort per-rule cost model + critical-time scheduling weights.

The Shake scheduler orders ready rules by *critical time* -- the longest
remaining chain of work from a rule to the final target. Costs are learned from
observed ``elapsed_s`` (persisted next to the trace store) and fall back to a
type-ordered heuristic on first sight. Everything here is best-effort: any
failure yields empty data, which the caller treats as uniform priority 0
(today's FIFO). Cost data must never fail a build.
"""

from __future__ import annotations

import json
import os

from compiletools.build_graph import BuildGraph, BuildRule

COST_FILE = ".ct-rule-costs.json"

# Cold-start base seconds by rule type. Only the ORDERING matters (it decides
# which ready rule starts first); the magnitudes are deliberately coarse.
_COLD_BASE: dict[str, float] = {
    "header_unit": 40.0,  # PCH / BMI precompile -- the classic long pole
    "link": 8.0,
    "shared_library": 8.0,
    "static_library": 8.0,
    "compile": 2.0,
    "test": 1.0,
}
_KEY_SEP = "\x1f"


def cost_key(rule: BuildRule) -> str:
    """Stable identity: ``(rule_type, first_input)``. First input is the source
    for compiles and a good discriminator for links; output paths are unstable
    (they encode content hashes), so they are deliberately not used."""
    first = rule.inputs[0] if rule.inputs else ""
    return f"{rule.rule_type}{_KEY_SEP}{first}"


def load_cost_history(path: str) -> dict[str, float]:
    """Load the JSON cost sidecar. Missing or corrupt -> empty dict, never
    raises. Non-numeric values are dropped."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        if isinstance(v, bool):
            continue  # bool is an int subclass; a cost is never a bool
        if isinstance(v, (int, float)):
            out[str(k)] = float(v)
    return out


def save_cost_history(path: str, hist: dict[str, float]) -> None:
    """Atomically write the cost sidecar. Best-effort: swallows OSError/ValueError."""
    try:
        from compiletools.filesystem_utils import atomic_output_file

        with atomic_output_file(path, mode="w", encoding="utf-8") as f:
            json.dump(hist, f, sort_keys=True)
    except (OSError, ValueError):
        pass


def estimate_cost(rule: BuildRule, history: dict[str, float], *, sizeof=os.path.getsize) -> float:
    """Learned cost if seen before, else a type-ordered cold-start heuristic.
    Compile cost is scaled by source size so heavyweight TUs sort ahead of
    trivial ones. ``sizeof`` is injectable for tests."""
    hit = history.get(cost_key(rule))
    if hit is not None:
        return hit
    base = _COLD_BASE.get(rule.rule_type, 1.0)
    if rule.rule_type == "compile" and rule.inputs:
        try:
            base = 2.0 + sizeof(rule.inputs[0]) / 50_000.0
        except OSError:
            pass
    return base


def build_dependents_map(graph: BuildGraph) -> dict[str, list[str]]:
    """``output -> [outputs of rules that consume it]``. Only rule-producing
    inputs are edges; leaf inputs (source/header files) are skipped. BuildGraph
    has no reverse map, so build one in a single pass over inputs."""
    dependents: dict[str, list[str]] = {}
    for rule in graph.rules:
        for inp in rule.inputs:
            if graph.get_rule(inp) is not None:
                dependents.setdefault(inp, []).append(rule.output)
    return dependents


def compute_critical_times(graph: BuildGraph, cost_fn) -> dict[str, float]:
    """``crit(rule) = cost(rule) + max(crit(dependents), default 0)``, memoised.

    O(V+E). Cycle-guarded (a back-edge contributes 0) so a malformed graph
    cannot recurse forever. Returns a dict keyed by rule output."""
    dependents = build_dependents_map(graph)
    crit: dict[str, float] = {}

    def visit(output: str, stack: set[str]) -> float:
        cached = crit.get(output)
        if cached is not None:
            return cached
        if output in stack:
            return 0.0  # cycle guard
        rule = graph.get_rule(output)
        cost = cost_fn(rule) if rule is not None else 0.0
        stack.add(output)
        best = 0.0
        for dep in dependents.get(output, ()):
            best = max(best, visit(dep, stack))
        stack.discard(output)
        crit[output] = cost + best
        return crit[output]

    for rule in graph.rules:
        visit(rule.output, set())
    return crit
