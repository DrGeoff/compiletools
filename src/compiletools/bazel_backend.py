"""Bazel backend — generates BUILD.bazel files from a BuildGraph.

Aggregates low-level compile/link rules back into high-level Bazel
cc_binary targets, since Bazel operates at a higher abstraction level
than Make/Ninja.
"""

from __future__ import annotations

import collections
import os
import shlex
import shutil
import subprocess
import sys

import compiletools.apptools
import compiletools.filesystem_utils
import compiletools.wrappedos
from compiletools.build_backend import (
    BuildBackend,
    aggregate_rule_sources,
    build_obj_info,
    extract_include_paths,
    extract_linkopts,
    mangle_target_name,
    register_backend,
)
from compiletools.build_graph import BuildGraph, BuildRule, RuleType

# Header file extensions used when globbing include directories to populate
# Bazel's srcs=[...] list (Bazel's sandbox cannot infer header ownership from
# includes=[...] alone, so we enumerate them explicitly).
_HDR_EXTS = frozenset((".h", ".hpp", ".hxx", ".hh", ".H", ".inl", ".inc", ".ipp"))


@register_backend
class BazelBackend(BuildBackend):
    """Generate and execute Bazel build files.

    Note: --file-locking is not applied to this backend. Bazel manages its
    own build sandbox and parallelism; external file locking would conflict
    with its internal coordination.
    """

    # Preference order for selecting which binary to invoke: bazelisk first
    # because it pins the bazel version via .bazelversion (we generate one).
    # Falls back to plain `bazel` when bazelisk is absent.
    _BAZEL_INVOKE_PREFERENCE = ("bazelisk", "bazel")
    # Canonical-name-first ordering for tool_command(): the first element is
    # what backend_tool_command() reports in user-facing diagnostics
    # ("Skipping backend 'bazel': 'bazel' not found on PATH"). Both tuples
    # contain the same set; only the order differs.
    _BAZEL_TOOLS = ("bazel", "bazelisk")
    _CACERTS_CANDIDATES = (
        "/etc/pki/ca-trust/extracted/java/cacerts",  # RHEL/Fedora
        "/etc/ssl/certs/java/cacerts",  # Debian/Ubuntu
        "/usr/lib/jvm/default/lib/security/cacerts",  # Arch
    )
    # Bazel 9.1+ is the supported minimum (bzlmod-only, no WORKSPACE shim).
    # rules_cc 0.1.5 is the last 0.1.x; bazel 9.1's resolver may still
    # upgrade it to 0.2.x at link time, but the explicit pin silences the
    # version-mismatch warning.
    _MIN_BAZEL_VERSION = "9.1.0"
    _RULES_CC_VERSION = "0.1.5"
    _BAZELRC_FILENAME = ".bazelrc"

    def _has_native_cas_exe(self) -> bool:
        # Bazel has its own content-addressable action cache and emits
        # its own cc_binary outputs from BUILD.bazel. Threading
        # compiletools' cas-exedir layer through would conflict with
        # bazel's output naming. Use legacy single-rule shape.
        return True

    @staticmethod
    def name() -> str:
        return "bazel"

    @classmethod
    def tool_command(cls) -> tuple[str, ...]:
        # Either bazel or bazelisk satisfies this backend; canonical name
        # ("bazel") is first for diagnostics. Invocation preference is in
        # _BAZEL_INVOKE_PREFERENCE — a deliberately distinct ordering.
        return cls._BAZEL_TOOLS

    @staticmethod
    def build_filename() -> str:
        return "BUILD.bazel"

    @classmethod
    def _find_bazel_tool(cls) -> str | None:
        """Return the first available bazel-family tool on PATH, or None."""
        return next((p for p in (shutil.which(n) for n in cls._BAZEL_INVOKE_PREFERENCE) if p), None)

    @classmethod
    def _default_base_dir(cls) -> str:
        """Resolved directory of the BUILD.bazel file (always absolute).

        Uses ``os.path.realpath`` directly, NOT ``wrappedos.realpath``:
        the latter is ``@functools.cache``'d on the input string, so the
        relative input ``"BUILD.bazel"`` would lock in the cwd at the
        first call site and return stale absolute paths after any later
        ``chdir``. Production paths don't normally chdir between
        ``generate()`` and ``_execute_build``, but tests do.
        """
        return os.path.dirname(os.path.realpath(cls.build_filename())) or "."

    @staticmethod
    def add_arguments(cap) -> None:
        """Register Bazel-specific CLI arguments.

        Safe to call more than once on the same parser.
        """
        if compiletools.apptools._parser_has_option(cap, "--bazel-jvm-stack-size"):
            return
        cap.add(
            "--bazel-jvm-stack-size",
            default="256k",
            help=(
                "Per-thread JVM stack size passed to bazel as "
                "--host_jvm_args=-Xss<value>. Bazel sizes its internal "
                "thread pool by --jobs and reserves the default 1MB stack "
                "per slot, which OOMs on many-core hosts. 256k is "
                "sufficient for bazel's worker threads. Set empty to skip."
            ),
        )

    def generate(self, graph: BuildGraph, output=None) -> None:
        graph = self._apply_build_only_changed(graph)

        if output is not None:
            # When writing to a file handle, try to determine the base directory
            # from the file's name attribute (set when opened with open()).
            # When writing to an in-memory buffer (e.g. StringIO in tests),
            # name is not a real path so leave base_dir=None: _bazel_src
            # then returns relative path strings only and never copies
            # source files into ext/.
            base_dir = None
            name = getattr(output, "name", None)
            if isinstance(name, str) and os.path.isabs(name):
                base_dir = os.path.dirname(compiletools.wrappedos.realpath(name))
            self._write_build(graph, output, base_dir=base_dir)
        else:
            filename = self.build_filename()
            base_dir = self._default_base_dir()
            with compiletools.filesystem_utils.atomic_output_file(filename, mode="w", encoding="utf-8") as f:
                self._write_build(graph, f, base_dir=base_dir)
            self._ensure_workspace(base_dir)

    def _write_build(self, graph: BuildGraph, f, base_dir: str | None = None) -> None:
        f.write("# BUILD.bazel generated by compiletools\n")

        test_exe_paths = {
            self.namer.executable_pathname(compiletools.wrappedos.realpath(source))
            for source in (self.args.tests or [])
        }

        # Classify each rule by Bazel target kind in one pass.
        plan: list[tuple[str, BuildRule, bool]] = []  # (kind, rule, linkshared)
        for rule in graph.rules_by_type(RuleType.STATIC_LIBRARY):
            plan.append(("cc_library", rule, False))
        for rule in graph.rules_by_type(RuleType.SHARED_LIBRARY):
            plan.append(("cc_binary", rule, True))
        for rule in graph.rules_by_type(RuleType.LINK):
            kind = "cc_test" if rule.output in test_exe_paths else "cc_binary"
            plan.append((kind, rule, False))

        kinds_present = {k for k, _, _ in plan}
        for kind in ("cc_binary", "cc_library", "cc_test"):
            if kind in kinds_present:
                f.write(f'load("@rules_cc//cc:{kind}.bzl", "{kind}")\n')

        if base_dir is None:
            base_dir = os.getcwd()

        obj_info = build_obj_info(graph, strip_includes=True)

        # Named-module interface compile objects (e.g. the .o produced from
        # math.cppm) are built locally by _prebuild_aux_artefacts before bazel
        # runs. Bazel's rules_cc ALLOWED_SRC_FILES does not include .cppm (or
        # compiler-shipped .cc module sources), so listing the source in
        # cc_binary srcs=[...] causes analysis-time failure:
        #   "source file '@@//:math.cppm' is misplaced here"
        # Drop named-module interface objects from obj_info entirely so that:
        #  (a) their source files (.cppm / system .cc) are not added to srcs,
        #  (b) their copts (e.g. "-x c++" injected for gcc<14 .cppm compat) do
        #      not bleed into the cc_binary copts list for the importer.
        # The prebuilt .o files themselves ARE added directly to srcs as
        # prebuilt object files (bazel cc_binary allows .o in srcs), so the
        # module's definitions are still linked into the final binary.
        _module_iface_obj_paths: frozenset[str] = frozenset(self._module_iface_obj.values())
        bazel_obj_info = {obj: info for obj, info in obj_info.items() if obj not in _module_iface_obj_paths}
        # Prebuilt .o paths for named-module interface units, workspace-relative.
        # These are added to each link/library target's srcs (not compile
        # targets) to ensure the object code is linked without recompilation.
        _module_iface_obj_rel: list[str] = sorted(
            rel
            for obj_path in _module_iface_obj_paths
            for rel in [self._workspace_relative(obj_path, base_dir)]
            if rel is not None
        )

        for kind, rule, linkshared in plan:
            srcs, all_copts = aggregate_rule_sources(rule, bazel_obj_info)
            target_name = mangle_target_name(os.path.basename(rule.output))
            # plan is built exclusively from cc_library / cc_binary / cc_test
            # rules (see the classification loop above). For all three kinds,
            # include prebuilt interface .o files so the module's definitions
            # are linked. Compile-only targets (if any) don't need the
            # interface .o — the importer's own compile uses -fmodule-mapper=
            # to find the prebuilt .gcm at runtime.
            srcs_set = {self._bazel_src(s, base_dir) for s in srcs}
            srcs_set.update(_module_iface_obj_rel)
            rel_srcs = sorted(srcs_set)
            linkopts: list[str] | None = None
            if kind != "cc_library":
                object_files = set(rule.inputs)
                linkopts = self._resolve_linkopts(extract_linkopts(rule.command, object_files) if rule.command else [])
            # Collect include paths that extract_copts(strip_includes=True) dropped.
            # Re-emit them via cc_binary(includes=[...]) so Bazel's include mechanism
            # sees //#INCLUDE= annotations and --include CLI paths.
            includes_seen: set[str] = set()
            includes: list[str] = []
            for obj in rule.inputs:
                compile_rule = graph.get_rule(obj)
                if compile_rule is None or compile_rule.command is None:
                    continue
                for inc in extract_include_paths(compile_rule.command):
                    if os.path.isabs(inc):
                        try:
                            rel = os.path.relpath(inc, base_dir)
                        except ValueError:
                            continue  # different drive / unresolvable; let toolchain handle
                        if rel.startswith(".."):
                            continue  # outside workspace; skip
                        inc = rel
                    # Skip "." — Bazel forbids includes=["."] since it
                    # would expose the entire workspace root to dependents.
                    if inc and inc != "." and inc not in includes_seen:
                        includes.append(inc)
                        includes_seen.add(inc)
            # Bazel's undeclared-inclusion check requires every header that
            # is actually #include'd to be listed in srcs (or reachable via
            # a cc_library dep). For directories exposed via includes=[...],
            # the compiler can see their headers but Bazel's sandbox cannot
            # infer ownership — so we glob all header files from each include
            # directory and add them to srcs. Only do this when base_dir is
            # a real path (not None / StringIO case) and the directory exists.
            existing_rel_srcs: set[str] = set(rel_srcs)
            extra_hdrs: list[str] = []
            if base_dir is not None:
                for inc in includes:
                    inc_abs = os.path.join(base_dir, inc) if not os.path.isabs(inc) else inc
                    if not os.path.isdir(inc_abs):
                        continue
                    for fname in os.listdir(inc_abs):
                        if os.path.splitext(fname)[1] in _HDR_EXTS:
                            hdr_rel = self._bazel_src(os.path.join(inc_abs, fname), base_dir)
                            if hdr_rel not in existing_rel_srcs:
                                extra_hdrs.append(hdr_rel)
                                existing_rel_srcs.add(hdr_rel)
            if extra_hdrs:
                rel_srcs = sorted(set(rel_srcs) | set(extra_hdrs))
            module_inputs, all_copts = self._bazel_module_inputs_and_copts(rule, all_copts, base_dir)
            self._emit_target(
                f,
                kind,
                target_name,
                rel_srcs,
                all_copts,
                linkopts,
                includes=includes if includes else None,
                linkshared=linkshared,
                additional_compiler_inputs=module_inputs,
            )

    @staticmethod
    def _starlark_str(s: str) -> str:
        """Quote *s* as a Starlark double-quoted string literal.

        Escapes backslash, double-quote, and the printable whitespace
        controls (``\\n`` / ``\\r`` / ``\\t``). Other ASCII controls
        (``< 0x20`` or ``0x7F``) raise ``ValueError``: Bazel's Java
        Starlark interpreter does not accept ``\\xNN`` hex escapes (the
        Go reference does, but BUILD files are parsed by the Java
        implementation), and rather than smuggle them through as octal
        escapes we treat them as a precondition violation — control
        chars in source filenames or flag tokens are pathological enough
        that erroring with the offending path is more useful than
        emitting BUILD bytes the parser will reject anyway.

        Python's ``repr()`` is unsuitable: it sometimes picks single
        quotes (legal Starlark but inconsistent with the file) and emits
        Python-only escapes like ``\\u00ff``.
        """
        out = ['"']
        for ch in s:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    f"refusing to emit control char ord={ord(ch)} in BUILD.bazel "
                    f"(Java Starlark parser rejects \\xNN escapes); offending "
                    f"input: {s!r}"
                )
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)

    @staticmethod
    def _starlark_copt(s: str) -> str:
        """Quote *s* as a Starlark string for use in a ``copts`` list.

        Bazel applies Bourne-shell tokenization on top of Starlark string
        parsing when it expands ``copts`` values into compiler argv.  A
        plain Starlark ``"`` escape (``\\"``) survives Starlark parsing as a
        literal ``"`` character, but Bazel's shell tokenizer then treats
        that ``"`` as a quoting delimiter and **strips it**, so
        ``-DFOO=\\"1.2.3\\"`` (Starlark: ``-DFOO="1.2.3"``) reaches the
        compiler as ``-DFOO=1.2.3`` — a number, not a string literal.

        The fix is one extra level of escaping for ``"``: emit ``\\\\\\\"``
        in the Starlark source (four bytes: ``\\``, ``\\``, ``\\``, ``"``).
        Starlark parses the leading ``\\\\`` as a literal backslash and
        ``\\"`` as a literal ``"``; the string value is therefore
        ``\\"`` (two chars: backslash + double-quote).  Bazel's Bourne-shell
        tokenizer then sees that ``\\"`` as a shell-escaped quote and passes a
        literal ``"`` to the compiler, giving gcc the argv token
        ``-DFOO="value"`` it expects.

        All other characters are handled identically to ``_starlark_str``.
        """
        out = ['"']
        for ch in s:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                # \\\" in the BUILD.bazel file:
                #   Starlark: \\\\ -> \, \" -> " => string value = \"
                #   Bazel shell tokenizer: \" -> literal "
                #   gcc argv: " (preserved double-quote)
                out.append('\\\\\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(
                    f"refusing to emit control char ord={ord(ch)} in BUILD.bazel "
                    f"(Java Starlark parser rejects \\xNN escapes); offending "
                    f"input: {s!r}"
                )
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)

    @staticmethod
    def _emit_target(
        f,
        kind: str,
        target_name: str,
        rel_srcs: list[str],
        all_copts: list[str],
        linkopts: list[str] | None = None,
        *,
        includes: list[str] | None = None,
        linkshared: bool = False,
        additional_compiler_inputs: list[str] | None = None,
    ) -> None:
        """Write a single ``cc_library`` / ``cc_binary`` / ``cc_test`` stanza."""
        q = BazelBackend._starlark_str
        qcopt = BazelBackend._starlark_copt
        lines = [f"\n{kind}(", f"    name = {q(target_name)},"]
        for attr, values, quoter in (
            ("srcs", rel_srcs, q),
            ("additional_compiler_inputs", additional_compiler_inputs, q),
            # copts use _starlark_copt: Bazel applies Bourne-shell tokenization
            # on top of Starlark parsing, which strips bare " chars.  Double-
            # escaping (\") lets the shell tokenizer preserve them as literals.
            ("copts", all_copts, qcopt),
            ("includes", includes, q),
            ("linkopts", linkopts, q),
        ):
            if not values:
                continue
            lines.append(f"    {attr} = [")
            lines.extend(f"        {quoter(v)}," for v in values)
            lines.append("    ],")
        if linkshared:
            lines.append("    linkshared = True,")
        lines.append(")\n")
        f.write("\n".join(lines))

    _BAZEL_MODULE_MAPPER_BASENAME = ".module-mapper.bazel.txt"

    def _bazel_module_inputs_and_copts(
        self,
        rule: BuildRule,
        all_copts: list[str],
        base_dir: str,
    ) -> tuple[list[str], list[str]]:
        """Collect the BMI/mapper inputs needed by *rule* and rewrite copts.

        Bazel's CcCompileAction enforces that every path appearing in the
        compile action's ``.d`` output is either declared as an input to
        the action or lives under a path in the toolchain's
        ``cxx_builtin_include_directories``. Module-mapper files and the
        ``.gcm`` / ``.pcm`` artefacts they reference do not satisfy
        either condition by default, so the absolute paths
        ``compiletools`` would otherwise embed in the compile command
        ("``-fmodule-mapper=/abs/path``") fail Bazel's input-validation
        step with "absolute path inclusion(s) found in rule ...".

        Returns ``(additional_compiler_inputs, rewritten_copts)``:

        * ``additional_compiler_inputs`` -- workspace-relative paths to
          every BMI/PCH artefact this rule depends on (sourced from
          ``rule.inputs`` filtered by ``_BMI_PCH_ARTEFACT_EXTS``: just
          the .gch / .pcm / .gcm files the compiler actually opens at
          compile time, NOT the .o link inputs that share the broader
          ``_COMPILE_ORDERING_INPUT_EXTS`` filter) plus the bazel-
          specific module-mapper file. Bazel symlinks each into the
          action's exec root, so gcc resolves workspace-relative
          paths to the right files.
        * ``rewritten_copts`` -- ``all_copts`` with any
          ``-fmodule-mapper=<abs>`` rewritten to point at the
          workspace-relative bazel-specific mapper file
          (``<base_dir>/<_BAZEL_MODULE_MAPPER_BASENAME>``).

        When the rule references no BMI artefacts the function is a
        no-op (returns empty inputs and the copts unchanged); the bazel
        backend then emits a normal cc_binary with no module wiring.
        """
        from compiletools.build_backend import _BMI_PCH_ARTEFACT_EXTS

        inputs: list[str] = []
        for path in rule.inputs:
            if not path.endswith(_BMI_PCH_ARTEFACT_EXTS):
                continue
            rel = self._workspace_relative(path, base_dir)
            if rel is None:
                # Outside the workspace -- bazel can't symlink it via
                # additional_compiler_inputs (the file path it stores
                # is workspace-relative). Skip rather than crash; the
                # importer compile will fail at compile time with a
                # clearer message naming the missing module.
                continue
            inputs.append(rel)
        # Walk transitively through any pcm_rule / obj_rule chain so an
        # importer that only directly references a single .pcm still
        # gets all its predecessors' BMIs wired in. The graph stores
        # those edges in ``inputs`` after _wire_module_inputs ran during
        # build_graph().
        if inputs and self._graph is not None:
            seen = set(inputs)
            queue = list(inputs)
            while queue:
                item = queue.pop()
                # Look up the rule that produces this artefact path so
                # we can pick up its own module inputs (e.g. a clang
                # pcm_rule with a partition .pcm in its inputs).
                producer = self._graph.get_rule(self._absolute_workspace_path(item, base_dir))
                if producer is None:
                    continue
                for path in producer.inputs:
                    if not path.endswith(_BMI_PCH_ARTEFACT_EXTS):
                        continue
                    rel = self._workspace_relative(path, base_dir)
                    if rel is None or rel in seen:
                        continue
                    seen.add(rel)
                    inputs.append(rel)
                    queue.append(rel)

        # GCC named-module interface .gcm artefacts are side effects of the
        # interface .o compile, NOT outputs of any rule, so they never appear
        # in rule.inputs of the importer. Walk _module_iface_gcm directly so
        # bazel's input validation sees the prebuilt .gcm files that the
        # module-mapper file references. Without this, bazel rejects the build
        # with "undeclared inclusion(s) ... 'std.c++-module'" or similar.
        # Rebuild seen-set after transitive walk above (which used its own
        # `seen` at the start of the walk) so GCM paths are deduped against
        # the full post-walk inputs list rather than just the initial direct inputs.
        seen_inputs: set[str] = set(inputs)
        for gcm_path in self._module_iface_gcm.values():
            rel = self._workspace_relative(gcm_path, base_dir)
            if rel is not None and rel not in seen_inputs:
                inputs.append(rel)
                seen_inputs.add(rel)

        copts = list(all_copts)
        mapper_basename = self._BAZEL_MODULE_MAPPER_BASENAME
        for i, c in enumerate(copts):
            if c.startswith("-fmodule-mapper="):
                copts[i] = f"-fmodule-mapper={mapper_basename}"
                if mapper_basename not in inputs:
                    inputs.append(mapper_basename)
                break

        return sorted(set(inputs)), copts

    @staticmethod
    def _workspace_relative(path: str, base_dir: str) -> str | None:
        """Return *path* as a workspace-relative string, or None if outside.

        ``..``-prefixed results indicate the path is outside the
        workspace; bazel cannot reach those via
        ``additional_compiler_inputs`` (which expects workspace
        labels), so the caller treats ``None`` as "skip this entry".
        """
        try:
            rel = os.path.relpath(path, base_dir)
        except ValueError:
            return None
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        return rel

    @staticmethod
    def _absolute_workspace_path(rel: str, base_dir: str) -> str:
        """Inverse of ``_workspace_relative`` — used to look up rules in the graph."""
        return os.path.normpath(os.path.join(base_dir, rel))

    @staticmethod
    def _resolve_linkopts(linkopts: list[str]) -> list[str]:
        """Resolve relative -L paths to absolute.

        Bazel executes the linker from a sandbox, so relative paths
        would not resolve to the correct directory.
        """
        resolved = []
        for opt in linkopts:
            if opt.startswith("-L") and not os.path.isabs(opt[2:]):
                resolved.append(f"-L{compiletools.wrappedos.realpath(opt[2:])}")
            else:
                resolved.append(opt)
        return resolved

    @staticmethod
    def _bazel_src(source: str, base_dir: str | None) -> str:
        """Return a Bazel-safe relative source path (PURE, no I/O).

        Bazel rejects target paths containing '..'.  When a source file
        lives outside the workspace this only computes the path that
        ``_prepare_external_sources`` would later copy to under
        ``<base_dir>/ext/<basename>``; nothing is created on disk here so
        ``generate()`` to a StringIO is side-effect-free.

        When *base_dir* is None (e.g. writing to an unnamed in-memory
        buffer) we cannot compute a meaningful relative path, so fall
        back to the source's realpath.
        """
        if base_dir is None:
            return compiletools.wrappedos.realpath(source)

        rel = os.path.relpath(source, base_dir)
        if not rel.startswith(".."):
            return rel

        # Source is outside the workspace — the corresponding copy will
        # live under <base_dir>/ext/<basename>; compute the would-be path
        # without touching the filesystem. The actual copy happens in
        # _prepare_external_sources(), invoked from generate() after the
        # Bazel build file has been written.
        if os.path.exists(source):
            dest = os.path.join(base_dir, "ext", os.path.basename(source))
            return os.path.relpath(dest, base_dir)

        # Source doesn't exist (e.g. mock/test paths) — use absolute path.
        return compiletools.wrappedos.realpath(source)

    def _prepare_external_sources(self, graph: BuildGraph, base_dir: str) -> None:
        """Copy out-of-workspace source files into ``<base_dir>/ext/``.

        Mirrors the path computation in ``_bazel_src`` but performs the
        actual filesystem mutation. Split out of ``_bazel_src`` so that
        ``generate()`` can compute path strings without I/O (e.g. for
        tests that write to a StringIO) and only invoke this step when
        actually preparing for a real Bazel build.
        """
        ext_dir = os.path.join(base_dir, "ext")
        ext_made = False
        obj_info = build_obj_info(graph, strip_includes=True)
        for rule in graph.rules:
            srcs, _ = aggregate_rule_sources(rule, obj_info)
            for source in srcs:
                rel = os.path.relpath(source, base_dir)
                if not rel.startswith(".."):
                    continue
                if not os.path.exists(source):
                    continue
                if not ext_made:
                    os.makedirs(ext_dir, exist_ok=True)
                    ext_made = True
                dest = os.path.join(ext_dir, os.path.basename(source))
                if not os.path.exists(dest):
                    shutil.copy2(source, dest)

    def _write_bazel_module_mapper(self, base_dir: str) -> None:
        """Materialise a bazel-specific gcc module mapper file.

        Mirrors :meth:`BuildBackend._write_gcc_module_mapper` but
        writes ``<base_dir>/<_BAZEL_MODULE_MAPPER_BASENAME>`` with
        workspace-relative ``.gcm`` paths on the right-hand side. The
        global mapper continues to use absolute paths (consumed by the
        non-bazel-driven precompile and by every other backend); the
        bazel-specific mapper is consumed only by bazel's CcCompileAction
        spawns, which run with CWD=execroot/_main and rely on bazel's
        symlinking of declared inputs (see
        :meth:`_bazel_module_inputs_and_copts`) to resolve those
        relative paths.

        No-op when the build does not use gcc named modules / header
        units (i.e. when the global mapper writer also produced
        nothing). The bazel backend then emits cc_binary stanzas
        without ``additional_compiler_inputs`` and without the
        ``-fmodule-mapper=`` rewrite.
        """
        if self._module_compiler_kind != "gcc" or not self._module_pcm_cache_root:
            return
        lines: list[str] = []
        for name in sorted(self._module_iface_gcm):
            rel = self._workspace_relative(self._module_iface_gcm[name], base_dir)
            if rel is not None:
                lines.append(f"{name} {rel}")
        for token in sorted(self._gcc_header_unit_resolved):
            abs_paths = self._gcc_header_unit_resolved[token]
            gcm_path = self._header_unit_artefact.get(token)
            if not gcm_path:
                continue
            rel = self._workspace_relative(gcm_path, base_dir)
            if rel is None:
                continue
            for abs_path in abs_paths:
                if abs_path:
                    lines.append(f"{abs_path} {rel}")
        if not lines:
            return
        path = os.path.join(base_dir, self._BAZEL_MODULE_MAPPER_BASENAME)
        new_content = "\n".join(lines) + "\n"
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == new_content:
                    return
        except FileNotFoundError:
            pass
        with compiletools.filesystem_utils.atomic_output_file(path, mode="w", encoding="utf-8") as fh:
            fh.write(new_content)

    def _ensure_workspace(self, output_dir: str) -> None:
        """Create a minimal MODULE.bazel and .bazelversion if absent.

        Bazel 9.1+ is the supported minimum; that line uses bzlmod
        exclusively, so no WORKSPACE shim is emitted. ``.bazelversion``
        pins the toolchain when invoked via bazelisk; raw ``bazel``
        ignores it.
        """
        module_path = os.path.join(output_dir, "MODULE.bazel")
        if not os.path.exists(module_path):
            with open(module_path, "w") as f:
                f.write('module(name = "compiletools_project")\n')
                f.write(f'bazel_dep(name = "rules_cc", version = "{self._RULES_CC_VERSION}")\n')
        version_path = os.path.join(output_dir, ".bazelversion")
        if not os.path.exists(version_path):
            with open(version_path, "w") as f:
                f.write(f"{self._MIN_BAZEL_VERSION}\n")

    def _all_outputs_current(self, graph: BuildGraph) -> bool:
        """Always re-execute the build.

        The base-class pre-check looks for compile/link outputs at the
        ``namer``-derived objdir/exedir paths.  Bazel builds in
        ``bazel-bin/`` (and uses its own action cache), so those paths
        are almost never populated by Bazel itself — the pre-check would
        return False "by accident" and skip the post-build copy of
        binaries from ``bazel-bin`` to ``topbindir``.  Make the contract
        explicit: always defer to Bazel, which has its own incremental
        build logic, and guarantee the post-build copy runs every time
        so the user-visible ``bin/`` stays in sync.
        """
        return False

    def _execute_build(self, target: str) -> None:
        tool = self._find_bazel_tool()
        if tool is None:
            raise RuntimeError("Neither 'bazelisk' nor 'bazel' found on PATH")

        # Bazel's spawn strategy is pinned to local in .bazelrc, so the
        # CAS-absolute paths the prebuilt artefacts live at resolve
        # correctly from inside the bazel-spawned compile.
        self._prebuild_aux_artefacts()

        base_dir = self._default_base_dir()
        # After the .gcm/.pcm artefacts exist on disk, materialise the
        # bazel-specific module mapper (workspace-relative paths) so
        # the importer compiles bazel later spawns find the BMIs via
        # paths that pass bazel's absolute-path-inclusion check.
        self._write_bazel_module_mapper(base_dir)
        # Materialise out-of-workspace sources into <base_dir>/ext/ now,
        # immediately before the actual Bazel build. Done here (not in
        # generate()) so that generate() to a StringIO stays a pure file
        # emission — no orphan ext/ dirs from test runs.
        if self._graph is not None:
            self._prepare_external_sources(self._graph, base_dir)
        # Refresh .bazelrc each invocation: its contents depend on
        # args (CC/CXX/parallel/jvm_stack) and on graph state
        # (_user_set_fuse_ld walks per-rule commands), all of which
        # may change between calls.
        self._write_bazelrc(base_dir)

        # The base-class ``execute()`` routes "runtests" through ``_run_tests``
        # before calling here, so the only targets that reach ``_execute_build``
        # are "build", "all", or a specific user-named target — all of which
        # we map to a valid Bazel label.
        if target in ("build", "all"):
            bazel_target = "//:all"
        else:
            bazel_target = f"//:{target}"
        # CLI is intentionally minimal — every static-per-build flag lives
        # in .bazelrc so ``bazel build //:all`` works directly from a
        # checkout without the compiletools wrapper. Only the target and
        # the dynamic --jobs override stay on the CLI.
        cmd = [tool, "build", *self._jobs_args(), bazel_target]
        self._run_bazel(cmd)
        self._publish_bazel_outputs()

    def _build_bazelrc_content(self) -> str:
        """Compose the .bazelrc body — pure, no I/O.

        Splits into ``startup`` (JVM-level flags consumed before the
        ``build`` subcommand) and ``build`` (per-action flags). Order
        within each section matches the previous CLI ordering so that
        ``grep .bazelrc`` is comparable to historical command lines.
        """
        lines = ["# .bazelrc generated by compiletools — regenerated each build"]

        for cacerts in self._CACERTS_CANDIDATES:
            if os.path.exists(cacerts):
                lines.append(f"startup --host_jvm_args=-Djavax.net.ssl.trustStore={cacerts}")
                lines.append("startup --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit")
                break
        # Bazel sizes its internal thread pool by --jobs and reserves the
        # default 1MB JVM thread stack per slot. On many-core hosts that
        # pre-reserves >100 MB of native memory and OOMs before any compile
        # runs. Default 256k via --bazel-jvm-stack-size; empty disables.
        jvm_stack = getattr(self.args, "bazel_jvm_stack_size", "256k") or ""
        if jvm_stack:
            lines.append(f"startup --host_jvm_args=-Xss{jvm_stack}")
        # During server startup bazel sizes ForkJoinPool.commonPool() and
        # IncrementalArtifactConflictFinder from Runtime.availableProcessors().
        # On many-core hosts that pre-spawns >100 native threads and OOMs before
        # --jobs even applies. Cap the JVM's CPU view to args.parallel.
        parallel = getattr(self.args, "parallel", None)
        if parallel:
            lines.append(f"startup --host_jvm_args=-XX:ActiveProcessorCount={parallel}")

        lines.append("build --spawn_strategy=local")
        lines.append("build --action_env=PATH")
        # Bazel's gcc autoconfig disables canonical system headers for
        # path-stable outputs, which makes gcc's <vector> resolution miss the
        # canonical path key in the module mapper. Re-enable so module-mapper
        # lookups land on the path compiletools wrote. (We additionally emit
        # both canonical and non-canonical mapper keys via
        # _resolve_system_header_abs_paths so the lookup hits even when
        # bazel's autoconfig flag-ordering wins after this option.)
        if self._graph_uses_gcc_modules():
            lines.append("build --cxxopt=-fcanonical-system-headers")
            # gcc's -MD depfile output for named-module imports includes virtual
            # entries like `math.c++-module` (the CXX_IMPORTS make-variable
            # format gcc uses for module dependency tracking). These are not
            # real files on disk; bazel's depfile inclusion scanner
            # misinterprets them as undeclared file inclusions and rejects
            # the build with "undeclared inclusion(s) ... 'math.c++-module'".
            # `-Mno-modules` suppresses the CXX_IMPORTS lines from the .d
            # output so bazel only sees real file dependencies; gcc's
            # -fmodule-mapper= already ensures correct build ordering.
            if self._module_iface_gcm:
                lines.append("build --cxxopt=-Mno-modules")
            # Bazel's gcc autoconfig appends `-std=c++17` to all C++ compile
            # actions. The header-unit precompile that compiletools runs
            # locally uses gcc's default `-std=` (typically C++20+ on
            # gcc-14+), producing a C++20-dialect .gcm. The bazel-driven
            # importer compile then sees `-std=c++17` (autoconfig's
            # default) and rejects with "language dialect differs 'C++20',
            # expected 'C++17'". Inject the std actually used by the
            # precompile so the importer matches. --cxxopt is passed as a
            # single token; bazel autoconfig's later -std=c++17 appears
            # too, but per-target/global cxxopts win when re-emitted
            # after autoconfig (last -std= wins). When the user has
            # explicitly set a newer standard (-std=c++23 / -std=c++26)
            # we must NOT downgrade them to c++20 -- modules work on
            # any C++20+; only inject the c++20 default when no -std=
            # is present at all.
            std_flag = next(
                (str(t) for t in self.args.flags.cxx if str(t).startswith("-std=")),
                "-std=c++20",
            )
            lines.append(f"build --cxxopt={std_flag}")
        # rules_cc 0.2.x defaults to -fuse-ld=lld at the GLOBAL bazel link
        # action, which gcc-only toolchains (e.g. gcc-15.2.0 ships gold, not
        # lld) cannot satisfy. Per-target linkopts (driven by LDFLAGS and magic
        # flags via extract_linkopts) propagate normally, but don't reliably
        # override the global rules_cc default. Trust an explicit user choice
        # (same contract as every other backend).
        if not self._user_set_fuse_ld():
            lines.append("build --linkopt=-fuse-ld=gold")
        # Bazel's local_config_cc autoconfig is a repo rule and does NOT
        # inherit the client PATH, so pass CC/CXX as absolute paths via
        # --repo_env (so the autoconfig wraps the right compiler) and
        # --action_env (so any toolchain that re-reads them at action
        # time agrees). Without this, bazel falls back to /bin/gcc, which
        # on RHEL 8 is gcc 8 and rejects -std=c++20.
        for var in ("CC", "CXX"):
            value = getattr(self.args, var, None)
            if not value:
                continue
            resolved = value if os.path.isabs(value) else shutil.which(value)
            if not resolved:
                continue
            lines.append(f"build --repo_env={var}={resolved}")
            lines.append(f"build --action_env={var}={resolved}")
        return "\n".join(lines) + "\n"

    def _write_bazelrc(self, base_dir: str) -> None:
        """Write .bazelrc next to BUILD.bazel, skipping the rewrite if unchanged.

        Concurrency: bazel reads ``.bazelrc`` per-command and only honours
        ``startup`` lines at server-startup, so two peer compiletools runs
        against the same workspace can interleave content and silently
        confuse the bazel server (it daemonises per startup-arg-fingerprint
        and outlives any single run). Compare-then-skip eliminates the
        race in the common case where peers want the same flags; when
        peers really do disagree, compiletools assumes a single
        concurrent run per workspace for the bazel backend (documented).
        The atomic temp+rename on write keeps individual rewrites
        torn-write safe even if the contract is violated.
        """
        path = os.path.join(base_dir, self._BAZELRC_FILENAME)
        new_content = self._build_bazelrc_content()
        compiletools.filesystem_utils.atomic_write_if_changed(path, new_content)

    def _jobs_args(self) -> list[str]:
        parallel = getattr(self.args, "parallel", None)
        return [f"--jobs={parallel}"] if parallel else []

    def _publish_bazel_outputs(self) -> None:
        """Copy bazel-bin/ outputs to namer paths and library paths.

        Executables land at ``namer.executable_pathname()`` (variant-specific
        dir like ``bin/<variant>/<name>``) so that ``_run_tests`` can find
        test executables at the path it computes from ``args.tests``;
        ``cake._copyexes`` afterwards handles the user-facing copy to
        ``topbindir`` / ``--output`` for ``args.filename`` targets.
        """
        bazel_bin = os.path.join(os.getcwd(), "bazel-bin")
        if not os.path.isdir(bazel_bin):
            return
        self._copy_built_executables(bazel_bin)
        self._copy_bazel_libraries(bazel_bin)

    @staticmethod
    def _token_picks_linker(tok: str) -> bool:
        """Return True if *tok* is a linker-selection flag like -fuse-ld=gold.

        Recognises the bare ``-fuse-ld=…`` form and the ``-Wl,-fuse-ld=…``
        passthrough (possibly with comma-separated peers like
        ``-Wl,-fuse-ld=gold,--no-as-needed``).
        """
        if tok.startswith("-fuse-ld="):
            return True
        if tok.startswith("-Wl,") and any(p.startswith("-fuse-ld=") for p in tok[4:].split(",")):
            return True
        return False

    def _graph_uses_gcc_modules(self) -> bool:
        """True if any rule's command carries ``-fmodule-mapper=`` (gcc-only)."""
        if self._graph is None:
            return False
        for rule in self._graph.rules:
            for tok in rule.command or ():
                if tok.startswith("-fmodule-mapper="):
                    return True
        return False

    def _user_set_fuse_ld(self) -> bool:
        """Return True if the user has already chosen a linker via -fuse-ld=...

        Checks LDFLAGS (CLI/config) and any per-target link command in the
        graph (covers magic-flag-injected linker choices). Tokenises LDFLAGS
        with shlex so quoted strings like ``-DSOMETHING="-fuse-ld=lld"``
        don't false-positive — a token only triggers if it is itself a
        linker flag, not if one is embedded inside it.
        """
        ldflags = getattr(self.args, "LDFLAGS", "") or ""
        try:
            ldflags_tokens = shlex.split(ldflags)
        except ValueError:
            # Malformed (unmatched quote, etc.); fall back to whitespace
            # split — the explicit user choice is more important than
            # precise tokenisation when LDFLAGS itself is broken.
            ldflags_tokens = ldflags.split()
        if any(self._token_picks_linker(t) for t in ldflags_tokens):
            return True
        if self._graph is not None:
            for rule in self._graph.rules:
                if rule.rule_type not in (RuleType.LINK, RuleType.SHARED_LIBRARY):
                    continue
                for tok in rule.command or ():
                    if self._token_picks_linker(tok):
                        return True
        return False

    def _run_bazel(self, cmd: list[str]) -> None:
        """Run *cmd*, streaming stderr through and diagnosing toolchain failures.

        On non-zero exit, if stderr matches the rules_cc-defaults-to-lld /
        missing-toolchain failure mode, augment the error with a clear
        remediation hint before raising CalledProcessError so cake.main's
        existing handler renders it normally and tests can capfd-match it.
        """
        proc = subprocess.Popen(cmd, text=True, stderr=subprocess.PIPE)
        if proc.stderr is None:  # pragma: no cover — Popen with PIPE always sets this
            raise RuntimeError("subprocess.Popen returned no stderr handle")
        # 32k lines (~3 MB worst case) is enough headroom for noisy bazel
        # builds — verbose linker chatter and per-target deprecation warnings
        # can easily push the real failure signature past a few hundred lines.
        tail: collections.deque[str] = collections.deque(maxlen=32000)
        for line in proc.stderr:
            sys.stderr.write(line)
            tail.append(line)
        rc = proc.wait()
        if rc == 0:
            return
        captured = "".join(tail)
        lower = captured.lower()
        lld_markers = ("cannot find 'ld'", "cannot find -lld", "fuse-ld=lld")
        toolchain_markers = ("could not find a c++ toolchain",)
        if any(m in lower for m in lld_markers) or any(m in lower for m in toolchain_markers):
            hint = (
                "\n"
                "Bazel's link step failed because the toolchain cannot find lld.\n"
                "rules_cc 0.2.x defaults to -fuse-ld=lld, which gcc-only modules\n"
                "(e.g. the gcc-15.2.0 module on this system) do not provide.\n"
                "\n"
                "Fix: load a clang module that ships lld (for example clang-latest)\n"
                "and rerun, or remove an explicit -fuse-ld= setting from your\n"
                "LDFLAGS / magic flags so the bazel backend can default to gold.\n"
            )
            sys.stderr.write(hint)
            captured += hint
        raise subprocess.CalledProcessError(rc, cmd, stderr=captured)

    def _copy_bazel_libraries(self, bazel_bin: str) -> None:
        """Copy Bazel-built libraries to namer paths.

        Bazel names libraries lib<target>.a in bazel-bin/.  Walk the
        graph to find expected library outputs and copy them.
        """
        if self._graph is None:
            return
        for lib_type in (RuleType.STATIC_LIBRARY, RuleType.SHARED_LIBRARY):
            ext = ".a" if lib_type == RuleType.STATIC_LIBRARY else ".so"
            for rule in self._graph.rules_by_type(lib_type):
                target_name = mangle_target_name(os.path.basename(rule.output))
                bazel_lib = os.path.join(bazel_bin, f"lib{target_name}{ext}")
                if os.path.exists(bazel_lib):
                    os.makedirs(os.path.dirname(rule.output), exist_ok=True)
                    compiletools.filesystem_utils.atomic_copy(bazel_lib, rule.output)

    def _bazel_clean(self, *extra: str) -> None:
        """Best-effort ``bazel clean [extra…]``; ignored if bazel is absent."""
        tool = self._find_bazel_tool()
        if tool is None:
            return
        try:
            subprocess.check_call([tool, "clean", *extra], text=True)
        except subprocess.CalledProcessError:
            pass

    def clean(self) -> None:
        """Run bazel clean, then remove build artifact directories."""
        self._bazel_clean()
        super().clean()

    def realclean(self, graph) -> None:
        """Run bazel clean --expunge, then selectively remove build products."""
        self._bazel_clean("--expunge")
        super().realclean(graph)
