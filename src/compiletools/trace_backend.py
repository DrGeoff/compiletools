"""Verifying-traces build backend: Shake (local threads).

Implements the Shake rebuild strategy from "Build Systems à la Carte"
(Mokhov, Mitchell, Jones 2018), specifically:
- Suspending scheduler: build dependencies on-demand recursively
- Verifying traces: content-hash-based change detection for minimal rebuilds
- Early cutoff: if rebuilt output is byte-identical, skip rebuilding dependents

Content-addressable short-circuit: compile rules produce output filenames that
encode source hash, dependency hash, and macro state hash.  If such an output
already exists on disk it is correct by construction, so verifying traces
degenerates to a single os.path.exists() call — skipping all hashing, trace
lookup, and input comparison for no-op rebuilds.

ShakeBackend drives compilation directly from Python using asyncio coroutines
with a PriorityGate limiting subprocess concurrency — ready rules are admitted
highest critical time first (see rule_cost.py), not FIFO.

The dependency graph is static (pre-computed by Hunter), not dynamic as in the
original Shake (which uses monadic tasks for dynamic dependency discovery).
This is sufficient because compiletools resolves all dependencies at a higher
level before the backend executes.

No external build tool required — compilation is driven directly from Python.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

import compiletools.apptools
import compiletools.filesystem_utils
import compiletools.git_utils
import compiletools.wrappedos
from compiletools import rule_cost
from compiletools.build_backend import (
    _ORDER_ONLY_DEP_FORBIDDEN_EXTS,
    BuildBackend,
    _compiler_identity,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule, RuleType
from compiletools.build_timer import _cas_kind_for_rule_type
from compiletools.global_hash_registry import get_file_hash
from compiletools.locking import (
    _run_child_async,
    execute_compile_rule,
    execute_compile_rule_async,
    execute_link_rule,
    execute_link_rule_async,
)
from compiletools.priority_gate import PriorityGate

logger = logging.getLogger(__name__)

TRACE_VERSION = 1


@dataclass
class TraceEntry:
    """Record of a successful build action for verifying traces."""

    output_hash: str
    input_hashes: dict[str, str]
    command_hash: str


class TraceStore:
    """Persistent store for build traces, backed by JSON on disk."""

    def __init__(self, path: str):
        self._path = path
        self._traces: dict[str, TraceEntry] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("trace store %s is corrupt (%s); discarding", self._path, e)
            self._traces = {}
            return
        if not isinstance(data, dict) or data.get("version") != TRACE_VERSION:
            return
        for output, entry_dict in data.get("traces", {}).items():
            try:
                self._traces[output] = TraceEntry(**entry_dict)
            except (KeyError, TypeError) as e:
                logger.warning("dropping corrupt trace entry for %s: %s", output, e)

    def get(self, output: str) -> TraceEntry | None:
        return self._traces.get(output)

    def put(self, output: str, entry: TraceEntry) -> None:
        self._traces[output] = entry

    def save(self) -> None:
        data = {
            "version": TRACE_VERSION,
            "traces": {output: asdict(entry) for output, entry in self._traces.items()},
        }
        # force_mode=0o666: the store lives in a shared CAS pool cell, and a
        # first-creator with a restrictive umask would silently lock peers out
        # of the trace history (same rationale as locking.py's explicit fchmod).
        with compiletools.filesystem_utils.atomic_output_file(
            self._path, mode="w", encoding="utf-8", force_mode=0o666
        ) as f:
            json.dump(data, f, indent=2, sort_keys=True)


def _canonicalize_cmd_for_hash(cmd: list[str], anchor_root: str) -> list[str]:
    """Anchor-relative every path token in *cmd* for stable cross-workspace hashing.

    Two passes are needed because path-bearing flags appear in two forms:
    attached (``-I/abs/path``) and bare-positional (``-c /abs/path/foo.cpp``,
    ``/abs/path/foo.o``). ``canonicalize_for_cache_key`` handles the
    flag-attached forms (recognises ``-I``/``-isystem``/``-iquote``/``-include``
    /``-include-pch``/``-F``/``-B``/``-idirafter``); the second pass then
    rewrites any remaining bare absolute-path tokens that live under
    *anchor_root*. Tokens outside *anchor_root* and non-path tokens pass
    through unchanged in both passes.
    """
    if not anchor_root:
        return list(cmd)
    flag_canonicalized = compiletools.apptools.canonicalize_for_cache_key(list(cmd), anchor_root)
    return compiletools.apptools.canonicalize_paths_for_cache_key(flag_canonicalized, anchor_root)


def hash_command(cmd: list[str], compiler_identity: str | None = None) -> str:
    """Compute a stable hash of a shell command list.

    *compiler_identity* folds in the resolved binary's realpath + size + mtime
    for the tool that runs the command, so an in-place compiler upgrade
    invalidates traces even when the argv is byte-identical.

    Callers wanting cross-workspace stability must pre-canonicalize *cmd*
    via ``_canonicalize_cmd_for_hash`` so absolute paths embedded in the
    argv (-c <src>, -o <out>, -I/abs/path, ...) become anchor-relative.
    """
    payload = [compiler_identity, cmd] if compiler_identity is not None else cmd
    return hashlib.sha256(json.dumps(payload, sort_keys=False).encode()).hexdigest()


def _is_build_artifact(rule) -> bool:
    """Rules whose output names encode all inputs — existence implies correctness."""
    return rule.rule_type in (RuleType.COMPILE, RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY)


def _make_trace_entry(rule: BuildRule, context, output_hash: str | None = None) -> TraceEntry:
    """Build a TraceEntry for a successfully executed rule.

    Pass *output_hash* when already computed (avoids a redundant disk read).
    """
    assert rule.command is not None, "only call _make_trace_entry after a rule executes"
    if output_hash is None and not os.path.isfile(rule.output):
        raise RuntimeError(
            f"_make_trace_entry: rule {rule.output!r} executed successfully but its "
            f"output file is missing. The rule's command may have side-effect-only "
            f"semantics (e.g., a test rule whose success_marker was never touched) "
            f"and should not be in the trace-execution path."
        )
    # Anchor the input keys to gitroot so a trace written under workspace A
    # verifies under workspace B (cross-CI-runner CAS reuse). Mirrors the
    # 9.1.0 path-canonical CAS-key fix for per-TU object / PCH / PCM caches;
    # without it, _verify's set-equality check on input_hashes keys rejects
    # every cross-workspace hit even when the cached output is byte-identical.
    anchor_root = compiletools.git_utils.find_git_root()
    input_hashes = {}
    for p in rule.inputs:
        if os.path.isfile(p):
            key = compiletools.apptools.canonicalize_path_for_cache_key(compiletools.wrappedos.realpath(p), anchor_root)
            input_hashes[key] = get_file_hash(p, context)
        else:
            logger.debug("_make_trace_entry: skipping non-file input %s for %s", p, rule.output)
    identity = _compiler_identity(rule.command[0], anchor_root=anchor_root) if rule.command else None
    canonical_cmd = _canonicalize_cmd_for_hash(rule.command, anchor_root)
    return TraceEntry(
        output_hash=output_hash if output_hash is not None else get_file_hash(rule.output, context),
        input_hashes=input_hashes,
        command_hash=hash_command(canonical_cmd, compiler_identity=identity),
    )


@register_backend
class ShakeBackend(BuildBackend):
    """Self-executing backend using Shake-style verifying traces."""

    def __init__(self, args, hunter, *, context=None):
        super().__init__(args, hunter, context=context)
        self._graph: BuildGraph | None = None
        # Test rules run in-process during the build phase. A failing test
        # appends here instead of raising from inside the rule executor --
        # raising mid-flight would abort sibling rules that asyncio.gather
        # already has in flight. The aggregated list is raised as a single
        # RuntimeError once the top-level traversal in execute() returns.
        self._test_failures: list[str] = []

    @staticmethod
    def name() -> str:
        return "shake"

    @staticmethod
    def tool_command() -> str | None:
        # Self-executing — runs each rule directly via subprocess.
        return None

    @staticmethod
    def build_filename() -> str:
        return ".ct-traces.json"

    def generate(self, graph: BuildGraph, output=None) -> None:
        self._graph = graph
        if output is not None:
            self._write_summary(graph, output)

    def _write_summary(self, graph: BuildGraph, f) -> None:
        f.write("# Shake build graph summary\n\n")
        for rule in graph.rules:
            if rule.rule_type == RuleType.PHONY:
                f.write(f"phony {rule.output}: {' '.join(rule.inputs)}\n")
            elif rule.command:
                f.write(f"{rule.rule_type} {rule.output}:\n")
                f.write(f"  inputs: {' '.join(rule.inputs)}\n")
                f.write(f"  command: {' '.join(rule.command)}\n")
        f.write("\n")

    def _ca_target(self, rule: BuildRule) -> str:
        """Content-addressable output path for a link/library rule.

        Hashes sorted inputs + command (with output path stripped to avoid
        circularity).  The CA filename lives alongside the human-readable
        output so directory creation is already handled.
        """
        assert rule.command is not None, "_ca_target only applies to link/library rules"
        cmd_filtered = [a for a in rule.command if a != rule.output]
        key = json.dumps({"inputs": sorted(rule.inputs), "cmd": cmd_filtered}, sort_keys=True)
        h = hashlib.sha256(key.encode()).hexdigest()[:20]
        base = os.path.basename(rule.output)
        name, ext = os.path.splitext(base)
        return os.path.join(os.path.dirname(rule.output), f"{name}_{h}{ext}")

    def _execute_build(self, target: str) -> None:
        # Not used: ShakeBackend overrides execute() with its own build engine.
        del target
        raise NotImplementedError  # pragma: no cover

    def execute(self, target: str = "build") -> None:
        """Run the Shake build engine.

        ``execute("build")`` is retargeted to the ``all`` phony, whose
        transitive deps include ``runtests`` -> every test rule fires the
        moment its exe's link future resolves. ``execute("runtests")`` walks
        the ``runtests`` phony directly so a standalone test run uses the
        same native, in-process ``RuleType.TEST`` rules as the in-build path.
        """
        if self._graph is None:
            raise RuntimeError("generate() must be called before execute()")
        graph = self._graph  # non-None local: attribute narrowing is lost inside the closure below

        if target == "runtests":
            walk_target = "runtests" if graph.get_rule("runtests") is not None else target
        elif target == "build":
            walk_target = "all" if graph.get_rule("all") is not None else target
        else:
            walk_target = target

        # Each execute() call is a fresh top-level traversal; clear any
        # aggregated failures from a prior call on the same backend instance.
        self._test_failures = []

        # M2 leaf-skip: precompute, per rule output, the inputs that have a
        # producing rule. Leaf inputs (source/header files) return False
        # immediately from _do_build, so the async scheduler skips allocating a
        # child task for each of them. Verify-trace still hashes every
        # ``rule.inputs`` entry (it reads ``rule.inputs`` directly), so this
        # only trims the recursion fan-out, never the correctness inputs.
        self._rule_inputs = {r.output: [i for i in r.inputs if graph.get_rule(i) is not None] for r in graph.rules}

        trace_path = os.path.join(self.args.cas_objdir, self.build_filename())
        traces = TraceStore(trace_path)

        parallel = getattr(self.args, "parallel", 1)
        max_workers = parallel if parallel and parallel > 0 else 1
        memo: dict[str, asyncio.Task[bool]] = {}

        # M1: critical-path scheduling. Load the learned cost history (best
        # effort) and precompute each rule's critical time (longest remaining
        # path to the target) so the PriorityGate starts long poles first. Any
        # failure degrades to an empty crit map -> priority 0 -> today's FIFO.
        cost_path = os.path.join(self.args.cas_objdir, rule_cost.COST_FILE)
        try:
            history = rule_cost.load_cost_history(cost_path)
            crit = rule_cost.compute_critical_times(graph, lambda r: rule_cost.estimate_cost(r, history))
        except Exception:
            history, crit = {}, {}
        gate = PriorityGate(max_workers)
        # Observed per-rule elapsed times accumulate here during the build and
        # are folded back into the cost history on the way out (best effort).
        self._observed_costs: dict[str, float] = {}

        # M2 single signal handler: one asyncio signal handler on the build's
        # event loop forwards SIGINT/SIGTERM to every live child process group
        # AND cancels the root build task. Children are spawned in their own
        # session (start_new_session=True), so a Ctrl-C reaches the whole
        # compiler process tree, not just the direct child. Replaces the
        # per-subprocess ``graceful_shutdown`` handlers the sync
        # ``_run_with_signal_forwarding`` installed once per rule.
        #
        # The cancel is load-bearing, not belt-and-braces: forwarding alone
        # aborts the build only incidentally (killed compiler -> non-zero rc ->
        # CalledProcessError). A signal arriving while NO child is in flight
        # (verify/hash phase of a warm build) would otherwise be swallowed
        # entirely, and a killed TEST child merely appends to _test_failures --
        # siblings would keep launching after Ctrl-C. Cancelling the root task
        # tears down the whole gather tree; _run_child_async's CancelledError
        # path SIGKILLs + reaps each remaining child pgid before the lock
        # release, so no orphan survives.
        self._live_child_pgids: set[int] = set()
        aborted_by: list[int] = []

        async def _run_build() -> None:
            loop = asyncio.get_running_loop()
            build_task = asyncio.ensure_future(self._build_async(walk_target, graph, traces, memo, gate, crit))

            def _forward_signal(signum: int) -> None:
                aborted_by.append(signum)
                for pgid in list(self._live_child_pgids):
                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.killpg(pgid, signum)
                build_task.cancel()

            installed: list[int] = []
            for sig in (signal.SIGINT, signal.SIGTERM):
                # add_signal_handler only works on the main thread of the main
                # interpreter; degrade gracefully off-thread / on unsupported
                # platforms (children still reaped, just not signal-forwarded).
                try:
                    loop.add_signal_handler(sig, _forward_signal, sig)
                    installed.append(sig)
                except (NotImplementedError, ValueError, RuntimeError):
                    pass
            try:
                await build_task
            finally:
                for sig in installed:
                    with contextlib.suppress(Exception):
                        loop.remove_signal_handler(sig)

        try:
            asyncio.run(_run_build())
        except asyncio.CancelledError:
            if not aborted_by:
                raise  # cancelled by something other than our signal handler
        # Known edge: if a genuine rule failure races the signal, a
        # CalledProcessError can win over the CancelledError and propagate out
        # of asyncio.run above, skipping the re-delivery below — the process
        # then exits via the build error instead of signal status. Effectively
        # unreachable in practice (_run_child_async converts a cancelled
        # proc.wait() to CancelledError before the rc check) and benign when
        # hit, so not worth a second exception path.
        finally:
            traces.save()
            try:
                history.update(self._observed_costs)
                rule_cost.save_cost_history(cost_path, history, prefer=set(self._observed_costs))
            except Exception:
                pass

        # Re-deliver the aborting signal now that traces/costs are saved and
        # every child is reaped, so the process reports a conventional
        # termination status (Ctrl-C -> KeyboardInterrupt, letting cake.py's
        # outer handling decide the exit status; SIGTERM -> killed-by-SIGTERM
        # via the restored default handler).
        if aborted_by:
            signum = aborted_by[0]
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

        # A failed test only appended to _test_failures (so sibling rules
        # already in flight could finish); surface the aggregate now so a
        # failing test makes ct-cake exit non-zero.
        if self._test_failures:
            raise RuntimeError("test execution failed:\n  " + "\n  ".join(self._test_failures))

    async def _build_async(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        memo: dict[str, asyncio.Task[bool]],
        gate: PriorityGate,
        crit: dict[str, float],
    ) -> bool:
        """Async suspending scheduler with verifying traces and early cutoff.

        Uses asyncio.gather for fan-out (no deadlock risk) and a PriorityGate
        to limit subprocess concurrency, admitting the highest critical-time
        rule first (``crit`` maps rule output -> critical time).  Memoization
        via the memo dict ensures each target is built at most once (diamond
        deps await the same task).

        Returns True if the target's output changed (dependents should rebuild).
        """
        if target not in memo:
            memo[target] = asyncio.ensure_future(self._do_build(target, graph, traces, memo, gate, crit))
        return await memo[target]

    async def _do_build(
        self,
        target: str,
        graph: BuildGraph,
        traces: TraceStore,
        memo: dict[str, asyncio.Task[bool]],
        gate: PriorityGate,
        crit: dict[str, float],
    ) -> bool:
        """Build a single target, recursing into dependencies via gather."""
        rule = graph.get_rule(target)
        if rule is None:
            return False  # Leaf node (source/header file)

        if rule.rule_type == RuleType.PHONY:
            results = await asyncio.gather(
                *(self._build_async(inp, graph, traces, memo, gate, crit) for inp in self._inputs_to_walk(target, rule))
            )
            return any(results)

        if rule.rule_type == RuleType.TEST:
            # Build the test's prerequisites first, then run the test
            # in-process. A test rule's order_only_deps carry the exe path
            # (produced by a LINK/SYMLINK rule -- recurse into it) plus, when
            # --test-xml-dir is set, an XML-bucket directory (an MKDIR rule --
            # just mkdir it; do not walk it, an MKDIR rule has no on-disk file
            # output and would trip _make_trace_entry). ``inputs`` is populated
            # in mtime mode (the exe path again) and empty in CAS-only mode.
            await asyncio.gather(
                *(self._build_async(inp, graph, traces, memo, gate, crit) for inp in self._inputs_to_walk(target, rule))
            )
            for dep in rule.order_only_deps:
                dep_rule = graph.get_rule(dep)
                if dep_rule is not None and dep_rule.rule_type != RuleType.MKDIR:
                    await self._build_async(dep, graph, traces, memo, gate, crit)
                else:
                    os.makedirs(dep, exist_ok=True)
            # Rerun-skip predicate (CAS-only mode): a test rule's ``output`` is
            # the JUnit XML path (framework tests) or the ``.result`` marker
            # (no-framework tests). When it already exists on disk the test's
            # exe bytes were tested before -- skip the re-run, mirroring the
            # make/ninja ``<output>: | <exe>`` order-only-prereq rule and the
            # ``_all_outputs_current`` RuleType.TEST branch. In ``--use-mtime``
            # mode the exe path is a real input, so always re-run (the user
            # opted into "touch to force rebuild" semantics).
            if not getattr(self.args, "use_mtime", False) and os.path.exists(rule.output):
                return False
            assert rule.command is not None, "test rules always carry a command"
            queued_at = time.monotonic()
            await gate.acquire(crit.get(target, 0.0))
            try:
                await self._execute_rule_async(rule, target, list(rule.command), queued_at)
            finally:
                gate.release()
            # Test rules never enter the trace store: _execute_rule records the
            # outcome (success marker touched, or failure appended to
            # _test_failures) and we deliberately do NOT call _make_trace_entry
            # here -- a test rule's "output" is a framework XML file or the
            # .result marker, not a content-addressed build artefact, and a
            # failed test must not assert its output exists.
            return False

        # Ensure order-only deps (bucket directories) exist. Reject
        # artefact-shaped paths up front: ``order_only_deps`` is reserved
        # for *bucket directories* (see
        # ``_ORDER_ONLY_DEP_FORBIDDEN_EXTS``), and a silent mkdir on an
        # artefact path would clobber the file the producer rule is
        # supposed to write -- the original defect class behind the
        # C++20-modules trace_backend xfail. Mirrors the check in
        # ``BuildBackend._prebuild_aux_artefacts`` so producers can't
        # smuggle a file path past one backend by hitting the other
        # first. Module/PCH/BMI deps belong in ``inputs``, where
        # ``_build_async`` recurses into the producer.
        for dep in rule.order_only_deps:
            if dep.endswith(_ORDER_ONLY_DEP_FORBIDDEN_EXTS):
                raise AssertionError(
                    f"order_only_dep {dep!r} on rule {rule.output!r} has an "
                    f"artefact suffix; order_only_deps must be bucket "
                    f"directories. Route artefact dependencies through "
                    f"`inputs` (see build_backend._wire_module_inputs)."
                )
            os.makedirs(dep, exist_ok=True)

        # CONTENT-ADDRESSABLE SHORT-CIRCUIT
        if _is_build_artifact(rule):
            if rule.rule_type in (RuleType.COMPILE, RuleType.LINK):
                # Both compile and link rule outputs are content-addressable
                # by construction: object names encode file_h+dep_h+macro_h;
                # cas-exe paths encode the link key (linker identity + sorted
                # canonical objects + ldflags). Existence is sufficient.
                # The publish-as-symlink rule (separate, downstream) exposes
                # link outputs at user-facing bin/<name>.
                if os.path.exists(target):
                    return False
            else:
                # static_library / shared_library still use the legacy
                # in-place CA layout (output adjacent CA file then copy).
                # When/if those move into a cas-libdir, this branch can
                # collapse into the existence-only fast path above.
                ca = self._ca_target(rule)
                if os.path.exists(ca):
                    compiletools.filesystem_utils.atomic_copy(ca, target)
                    return False
        elif rule.rule_type == RuleType.SYMLINK:
            # SYMLINK publishes a content-addressable artefact (the single
            # input, a cas-exedir/cas-libdir path) at a user-facing target
            # via ct-cas-publish. The publish recipe hardlinks by default
            # and falls back to symlink only on EXDEV. If the target
            # already resolves to the same on-disk file as the cas input
            # the rule is a no-op. samefile handles both wirings: same
            # inode (hardlink) and follow-through (symlink). Without this
            # branch the rule falls through to _verify, which would hash
            # both the target and the cas input on every no-op build.
            # A prior publish's best-effort <cas-path>.manifest sidecar is
            # deliberately NOT re-created here: manifest recreation is not a
            # republish trigger (the manifest is best-effort -- ct-cas-publish
            # swallows OSError writing it -- and trim_exedir falls back to
            # basename bucketing when it is absent).
            try:
                if rule.inputs and os.path.samefile(target, rule.inputs[0]):
                    return False
            except OSError:
                pass  # target missing or cas input not yet built; fall through

        # SUSPEND: build all inputs concurrently via gather.
        results = await asyncio.gather(
            *(self._build_async(inp, graph, traces, memo, gate, crit) for inp in self._inputs_to_walk(target, rule))
        )
        any_input_rebuilt = any(results)

        # VERIFY TRACE (non-CA rules only)
        if not _is_build_artifact(rule) and not any_input_rebuilt:
            trace = traces.get(target)
            if trace is not None and self._verify(rule, trace):
                return False  # up to date

        # EXECUTE (PriorityGate limits subprocess concurrency)
        old_hash = None
        if not _is_build_artifact(rule):
            old_hash = get_file_hash(target, self.context) if os.path.exists(target) else None

        assert rule.command is not None, "only rules with commands reach EXECUTE"
        cmd = rule.command
        verbose = getattr(self.args, "verbose", 0)
        if verbose >= 1:
            print(" ".join(cmd), file=sys.stderr)

        flat_cmd = list(cmd)

        queued_at = time.monotonic()
        await gate.acquire(crit.get(target, 0.0))
        try:
            await self._execute_rule_async(rule, target, flat_cmd, queued_at)
        finally:
            gate.release()

        # CA outputs don't need trace recording or early cutoff
        if _is_build_artifact(rule):
            return True  # New output -> dependents must rebuild

        new_hash = get_file_hash(target, self.context)
        traces.put(target, _make_trace_entry(rule, self.context, output_hash=new_hash))

        # EARLY CUTOFF
        return old_hash != new_hash

    def _run_test_rule(self, rule: BuildRule, flat_cmd: list[str]) -> None:
        """Run a TEST rule in-process and record its outcome.

        Pure-argv test invocation -- NOT routed through
        execute_compile_rule / execute_link_rule (those are for
        lock-guarded build artefacts; a test is neither, so there are no
        atomic-output / trace-store semantics either). flat_cmd already
        carries TESTPREFIX + exe + framework XML argv, baked in at
        graph-build time by _test_command_for. On success touch the
        .result marker (always success_marker, even for framework tests);
        on failure append to _test_failures and return so sibling rules
        already in flight can finish -- execute() raises the aggregate
        once the traversal returns. Called from ShakeBackend._execute_rule.
        """
        result = subprocess.run(flat_cmd)
        self._record_test_outcome(rule, result.returncode, flat_cmd)

    def _record_test_outcome(self, rule: BuildRule, returncode: int, flat_cmd: list[str]) -> None:
        """Record a TEST rule's result. On success touch the success marker
        (always ``success_marker``, even for framework tests); on failure append
        to ``_test_failures`` so sibling rules already in flight can finish and
        ``execute()`` raises the aggregate once the traversal returns. Shared by
        the sync ``_run_test_rule`` and the async ``_execute_rule_async``."""
        if returncode == 0:
            self._touch_result_marker(rule.success_marker or "")
        else:
            self._test_failures.append(f"{rule.output} (exit {returncode}): {' '.join(flat_cmd)}")

    def _inputs_to_walk(self, target: str, rule: BuildRule) -> list[str]:
        """The rule's inputs worth recursing into (M2 leaf-skip).

        Leaf inputs (source/header files with no producing rule) return False
        immediately from ``_do_build``, so the scheduler can skip allocating a
        child task for each. ``execute()`` precomputes ``_rule_inputs`` once;
        direct-call tests that reach ``_build_async`` without going through
        ``execute()`` fall back to the full ``rule.inputs`` (correct, just
        unoptimized). Verify-trace still hashes every ``rule.inputs`` entry."""
        rule_inputs = getattr(self, "_rule_inputs", None)
        if rule_inputs is not None and target in rule_inputs:
            return rule_inputs[target]
        return rule.inputs

    def _track_child_spawn(self, pgid: int) -> None:
        """Register a live child process group so the single event-loop signal
        handler can forward SIGINT/SIGTERM to it. No-op when ``execute()`` did
        not install the registry (direct-call tests)."""
        registry = getattr(self, "_live_child_pgids", None)
        if registry is not None:
            registry.add(pgid)

    def _track_child_reap(self, pgid: int) -> None:
        """Deregister a reaped child process group (see ``_track_child_spawn``)."""
        registry = getattr(self, "_live_child_pgids", None)
        if registry is not None:
            registry.discard(pgid)

    def _execute_rule(self, rule: BuildRule, target: str, flat_cmd: list[str], queued_at: float | None = None) -> None:
        """Run the subprocess for a single build rule (synchronous reference).

        The production dispatch path is the async twin ``_execute_rule_async``;
        this sync version is retained for direct unit tests and as the readable
        reference for the branch dispatch + timing/metadata contract.

        ``skip_if_exists=True`` on the three CA branches closes the TOCTOU
        window between the pre-lock fast-path in ``_do_build`` and the
        helper's own lock acquire. The ``else`` branch keeps the default
        False — verify-trace already decided the output is stale.

        ``queued_at`` is the ``time.monotonic()`` stamp taken in ``_do_build``
        immediately before the concurrency-gate acquire. When supplied it is
        recorded as ``metadata["queue_wait_s"]`` so gate-wait is separable from
        run-time in the timing report (M0 attribution).
        """
        start = time.monotonic()
        # cas_hit semantics: True iff execute_*_rule returned True because
        # skip_if_exists short-circuited (peer / prior build produced the
        # artefact).  None for branches where the concept doesn't apply
        # (TEST runs the binary; the ``else`` branch executes
        # unconditionally without skip_if_exists).  None disables the
        # outcomes-log append for that rule, leaving cas.* absent on the
        # span — distinct from "ran the compiler, no cache hit".
        cas_hit: bool | None = None
        if rule.rule_type == RuleType.TEST:
            self._run_test_rule(rule, flat_cmd)
        elif rule.rule_type == RuleType.COMPILE:
            cas_hit = execute_compile_rule(target, flat_cmd, self.args, skip_if_exists=True, cwd=rule.cwd)
        elif rule.rule_type == RuleType.LINK:
            # Link output IS the cas-exe path; the downstream publish-as-symlink
            # rule materialises bin/<name>. No per-rule CA-then-copy here —
            # that's only for static_library / shared_library.
            cas_hit = execute_link_rule(target, flat_cmd, self.args, skip_if_exists=True)
        elif _is_build_artifact(rule):
            ca = self._ca_target(rule)
            ca_cmd = [ca if a == target else a for a in flat_cmd]
            cas_hit = execute_link_rule(ca, ca_cmd, self.args, skip_if_exists=True)
            compiletools.filesystem_utils.atomic_copy(ca, target)
        else:
            execute_link_rule(target, flat_cmd, self.args)

        self._record_rule_timing(rule, target, cas_hit, start, queued_at)

    async def _execute_rule_async(
        self, rule: BuildRule, target: str, flat_cmd: list[str], queued_at: float | None = None
    ) -> None:
        """Async twin of ``_execute_rule`` — the production dispatch path.

        Mirrors ``_execute_rule``'s branch dispatch, CAS-hit semantics, and
        timing/metadata recording verbatim; only the subprocess spawn differs
        (the ``*_async`` locking helpers use ``asyncio.create_subprocess_exec``
        instead of a thread-pool + blocking ``Popen``, so the event loop keeps
        scheduling other ready rules while this one runs). Child process groups
        are registered via ``_track_child_spawn`` / ``_track_child_reap`` so the
        single ``execute()`` signal handler can forward SIGINT/SIGTERM. The sync
        ``_execute_rule`` is retained for direct unit tests and as the reference
        implementation of the timing/metadata contract."""
        start = time.monotonic()
        cas_hit: bool | None = None
        if rule.rule_type == RuleType.TEST:
            rc = await _run_child_async(flat_cmd, on_spawn=self._track_child_spawn, on_reap=self._track_child_reap)
            self._record_test_outcome(rule, rc, flat_cmd)
        elif rule.rule_type == RuleType.COMPILE:
            cas_hit = await execute_compile_rule_async(
                target,
                flat_cmd,
                self.args,
                skip_if_exists=True,
                cwd=rule.cwd,
                on_spawn=self._track_child_spawn,
                on_reap=self._track_child_reap,
            )
        elif rule.rule_type == RuleType.LINK:
            cas_hit = await execute_link_rule_async(
                target,
                flat_cmd,
                self.args,
                skip_if_exists=True,
                on_spawn=self._track_child_spawn,
                on_reap=self._track_child_reap,
            )
        elif _is_build_artifact(rule):
            ca = self._ca_target(rule)
            ca_cmd = [ca if a == target else a for a in flat_cmd]
            cas_hit = await execute_link_rule_async(
                ca,
                ca_cmd,
                self.args,
                skip_if_exists=True,
                on_spawn=self._track_child_spawn,
                on_reap=self._track_child_reap,
            )
            compiletools.filesystem_utils.atomic_copy(ca, target)
        else:
            await execute_link_rule_async(
                target,
                flat_cmd,
                self.args,
                on_spawn=self._track_child_spawn,
                on_reap=self._track_child_reap,
            )

        self._record_rule_timing(rule, target, cas_hit, start, queued_at)

    def _record_rule_timing(
        self, rule: BuildRule, target: str, cas_hit: bool | None, start: float, queued_at: float | None
    ) -> None:
        """Record per-rule elapsed time, observed cost (M1), and CAS/queue-wait
        metadata (M0). Shared by ``_execute_rule`` and ``_execute_rule_async``.

        trace_backend executes rules in-process, so the in-memory
        ``record_rule(metadata=...)`` is the source of truth and the cake.py
        post-build ``merge_rule_outcomes`` roundtrip is unnecessary here —
        skipping ``append_rule_outcome`` saves ~3 syscalls per rule on builds
        with thousands of rules. Other backends (ninja/make/shake) still need
        the outcomes log because they execute rules out-of-process (via
        ct-lock-helper) and cannot reach this in-memory timer."""
        elapsed = time.monotonic() - start
        # M1: feed the observed elapsed back into the cost model so the next
        # build schedules by learned costs. Best effort; TEST rules are excluded
        # (their duration is exe-runtime, not build work on the critical path).
        observed = getattr(self, "_observed_costs", None)
        if observed is not None and rule.rule_type != RuleType.TEST:
            try:
                observed[rule_cost.cost_key(rule)] = elapsed
            except Exception:
                pass
        timer = self._timer
        # cas_hit is None for branches where the concept doesn't apply (TEST runs
        # the binary; the ``else`` branch executes unconditionally). None leaves
        # cas.* absent on the span — distinct from "ran the compiler, no hit".
        metadata: dict[str, object] | None = None
        if cas_hit is not None:
            cas_kind = _cas_kind_for_rule_type(rule.rule_type)
            try:
                bytes_reused = os.path.getsize(target) if cas_hit and os.path.exists(target) else 0
            except OSError:
                bytes_reused = 0
            metadata = {
                "cas.hit": cas_hit,
                "cas.bytes_reused": bytes_reused,
            }
            if cas_kind:
                metadata["cas.kind"] = cas_kind
        if queued_at is not None:
            if metadata is None:
                metadata = {}
            metadata["queue_wait_s"] = round(start - queued_at, 6)
        if timer:
            source = rule.inputs[0] if rule.inputs else ""
            timer.record_rule(
                rule_type=rule.rule_type,
                target=target,
                source=source,
                elapsed_s=elapsed,
                start_s=start,
                end_s=start + elapsed,
                metadata=metadata,
            )

    def _verify(self, rule, trace: TraceEntry) -> bool:
        """Check if a trace is still valid (output exists, inputs unchanged, same command)."""
        assert rule.command is not None, "_verify only applies to rules with commands"
        try:
            if get_file_hash(rule.output, self.context) != trace.output_hash:
                return False
        except (FileNotFoundError, OSError):
            return False

        # Symmetric with _make_trace_entry: anchor on the current workspace's
        # gitroot so cross-workspace traces still verify (the trace's keys
        # and command_hash were canonicalized at write time).
        anchor_root = compiletools.git_utils.find_git_root()
        identity = _compiler_identity(rule.command[0], anchor_root=anchor_root) if rule.command else None
        canonical_cmd = _canonicalize_cmd_for_hash(rule.command, anchor_root)
        if hash_command(canonical_cmd, compiler_identity=identity) != trace.command_hash:
            return False
        # Map each canonical key to a real on-disk path for the current
        # workspace so we can read the file hash. Since _make_trace_entry
        # canonicalizes (realpath -> <GITROOT>/relpath), the reverse here is
        # taking each rule input, realpath'ing it, then canonicalizing —
        # producing the same canonical key.
        canonical_to_real = {
            compiletools.apptools.canonicalize_path_for_cache_key(compiletools.wrappedos.realpath(p), anchor_root): p
            for p in rule.inputs
        }
        if set(canonical_to_real.keys()) != set(trace.input_hashes.keys()):
            return False

        for canonical_key, stored_hash in trace.input_hashes.items():
            try:
                current_hash = get_file_hash(canonical_to_real[canonical_key], self.context)
            except (FileNotFoundError, OSError):
                return False
            if current_hash != stored_hash:
                return False

        return True
