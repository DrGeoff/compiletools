"""Cross-file macro visibility and the convergence iteration loop.

``SimplePreprocessor`` does not follow ``#include``, so a ``#if`` whose
controlling macro is defined in another file is unevaluable on its own and
falls back to assume-false. Only the ``DirectMagicFlags`` convergence, which
accumulates macro state across files and re-processes them until it settles,
makes those conditions resolve correctly.

Both gates in the fixture pick a different ``-l`` library on their false
branch, so a regression here is a mislink, not a warning.

Each property has a paired control that ablates the mechanism and asserts the
computed flags change. Without the control, a fixture that stopped exercising
the mechanism (for example by someone sorting ``chain_main.cpp``'s deliberately
reversed includes into dependency order) would keep passing while testing
nothing.
"""

import subprocess
import tempfile

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
        """Proof the chain still needs the loop. The ablation is what deleting
        the loop would leave behind: one accumulating pass that claims
        convergence, so the exhaustion error cannot mask the wrong flags.
        This fails if someone sorts chain_main.cpp's includes into dependency
        order, which would leave the build correct while removing the loop's
        only coverage.

        It also fails loudly if ``_converge_macro_state`` is deleted outright,
        since the patch target would no longer exist.
        """
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
        flags = _flags_for("chain_main.cpp")

        assert calls, "the ablation never ran, so this control proves nothing"
        assert "-DCHAIN_LOW=1" in flags.get("CXXFLAGS", ""), (
            f"without the iteration loop the chain should have resolved to the low branch, got {flags}"
        )
        assert "-lchain_low" in flags.get("LDFLAGS", ""), flags


class TestTheComputedFlagsSatisfyARealCompiler:
    """Each fixture TU static_asserts on the macro its gate emits, so feeding
    the computed flags to a compiler checks both that the right branch was
    taken and that the flags are actually deliverable."""

    @pytest.mark.parametrize(
        ("source", "expected_define"),
        [("simple_main.cpp", "-DSIMPLE_MODERN=1"), ("chain_main.cpp", "-DCHAIN_HIGH=1")],
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
