"""Makefile backend — generates GNU Makefiles from a BuildGraph."""

from __future__ import annotations

import functools
import json
import os
import shlex
import subprocess
import time

import compiletools.apptools
import compiletools.filesystem_utils
import compiletools.headerdeps
import compiletools.hunter
import compiletools.jobs
import compiletools.magicflags
import compiletools.namer
import compiletools.utils
from compiletools.build_backend import (
    CAS_PRODUCER_TYPES,
    BuildBackend,
    _register_make_cli_arguments,
    cas_demoted_order_only,
    register_backend,
)
from compiletools.build_context import BuildContext
from compiletools.build_graph import BuildGraph, RuleType, render_shell_recipe

# Shell snippet that prints a nanosecond timestamp portably:
#   1. Prefer bash 5+'s $EPOCHREALTIME (works on macOS/BSD where date lacks %N).
#   2. Fall back to `date +%s%N` and validate the output is purely numeric
#      (BSD date returns e.g. "1745247600N" literally — Python's int() would
#      drop the suffix, corrupting timing data).
#   3. Emit 0 if both fail so the JSONL line stays well-formed.
# Every shell `$` is escaped as `$$` so Make passes the literal `$` to the
# shell. In particular `$${EPOCHREALTIME-}` is critical: without the `$$`,
# Make would parse `${EPOCHREALTIME-}` as a Make variable reference (Make
# treats `${X}` and `$(X)` identically), substitute empty, and the bash 5
# fast path would silently never fire.
# Used both inline (test path) and as the body of a recursive Make
# variable CT_NS_EXPR (production path) so the snippet appears once on
# disk instead of being duplicated into every timed recipe.
_NS_EXPR_INLINE = (
    "{ "
    'if [ -n "$${EPOCHREALTIME-}" ]; then '
    'printf %s "$${EPOCHREALTIME//./}000"; '
    "else "
    "_ct_d=$$(date +%s%N 2>/dev/null); "
    'case "$$_ct_d" in '
    "''|*[!0-9]*) printf 0 ;; "
    '*) printf %s "$$_ct_d" ;; '
    "esac; "
    "fi; "
    "}"
)
_NS_EXPR_VAR_REF = "$(CT_NS_EXPR)"

# Cap each `rm -f` invocation in clean rules at this many paths so a build
# with tens of thousands of objects doesn't trip ARG_MAX (256KB on macOS,
# 2MB on Linux). 1000 paths * ~256-byte mean = ~256KB, well under the limit.
_RM_CHUNK_SIZE = 1000


def _rm_f_chunked(paths: list[str], chunk_size: int = _RM_CHUNK_SIZE) -> list[str]:
    """Split paths into multiple ``rm -f`` invocations to stay under ARG_MAX."""
    return ["rm -f " + " ".join(paths[i : i + chunk_size]) for i in range(0, len(paths), chunk_size)]


@register_backend
class MakefileBackend(BuildBackend):
    """Generate and execute GNU Makefiles.

    PCH rules and link/library rules with merged LDFLAGS come from
    BuildBackend.build_graph(); this class only renders the resulting
    BuildGraph as Makefile syntax.
    """

    def _honors_use_mtime(self) -> bool:
        return True

    @staticmethod
    def name() -> str:
        return "make"

    @staticmethod
    def tool_command() -> str:
        return "make"

    @staticmethod
    def build_filename() -> str:
        return "Makefile"

    @staticmethod
    def add_arguments(cap) -> None:
        """Register Make-specific CLI arguments.

        Safe to call more than once on the same parser.
        """
        _register_make_cli_arguments(cap)

    def generate(self, graph: BuildGraph, output=None) -> None:
        """Write Makefile from BuildGraph.

        Args:
            graph: The build graph to render.
            output: A file-like object to write to. If None, writes to
                the file specified by args.makefilename.
        """
        self._setup_file_locking()
        graph = self._apply_build_only_changed(graph)

        if output is not None:
            self._write_makefile(graph, output)
        else:
            if self._build_file_uptodate(graph):
                return
            with compiletools.filesystem_utils.atomic_output_file(
                self.args.makefilename, mode="w", encoding="utf-8"
            ) as f:
                self._write_makefile(graph, f)

    def _build_file_path(self) -> str:
        return self.args.makefilename

    @functools.cached_property
    def _timing_log_path(self) -> str:
        """Per-invocation JSONL log path.

        Namespaced by PID + monotonic_ns so that concurrent ``ct-cake --timing``
        invocations against the same Makefile do not race on the same file.
        """
        suffix = f"{os.getpid()}.{time.monotonic_ns()}"
        return os.path.join(
            os.path.dirname(self.args.makefilename) or ".",
            f".ct-make-timing.{suffix}.jsonl",
        )

    def _write_makefile(self, graph: BuildGraph, f) -> None:
        """Write a complete Makefile from the BuildGraph."""
        f.write(f"{self._build_file_header_token()}\n\n")
        f.write(".DELETE_ON_ERROR:\n\n")
        # A framework-detected test rule's ``output`` is its JUnit XML path
        # (so make reruns the test when the XML is deleted), while
        # ``success_marker`` stays the ``.result`` stamp. Such a test writes
        # its XML report and *then* exits non-zero on failure — without this
        # exemption ``.DELETE_ON_ERROR`` would delete the just-written XML,
        # contradicting the contract that a failed test still leaves its
        # report behind. ``.PRECIOUS`` exempts these targets from
        # deletion-on-error (and on interrupt). Tests with no framework keep
        # ``output == success_marker`` and are intentionally NOT protected.
        precious_xml = [
            rule.output
            for rule in graph.rules
            if rule.rule_type == RuleType.TEST and rule.output != rule.success_marker
        ]
        framework_test_success_markers = [
            rule.success_marker
            for rule in graph.rules
            if rule.rule_type == RuleType.TEST and rule.output != rule.success_marker and rule.success_marker
        ]
        # Grouped-target syntax (``a b &: deps``) ships in GNU Make 4.3+
        # and runs the recipe once to produce both targets. On older Make,
        # fall back to the multi-target form (``a b: deps``), which is
        # parsed as two independent rules sharing one recipe. The test
        # recipe is idempotent so a double-run is harmless, but the
        # grouped form is preferred when available so the contract holds
        # even if a future recipe edit breaks idempotency.
        grouped_target_supported = compiletools.apptools.tool_version("make") >= (4, 3)
        if precious_xml:
            f.write(".PRECIOUS: " + " ".join(precious_xml) + "\n\n")
        f.write("MAKEFLAGS += -rR\n\n")
        if os.path.isfile("/bin/bash"):
            f.write("SHELL := /bin/bash\n\n")
        if self._timer is not None:
            # Hoist the ns-timestamp shell helper into a Make variable so
            # it appears once on disk instead of in every timed recipe.
            # Recursive `=` defers expansion to the use site; the single
            # `$$` escape in `_NS_EXPR_INLINE` is then collapsed once by
            # recipe expansion, yielding the right text for the shell.
            # `:=` would collapse early and Make would re-parse the
            # surviving `${EPOCHREALTIME-}` as a Make variable lookup at
            # recipe expansion time, eating it.
            f.write(f"CT_NS_EXPR = {_NS_EXPR_INLINE}\n\n")

        # Write phony rules first so "all" is the default target
        phony_rules = [r for r in graph.rules if r.rule_type == RuleType.PHONY]
        non_phony_rules = [r for r in graph.rules if r.rule_type != RuleType.PHONY]

        # Ensure "all" comes first among phony rules
        phony_rules.sort(key=lambda r: (0 if r.output == "all" else 1, r.output))

        cas_only = not self.args.use_mtime
        for rule in phony_rules + non_phony_rules:
            if rule.rule_type == RuleType.PHONY:
                f.write(f".PHONY: {rule.output}\n")

            outputs = rule.output
            target_separator = ":"
            if rule.rule_type == RuleType.TEST and rule.output != rule.success_marker and rule.success_marker:
                # Framework tests with --test-xml-dir produce two observable
                # files on success: the JUnit XML report and the .result stamp.
                # The XML is .PRECIOUS so failed reports survive, but a later
                # make must still re-run the test when only that failed XML
                # exists. Emitting both targets lets runtests depend on both
                # without creating a separate recipe for the stamp.
                outputs = f"{rule.output} {rule.success_marker}"
                if grouped_target_supported:
                    target_separator = " &:"

            inputs = list(rule.inputs)
            if rule.rule_type == RuleType.PHONY and rule.output == "runtests":
                # See the multi-target TEST rule above: the phony aggregate must
                # require the success stamps as well as XML reports, otherwise
                # a preserved failed XML file would satisfy runtests.
                inputs.extend(m for m in framework_test_success_markers if m not in inputs)

            if cas_only and rule.rule_type in CAS_PRODUCER_TYPES:
                ordering = list(rule.order_only_deps) + cas_demoted_order_only(rule)
                line = f"{outputs}{target_separator}"
                if ordering:
                    line += f" | {' '.join(ordering)}"
            else:
                line = f"{outputs}{target_separator} {' '.join(inputs)}"
                if rule.order_only_deps:
                    line += f" | {' '.join(rule.order_only_deps)}"
            f.write(line + "\n")

            if rule.command:
                recipe = self._format_recipe(rule)
                f.write("\t" + recipe + "\n")
            f.write("\n")

        # Serialise tests: prevent Make from parallelising test execution
        if self.args.serialisetests and graph.rules_by_type(RuleType.TEST):
            f.write(".NOTPARALLEL: runtests\n\n")

        # Write clean rules
        self._write_clean_rules(graph, f)

    def _format_recipe(self, rule) -> str:
        """Format a BuildRule's command into a Makefile recipe string."""
        rt = rule.rule_type
        if rt == RuleType.COMPILE:
            cmd_str = self._wrap_compile_cmd(rule.command, cwd=rule.cwd)
            echo_target = rule.inputs[0] if rule.inputs else rule.output
            echo_prefix = "@"
        elif rt in (RuleType.LINK, RuleType.SHARED_LIBRARY, RuleType.STATIC_LIBRARY):
            cmd_str = self._wrap_link_cmd(rule.command)
            echo_target = rule.output
            echo_prefix = "+@"
        elif rt == RuleType.TEST:
            cmd_str = render_shell_recipe(rule)
            # The test exe is the rule's sole real dependency: it lives in
            # ``inputs`` under legacy mtime mode and in ``order_only_deps``
            # under CAS-only mode. ``command`` is no longer a reliable
            # source — ``_test_command_for`` appends framework XML argv
            # (e.g. ``--gtest_output=xml:...``) after the exe, so
            # ``command[-1]`` can be an XML flag rather than the exe.
            echo_target = (rule.inputs or rule.order_only_deps)[0]
            echo_prefix = "@"
        else:
            cmd_str = render_shell_recipe(rule)
            echo_target = None
            echo_prefix = ""

        if echo_target is not None and self.args.verbose >= 1:
            recipe = f"{echo_prefix}echo ... {echo_target} ; {cmd_str}"
        else:
            recipe = cmd_str

        if self._timer is not None and rt in CAS_PRODUCER_TYPES:
            recipe = self._wrap_with_timing(recipe, rule.output, ns_expr=_NS_EXPR_VAR_REF)
        return recipe

    def _wrap_with_timing(self, recipe: str, target: str, ns_expr: str | None = None) -> str:
        """Wrap a recipe with shell timing that writes to the JSONL log.

        ``ns_expr`` is the shell expression (or Make-variable reference) that
        prints a nanosecond timestamp on stdout. Defaults to the full
        portable inline snippet so test callers get a self-contained shell
        recipe; production callers in ``_format_recipe`` pass
        ``_NS_EXPR_VAR_REF`` to reference the once-defined Make variable
        and avoid bloating the on-disk Makefile.
        """
        if ns_expr is None:
            ns_expr = _NS_EXPR_INLINE
        log = shlex.quote(self._timing_log_path)
        # Embed the target as a JSON string literal (not a bare token); a
        # bare path produces invalid JSON like {"target":/foo.o,...} which
        # ``BuildTimer.record_rules_from_make_timing`` silently drops on
        # ``JSONDecodeError`` — leaving ``timing.json`` with phase rows
        # but no per-rule entries.  Single quotes inside the JSON encoding
        # must be escaped because the surrounding shell echo is a
        # single-quoted string.
        tgt_json = json.dumps(target).replace("'", "'\\''")
        return (
            f"@_ct_s=$$({ns_expr}); "
            f"{recipe.lstrip('@+')}; _ct_rc=$$?; "
            f"_ct_e=$$({ns_expr}); "
            f"echo '{{\"target\":{tgt_json},\"start_ns\":'$$_ct_s',\"end_ns\":'$$_ct_e'}}' >> {log}; "
            f"exit $$_ct_rc"
        )

    @staticmethod
    def _write_phony_recipe(f, name: str, parts: list[str]) -> None:
        f.write(f".PHONY: {name}\n{name}:\n\t-{';'.join(parts)}\n\n")

    def _write_clean_rules(self, graph: BuildGraph, f) -> None:
        """Write clean and realclean rules to the Makefile."""
        exe_dir = self.namer.executable_dir()
        obj_dir = self.namer.object_dir()

        # PCH .gch outputs are emitted as compile rules so they are
        # cleaned here. .gch files in a shared pchdir cache outside
        # obj_dir are intentionally NOT cleaned — that cache is
        # cross-variant/cross-build and may be in use by peers; use
        # ct-trim-cache to age them out.
        all_outputs = []
        all_objects = []
        for rule in graph.rules:
            if rule.rule_type == RuleType.COMPILE:
                all_objects.append(rule.output)
            elif rule.rule_type in (RuleType.LINK, RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY, RuleType.COPY):
                all_outputs.append(rule.output)

        clean_parts = [f"find {exe_dir} -type f -executable -delete 2>/dev/null"]
        clean_parts.extend(_rm_f_chunked(all_outputs + all_objects))
        clean_parts.append(f"find {obj_dir} -type d -empty -delete")
        if exe_dir != obj_dir:
            clean_parts.append(f"find {exe_dir} -type d -empty -delete")
        self._write_phony_recipe(f, "clean", clean_parts)

        # realclean: rm -rf the per-project exe_dir, but only this build's
        # products from obj_dir (which may be shared with peer sub-projects).
        # Mirrors BuildBackend.realclean() so `make realclean` and
        # `ct-cake --realclean` are equivalent.
        realclean_parts = [f"rm -rf {exe_dir}"]
        if exe_dir != obj_dir:
            realclean_parts.extend(_rm_f_chunked(all_outputs + all_objects))
            if all_outputs or all_objects:
                realclean_parts.append(f"find {obj_dir} -type d -empty -delete")
        self._write_phony_recipe(f, "realclean", realclean_parts)

    def _execute_build(self, target: str) -> None:
        timer = self._timer
        timing_log = self._timing_log_path if timer is not None else None
        if timing_log:
            # Defensive: remove our own fragment if it somehow exists already
            try:
                os.remove(timing_log)
            except FileNotFoundError:
                pass

        make_target = self._native_target_for(target)

        make_version = compiletools.apptools.tool_version("make")
        cmd = ["make"]
        if self.args.verbose <= 1:
            cmd.append("-s")
        if self.args.verbose >= 4 and make_version >= (4, 0):
            cmd.append("--trace")
        parallel = self.args.parallel
        if parallel > 1 and make_version >= (4, 0):
            cmd.append("--output-sync=target")
        if self.args.shuffle and make_version >= (4, 4):
            cmd.append("--shuffle")
        cmd.extend(["-j", str(parallel)])
        cmd.extend(["-f", self.args.makefilename, make_target])
        if self.args.verbose >= 1:
            print(" ".join(cmd))
        subprocess.check_call(cmd)

        if timer is not None and timing_log and os.path.exists(timing_log):
            timer.record_rules_from_make_timing(timing_log, graph=self._graph)
            try:
                os.remove(timing_log)
            except FileNotFoundError:
                pass


def main(argv=None):
    """Generate a Makefile for the given source files.

    CLI entry point bound to ``ct-create-makefile`` in ``pyproject.toml``.
    """
    cap = compiletools.apptools.create_parser(
        "Create a Makefile that will compile the given source file into an executable (or library)",
        argv=argv,
    )
    compiletools.apptools.add_target_arguments_ex(cap)
    compiletools.apptools.add_link_arguments(cap)
    compiletools.namer.Namer.add_arguments(cap)
    compiletools.hunter.add_arguments(cap)
    compiletools.jobs.add_arguments(cap)
    MakefileBackend.add_arguments(cap)

    args = None
    try:
        context = BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=context)
        headerdeps = compiletools.headerdeps.create(args, context=context)
        magicparser = compiletools.magicflags.create(args, headerdeps, context=context)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=context)

        backend = MakefileBackend(args=args, hunter=hunter)
        graph = backend.build_graph()
        backend.generate(graph)

    except OSError as ioe:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(f"Error processing {ioe.filename}: {ioe.strerror}")
            return 1
        else:
            raise
    except Exception as err:
        verbose = getattr(args, "verbose", 0) if args is not None else 0
        if verbose < 2:
            print(err)
            return 1
        else:
            raise
    return 0
