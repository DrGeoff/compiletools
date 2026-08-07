"""Drift-guard lint-test: block low-value ("slop") tests at authorship.

The suite is currently clean (<1% slop). The point of this file is to
KEEP it clean forever by statically walking every ``src/compiletools/
test_*.py`` with the stdlib ``ast`` module and refusing to let a new test
land that verifies nothing, asserts a tautology, or swallows exceptions
without checking anything.

It mirrors the established in-repo lint style — ``test_entry_point_surface.py``
(its ``PINNED_CLI_TOOLS`` allowlist) and ``test_cas_dir_resolver_contract.py``
(its ``_RESOLVER_EXEMPT`` allowlist). Each rule has an explicit
``frozenset[str]`` allowlist keyed ``"filename::test_name"`` so an
intentional exception is documented inline, never silent.

Rules (all backed by ``ast`` only — no new deps, runs in well under a second
over the whole tree). All seven FAIL THE BUILD; there is no advisory tier, so
an unreviewed hit is an error. R4-R6 were labelled W1-W3 and were advisory
(``warnings.warn`` + an always-passing report test) until 2026-07-29 — commit
messages up to 9fc5a61a use the old names.

  R1 NO VERIFICATION  — a ``def test_*`` with zero ``assert`` / ``self.assert*``,
     zero ``pytest.raises``/``warns``/``fail``/``xfail``, no call to an
     asserting helper (name matches ``assert|compare|verify|expect|check|
     _still_raises|_pass``), and no ``asyncio.wait_for`` timeout wrapper.
  R2 TAUTOLOGY        — ``assert True`` / ``assert <truthy literal>`` /
     ``assert x == x`` (structurally identical operands) / ``assertTrue(True)``
     / ``assertEqual(x, x)``.  ``assert False, "msg"`` (a deliberate
     "unreachable" marker) is intentionally NOT flagged.
  R3 BARE EXCEPT      — a bare ``except:`` (NOT ``except Exception:``) with no
     assertion following it in the same function body.
  R4 MOCK-ASSERT-ONLY — the only verification is ``assert_called*`` /
     ``assert_not_called`` / ``assert_has_calls`` with no real
     ``assert`` / ``pytest.raises``.
  R5 CAPTURE-DISCARDED — a ``readouterr()`` result assigned to a name that is
     then never read.
  R6 NAME-PROMISE     — a name *token* says creates/writes/removes and no
     assert observes the filesystem (directly or one hop through a local), or
     says equals and no assert compares anything.
  R7 PHANTOM FLAG     — an argv list/tuple literal inside a test contains a
     ``--flag`` token that no parser in the package registers. argparse
     resolves an unambiguous prefix, so ``--filename`` silently became
     ``--filenametestmatch`` (a nargs=0 boolean defaulting to True) and its
     path argument fell through to the ``filename`` positional — the test
     passed while exercising a flag that does not exist.

R7's helper-body limitation: the rule sees only argv sequences written
literally inside a ``def test_*``. A phantom flag appended by a module-level
helper (``def _argv(*extras): return ["--bindir", d, *extras]``) is invisible,
as is one built by string concatenation or ``shlex.split``. It is a
lands-at-authorship guard for the common shape, not a proof of absence — the
23 ``--filename`` sites removed in 555a5045 included 3 helper-call sites and 1
helper docstring that only a manual grep found.

R4 is the one rule that routinely fires on *sound* tests: all 21 of its hits at
promotion time were legitimate interaction tests, and a mock assertion is the
only observable a dispatch-wiring or work-was-skipped test has. Expect new
interaction tests to need an allowlist entry — that is the rule working as a
sign-off gate ("is a call really the whole contract here?"), not as a defect
detector. R1-R3 and R5-R6, by contrast, only fire on tests that are actually
missing a check.

Precision history (why the R5/R6 predicates look the way they do): the first
cut of R5 asked "is the captured name referenced inside an ``assert``", which
flagged all 10 captures in the suite — every one of them the ordinary
``cap = capsys.readouterr(); parsed = json.loads(cap.out); assert parsed[...]``
shape, where the capture IS verified, one hop removed. The slop worth catching
is a capture taken and dropped on the floor, so the predicate is now "is the
name ever read at all". R6 likewise fired on 27 tests of which 26 were noise:
substring hits (``rewrites`` ⊃ ``writes``, ``flag_equals_value`` ⊃ ``equals``),
verbs used in a non-filesystem sense, and fs checks made through assertions
*stronger* than the whitelisted ``os.path`` predicates (``read_text()``,
``os.stat()``, ``rglob()``). Hence token-level matching, the wider evidence
set, and the one-hop dataflow below.
"""

from __future__ import annotations

import ast
import functools
import os
import re

# ---------------------------------------------------------------------------
# Allowlists — one per rule. Key format: "<filename>::<test_name>".
# Every entry MUST carry an inline comment justifying the exception, and
# every entry is typo-guarded by test_allowlist_entries_are_live below.
# ---------------------------------------------------------------------------

# R1: tests that legitimately verify without a syntactic assert/raises/helper.
# Every entry is a "does-not-raise" / "does-not-crash" / no-op negative test:
# the verification IS that the call under test completes without throwing.
# Adding a *new* zero-verification test still trips R1 — these are the
# enumerated, reviewed exceptions, not a blanket exemption.
_R1_NO_VERIFICATION_ALLOWLIST: frozenset[str] = frozenset(
    {
        # BazelBackend success path returns quietly (no exception, no output).
        "test_bazel_backend.py::test_success_returns_quietly",
        # clean()/realclean() must silently skip absent dirs/files.
        "test_build_backend.py::test_clean_skips_nonexistent_directories",
        "test_build_backend.py::test_realclean_skips_nonexistent_files",
        # BuildTimer is a no-op when given no output path.
        "test_build_timer.py::test_noop_without_path",
        # --static is accepted (does not raise) for the make backend.
        "test_cake_backend.py::test_static_flag_allowed_for_make_backend",
        # ccache statslog parsing tolerates a missing / unparseable file.
        "test_cake_ccache_statslog.py::test_missing_statslog_does_not_crash",
        "test_cake_ccache_statslog.py::test_unparseable_statslog_does_not_crash",
        # file_analyzer must not raise on multibyte / binary-garbage input.
        "test_file_analyzer.py::test_multibyte_before_raw_string_window",
        "test_file_analyzer.py::test_binary_garbage_does_not_crash",
        # findtargets.process at verbose>=2 must run without raising.
        "test_findtargets.py::test_process_verbose",
        # -isystem parsing / DirectHeaderDeps construction must not raise.
        "test_headerdeps.py::test_isystem_flag_parsing",
        # clear_instance_cache is a "just call it, must not raise" smoke test.
        "test_headerdeps.py::test_direct_clear_instance_cache",
        "test_hunter.py::test_clear_instance_cache_without_dynamic_attrs",
        # locking error/no-op paths must degrade gracefully, not crash.
        "test_locking.py::test_set_lockdir_permissions_chown_permission_error",
        "test_locking.py::test_release_error_handled",
        "test_locking.py::test_release_without_acquire",
        "test_locking.py::test_context_manager_no_file_locking",
        # touch-result-marker with an empty path is a documented no-op.
        "test_test_command_for.py::test_touch_result_marker_empty_path_is_noop",
        # skip_if_bazel_env_error is a no-op on unrelated stderr.
        "test_testhelper.py::test_skip_if_bazel_env_error_noop_on_unrelated_stderr",
    }
)

# R2: intentional tautologies (none expected — a tautology is never useful).
_R2_TAUTOLOGY_ALLOWLIST: frozenset[str] = frozenset(set())

# R3: bare-except tests where the swallow itself is the thing under test.
_R3_BARE_EXCEPT_ALLOWLIST: frozenset[str] = frozenset(set())

# R4: interaction tests where a mock assertion IS the correct observable — the
# contract under test is "this call was made (with these arguments)", so there
# is no resulting state to assert on. Reviewed 2026-07-29.
_R4_MOCK_ASSERT_ONLY_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Delegation / dispatch wiring: the postcondition IS "X was called".
        # Note the dispatch pair patches methods on the *expected* class, so a
        # wrong-class regression shows up as the patched method never firing.
        "test_apptools.py::test_verbose_level_2",
        "test_bazel_backend.py::test_clean_runs_bazel_clean_command",
        "test_cake.py::test_process_filelist_branch",
        "test_cake_backend.py::test_backend_dispatch_instantiates_correct_backend",
        "test_cake_backend.py::test_backend_dispatch_generates_compilation_database",
        "test_cake_backend.py::test_clean_calls_backend_clean_method",
        "test_cake_backend.py::test_realclean_calls_backend_realclean_method",
        "test_config.py::test_main_calls_create_parser_with_correct_args",
        "test_config.py::test_main_adds_cake_arguments",
        "test_config.py::test_main_calls_parseargs",
        # "the work was SKIPPED" guards (cache hit / short-circuit): the absence
        # of the subprocess IS the postcondition, so assert_not_called is the
        # only available observable.
        "test_build_backend.py::test_pre_lock_fast_path_resolves_relative_output_against_rule_cwd",
        "test_build_backend.py::test_no_op_when_graph_has_no_aux_rules",
        "test_cake.py::test_call_compilation_database_skipped_on_realclean",
        "test_shake_backend.py::test_verify_passes_when_hashes_match",
        "test_shake_backend.py::test_compile_skipped_when_object_exists_no_traces",
        "test_shake_backend.py::test_skipped_when_hardlinked_to_cas_input",
        "test_shake_backend.py::test_skipped_when_symlinked_to_cas_input",
        # The exact operand string handed to the compiler probe is the whole
        # contract (macro expansion of __has_attribute / __has_include operands),
        # and it is only observable in the call arguments.
        "test_simple_preprocessor.py::test_object_macro_operand_expanded_for_has_attribute",
        "test_simple_preprocessor.py::test_chained_object_macro_operand_fully_expanded",
        "test_simple_preprocessor.py::test_quoted_literal_header_operand_not_macro_expanded",
        "test_simple_preprocessor.py::test_angle_literal_header_operand_not_macro_expanded",
    }
)

# R5: captures deliberately taken and not read (none — a discarded capture is
# never useful; drain with a bare ``capsys.readouterr()`` instead of assigning).
_R5_CAPTURE_DISCARDED_ALLOWLIST: frozenset[str] = frozenset(set())

# R6: "creates"/"writes"/"removes" used in a NON-filesystem sense — the verb
# refers to an in-memory object (a BuildRule, an env var, a list/dict entry, a
# string buffer), so there is no path to probe and each of these asserts the
# real postcondition instead. Reviewed 2026-07-29.
_R6_NAME_PROMISE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # "creates" a BuildRule in the graph, not a file on disk.
        "test_build_backend.py::test_pch_header_creates_gch_compile_rule",
        "test_build_backend.py::test_pch_with_pchdir_creates_content_addressable_gch",
        "test_cake_startup_performance.py::test_auto_discovery_creates_analysis_objects_once_after_targets_are_known",
        # "removes" an environment variable / a list element / a dict key.
        "test_build_context.py::test_removes_var_when_it_was_unset",
        "test_cmake_backend.py::test_removes_orphan_x_flag",
        "test_cmake_backend.py::test_removes_x_with_paired_lang_arg",
        "test_compiler_macros.py::test_removes_bare_linux_and_unix",
        "test_flag_ops_properties.py::test_dedup_combined_form_preserves_set_and_removes_dups",
        "test_flag_ops_properties.py::test_strip_d_u_removes_all_define_undef",
        # "writes" into an in-memory buffer that the test then asserts on.
        "test_makefile_backend.py::test_generate_writes_makefile_syntax",
        "test_ninja_backend.py::test_generate_writes_ninja_syntax",
        "test_shake_backend.py::test_writes_summary_to_output",
        # "writes" an environment variable (os.environ), not a file.
        "test_build_apply.py::test_setenv_writes_and_saves_original",
        # Compares the full recursive file listing before/after — a stronger
        # check than any single existence probe — but the fs access happens
        # inside the _all_files_under helper, out of the collector's reach.
        # Key is shared with TestTrimPcmdir's same-named test, which does probe
        # os.path.isdir directly.
        "test_trim_cache.py::test_dry_run_removes_nothing",
    }
)

# R7: phantom --flags awaiting the fix on a sibling review branch. These are
# NOT exceptions — they are real defects owned by another member, allowlisted
# only so this branch lands green. ct-findtargets registers no --filename; the
# token prefix-resolves to --filenametestmatch and the path falls through to
# the positional. Fixed on gericksson/ct-review-fixups-cliflow @ 9d5fd488;
# DELETE these three lines when that merges (test_allowlist_entries_still_
# trip_their_rule will name them the moment they stop firing).
_R7_PHANTOM_FLAG_ALLOWLIST: frozenset[str] = frozenset(
    {
        "test_findtargets.py::test_a_library_slot_combined_with_filename_is_rejected",
        "test_findtargets.py::test_a_named_executable_still_reports",
        "test_findtargets.py::test_an_explicit_target_suppresses_discovery",
    }
)


# ---------------------------------------------------------------------------
# Regex vocabularies
# ---------------------------------------------------------------------------

# A call whose name matches this counts as "verification" for R1. Covers real
# asserts (self.assertEqual), delegating helpers (_check_*, _verify_*,
# expect_*, compare_*), negative-control helper names, and validator helpers
# (validate_*) whose contract is "raise on violation" — a call to one in a test
# body IS the negative "does-not-raise" assertion (e.g.
# validate_no_conf_contradictions).
_VERIFY_HELPER_RE = re.compile(r"assert|compare|verify|validat|expect|check|_still_raises|_pass", re.IGNORECASE)

# pytest verification callables (used as `with pytest.raises(...)` etc.).
_PYTEST_VERIFY_ATTRS = frozenset({"raises", "warns", "fail", "xfail"})

# timeout wrappers that ARE the verification (the test asserts "completes in time").
_TIMEOUT_ATTRS = frozenset({"wait_for"})

# mock-style pseudo-assertions (R4).
_MOCK_ASSERT_RE = re.compile(r"^assert_(called|not_called|has_calls|any_call)")

# fs-verb / equality name promises (R6). Matched against the name's underscore
# tokens, NOT as substrings: "rewrites" is not "writes" and
# "test_..._flag_equals_value" is not an equality claim.
_R6_FS_VERBS = frozenset({"creates", "writes", "removes"})
_R6_EQ_VERB = "equals"

# Attribute accesses that constitute an observation OF the filesystem (R6
# evidence). Deliberately wider than the os.path predicates: a test that asserts
# on read_text() / os.stat().st_mode / rglob() results has made a *stronger*
# claim than os.path.exists, and must not be told it forgot to check.
_R6_FS_EVIDENCE_ATTRS = frozenset(
    {
        "exists",
        "lexists",
        "isdir",
        "isfile",
        "islink",
        "getsize",
        "getmtime",
        "stat",
        "lstat",
        "listdir",
        "scandir",
        "walk",
        "glob",
        "rglob",
        "iterdir",
        "samefile",
        "is_file",
        "is_dir",
        "is_symlink",
        "read_text",
        "read_bytes",
        "read",
        "readlines",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime",
        "st_nlink",
    }
)

# Bare-name calls that open the filesystem (no attribute to key on).
_R6_FS_EVIDENCE_CALLS = frozenset({"open"})

# R7: the shapes that register an option string somewhere in the package.
# ``add_argument("--x")`` is the literal one; the three helpers below expand a
# bare name into a pair of option strings and would otherwise read as
# unregistered.
_R7_FLAG_HELPERS = frozenset({"add_flag_argument", "add_boolean_argument"})
_R7_XXPEND_ONE = "_add_xxpend_argument"
_R7_XXPEND_MANY = "_add_xxpend_arguments"

# Registered by argparse/configargparse itself, so they appear in no
# add_argument call in the tree.
_R7_BUILTIN_OPTIONS = frozenset({"--help", "--config"})

# Callees whose list/tuple argument IS an argv, regardless of what it holds.
_R7_ARGV_CALLEES = frozenset({"main", "parse_args", "parse_known_args", "parseargs"})


def _test_python_files():
    """Yield absolute paths of every ``test_*.py`` beside this file, except
    this linter itself (it would otherwise scan its own docstrings/regex)."""
    src_dir = os.path.dirname(__file__)
    me = os.path.basename(__file__)
    for fname in sorted(os.listdir(src_dir)):
        if fname.startswith("test_") and fname.endswith(".py") and fname != me:
            yield os.path.join(src_dir, fname)


def _iter_test_functions(tree: ast.AST):
    """Yield every ``def test_*`` / ``async def test_*`` node in a module tree."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def _call_names(func: ast.AST) -> list[str]:
    """Return the callee name of every Call in ``func`` (``func.attr`` for
    attribute calls, ``func.id`` for bare-name calls)."""
    names: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.append(f.attr)
            elif isinstance(f, ast.Name):
                names.append(f.id)
    return names


def _has_assert(func: ast.AST) -> bool:
    return any(isinstance(n, ast.Assert) for n in ast.walk(func))


def _has_pytest_raises(func: ast.AST) -> bool:
    return any(name in _PYTEST_VERIFY_ATTRS for name in _call_names(func))


def _is_verifying_call(name: str) -> bool:
    return name in _PYTEST_VERIFY_ATTRS or name in _TIMEOUT_ATTRS or _VERIFY_HELPER_RE.search(name) is not None


def _body_verifies(func: ast.AST, verifying_helpers: frozenset[str]) -> bool:
    """Does ``func``'s body verify anything, directly or by delegation?

    Direct: an ``assert`` / ``pytest.raises`` / an asserting-helper call
    (name matches ``_VERIFY_HELPER_RE`` or is a pytest/timeout attr).
    Delegated: a call to a locally-defined helper that itself verifies —
    ``verifying_helpers`` is the fixpoint set of such helper names. This is
    what stops the ~46 delegating tests (``self._compile_edit_compile(...)``,
    ``self._find_samples_targets(...)``, ``_test_library(...)``, ...) from
    tripping R1: the assertion lives in the helper, not the test body.
    """
    if _has_assert(func):
        return True
    return any(_is_verifying_call(name) or name in verifying_helpers for name in _call_names(func))


def _build_verifying_helper_names() -> frozenset[str]:
    """Fixpoint set of helper (non-``test_*``) function/method names — across
    every ``test_*.py`` — whose body verifies (transitively).

    Iterated to a fixpoint so a helper that only verifies by calling another
    helper is still recognised. Name-keyed (class scoping ignored): if ANY
    helper of a given name verifies, a call to that name counts as verifying.
    Over-permissive by design — it can only ever REDUCE false positives, and
    a brand-new zero-verification test that calls nothing still trips R1.
    """
    helpers: list[tuple[str, ast.AST]] = []
    for path in _test_python_files():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        helpers.extend(
            (node.name, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test_")
        )

    verifying: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, node in helpers:
            if name in verifying:
                continue
            if _body_verifies(node, frozenset(verifying)):
                verifying.add(name)
                changed = True
    return frozenset(verifying)


@functools.cache
def _verifying_helpers() -> frozenset[str]:
    return _build_verifying_helper_names()


def _has_verification(func: ast.AST) -> bool:
    """R1 predicate: does the test verify ANYTHING (direct or delegated)?"""
    return _body_verifies(func, _verifying_helpers())


# ---------------------------------------------------------------------------
# ERROR-rule collectors
# ---------------------------------------------------------------------------


def _collect_r1(func: ast.AST) -> bool:
    """True if the function is an R1 violation (no verification at all)."""
    return not _has_verification(func)


def _collect_r2(func: ast.AST) -> bool:
    """True if the function contains a tautological assertion."""
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            test = node.test
            # assert True / assert 1 / assert "x" — truthy literal only.
            # assert False/0/None is a deliberate "unreachable" marker; not slop.
            if isinstance(test, ast.Constant):
                try:
                    if bool(test.value):
                        return True
                except Exception:
                    pass
            # assert x == x  (structurally identical operands, single Eq).
            # Excludes operands that contain a Call: `assert f() == f()` is a
            # legitimate determinism/idempotency check (f could be impure), not
            # a tautology — e.g. hash_command(cmd) == hash_command(cmd).
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and ast.dump(test.left) == ast.dump(test.comparators[0])
                and not _contains_call(test.left)
            ):
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            # assertTrue(True) / assertFalse(False)
            if name in ("assertTrue", "assertFalse") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant):
                    want = name == "assertTrue"
                    if bool(a0.value) is want:
                        return True
            # assertEqual(x, x) / assertIs(x, x) — identical operands (same
            # Call-exclusion rationale as the assert x == x branch above).
            if (
                name in ("assertEqual", "assertEquals", "assertIs", "assertIsNot", "assertNotEqual")
                and len(node.args) >= 2
            ):
                if ast.dump(node.args[0]) == ast.dump(node.args[1]) and not _contains_call(node.args[0]):
                    return True
    return False


def _contains_call(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Call) for n in ast.walk(node))


def _collect_r3(func: ast.AST) -> bool:
    """True if the function has a bare ``except:`` with no assertion after it."""
    bare_lines = [h.lineno for h in ast.walk(func) if isinstance(h, ast.ExceptHandler) and h.type is None]
    if not bare_lines:
        return False
    assert_lines = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Assert)]
    # Violation if any bare-except has no assert on a later line.
    return any(not any(al > bl for al in assert_lines) for bl in bare_lines)


# ---------------------------------------------------------------------------
# R4-R6 collectors (build-failing; promoted from the advisory W1-W3 tier)
# ---------------------------------------------------------------------------


def _collect_r4(func: ast.AST) -> bool:
    """Mock-assert-only: mock pseudo-assert present, no real verification.

    "Real verification" is a bare ``assert``, a pytest verify attr, or a call
    whose name matches the asserting-helper regex (``self.assertEqual``,
    ``_verify_output``, ``check_...``) — computed over the call names with the
    mock pseudo-asserts removed first. That exclusion is load-bearing:
    ``assert_called_once`` itself matches the ``assert...`` verify-helper
    regex, so an unfiltered scan would count the mock assertion as its own
    alibi and R4 could never fire.

    Deliberately NOT extended to R1's ``_verifying_helpers`` fixpoint. The
    fixpoint is name-keyed across every test file, so ``config.main(...)`` —
    the production entry point under test — would count as verification just
    because some unrelated test file defines a verifying helper named ``main``
    (measured: that single collision exempts 5 of the 21 reviewed interaction
    tests). For R1 the over-permissiveness only suppresses false positives;
    for R4, whose job is to make the author confirm a call really is the whole
    contract, it would let the very call under test vouch for itself.
    """
    names = _call_names(func)
    has_mock = any(_MOCK_ASSERT_RE.match(n) for n in names)
    if not has_mock:
        return False
    if _has_assert(func):
        return False
    return not any(_is_verifying_call(n) for n in names if not _MOCK_ASSERT_RE.match(n))


def _collect_r5(func: ast.AST) -> bool:
    """A ``readouterr()`` capture assigned to names that are then never read.

    Not "never referenced inside an ``assert``" — a capture routed through an
    intermediate (``parsed = json.loads(cap.out)``) or consumed by a skip helper
    is verified, just not syntactically inside the assert node. The pattern this
    rule exists to catch is the capture that is taken and dropped on the floor.

    Tuple-unpacked captures (``out, err = capsys.readouterr()``) are grouped per
    assignment: the group counts as used when ANY of its names is read, so the
    common "assert on out, ignore err" shape is not flagged — only a capture
    discarded in its entirety is.
    """
    captured_groups: list[set[str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            f = node.value.func
            is_capture = isinstance(f, ast.Attribute) and f.attr == "readouterr"
            if is_capture:
                group: set[str] = set()
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        group.add(tgt.id)
                    elif isinstance(tgt, ast.Tuple):
                        group.update(e.id for e in tgt.elts if isinstance(e, ast.Name))
                if group:
                    captured_groups.append(group)
    if not captured_groups:
        return False
    loaded = {n.id for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return any(not (group & loaded) for group in captured_groups)


def _has_fs_evidence(node: ast.AST) -> bool:
    """Does this subtree observe the filesystem (a path predicate, a stat, a
    directory listing, or a read of a file's bytes)?"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _R6_FS_EVIDENCE_ATTRS:
            return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in _R6_FS_EVIDENCE_CALLS:
            return True
    return False


def _fs_derived_names(func: ast.AST) -> set[str]:
    """Locals bound to the result of a filesystem observation — the one hop of
    dataflow between ``text = log.read_text()`` and ``assert text.endswith(...)``
    that a purely syntactic "is it inside the assert" check misses."""
    derived: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _has_fs_evidence(node.value):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    derived.add(tgt.id)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            if _has_fs_evidence(node.context_expr) and isinstance(node.optional_vars, ast.Name):
                derived.add(node.optional_vars.id)
    return derived


def _collect_r6(name: str, func: ast.AST) -> bool:
    """Name promises a check the body lacks."""
    tokens = set(name.lower().split("_"))
    asserts = [n for n in ast.walk(func) if isinstance(n, ast.Assert)]
    if tokens & _R6_FS_VERBS:
        derived = _fs_derived_names(func)
        for a in asserts:
            if _has_fs_evidence(a):
                return False
            for sub in ast.walk(a):
                if isinstance(sub, ast.Name) and sub.id in derived:
                    return False
        return True
    if _R6_EQ_VERB in tokens:
        # Any comparison, any unittest-style assert call, or a pytest.raises
        # settles an "equals" claim — pinning it to `==` alone flagged
        # membership assertions and "...=... raises" negative tests.
        if _has_pytest_raises(func):
            return False
        # Every non-mock assert* call counts (assertEqual, assertDictEqual,
        # assertCountEqual, assertTrue(a == b), pandas' assert_frame_equal, ...)
        # rather than a hand-enumerated family: whether it is the *matching*
        # check is beyond static reach anyway — the same trade the any-Compare
        # relaxation below already makes — and a hardcoded list silently
        # excludes every variant it forgot. Mock pseudo-asserts stay excluded
        # (they verify a call happened, not a value).
        if any(n.startswith("assert") and not _MOCK_ASSERT_RE.match(n) for n in _call_names(func)):
            return False
        for a in asserts:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Compare):
                    return False
        return True
    return False


# ---------------------------------------------------------------------------
# R7 collector — phantom --flags in argv literals
# ---------------------------------------------------------------------------


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _non_test_python_files():
    """Yield absolute paths of every non-``test_`` module beside this file."""
    src_dir = os.path.dirname(__file__)
    for fname in sorted(os.listdir(src_dir)):
        if fname.endswith(".py") and not fname.startswith("test_"):
            yield os.path.join(src_dir, fname)


def _attr_name(node: ast.AST) -> str:
    """Trailing name of a Name/Attribute node (``argparse.Foo`` -> ``Foo``)."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


@functools.cache
def _boolean_optional_action_names() -> frozenset[str]:
    """Action classes that synthesize a ``--no-X`` for every ``--X``.

    ``argparse.BooleanOptionalAction`` does this inside ``__init__``, so its
    negated forms appear in no source literal — the package's own
    ``DocumentationAction`` subclasses it and contributes ``--no-man`` /
    ``--no-doc`` that only the live parser knows about.
    """
    names = {"BooleanOptionalAction"}
    for path in _non_test_python_files():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(_attr_name(b) in names for b in node.bases):
                names.add(node.name)
    return frozenset(names)


def _options_registered_in_tree(tree: ast.AST) -> set[str]:
    """Every option string one module tree registers, over the five shapes."""
    opts: set[str] = set()

    def named_arg(node: ast.Call, keyword: str) -> str | None:
        for kw in node.keywords:
            if kw.arg == keyword:
                return _const_str(kw.value)
        return _const_str(node.args[1]) if len(node.args) >= 2 else None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        callee = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if callee == "add_argument":
            literals: list[str] = []
            for arg in node.args:
                literal = _const_str(arg)
                if literal is None:
                    break
                if literal.startswith("-"):
                    literals.append(literal)
            opts.update(literals)
            action = next((kw.value for kw in node.keywords if kw.arg == "action"), None)
            if action is not None and _attr_name(action) in _boolean_optional_action_names():
                opts.update(f"--no-{lit[2:]}" for lit in literals if lit.startswith("--"))
        elif callee in _R7_FLAG_HELPERS:
            name = named_arg(node, "name")
            if name:
                opts.update({f"--{name}", f"--no-{name}"})
        elif callee == _R7_XXPEND_ONE:
            name = named_arg(node, "name")
            if name:
                opts.update({f"--prepend-{name.upper()}", f"--append-{name.upper()}"})
        elif callee == _R7_XXPEND_MANY:
            seq = node.args[1] if len(node.args) >= 2 else None
            for kw in node.keywords:
                if kw.arg == "xxpendableargs":
                    seq = kw.value
            if isinstance(seq, (ast.Tuple, ast.List)):
                for elt in seq.elts:
                    name = _const_str(elt)
                    if name:
                        opts.update({f"--prepend-{name.upper()}", f"--append-{name.upper()}"})
    return opts


@functools.cache
def _known_option_strings() -> frozenset[str]:
    """Every option string registered anywhere in the package.

    One global union, not a per-file set: a test module's own ad-hoc parser
    contributes to what counts as "known" in every other test module. That
    over-accepts by construction, but the direction is a false NEGATIVE (a
    missed phantom flag), never a false alarm, and it was measured to make no
    difference — global and per-file produce the same hit set on this tree.
    """
    opts: set[str] = set(_R7_BUILTIN_OPTIONS)
    for path in list(_non_test_python_files()) + list(_test_python_files()):
        with open(path, encoding="utf-8") as fh:
            opts |= _options_registered_in_tree(ast.parse(fh.read(), filename=path))
    return frozenset(opts)


def _argv_sequences_passed_to_a_parser(func: ast.AST) -> set[int]:
    """``id()`` of every list/tuple handed straight to a parser entry point."""
    found: set[int] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        callee = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        if callee in _R7_ARGV_CALLEES:
            found.update(id(a) for a in node.args if isinstance(a, (ast.List, ast.Tuple)))
    return found


def _collect_r7(func: ast.AST, known: frozenset[str]) -> bool:
    """A ``--flag`` literal in an argv sequence that no parser registers.

    A sequence qualifies as ct argv only if its first element is a ``-`` or
    ``ct-`` string literal AND it is either handed directly to a parser entry
    point or already carries at least one known ct option. Without both
    conditions the rule flags compiler-flag token fixtures (``--sysroot``),
    gtest/doctest/Catch2 xml argv, and ``git``/``make`` command lines.
    """
    direct = _argv_sequences_passed_to_a_parser(func)
    for node in ast.walk(func):
        if not (isinstance(node, (ast.List, ast.Tuple)) and node.elts):
            continue
        first = _const_str(node.elts[0])
        if first is None or not (first.startswith("-") or first.startswith("ct-")):
            continue
        tokens = [_const_str(e) for e in node.elts]
        bare = [t.split("=", 1)[0] for t in tokens if t and t.startswith("--") and len(t) > 2]
        if not bare:
            continue
        if id(node) not in direct and not any(b in known for b in bare):
            continue
        if any(b not in known for b in bare):
            return True
    return False


# ---------------------------------------------------------------------------
# Shared scan
# ---------------------------------------------------------------------------


def _scan(collector, allowlist: frozenset[str]) -> list[str]:
    """Run ``collector(func)`` over every test function; return non-allowlisted
    ``"<file>::<name>"`` keys where it returned True.

    Deduplicated: two classes in one file may define the same test name, which
    collapses to a single key (and to a single allowlist entry covering both).
    """
    hits: dict[str, None] = {}
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            key = f"{basename}::{func.name}"
            if key in allowlist:
                continue
            if collector(func):
                hits[key] = None
    return list(hits)


# ---------------------------------------------------------------------------
# Rule tests — all six fail the build
# ---------------------------------------------------------------------------


def test_no_test_lacks_verification():
    """R1: every ``def test_*`` must verify something."""
    hits = _scan(_collect_r1, _R1_NO_VERIFICATION_ALLOWLIST)
    assert not hits, (
        "These tests contain no verification (no assert / self.assert* / "
        "pytest.raises|warns|fail|xfail / asserting-helper call / timeout "
        "wrapper):\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: add a real assertion of the observable postcondition. "
        "If the test genuinely verifies via a helper the linter can't see, "
        "either rename the helper to match "
        "assert|compare|verify|expect|check|_still_raises|_pass, or add the "
        "test to _R1_NO_VERIFICATION_ALLOWLIST with a one-line justification."
    )


def test_no_tautological_assertions():
    """R2: no ``assert True`` / ``assert x == x`` / ``assertTrue(True)`` etc."""
    hits = _scan(_collect_r2, _R2_TAUTOLOGY_ALLOWLIST)
    assert not hits, (
        "These tests assert a tautology (always-true literal or identical "
        "operands) — they can never fail and verify nothing:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: assert the real value/postcondition instead."
    )


def test_no_bare_except_without_assertion():
    """R3: no bare ``except:`` that swallows without a following assertion."""
    hits = _scan(_collect_r3, _R3_BARE_EXCEPT_ALLOWLIST)
    assert not hits, (
        "These tests use a bare `except:` (which also swallows "
        "KeyboardInterrupt/SystemExit) with no assertion following it — a "
        "silent pass-through that hides failures:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: catch a specific exception (or `except Exception:`) and "
        "assert the expected outcome. If the swallow is deliberately the "
        "thing under test, add an assertion after it or allowlist it."
    )


def test_no_mock_assert_only_tests():
    """R4: a mock pseudo-assertion is not by itself proof of a postcondition."""
    hits = _scan(_collect_r4, _R4_MOCK_ASSERT_ONLY_ALLOWLIST)
    assert not hits, (
        "These tests verify only via assert_called*/assert_not_called/"
        "assert_has_calls — they pin the call graph, not the behaviour, so "
        "they keep passing while the thing the call was supposed to achieve "
        "breaks:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: assert the observable result as well (the file that got "
        "written, the value that got returned, the flag that got emitted). "
        "If the call itself IS the contract — dispatch/delegation wiring, or a "
        "'this work was skipped' guard where absence is the only observable — "
        "add the test to _R4_MOCK_ASSERT_ONLY_ALLOWLIST with a one-line "
        "justification, grouped under the matching comment block."
    )


def test_no_discarded_output_captures():
    """R5: a ``readouterr()`` result that is never read verifies nothing."""
    hits = _scan(_collect_r5, _R5_CAPTURE_DISCARDED_ALLOWLIST)
    assert not hits, (
        "These tests capture stdout/stderr into a name and then never read it "
        "— the capture looks like output verification but is dead:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: assert on the captured text, or — if the call is only "
        "there to drain the buffer so it doesn't leak into the next test — "
        "drop the assignment and call ``capsys.readouterr()`` bare, which this "
        "rule deliberately ignores."
    )


def test_no_unkept_name_promises():
    """R6: a name that says creates/writes/removes/equals must check it."""
    hits = _scan(lambda func: _collect_r6(func.name, func), _R6_NAME_PROMISE_ALLOWLIST)
    assert not hits, (
        "These test names promise a check the body does not make — the name "
        "token says creates/writes/removes but no assertion observes the "
        "filesystem, or says equals but no assertion compares anything:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: assert the promised postcondition (any of os.path.exists / "
        "isdir / read_text / os.stat / listdir counts, directly or one "
        "assignment away). If the verb is meant in a non-filesystem sense — "
        "'creates' a BuildRule, 'removes' a dict key, 'writes' to a string "
        "buffer — add the test to _R6_NAME_PROMISE_ALLOWLIST with a one-line "
        "justification, or rename the test so it doesn't claim a file."
    )


def test_no_phantom_cli_flags_in_argv_lists():
    """R7: every ``--flag`` in a test argv must be a registered option."""
    known = _known_option_strings()
    hits = _scan(functools.partial(_collect_r7, known=known), _R7_PHANTOM_FLAG_ALLOWLIST)
    assert not hits, (
        "These tests pass a --flag that no parser in the package registers. "
        "argparse resolves an unambiguous prefix, so the token either silently "
        "becomes a DIFFERENT option or is swallowed by parse_known_args — "
        "either way the test passes without exercising what it names:\n"
        + "\n".join(f"  {h}" for h in hits)
        + "\n\nFix: use the real option string (check the add_argument call), "
        "or drop the token if the value belongs on a positional. Truncating "
        "the flag until argparse errors is the quickest way to find which "
        "registered option it was resolving to. If the argv is genuinely for "
        "some other program, add the test to _R7_PHANTOM_FLAG_ALLOWLIST with "
        "a one-line justification."
    )


def test_known_option_strings_covers_the_real_ct_cake_parser():
    """The static extractor must not rot: every option the live ct-cake parser
    registers has to appear in the set R7 checks against.

    Containment, not equality — the static set is the union over every module
    in the package, so it is legitimately larger than any one tool's parser
    (it includes options registered by tools ct-cake does not compose).
    """
    import compiletools.apptools
    import compiletools.cake

    cap = compiletools.apptools.create_parser("test-no-slop-r7", argv=[])
    compiletools.cake.Cake.add_arguments(cap)
    live = {opt for action in cap._actions for opt in action.option_strings if opt.startswith("--")}

    missing = sorted(live - _known_option_strings())
    assert not missing, (
        "The live ct-cake parser registers option strings the R7 static "
        f"extractor does not see: {missing}\n\n"
        "A new registration shape was introduced (a wrapper helper, a loop "
        "over a name list, a dynamically built option string). Teach "
        "_options_registered_in_tree about it — until then R7 will flag every "
        "test that legitimately uses these flags."
    )


# ---------------------------------------------------------------------------
# Allowlist typo-guard (mirrors test_entry_point_surface / cas-dir-resolver)
# ---------------------------------------------------------------------------


def test_allowlist_entries_are_live():
    """Every allowlist key must name a real ``<file>::<test>`` that still
    exists, so a rename/removal can't leave a stale silent exemption."""
    live: set[str] = set()
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            live.add(f"{basename}::{func.name}")

    for label, allowlist in (
        ("_R1_NO_VERIFICATION_ALLOWLIST", _R1_NO_VERIFICATION_ALLOWLIST),
        ("_R2_TAUTOLOGY_ALLOWLIST", _R2_TAUTOLOGY_ALLOWLIST),
        ("_R3_BARE_EXCEPT_ALLOWLIST", _R3_BARE_EXCEPT_ALLOWLIST),
        ("_R4_MOCK_ASSERT_ONLY_ALLOWLIST", _R4_MOCK_ASSERT_ONLY_ALLOWLIST),
        ("_R5_CAPTURE_DISCARDED_ALLOWLIST", _R5_CAPTURE_DISCARDED_ALLOWLIST),
        ("_R6_NAME_PROMISE_ALLOWLIST", _R6_NAME_PROMISE_ALLOWLIST),
        ("_R7_PHANTOM_FLAG_ALLOWLIST", _R7_PHANTOM_FLAG_ALLOWLIST),
    ):
        stale = sorted(allowlist - live)
        assert not stale, f"{label} has entries that no longer exist: {stale}"


def test_allowlist_entries_still_trip_their_rule():
    """Every allowlist key must still FIRE its rule, not merely exist.

    ``_scan`` skips allowlisted keys before running the collector, and the
    liveness guard above only checks the test still exists — so an allowlisted
    test later rewritten to genuinely verify (e.g. an R4 interaction test that
    gains a real postcondition) would keep its entry forever as dead weight.
    This is the unused-``noqa`` check: entries self-expire, and the failure
    message says which line to delete.
    """
    funcs: dict[str, list[ast.AST]] = {}
    for path in _test_python_files():
        basename = os.path.basename(path)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        for func in _iter_test_functions(tree):
            # Same-name definitions in one file (two classes) collapse to one
            # key; the entry is live if ANY of them fires.
            funcs.setdefault(f"{basename}::{func.name}", []).append(func)

    collectors = {
        "_R1_NO_VERIFICATION_ALLOWLIST": (_R1_NO_VERIFICATION_ALLOWLIST, _collect_r1),
        "_R2_TAUTOLOGY_ALLOWLIST": (_R2_TAUTOLOGY_ALLOWLIST, _collect_r2),
        "_R3_BARE_EXCEPT_ALLOWLIST": (_R3_BARE_EXCEPT_ALLOWLIST, _collect_r3),
        "_R4_MOCK_ASSERT_ONLY_ALLOWLIST": (_R4_MOCK_ASSERT_ONLY_ALLOWLIST, _collect_r4),
        "_R5_CAPTURE_DISCARDED_ALLOWLIST": (_R5_CAPTURE_DISCARDED_ALLOWLIST, _collect_r5),
        "_R6_NAME_PROMISE_ALLOWLIST": (_R6_NAME_PROMISE_ALLOWLIST, lambda f: _collect_r6(f.name, f)),
        "_R7_PHANTOM_FLAG_ALLOWLIST": (
            _R7_PHANTOM_FLAG_ALLOWLIST,
            functools.partial(_collect_r7, known=_known_option_strings()),
        ),
    }
    for label, (allowlist, collector) in collectors.items():
        dead = sorted(key for key in allowlist if not any(collector(f) for f in funcs.get(key, [])))
        assert not dead, (
            f"{label} entries no longer trip their rule — the test now verifies properly, so delete these lines: {dead}"
        )


# ---------------------------------------------------------------------------
# Rule self-test
# ---------------------------------------------------------------------------

# Sources are parsed with ``ast``, never collected as tests — the linter walks
# module trees, so a string literal here is invisible to its own scan.
# (should_flag, rule, source)
_RULE_BEHAVIOUR_CASES: tuple[tuple[bool, str, str], ...] = (
    (True, "R4", "def test_x():\n    m.assert_called_once()\n"),
    (False, "R4", "def test_x():\n    m.assert_called_once()\n    assert m.rc == 0\n"),
    # unittest-style and conventionally-named-helper verification also count
    # (the mock pseudo-assert alone must not; hence the exclusion in the scan).
    (False, "R4", "def test_x(self):\n    m.assert_called_once()\n    self.assertEqual(a, b)\n"),
    (False, "R4", "def test_x():\n    m.assert_called_once()\n    _verify_output(x)\n"),
    (True, "R4", "def test_x():\n    m.assert_called_once()\n    run_build()\n"),
    (True, "R5", "def test_x(capsys):\n    run()\n    cap = capsys.readouterr()\n    assert rc == 0\n"),
    # Tuple-unpacked captures group per assignment: wholly discarded flags,
    # partially read (out asserted, err ignored) does not.
    (True, "R5", "def test_x(capsys):\n    run()\n    out, err = capsys.readouterr()\n    assert rc == 0\n"),
    (False, "R5", "def test_x(capsys):\n    out, err = capsys.readouterr()\n    assert 'x' in out\n"),
    (
        False,
        "R5",
        "def test_x(capsys):\n    cap = capsys.readouterr()\n    p = json.loads(cap.out)\n    assert p['a']\n",
    ),
    (True, "R6", "def test_creates_file():\n    write_it(p)\n    assert rc == 0\n"),
    (False, "R6", "def test_creates_file():\n    write_it(p)\n    assert os.path.exists(p)\n"),
    (False, "R6", "def test_creates_file():\n    write_it(p)\n    assert Path(p).read_text() == 'x'\n"),
    (False, "R6", "def test_creates_file():\n    t = Path(p).read_text()\n    assert t.endswith('x')\n"),
    # "rewrites" is not the token "writes"; "flag_equals_value" is not an
    # equality claim. Both were false positives before token-level matching.
    (False, "R6", "def test_rewrites_flag():\n    assert 'a' in out\n"),
    (True, "R6", "def test_a_equals_b():\n    assert thing\n"),
    (False, "R6", "def test_flag_equals_value():\n    assert '--x=1' in toks\n"),
    # Any non-mock assert* call settles an equals claim (unittest variants,
    # assertTrue(a == b) whose Compare hides inside the call args); a mock
    # pseudo-assert alone does not.
    (False, "R6", "def test_a_equals_b(self):\n    self.assertDictEqual(a, b)\n"),
    (False, "R6", "def test_a_equals_b(self):\n    self.assertTrue(a == b)\n"),
    (True, "R6", "def test_a_equals_b():\n    m.assert_called_once()\n"),
    (True, "R1", "def test_x():\n    do_thing()\n"),
    (True, "R2", "def test_x():\n    assert True\n"),
    (True, "R3", "def test_x():\n    try:\n        f()\n    except:\n        pass\n"),
    # R7 cases run against _R7_SELF_TEST_KNOWN, not the real registry, so they
    # stay stable when the package's option surface changes.
    (True, "R7", 'def test_x():\n    main(["--verbose", "--phantom", "f.cpp"])\n'),
    (False, "R7", 'def test_x():\n    main(["--verbose", "f.cpp"])\n'),
    # Handed to a parser entry point: no known-option anchor needed.
    (True, "R7", 'def test_x():\n    parse_args(["--phantom"])\n'),
    # Built into a local first, then used: qualifies on the known-option anchor.
    (True, "R7", 'def test_x():\n    argv = ["--verbose", "--phantom"]\n    run(argv)\n'),
    # An argv for some OTHER program (compiler, git, gtest): no ct option in
    # it and not handed to a ct parser, so the rule stays out of the way.
    (False, "R7", 'def test_x():\n    subprocess.run(["--sysroot", "/opt/sdk"])\n'),
    (False, "R7", 'def test_x():\n    run(["--gtest_output=xml:/tmp/x.xml"])\n'),
    # `=`-joined values are compared on the bare option string.
    (False, "R7", 'def test_x():\n    main(["--verbose=2"])\n'),
    (True, "R7", 'def test_x():\n    main(["--verbose", "--phantom=2"])\n'),
)

# Fixed option registry for the R7 self-test cases above.
_R7_SELF_TEST_KNOWN: frozenset[str] = frozenset({"--verbose", "--config"})


def test_rules_fire_on_slop_and_stay_quiet_on_good_tests():
    """Self-test: a rule that never fires is not a guard, and a rule that fires
    on sound tests gets ignored. Pins both directions for all six rules."""
    collectors = {
        "R1": lambda node: _collect_r1(node),
        "R2": lambda node: _collect_r2(node),
        "R3": lambda node: _collect_r3(node),
        "R4": lambda node: _collect_r4(node),
        "R5": lambda node: _collect_r5(node),
        "R6": lambda node: _collect_r6(node.name, node),
        "R7": lambda node: _collect_r7(node, _R7_SELF_TEST_KNOWN),
    }
    wrong: list[str] = []
    for should_flag, rule, source in _RULE_BEHAVIOUR_CASES:
        node = ast.parse(source).body[0]
        if collectors[rule](node) is not should_flag:
            verb = "did not flag" if should_flag else "wrongly flagged"
            wrong.append(f"  {rule} {verb}:\n{source}")
    assert not wrong, "Rule behaviour regressed:\n" + "\n".join(wrong)
