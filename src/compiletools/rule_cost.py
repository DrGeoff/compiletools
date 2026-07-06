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
import re

import compiletools.wrappedos
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

# PCH / BMI precompile rules are emitted with rule_type="compile" (see
# build_backend._create_pch_rules and the clang header-unit / module-interface
# rules), so the "header_unit" _COLD_BASE row alone would miss the classic
# 39-second PCH long pole on a cold cache. Recognise them by output suffix.
# Mirrors build_backend._BMI_PCH_ARTEFACT_EXTS (drift-guarded by
# test_precompile_exts_match_build_backend).
_PRECOMPILE_OUTPUT_EXTS = (".gch", ".pcm", ".gcm")

# CAS object paths embed content hashes twice:
# ``<objdir>/<file_hash[:2]>/{basename}_{file_hash_12}_{dep_hash_14}_{macro_state_hash_16}.o``.
# A LINK rule's first input is such a path, so keying on it verbatim would
# invalidate the link's learned cost whenever the first TU's content changes
# (and leak one dead sidecar entry per edit). Strip the hash triplet AND the
# 2-char bucket dir so the key survives content changes; compile-rule first
# inputs are source paths the basename pattern can never match.
_CAS_OBJ_HASHES_RE = re.compile(r"_[0-9a-f]{12}_[0-9a-f]{14}_[0-9a-f]{16}(\.o)$")


def _strip_cas_obj_hashes(path: str) -> str:
    # The dirname(dirname(...)) hard-codes the one-level ``<objdir>/<hash[:2]>/``
    # bucketing from namer.object_dir (drift shifts keys => one-time cold cost,
    # never a correctness issue). Two TUs with the same basename in different
    # source dirs share a link cost key when first in the object list --
    # acceptable blur for a scheduling heuristic.
    base = compiletools.wrappedos.basename(path)
    stripped = _CAS_OBJ_HASHES_RE.sub(r"\1", base)
    if stripped == base:
        return path
    bucket_parent = compiletools.wrappedos.dirname(compiletools.wrappedos.dirname(path))
    return compiletools.wrappedos.join(bucket_parent, stripped) if bucket_parent else stripped


# Cap on persisted entries so renamed/deleted sources cannot grow the sidecar
# without bound. Generous vs the ~500-rule builds this schedules; enforcement
# prefers the keys observed by the current build (see save_cost_history).
_MAX_COST_ENTRIES = 50_000


def cost_key(rule: BuildRule) -> str:
    """Stable identity: ``(rule_type, first_input)``. First input is the source
    for compiles and a good discriminator for links; output paths are unstable
    (they encode content hashes), so they are deliberately not used. CAS hash
    segments in an object-path first input (LINK rules) are stripped so the
    key also survives content changes of the first TU."""
    first = rule.inputs[0] if rule.inputs else ""
    first = _strip_cas_obj_hashes(first)
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


def save_cost_history(path: str, hist: dict[str, float], *, prefer: set[str] | frozenset[str] = frozenset()) -> None:
    """Atomically write the cost sidecar. Best-effort: swallows OSError/ValueError.

    Entries are capped at ``_MAX_COST_ENTRIES`` so keys for renamed or deleted
    sources cannot grow the file without bound. When trimming, keys in
    ``prefer`` (the current build's observed rules) are kept first — all of
    them, even if ``prefer`` alone exceeds the cap (they are this build's real
    rules, not stale growth); the remainder fills in insertion order."""
    try:
        from compiletools.filesystem_utils import atomic_output_file

        if len(hist) > _MAX_COST_ENTRIES:
            trimmed = {k: hist[k] for k in prefer if k in hist}
            for k, v in hist.items():
                if len(trimmed) >= _MAX_COST_ENTRIES:
                    break
                trimmed.setdefault(k, v)
            hist = trimmed
        with atomic_output_file(path, mode="w", encoding="utf-8") as f:
            json.dump(hist, f, sort_keys=True)
    except (OSError, ValueError):
        pass


def estimate_cost(rule: BuildRule, history: dict[str, float], *, sizeof=compiletools.wrappedos.getsize) -> float:
    """Learned cost if seen before, else a type-ordered cold-start heuristic.
    PCH/BMI precompiles (COMPILE rules with ``.gch``/``.pcm``/``.gcm`` outputs)
    get the header_unit long-pole cost; other compile costs scale with source
    size so heavyweight TUs sort ahead of trivial ones. ``sizeof`` is
    injectable for tests (default is the stat-cached ``wrappedos.getsize`` —
    inputs here are pre-existing source files, never build outputs)."""
    hit = history.get(cost_key(rule))
    if hit is not None:
        return hit
    if rule.rule_type == "compile" and rule.output.endswith(_PRECOMPILE_OUTPUT_EXTS):
        return _COLD_BASE["header_unit"]
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
