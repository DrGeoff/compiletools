"""Cross-target macro-verdict conflict detection.

Two targets can walk one shared header under different settled macro states,
and the deferred-diagnostic stores are partitioned per root target so one
target's resolution never silently retracts another's "cannot evaluate"
(``simple_preprocessor.verdict_root``). ``check_verdict_conflicts`` is where
the partitions meet, after every convergence has settled and before the
backend executes.

The fixture shape is the measured silent-green defect: ``shared_gate.h``
gates a ``//#CXXFLAGS`` row on ``#if GATE_CHAR == 120``; target A defines
``GATE_CHAR`` as an integer (evaluable), target B as a char constant
(``UnsafeExpressionError`` for the evaluator, TRUE for the compiler). Before
the classifier, that build went green with target B compiled under the wrong
flags and zero diagnostics at any verbosity.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import compiletools.apptools
import compiletools.testhelper as uth

_CAKE_DRIVER = "import sys, compiletools.cake as c; sys.exit(c.main(sys.argv[1:]))"

_SHARED_GATE = """#pragma once
#if GATE_CHAR == 120
//#CXXFLAGS=-DGATE_MODERN=1
#endif
"""

_B_DEF = """#pragma once
#define GATE_CHAR 'x'
"""

_A_MAIN = """#include "a_def.h"
#include "shared_gate.h"
int main() { return 0; }
"""

_B_MAIN = """#include "b_def.h"
#include "shared_gate.h"
int main() { return 0; }
"""


_FUNCLIKE_SHARED_GATE = """#pragma once
#if GATE_AT(1, 2) == 120
//#CXXFLAGS=-DGATE_MODERN=1
#endif
"""


def _write_funclike_workspace(root: Path) -> Path:
    """The conflict tree with a FUNCTION-LIKE gate condition.

    The condition text itself contains a parenthesized argument list, so any
    report that re-parses the composed message for a parenthesized reason
    extracts ``1, 2`` instead of the evaluator's error. The object-like
    fixture above cannot catch that: ``GATE_CHAR == 120`` has no parentheses.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "shared_gate.h").write_text(_FUNCLIKE_SHARED_GATE)
    (root / "a_def.h").write_text("#pragma once\n#define GATE_AT(a, b) 120\n")
    (root / "b_def.h").write_text("#pragma once\n#define GATE_AT(a, b) 'x'\n")
    (root / "a_main.cpp").write_text(_A_MAIN)
    (root / "b_main.cpp").write_text(_B_MAIN)
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _write_workspace(root: Path, *, a_gate_char: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "shared_gate.h").write_text(_SHARED_GATE)
    (root / "a_def.h").write_text(f"#pragma once\n#define GATE_CHAR {a_gate_char}\n")
    (root / "b_def.h").write_text(_B_DEF)
    (root / "a_main.cpp").write_text(_A_MAIN)
    (root / "b_main.cpp").write_text(_B_MAIN)
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _run_cake(workspace: Path, *extra_argv: str):
    """Subprocess for the same session-store-isolation reasons
    ``test_implied_source_closure.run_filelist`` documents."""
    compiler = compiletools.apptools.get_functional_cxx_compiler()
    assert compiler is not None
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CAKE_DRIVER,
            *extra_argv,
            f"--CXX={compiler}",
            f"--CC={compiler}",
            f"--bindir={workspace / 'bin'}",
            "a_main.cpp",
            "b_main.cpp",
        ],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestDivergentVerdictIsRefused:
    """One target resolves the gate TRUE, the other assumes it false: the
    products genuinely diverge, so the build must be refused before the
    backend runs — at EVERY verbosity, because the pre-fix behaviour was a
    wrong binary from a green, silent build."""

    @uth.requires_functional_compiler
    def test_the_conflict_is_a_hard_error_at_default_verbosity(self, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = _run_cake(workspace)

        assert proc.returncode != 0, proc.stderr
        assert "conflicting verdicts" in proc.stderr
        assert "evaluates TRUE" in proc.stderr
        assert "a_main.cpp" in proc.stderr and "b_main.cpp" in proc.stderr
        assert "Remedies:" in proc.stderr
        assert not (workspace / "bin" / "b_main").exists(), "the backend ran despite the conflict"

    @uth.requires_functional_compiler
    def test_warn_mode_reports_and_builds(self, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = _run_cake(workspace, "--macro-verdict-conflict=warn")

        assert proc.returncode == 0, proc.stderr
        assert "conflicting verdicts" in proc.stderr
        assert (workspace / "bin" / "b_main").exists()

    @uth.requires_functional_compiler
    def test_the_reason_names_the_evaluators_error_for_a_funclike_condition(self, tmp_path):
        """The 'cannot evaluate' line must carry the evaluator's error, not a
        fragment of the condition text. A function-like gate puts a
        parenthesized argument list inside the condition, so a reason
        extracted by re-parsing the message for its first parenthesized group
        prints '(1, 2)' where the error belongs."""
        workspace = _write_funclike_workspace(tmp_path / "ws")

        proc = _run_cake(workspace)

        assert proc.returncode != 0, proc.stderr
        assert "conflicting verdicts" in proc.stderr
        reason_lines = [line for line in proc.stderr.splitlines() if "cannot evaluate under target" in line]
        assert len(reason_lines) == 1, proc.stderr
        assert "Unsafe expression" in reason_lines[0], proc.stderr
        assert "(1, 2)" not in reason_lines[0], proc.stderr


class TestCoincidingFalseIsAWarningNotAnError:
    """Target A resolves the gate FALSE, target B assumes it false: the
    products agree byte-for-byte today, so refusing the build would be a
    false positive — but the gate diverges the day the macro bumps, so the
    latent conflict is still named."""

    @uth.requires_functional_compiler
    def test_the_build_succeeds_and_the_latent_conflict_is_named(self, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="90")

        proc = _run_cake(workspace, "-v")

        assert proc.returncode == 0, proc.stderr
        assert "conflicting verdicts" in proc.stderr
        assert "evaluates FALSE" in proc.stderr
        assert "coincide" in proc.stderr

    @uth.requires_functional_compiler
    def test_a_quiet_build_stays_quiet_for_the_coinciding_case(self, tmp_path):
        """The FALSE case is advisory, so it honours the verbose gate the
        recording run carried — unlike the TRUE case, which fires at every
        verbosity because it is a product inconsistency."""
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="90")

        proc = _run_cake(workspace)

        assert proc.returncode == 0, proc.stderr
        assert "conflicting verdicts" not in proc.stderr


_TOOL_DRIVERS = {
    "filelist": "import sys, compiletools.filelist as m; sys.exit(m.main(sys.argv[1:]))",
    "magicflags": "import sys, compiletools.magicflags as m; sys.exit(m.main(sys.argv[1:]))",
    "compilation_database": "import sys, compiletools.compilation_database as m; sys.exit(m.main(sys.argv[1:]))",
}


def _run_tool(tool: str, workspace: Path, *extra_argv: str):
    compiler = compiletools.apptools.get_functional_cxx_compiler()
    assert compiler is not None
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _TOOL_DRIVERS[tool],
            *extra_argv,
            f"--CXX={compiler}",
            f"--CC={compiler}",
            "a_main.cpp",
            "b_main.cpp",
        ],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestEveryMultiTargetToolAgreesWithCake:
    """Tool parity: a conflict ct-cake refuses must be refused by every other
    multi-target entry point over the same tree, with the same clean prose
    (no traceback) and the same warn-mode downgrade. One verdict_session
    implementation is the mechanism; these tests are the drift guard."""

    @uth.requires_functional_compiler
    @pytest.mark.parametrize("tool", sorted(_TOOL_DRIVERS))
    def test_the_divergent_conflict_is_refused_with_clean_prose(self, tool, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = _run_tool(tool, workspace)

        assert proc.returncode != 0, f"{tool} accepted a conflict cake refuses\n{proc.stderr}"
        assert "conflicting verdicts" in proc.stderr
        assert "Remedies:" in proc.stderr
        assert "Traceback" not in proc.stderr

    @uth.requires_functional_compiler
    @pytest.mark.parametrize("tool", sorted(_TOOL_DRIVERS))
    def test_warn_mode_downgrades_identically(self, tool, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = _run_tool(tool, workspace, "--macro-verdict-conflict=warn")

        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count("conflicting verdicts") == 1

    @uth.requires_functional_compiler
    @pytest.mark.parametrize("tool", sorted(_TOOL_DRIVERS))
    def test_the_report_names_the_tool_the_user_ran(self, tool, tmp_path):
        """A refusal attributed to ct-cake from a tool the user never
        invoked sends them hunting through the wrong command's docs."""
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = _run_tool(tool, workspace)

        entry_point = "ct-" + tool.replace("_", "-")
        assert f"{entry_point} error: conflicting verdicts" in proc.stderr, proc.stderr
        assert "ct-cake" not in proc.stderr, proc.stderr


class TestFetchDiscardsItsProvisionalVerdicts:
    """ct-fetch's scan is provisional by construction (no build settles it),
    so it must say nothing about conditions the next cake run resolves —
    and equally nothing about this conflict tree, which is not its job to
    judge. The armed control for this silence is the classifier tests
    above: the same tree IS reported by the tools that own a settling
    pass."""

    @uth.requires_functional_compiler
    def test_a_verbose_status_scan_prints_no_assume_false_lines(self, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, compiletools.fetch as m; sys.exit(m.main(sys.argv[1:]))",
                "-v",
                "--status",
                "a_main.cpp",
                "b_main.cpp",
            ],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert proc.returncode == 0, proc.stderr
        assert uth.unevaluable_lines(proc) == []
        assert "conflicting verdicts" not in proc.stderr


class TestAgreementIsSilent:
    """The must-not-move control: when every target evaluates the gate, no
    partition holds a pending verdict and the classifier has nothing to say.
    Guards against the classifier firing on mere multi-target builds."""

    @uth.requires_functional_compiler
    def test_two_targets_that_both_resolve_produce_no_conflict(self, tmp_path):
        workspace = _write_workspace(tmp_path / "ws", a_gate_char="120")
        (workspace / "b_def.h").write_text("#pragma once\n#define GATE_CHAR 121\n")

        proc = _run_cake(workspace, "-v")

        assert proc.returncode == 0, proc.stderr
        assert "conflicting verdicts" not in proc.stderr
        assert uth.unevaluable_lines(proc) == []
