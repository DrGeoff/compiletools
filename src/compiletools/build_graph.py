"""Backend-agnostic build graph representation.

BuildRule and BuildGraph provide an intermediate representation of the build
that is independent of any specific build system (Make, Ninja, CMake, etc.).
Backends consume a BuildGraph to produce their native output format.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_RULE_TYPES = frozenset(
    {"compile", "link", "test", "phony", "mkdir", "clean", "copy", "static_library", "shared_library"}
)


@dataclass
class BuildRule:
    """A single build action: produce `output` from `inputs` by running `command`.

    Attributes:
        output: The file this rule produces (or a phony target name).
        inputs: Files this rule depends on (source files, headers, objects).
        command: Shell command list to execute, or None for phony rules.
        rule_type: One of "compile", "link", "test", "phony", "mkdir", "clean",
            "copy", "static_library", "shared_library".
        order_only_deps: Dependencies that must exist but whose timestamps are
            not checked (e.g., output directories).
    """

    output: str
    inputs: list[str]
    command: list[str] | None
    rule_type: str
    order_only_deps: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.rule_type not in VALID_RULE_TYPES:
            raise ValueError(f"Invalid rule_type {self.rule_type!r}; must be one of {sorted(VALID_RULE_TYPES)}")

    def __eq__(self, other):
        if not isinstance(other, BuildRule):
            return NotImplemented
        return self.output == other.output

    def __hash__(self):
        return hash(self.output)


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
            if rule.rule_type == "phony":
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
