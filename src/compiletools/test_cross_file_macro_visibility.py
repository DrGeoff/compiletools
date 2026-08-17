"""Cross-file macro visibility and the convergence iteration loop.

``SimplePreprocessor`` does not follow ``#include``, so a ``#if`` whose
controlling macro is defined in another file is unevaluable on its own and
falls back to assume-false. Only the ``DirectMagicFlags`` convergence, which
accumulates macro state across files and re-processes them until it settles,
makes those conditions resolve correctly.

The object-like gates in the fixture pick a different ``-l`` library on their
false branch, so a regression there is a mislink, not a warning. The
function-like gate emits no flags at all when unresolved (its ``#ifdef``
wrapper is what keeps the fixture compilable by a real compiler), which its
``static_assert`` still turns into a compile error.

Each property has a paired control that ablates the mechanism and asserts the
computed flags change. Without the control, a fixture that stopped exercising
the mechanism (for example by someone sorting ``chain_main.cpp``'s deliberately
reversed includes into dependency order) would keep passing while testing
nothing.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path

_EXAMPLE = "cross_file_macro_visibility"


def _flags_for(source_name):
    """Return {flag_key: "joined values"} for one fixture translation unit."""
    sample_dir = example_path(_EXAMPLE)
    compiletools.magicflags.MagicFlagsBase.clear_cache()
    compiletools.headerdeps.HeaderDepsBase.clear_cache()
    context = BuildContext()
    # The temp config must NOT be created inside sample_dir: that is a
    # tracked fixture directory, and create_magic_parser never removes the
    # mkstemp'd .conf it creates there.
    with tempfile.TemporaryDirectory() as config_dir:
        parser = tb.create_magic_parser(
            ["--magic=direct", "--headerdeps=direct", "--include", sample_dir],
            tempdir=config_dir,
            context=context,
        )
        parser.clear_cache()
        result = parser.parse(f"{sample_dir}/{source_name}")
    return {str(key): " ".join(str(v) for v in values) for key, values in result.items()}


def _ablate_convergence_to_single_pass(monkeypatch):
    """Replace the convergence loop with what deleting it would leave behind:
    one accumulating pass that claims convergence — the real loop capped at
    max_iterations=1 would raise MacroConvergenceError instead of producing
    the ablated flags, and the exhaustion error must not mask the observable.
    Returns the call log; an empty log means the ablation never ran.

    Patching also fails loudly if ``_converge_macro_state`` is deleted
    outright, since the patch target would no longer exist."""
    calls = []

    def single_pass(self, all_files):
        calls.append(len(all_files))
        macro_key = self.defined_macros.get_cache_key()
        for fname in all_files:
            self._process_file_for_macros(fname, macro_key)
        return 1, True

    monkeypatch.setattr(
        compiletools.magicflags.DirectMagicFlags,
        "_converge_macro_state",
        single_pass,
    )
    return calls


class TestCrossFileMacroVisibility:
    """platform_level.h defines the macro simple_gate.h gates on."""

    def test_a_gate_resolves_against_a_macro_defined_in_another_file(self):
        flags = _flags_for("simple_main.cpp")

        assert "-DSIMPLE_MODERN=1" in flags.get("CXXFLAGS", ""), flags
        assert "-DSIMPLE_LEGACY" not in flags.get("CXXFLAGS", ""), flags

    def test_the_wrong_branch_would_select_a_different_library(self):
        """The consequence that makes this worth pinning: the gate chooses a
        link library, so an unevaluated condition mislinks silently."""
        flags = _flags_for("simple_main.cpp")

        assert "-lmodern_platform" in flags.get("LDFLAGS", ""), flags
        assert "-llegacy_platform" not in flags.get("LDFLAGS", ""), flags

    def test_control_removing_cross_file_visibility_changes_the_answer(self, monkeypatch):
        """Proof the fixture exercises the mechanism rather than passing for
        free. Clearing the accumulated variable macros before each file leaves
        the gate unevaluable, which must flip it to the legacy branch."""
        original = compiletools.magicflags.DirectMagicFlags._process_file_for_macros
        resets = []

        def forget_other_files(self, fname, macro_key=None):
            before = self.defined_macros
            self.defined_macros = before.without_keys(frozenset(before.variable.keys()))
            resets.append(fname)
            return original(self, fname, macro_key)

        monkeypatch.setattr(
            compiletools.magicflags.DirectMagicFlags,
            "_process_file_for_macros",
            forget_other_files,
        )
        flags = _flags_for("simple_main.cpp")

        assert resets, "the ablation never ran, so this control proves nothing"
        assert "-DSIMPLE_LEGACY=1" in flags.get("CXXFLAGS", ""), (
            f"without cross-file visibility the gate should have taken the false branch, got {flags}"
        )
        assert "-llegacy_platform" in flags.get("LDFLAGS", ""), flags


class TestConvergenceIterationLoop:
    """A three-deep macro chain included in reverse dependency order.

    No single accumulating pass resolves the chain, so this is the shape that
    distinguishes "convergence accumulates macros" from "convergence iterates
    until it settles". Neither the bundled examples nor a 500-source sample of
    a large external codebase contains it, which is why it is constructed here.
    """

    def test_a_reverse_order_macro_chain_resolves_to_the_settled_branch(self):
        flags = _flags_for("chain_main.cpp")

        assert "-DCHAIN_HIGH=1" in flags.get("CXXFLAGS", ""), flags
        assert "-DCHAIN_LOW" not in flags.get("CXXFLAGS", ""), flags
        assert "-lchain_high" in flags.get("LDFLAGS", ""), flags
        assert "-lchain_low" not in flags.get("LDFLAGS", ""), flags

    def test_control_replacing_the_loop_with_one_pass_changes_the_answer(self, monkeypatch):
        """Proof the chain still needs the loop. This fails if someone sorts
        chain_main.cpp's includes into dependency order, which would leave the
        build correct while removing the loop's only coverage."""
        calls = _ablate_convergence_to_single_pass(monkeypatch)
        flags = _flags_for("chain_main.cpp")

        assert calls, "the ablation never ran, so this control proves nothing"
        assert "-DCHAIN_LOW=1" in flags.get("CXXFLAGS", ""), (
            f"without the iteration loop the chain should have resolved to the low branch, got {flags}"
        )
        assert "-lchain_low" in flags.get("LDFLAGS", ""), flags


class TestFunctionLikeGateAcrossFiles:
    """funclike_def.h defines the function-like macro funclike_gate.h calls,
    behind a guard funclike_level.h unlocks, and the gate header is included
    first. Neither simple_main.cpp nor chain_main.cpp covers this: their gates
    are all object-like, so a regression that drops a macro's parameter list
    between convergence rounds would leave both of them green."""

    def test_a_function_like_gate_resolves_across_files_and_rounds(self):
        flags = _flags_for("funclike_main.cpp")

        assert "-DFUNCLIKE_GATE_SEEN=1" in flags.get("CXXFLAGS", ""), flags
        assert "-DFUNCLIKE_MODERN=1" in flags.get("CXXFLAGS", ""), flags
        assert "-DFUNCLIKE_LEGACY" not in flags.get("CXXFLAGS", ""), flags
        assert "-lfunclike_modern" in flags.get("LDFLAGS", ""), flags
        assert "-lfunclike_legacy" not in flags.get("LDFLAGS", ""), flags

    def test_control_replacing_the_loop_with_one_pass_changes_the_answer(self, monkeypatch):
        """One pass leaves the #ifdef wrapper false, so neither branch's flags
        are emitted — the observable difference the settled run must show. The
        unconditional sentinel proves the gate header was still reached and
        scanned, so the branch-flag absence is the gate staying unresolved,
        not the fixture silently dropping out of the walk."""
        calls = _ablate_convergence_to_single_pass(monkeypatch)
        flags = _flags_for("funclike_main.cpp")

        assert calls, "the ablation never ran, so this control proves nothing"
        assert "-DFUNCLIKE_GATE_SEEN=1" in flags.get("CXXFLAGS", ""), (
            f"the gate header was never reached, so the absences below prove nothing: {flags}"
        )
        assert "-DFUNCLIKE_MODERN" not in flags.get("CXXFLAGS", ""), (
            f"without the iteration loop the wrapped gate should have emitted no branch flags, got {flags}"
        )
        assert "-DFUNCLIKE_LEGACY" not in flags.get("CXXFLAGS", ""), flags
        assert "funclike" not in flags.get("LDFLAGS", ""), flags


class TestTheComputedFlagsSatisfyARealCompiler:
    """Each fixture TU static_asserts on the macro its gate emits, so feeding
    the computed flags to a compiler checks both that the right branch was
    taken and that the flags are actually deliverable."""

    @pytest.mark.parametrize(
        ("source", "expected_define"),
        [
            ("simple_main.cpp", "-DSIMPLE_MODERN=1"),
            ("chain_main.cpp", "-DCHAIN_HIGH=1"),
            ("funclike_main.cpp", "-DFUNCLIKE_MODERN=1"),
        ],
    )
    @uth.requires_functional_compiler
    def test_compiling_with_the_computed_flags_succeeds(self, source, expected_define):
        compiler = compiletools.apptools.get_functional_cxx_compiler()
        assert compiler is not None
        sample_dir = example_path(_EXAMPLE)
        cxxflags = _flags_for(source).get("CXXFLAGS", "")
        assert expected_define in cxxflags, f"precondition: {source} did not get {expected_define}"

        proc = subprocess.run(
            [compiler, "-fsyntax-only", *cxxflags.split(), f"{sample_dir}/{source}"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"compiling {source} with the computed flags failed:\n{proc.stderr}"

    @uth.requires_functional_compiler
    def test_control_the_same_source_fails_without_those_flags(self):
        """The static_assert is what makes the compile check discriminating, so
        prove it actually rejects the unflagged build."""
        compiler = compiletools.apptools.get_functional_cxx_compiler()
        assert compiler is not None
        sample_dir = example_path(_EXAMPLE)

        proc = subprocess.run(
            [compiler, "-fsyntax-only", f"{sample_dir}/simple_main.cpp"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "the fixture compiled without its computed flags, so the check is vacuous"
        assert "cross-file macro visibility regressed" in proc.stderr, proc.stderr


def test_the_chain_headers_are_included_in_reverse_dependency_order():
    """Guards the fixture's own shape. The reverse order is what makes the
    loop observable; a tidy-up that sorts these would silently defeat
    TestConvergenceIterationLoop's control."""
    source = f"{example_path(_EXAMPLE)}/chain_main.cpp"
    with open(source) as fh:
        text = fh.read()

    positions = {name: text.index(f'#include "{name}"') for name in ("chain_gate.h", "chain_mid.h", "chain_base.h")}
    assert positions["chain_gate.h"] < positions["chain_mid.h"] < positions["chain_base.h"], (
        "chain_main.cpp must include the chain top-first; sorting it into dependency order "
        "removes the only coverage of the convergence iteration loop"
    )


def test_the_funclike_headers_are_included_in_reverse_dependency_order():
    """Same shape guard for the function-like pair: the gate header must come
    before the header defining the macro it calls."""
    source = f"{example_path(_EXAMPLE)}/funclike_main.cpp"
    with open(source) as fh:
        text = fh.read()

    positions = {
        name: text.index(f'#include "{name}"') for name in ("funclike_gate.h", "funclike_def.h", "funclike_level.h")
    }
    assert positions["funclike_gate.h"] < positions["funclike_def.h"] < positions["funclike_level.h"], (
        "funclike_main.cpp must include the chain top-first; sorting it into dependency order "
        "removes the only function-like coverage of the convergence iteration loop"
    )


_CAKE_DRIVER = "import sys, compiletools.cake as c; sys.exit(c.main(sys.argv[1:]))"


def _run_cake(workspace, source_name, *, compiler):
    """Build one fixture source with ``ct-cake`` in a fresh interpreter.

    ``ct-cake`` rather than ``ct-magicflags``, because only cake runs the
    ``//#GIT=`` pre-fetch scan, and only a subprocess isolates the
    session-wide report stores that make an in-process "nothing was printed"
    assertion pass for the wrong reason (the same reasoning
    ``test_implied_source_closure.run_filelist`` documents).
    """
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CAKE_DRIVER,
            "-v",
            f"--CXX={compiler}",
            f"--CC={compiler}",
            f"--bindir={workspace / 'bin'}",
            source_name,
        ],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=300,
    )


_unevaluable_lines = uth.unevaluable_lines


class TestProvisionalScanDoesNotReportToTheUser:
    """A pass that runs before the macro state settles must stay silent.

    ``ct-cake`` scans every target for ``//#GIT=`` declarations before it
    builds, and that scan walks headers with an empty macro state
    (``fetch._reachable_sources`` -> ``headerdeps.process(target,
    frozenset())``). ``bare_gate.h``'s function-like gate is unevaluable
    there and evaluable once the build settles, so a report from the scan is
    contradicted by the flags the same run goes on to emit.

    Only ``ct-cake`` reaches that scan -- ``ct-magicflags``, ``ct-filelist``
    and ``ct-headertree`` never fetch -- which is why this class drives cake
    and the rest of the module does not.
    """

    @uth.requires_functional_compiler
    def test_the_prefetch_scan_says_nothing_about_a_gate_the_build_resolves(self, tmp_path):
        compiler = compiletools.apptools.get_functional_cxx_compiler()
        assert compiler is not None
        workspace = uth.copy_example_workspace(Path(example_path(_EXAMPLE)), tmp_path / "resolved")

        proc = _run_cake(workspace, "bare_main.cpp", compiler=str(compiler))

        assert proc.returncode == 0, proc.stderr
        assert _unevaluable_lines(proc) == [], "the pre-fetch scan reported a condition the settled build resolves"

    @uth.requires_functional_compiler
    def test_a_condition_no_pass_can_resolve_is_still_reported(self, tmp_path):
        """The armed control for the assertion above.

        Deleting the ``#include`` leaves ``BARE_AT_LEAST`` defined nowhere in
        the translation unit, so no amount of convergence makes the gate
        evaluable and the report is the build's real answer. Without this,
        "nothing was printed" would also be satisfied by a fix that silenced
        the diagnostic wholesale instead of letting it settle first.
        """
        compiler = compiletools.apptools.get_functional_cxx_compiler()
        assert compiler is not None
        workspace = uth.copy_example_workspace(Path(example_path(_EXAMPLE)), tmp_path / "unresolvable")
        gate = workspace / "bare_gate.h"
        gate.write_text(gate.read_text().replace('#include "bare_def.h"\n', ""))

        proc = _run_cake(workspace, "bare_main.cpp", compiler=str(compiler))

        reported = _unevaluable_lines(proc)
        assert len(reported) == 1, f"expected exactly one report, got {reported}\n{proc.stderr}"
        assert "BARE_AT_LEAST" in reported[0]
        assert "bare_gate.h" in reported[0]


def test_the_bare_gate_header_includes_its_definer_itself():
    """Guards the fixture's shape. bare_gate.h must pull in its own definer:
    that is what keeps the tree compilable while leaving a standalone,
    empty-macro-state pass unable to evaluate the gate. Moving the include
    out to bare_main.cpp would leave the build correct and remove the only
    coverage of a provisional pass reporting."""
    text = (Path(example_path(_EXAMPLE)) / "bare_gate.h").read_text()

    assert text.index('#include "bare_def.h"') < text.index("#if BARE_AT_LEAST"), (
        "bare_gate.h must include bare_def.h above its own gate"
    )
