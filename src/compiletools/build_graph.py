"""Backend-agnostic build graph representation.

BuildRule and BuildGraph provide an intermediate representation of the build
that is independent of any specific build system (Make, Ninja, CMake, etc.).
Backends consume a BuildGraph to produce their native output format.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass


class RuleType:
    """Named string constants for ``BuildRule.rule_type``.

    Comparisons remain string-equality because the field type is ``str``
    — but these names give IDE-aware refactoring and make typos a
    NameError instead of a silently-False comparison.

    Documentation of the per-type semantics:

    * ``LINK`` / ``STATIC_LIBRARY`` / ``SHARED_LIBRARY`` — CAS-aware
      producers. In ``--use-mtime=False`` mode (default), each writes
      to a content-addressable ``<cas-exedir>/<shard>/<name>_<key>.<ext>``
      and is paired with a downstream ``SYMLINK`` rule that publishes
      the user-facing path. Make/Ninja backends lift their inputs to
      order-only so existence-on-disk is the sole rebuild signal. In
      ``--use-mtime=True`` mode (legacy), producers write directly to
      the user-facing path with normal mtime semantics.
    * ``HEADER_UNIT`` — C++20 header-unit precompile that writes its
      actual artefact to an under-the-hood location (gcc:
      ``gcm.cache/<abs-path>.gcm``) and cannot conform to the
      ``COMPILE`` contract of producing the rule's named output. The
      named output is a stamp file, touched via ``success_marker``.
    * ``SYMLINK`` — idempotent hard-link/symlink publish step that
      exposes a content-addressable artefact at a stable, user-facing
      path. Single input (the cas artefact path), single output (the
      user-facing path).
    """

    COMPILE = "compile"
    LINK = "link"
    STATIC_LIBRARY = "static_library"
    SHARED_LIBRARY = "shared_library"
    TEST = "test"
    PHONY = "phony"
    MKDIR = "mkdir"
    CLEAN = "clean"
    COPY = "copy"
    HEADER_UNIT = "header_unit"
    SYMLINK = "symlink"


VALID_RULE_TYPES = frozenset(
    {
        RuleType.COMPILE,
        RuleType.LINK,
        RuleType.STATIC_LIBRARY,
        RuleType.SHARED_LIBRARY,
        RuleType.TEST,
        RuleType.PHONY,
        RuleType.MKDIR,
        RuleType.CLEAN,
        RuleType.COPY,
        RuleType.HEADER_UNIT,
        RuleType.SYMLINK,
    }
)


@dataclass(frozen=True)
class BuildRule:
    """A single build action: produce `output` from `inputs` by running `command`.

    Immutable value object: the sequence fields are coerced to tuples in
    ``__post_init__`` and instances are frozen, so a rule is complete at
    its construction site and safe to share, hash, and snapshot. To
    "edit" a rule, build a new one with ``dataclasses.replace(rule, ...)``
    and re-``add_rule`` it — ``BuildGraph.add_rule`` is last-write-wins
    keyed by ``output``, and a dict overwrite keeps the rule's original
    insertion position, so regenerated build files stay byte-stable.

    Invariant: **one output -> one command.** ``BuildGraph.add_rule`` is
    keyed by ``output`` and last-write-wins, so two rules producing the
    same path collapse into one. Callers that build up a graph
    incrementally are expected to honour that invariant — emit a
    consistent command for any given output.

    Attributes:
        output: The file this rule produces (or a phony target name).
        inputs: Files this rule depends on (source files, headers, objects).
        command: argv list passed directly to ``subprocess.run`` (with
            ``shell=False`` semantics), or None for phony rules. Tokens
            MUST NOT contain shell metacharacters (``&&``, ``||``, ``>``,
            ``$VAR``, ...) — side effects like "touch a marker on success"
            belong in ``success_marker``, not in command tokens. Shell-rendered
            backends (Make, Ninja) join with spaces and let the underlying
            tool run the result through ``/bin/sh -c``; the argv-executing
            backend (Shake) hands the list straight to subprocess.
            Both behaviours agree as long as no token is shell-active.
        rule_type: One of "compile", "link", "test", "phony", "mkdir", "clean",
            "copy", "static_library", "shared_library".
        order_only_deps: Dependencies that must exist but whose timestamps are
            not checked (e.g., output directories).
        success_marker: Optional path to ``touch`` after the command exits
            successfully. Used by test rules to record passes for incremental
            re-runs. Each backend renders this in its native idiom: shell
            backends append ``&& touch <marker>`` to the recipe; argv backends
            call ``Path(marker).touch()`` after the subprocess returns 0.
            Backends that own their own pass/fail bookkeeping (CMake CTest,
            Bazel cc_test) ignore this field.
        success_copies: ``(src, dst)`` pairs atomically copied (temp +
            rename, never hardlink -- src is rewritten in place on later
            runs) after the command succeeds and BEFORE the
            success_marker touch, so the marker asserts the copies
            landed. Used by framework test rules to deposit the
            workspace-written JUnit XML at its content-addressed
            ``<cas_exe>.xml`` home; a failing run performs no copy, so
            failure artefacts never enter the CAS. Rendered by shell
            backends as ``&& cp src tmp && mv -f tmp dst`` with a temp
            name deterministic per src (regenerated build files stay
            byte-identical) and distinct across peer workspaces; argv
            backends copy in-process.
        cwd: Optional working directory the command must execute from. ``None``
            (default) means "no cwd constraint" — the recipe runs from
            wherever the backend's invocation happens to be. When set, shell
            backends prepend ``cd <cwd> && `` to the recipe (placed inside any
            lock wrapper so the lockfile path stays absolute and cwd-independent);
            argv-executing backends pass ``cwd=`` to ``subprocess.run``. The
            primary user is the PCH precompile rule in ``_create_pch_rules``,
            which sets ``cwd=anchor_root`` to keep gcc's PCH path-table
            workspace-relative — without that discipline, alice's PCH bakes
            ``/home/alice/proj/...`` into its DWARF include-dir table, and any
            consumer (including bob on a shared cas-pchdir) inherits those
            paths into their .o files.

    Equality and hash are structural (dataclass-generated, field-wise):
    two rules are equal iff every field matches. Output-keyed dedup is
    the graph's job (its dict keys on the ``output`` string), not the
    rule's.
    """

    output: str
    inputs: Sequence[str]
    command: Sequence[str] | None
    rule_type: str
    order_only_deps: Sequence[str] = ()
    success_marker: str | None = None
    cwd: str | None = None
    success_copies: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        # Coerce here, not in add_rule, so callers keep object identity
        # across registration (tests pin ``graph.get_rule(out) is rule``).
        # tuple() on an existing tuple returns the same object, so
        # dataclasses.replace round-trips are copy-free.
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "order_only_deps", tuple(self.order_only_deps))
        if self.command is not None:
            object.__setattr__(self, "command", tuple(self.command))
        if self.rule_type not in VALID_RULE_TYPES:
            raise ValueError(f"Invalid rule_type {self.rule_type!r}; must be one of {sorted(VALID_RULE_TYPES)}")


class BuildGraph:
    """Ordered collection of BuildRules forming a complete build description.

    Rules are stored in insertion order and deduplicated by output path.
    """

    def __init__(self):
        self._rules: dict[str, BuildRule] = {}

    def add_rule(self, rule: BuildRule) -> None:
        self._rules[rule.output] = rule

    def get_rule(self, output: str) -> BuildRule | None:
        return self._rules.get(output)

    @property
    def rules(self) -> list[BuildRule]:
        return list(self._rules.values())

    def __len__(self) -> int:
        return len(self._rules)

    def __contains__(self, output: str) -> bool:
        return output in self._rules

    def rules_by_type(self, rule_type: str) -> list[BuildRule]:
        """Return all rules matching the given type."""
        return [r for r in self._rules.values() if r.rule_type == rule_type]

    @property
    def outputs(self) -> set[str]:
        """Return the set of all output paths."""
        return set(self._rules.keys())

    def filter_to_changed(self, changed_files: set[str], verbose: int = 0) -> BuildGraph:
        """Return a new BuildGraph containing only rules affected by changed_files.

        Uses transitive closure: if a rule's inputs intersect changed_files,
        its output is added to changed_files and the walk repeats until a
        fixed-point is reached. Phony rules have their inputs pruned to only
        reference affected targets.
        """
        changed = set(changed_files)
        targets: set[str] = set()

        # Fixed-point iteration: discover all affected outputs
        done = False
        while not done:
            done = True
            for rule in self._rules.values():
                if rule.output in targets:
                    continue
                affected_inputs = set(rule.inputs) & changed
                if not affected_inputs:
                    continue
                changed.add(rule.output)
                targets.add(rule.output)
                done = False
                if verbose >= 3:
                    print(f"Building {rule.output} because it depends on changed: {sorted(affected_inputs)}")

        # Build new graph with only affected rules
        filtered = BuildGraph()
        for rule in self._rules.values():
            if rule.rule_type == RuleType.PHONY:
                pruned_inputs = [i for i in rule.inputs if i in targets]
                filtered.add_rule(
                    BuildRule(
                        output=rule.output,
                        inputs=pruned_inputs,
                        command=rule.command,
                        rule_type=rule.rule_type,
                        order_only_deps=rule.order_only_deps,
                    )
                )
            elif rule.output in targets:
                filtered.add_rule(rule)

        return filtered


def render_shell_recipe(rule: BuildRule) -> str:
    """Render rule.command as a shell-executable string, appending the
    success_marker touch tail when set.

    Used by shell-rendered backends (Make, Ninja) for non-compile/non-link
    rules whose command is intended to run through ``/bin/sh -c``.

    .. note:: ``rule.command`` tokens MUST be shell-naive at this point
       -- ``shlex.join`` quotes each token as a single shell word,
       which means a token containing already-shell-quoted content
       (e.g. ``'\\'-flag=<v>\\''``) would be DOUBLE-quoted on output.
       Callers that pre-quote tokens for the ``compile`` rule type's
       ``" ".join`` rendering path (the makefile backend's
       ``_wrap_compile_cmd``) must NOT pre-quote when emitting via a
       rule type that flows through this function (``header_unit``,
       ``test``, etc.). The ``_create_header_unit_precompile_rule``
       respects this contract -- its bare-header argument
       (``vector``) has no shell-active characters.
    """
    if rule.command is None:
        raise ValueError(f"render_shell_recipe called on rule {rule.output!r} with no command")
    cmd_str = shlex.join(rule.command)
    for src, dst in rule.success_copies:
        tmp = success_copy_tmp_path(src, dst)
        cmd_str += f" && cp {shlex.quote(src)} {shlex.quote(tmp)} && mv -f {shlex.quote(tmp)} {shlex.quote(dst)}"
    if rule.success_marker:
        cmd_str += f" && touch {shlex.quote(rule.success_marker)}"
    return cmd_str


def success_copy_tmp_path(src: str, dst: str) -> str:
    """Temp path for a ``success_copies`` deposit of *src* at *dst*.

    Deterministic (recipes are static text; regenerated build files must
    stay byte-identical) yet unique per depositing workspace: *src* is a
    workspace-absolute path, so peer workspaces depositing at the same
    *dst* get distinct temps and the final ``mv -f`` is the only write
    that touches the shared name. The ``.publish.tmp`` suffix keeps
    orphans (a build killed mid-copy) reclaimable by
    ``trim_cache.reclaim_orphan_temps``.
    """
    return f"{dst}.{hashlib.sha1(src.encode()).hexdigest()[:12]}.publish.tmp"
