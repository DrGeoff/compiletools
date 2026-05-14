"""Ninja backend — generates build.ninja files from a BuildGraph."""

from __future__ import annotations

import os
import subprocess
import time

import compiletools.filesystem_utils
from compiletools.build_backend import (
    CAS_PRODUCER_TYPES,
    BuildBackend,
    cas_demoted_order_only,
    register_backend,
)
from compiletools.build_graph import BuildGraph, RuleType, render_shell_recipe


@register_backend
class NinjaBackend(BuildBackend):
    """Generate and execute Ninja build files."""

    def _honors_use_mtime(self) -> bool:
        return True

    @staticmethod
    def name() -> str:
        return "ninja"

    @staticmethod
    def tool_command() -> str:
        return "ninja"

    @staticmethod
    def build_filename() -> str:
        return "build.ninja"

    def _build_file_path(self) -> str:
        return getattr(self.args, "ninja_filename", "build.ninja")

    def generate(self, graph: BuildGraph, output=None) -> None:
        self._setup_file_locking()
        graph = self._apply_build_only_changed(graph)

        if output is not None:
            self._write_ninja(graph, output)
        else:
            if self._build_file_uptodate(graph):
                return
            with compiletools.filesystem_utils.atomic_output_file(
                self._build_file_path(), mode="w", encoding="utf-8"
            ) as f:
                self._write_ninja(graph, f)

    def _write_ninja(self, graph: BuildGraph, f) -> None:
        f.write(f"{self._build_file_header_token()}\n\n")

        # Compute module-interface outputs once.  Named-module interface
        # compile rules use -fmodule-mapper= (gcc) or --precompile -o
        # (clang); appending -MMD -MF would conflict with the module-mapper
        # protocol.  GCC reports "inputs may not also have inputs" because the
        # module mapper makes the compile look like a multi-input action to
        # ninja, and deps=gcc cannot handle that.  Hunter has already computed
        # transitive header deps for these rules; a depfile is unnecessary.
        module_iface_outputs: set[str] = set(self._module_iface_obj.values()) | set(self._module_iface_pcm.values())

        rule_types_seen = set()
        for rule in graph.rules:
            if rule.command and rule.rule_type not in rule_types_seen:
                ninja_rule = f"{rule.rule_type}_cmd"
                f.write(f"rule {ninja_rule}\n")
                f.write("  command = $cmd\n")
                if rule.rule_type == RuleType.COMPILE:
                    f.write("  description = Compiling $out\n")
                    f.write("  depfile = $out.d\n")
                    f.write("  deps = gcc\n")
                elif rule.rule_type == RuleType.LINK:
                    f.write("  description = Linking $out\n")
                elif rule.rule_type == RuleType.STATIC_LIBRARY:
                    f.write("  description = Archiving $out\n")
                elif rule.rule_type == RuleType.SHARED_LIBRARY:
                    f.write("  description = Linking shared library $out\n")
                else:
                    f.write(f"  description = {rule.rule_type} $out\n")
                # restat=1 lets Ninja skip downstream rebuilds when the
                # output's mtime doesn't actually change. Suppress for
                # mkdir/phony — a directory's mtime updates every time a
                # child file is added/removed inside it, and a phony rule
                # has no real output. If those rule_types ever flow
                # through this command-emission path and someone takes a
                # genuine dependency on them, restat=1 would silently
                # skip downstream rebuilds.
                if rule.rule_type not in (RuleType.MKDIR, RuleType.PHONY):
                    f.write("  restat = 1\n")
                f.write("\n")
                rule_types_seen.add(rule.rule_type)

        # Emit a separate compile rule without depfile/deps for module-interface
        # units.  This rule is only written when the graph actually contains
        # such units, so ordinary projects are not affected.
        # module_iface_outputs is empty iff there are no module-interface
        # compile rules in the graph; checking its truthiness is equivalent
        # to scanning rules and faster.
        if module_iface_outputs:
            f.write("rule compile_module_iface_cmd\n")
            f.write("  command = $cmd\n")
            f.write("  description = Compiling module interface $out\n")
            f.write("  restat = 1\n")
            f.write("\n")

        cas_only = not self.args.use_mtime
        for rule in graph.rules:
            if rule.rule_type == RuleType.PHONY:
                f.write(f"build {rule.output}: phony {' '.join(rule.inputs)}\n")
            elif rule.command:
                is_module_iface = rule.rule_type == RuleType.COMPILE and rule.output in module_iface_outputs
                ninja_rule = "compile_module_iface_cmd" if is_module_iface else f"{rule.rule_type}_cmd"
                if cas_only and rule.rule_type in CAS_PRODUCER_TYPES:
                    # CAS-only: producer's cached path encodes the cache
                    # key, so inputs become order-only — ninja builds
                    # them first but does not retrigger the producer on
                    # their mtime change.
                    line = f"build {rule.output}: {ninja_rule}"
                    ordering = list(rule.order_only_deps) + cas_demoted_order_only(rule)
                    if ordering:
                        line += f" || {' '.join(ordering)}"
                else:
                    # First input is the primary source, rest are implicit deps
                    primary = rule.inputs[0] if rule.inputs else ""
                    implicit = rule.inputs[1:] if len(rule.inputs) > 1 else []
                    line = f"build {rule.output}: {ninja_rule} {primary}"
                    if implicit:
                        line += f" | {' '.join(implicit)}"
                    if rule.order_only_deps:
                        line += f" || {' '.join(rule.order_only_deps)}"
                f.write(line + "\n")

                if rule.rule_type == RuleType.COMPILE:
                    if is_module_iface:
                        # Skip -MMD -MF: module-interface rules use
                        # -fmodule-mapper= (gcc) or --precompile -o (clang),
                        # which conflict with depfile generation.
                        cmd_str = self._wrap_compile_cmd(rule.command)
                    else:
                        cmd_str = self._wrap_compile_cmd(rule.command + ["-MMD", "-MF", rule.output + ".d"])
                elif rule.rule_type in (RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
                    cmd_str = self._wrap_link_cmd(rule.command)
                else:
                    # RuleType.TEST and friends. A framework-detected test
                    # rule's ``output`` is its JUnit XML path; a failing
                    # framework test writes that report and *then* exits
                    # non-zero. Ninja, unlike make, does not delete outputs on
                    # rule failure (it only deletes them when interrupted), so
                    # no ``.PRECIOUS`` equivalent is needed — the XML survives
                    # a failed build. Verified by
                    # test_ninja_framework_test_failure_preserves_xml.
                    cmd_str = render_shell_recipe(rule)
                f.write(f"  cmd = {cmd_str}\n")
            f.write("\n")

    def _execute_build(self, target: str) -> None:
        filename = getattr(self.args, "ninja_filename", "build.ninja")
        ninja_log = os.path.join(os.path.dirname(filename) or ".", ".ninja_log")

        # Record log offset before build for timing parsing
        timer = self._timer
        log_offset = 0
        if timer and os.path.exists(ninja_log):
            log_offset = os.path.getsize(ninja_log)

        ninja_target = self._native_target_for(target)

        cmd = ["ninja", "-f", filename]
        parallel = getattr(self.args, "parallel", None)
        if parallel:
            cmd.extend(["-j", str(parallel)])
        if self.args.verbose >= 1:
            cmd.append("-v")
        cmd.append(ninja_target)
        # Capture monotonic time immediately before invoking ninja so the
        # build-relative timestamps in .ninja_log can be folded onto this
        # timer's monotonic timeline (required for a coherent Chrome trace
        # spanning phases + ninja rules).
        build_start_mono = time.monotonic() if timer else None
        subprocess.check_call(cmd, text=True)

        # Parse timing from newly appended ninja log entries
        if timer:
            timer.record_rules_from_ninja_log(
                ninja_log,
                offset=log_offset,
                graph=self._graph,
                build_start_mono=build_start_mono,
            )
