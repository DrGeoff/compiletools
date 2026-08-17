"""Dependency-closure correctness for a file reached as an implied source.

``hunter`` walks a header's sibling ``.cpp`` as an implied source under that
source's *own* converged macro state, so a ``#if`` guard whose operands are
defined in a header the implied source itself includes resolves the way the
compiler resolves it. Walked under the root TU's state instead, the guard falls
to the assume-false fallback and the closure names the branch the compiler did
not take. The shipped ``implied_source_version_guard`` example and its closure
test live in ``test_function_like_macros.py``; this module covers the five
things that one test cannot cover on its own:

* **The object-like guard form.** ``#if PUMPLIB_VERSION_MAJOR >= 2`` is wrong
  in exactly the same way and emits no warning at all, because an undefined
  object-like macro is legally 0. Only a closure assertion can catch it, so a
  fix that moves only the audible function-like row leaves this one wrong.
* **The silence control.** After the fix ``ct-filelist -v`` must not print the
  unevaluable-condition warning for the example -- and must not print it from
  a superseded convergence pass either, which is why the control runs the real
  CLI in a fresh process rather than inspecting one preprocessor object.
  ``test_the_walk_still_reports_a_genuinely_unevaluable_condition`` is its
  armed counterpart: a guard macro defined nowhere stays unevaluable after the
  fix, so that row must keep warning. Without it, "no warning" would be
  satisfied by a harness that cannot warn at all.
* **The hop below the implied source.** The shipped example puts the guard in
  the implied source itself. ``TestGuardBelowTheImpliedSource`` puts it in a
  header the implied source includes, so the closure is only right if the
  source's converged state is threaded into the descent as well as into the
  source's own header scan. Applied at one of those two sites and not the
  other, the closure unions both branches instead of choosing one.
* **A header read at two macro states.** ``TestHeaderSeenAtTwoMacroStates``
  pins the other side of the same change: once each source carries its own
  state, a file already in the closure can arrive again under a second one.
  The closure has to union the two readings and still terminate.
* **Boundary rows.** Eight shipped examples whose closures already agree with
  ``g++ -MM``. They must not move. Every one of them asserts an *absence* --
  the header on the branch the compiler did not take -- because the failure
  mode of a half-applied fix is a closure that unions both branches.
"""

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import configargparse
import pytest

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_path

_SUBPROCESS_TIMEOUT = 120

_GUARD_EXAMPLE = "implied_source_version_guard"

_FUNCTION_LIKE_GUARD = "#if PUMPLIB_AT_LEAST(1, 2, 0)"
_OBJECT_LIKE_GUARD = "#if PUMPLIB_VERSION_MAJOR >= 2"
_NEVER_DEFINED_GUARD = "#if PUMPLIB_DEFINED_NOWHERE(1, 2, 0)"


_FILELIST_DRIVER = "import sys, compiletools.filelist as f; sys.exit(f.main(sys.argv[1:]))"


def _functional_cxx() -> str:
    cxx = compiletools.apptools.get_functional_cxx_compiler()
    if not cxx:
        pytest.skip("no functional C++ compiler detected")
    return str(cxx)


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe the global configargparse parser cache around every test."""
    uth.reset()
    yield
    uth.reset()


@contextmanager
def hunter_over(include_dir):
    """A Hunter and the args it resolved, on the same call path as ``ct-filelist``.

    A headerdeps/magicflags pair built from a throwaway config, in its own
    BuildContext. ``args`` is yielded alongside so a test can check which
    compiler the walk resolved against the one an oracle shells out to.
    """
    with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
        cap = configargparse.ArgumentParser(
            conflict_handler="resolve",
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.hunter.add_arguments(cap)
        context = BuildContext()
        args = compiletools.apptools.parseargs(cap, ["-c", temp_config, "--include", str(include_dir)], context=context)
        headerdeps = compiletools.headerdeps.create(args, context=context)
        magicparser = compiletools.magicflags.create(args, headerdeps, context=context)
        yield compiletools.hunter.Hunter(args, headerdeps, magicparser, context=context), args


def closure(include_dir, entry):
    """Basenames of the dependency closure hunter reports for ``entry``."""
    with hunter_over(include_dir) as (hunter, _args):
        files = hunter.required_files(os.path.join(str(include_dir), entry))
    return {os.path.basename(str(path)) for path in files}


def _guard_workspace(tmp_path, name, guard):
    """The shipped guard example with its ``#if`` rewritten to ``guard``.

    The rewrite is the workspace's only difference from the shipped example,
    which is what makes the guard form the sole variable under test. Both
    assertions exist to stop the fixture degrading into a copy of the example
    if the example's directive text ever changes: the first catches a
    substitution that matched nothing, the second a substitution that matched
    more than the directive.
    """
    workspace = uth.copy_example_workspace(Path(example_path(_GUARD_EXAMPLE)), tmp_path / name)
    pump = workspace / "pump.cpp"
    original = pump.read_text()
    patched = original.replace(_FUNCTION_LIKE_GUARD, guard)
    assert patched != original, f"{_FUNCTION_LIKE_GUARD!r} is no longer in pump.cpp; the fixture patched nothing"
    assert patched.count(guard) == 1, f"expected exactly one {guard!r} in the patched pump.cpp"
    pump.write_text(patched)
    return workspace


@pytest.fixture
def object_like_workspace(tmp_path):
    """The guard example with an object-like version test in place of the macro call."""
    return _guard_workspace(tmp_path, "object_like", _OBJECT_LIKE_GUARD)


@pytest.fixture
def never_defined_workspace(tmp_path):
    """The guard example with a guard macro that no header in the tree defines."""
    return _guard_workspace(tmp_path, "never_defined", _NEVER_DEFINED_GUARD)


@pytest.fixture
def guard_workspace(tmp_path):
    """The guard example verbatim, in a workspace of its own."""
    return uth.copy_example_workspace(Path(example_path(_GUARD_EXAMPLE)), tmp_path / "as_shipped")


def run_filelist(workspace, *argv):
    """Run ``ct-filelist`` over ``workspace`` in a fresh interpreter.

    A subprocess, not an in-process ``filelist.main`` call, for two reasons.
    The preprocessing caches and ``BuildContext.warned_preprocessor_conditions``
    are reachable across tests in one pytest process, so an in-process
    "no warning was printed" assertion can pass because an earlier test already
    consumed the report. And a subprocess captures stderr from every
    convergence pass, including a superseded one, which is the specific thing
    the deferred-warning machinery is supposed to prevent from reaching the user.
    """
    return subprocess.run(
        [sys.executable, "-c", _FILELIST_DRIVER, *argv],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _stdout_basenames(proc):
    return {os.path.basename(line.strip()) for line in proc.stdout.splitlines() if line.strip()}


_unevaluable_lines = uth.unevaluable_lines


class TestObjectLikeImpliedSourceGuard:
    """The silent half of the defect: same wrong closure, no diagnostic at all.

    ``PUMPLIB_VERSION_MAJOR`` is defined in ``pumpver.h``, two includes away
    from ``pump.cpp``. Walk ``pump.cpp`` under ``main.cpp``'s macro state and
    that macro has never been seen; an undefined object-like macro evaluates to
    0, ``0 >= 2`` is false, and the legacy branch is taken with nothing written
    to stderr. Nothing but a closure assertion can catch that, which is why a
    fix graded only on the audible function-like row would leave it wrong.
    """

    def test_the_closure_names_the_branch_the_compiler_takes(self, object_like_workspace):
        found = closure(object_like_workspace, "main.cpp")
        assert "modern_pump.h" in found
        assert "legacy_pump.h" not in found

    def test_the_same_guard_resolves_correctly_when_the_file_is_the_target(self, object_like_workspace):
        """Isolates the defect to the walk rather than to the guard.

        Naming ``pump.cpp`` directly gives it its own converged macro state and
        the closure is right, so nothing about the object-like guard is beyond
        the evaluator. Only the implied-source route gets it wrong.
        """
        found = closure(object_like_workspace, "pump.cpp")
        assert "modern_pump.h" in found
        assert "legacy_pump.h" not in found

    @uth.requires_functional_compiler
    def test_the_compiler_takes_the_modern_branch(self, object_like_workspace):
        """Pins the expectation above against cpp, so it is not an invention."""
        cxx = _functional_cxx()
        proc = subprocess.run(
            [cxx, "-E", "-P", "-I", str(object_like_workspace), str(object_like_workspace / "pump.cpp")],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        assert proc.returncode == 0, proc.stderr
        assert '"modern"' in proc.stdout
        assert '"legacy"' not in proc.stdout

    @uth.requires_functional_compiler
    def test_the_wrong_branch_is_taken_with_nothing_on_stderr(self, object_like_workspace):
        """Why the row above needs a closure assertion and not a stderr one.

        Paired with ``test_the_walk_still_reports_a_genuinely_unevaluable_condition``,
        which proves this same harness does print when there is something to
        print. Without that pair, an empty stderr here would be indistinguishable
        from a harness that never warns.
        """
        proc = run_filelist(object_like_workspace, "-v", "main.cpp")
        assert proc.returncode == 0, proc.stderr
        assert _unevaluable_lines(proc) == []


class TestUnevaluableWarningSilence:
    """``ct-filelist -v`` on the shipped example, which the done criteria name.

    The verbose run must agree with itself: the closure it prints and the
    warnings it does not print describe the same branch.
    """

    @uth.requires_functional_compiler
    def test_the_verbose_run_names_the_modern_branch_and_warns_about_nothing(self, guard_workspace):
        proc = run_filelist(guard_workspace, "-v", "main.cpp")
        assert proc.returncode == 0, proc.stderr
        found = _stdout_basenames(proc)
        assert "modern_pump.h" in found
        assert "legacy_pump.h" not in found
        assert _unevaluable_lines(proc) == []

    @uth.requires_functional_compiler
    def test_the_walk_still_reports_a_genuinely_unevaluable_condition(self, never_defined_workspace):
        """The armed control for the assertion above, and a regression guard.

        ``PUMPLIB_DEFINED_NOWHERE`` is defined in no header in the tree, so no
        amount of convergence makes it evaluable and the warning is accurate
        both before and after the fix. Deferring the implied-source walk's
        warnings must not swallow this one: a fix that silences the walk
        wholesale, rather than letting it settle and then reporting, fails here.
        """
        proc = run_filelist(never_defined_workspace, "-v", "main.cpp")
        assert proc.returncode == 0, proc.stderr
        reported = _unevaluable_lines(proc)
        assert len(reported) == 1, f"expected exactly one report, got {reported}"
        assert "PUMPLIB_DEFINED_NOWHERE" in reported[0]
        assert "pump.cpp" in reported[0]


_GIZMO_COMMON = {
    "main.cpp": '#include "gizmo.h"\nint main() { return gizmo(); }\n',
    "gizmo.h": "#ifndef GIZMO_H\n#define GIZMO_H\nint gizmo();\n#endif\n",
    "gizmo_detail.h": (
        "#ifndef GIZMO_DETAIL_H\n"
        "#define GIZMO_DETAIL_H\n"
        "#if GIZMO_IMPL\n"
        '#include "impl_yes.h"\n'
        "#else\n"
        '#include "impl_no.h"\n'
        "#endif\n"
        "#endif\n"
    ),
    "impl_yes.h": "#ifndef IMPL_YES_H\n#define IMPL_YES_H\ninline int detail_value() { return 1; }\n#endif\n",
    "impl_no.h": "#ifndef IMPL_NO_H\n#define IMPL_NO_H\ninline int detail_value() { return 0; }\n#endif\n",
}

# Where ``GIZMO_IMPL`` is defined, which is the whole variable. In both trees
# it is invisible from ``main.cpp``, so the root TU's macro state resolves the
# guard to the ``impl_no.h`` branch the compiler never takes.
_GIZMO_TREES = {
    # The shipped example's depth: the guard's operand is defined in the
    # implied source itself.
    "in_the_source": {
        **_GIZMO_COMMON,
        "gizmo.cpp": (
            '#define GIZMO_IMPL 1\n#include "gizmo.h"\n#include "gizmo_detail.h"\nint gizmo() { return detail_value(); }\n'
        ),
    },
    # One hop further out: the operand arrives from a header the implied
    # source includes, so it exists only in that source's converged state.
    "in_a_header_below_the_source": {
        **_GIZMO_COMMON,
        "gizmo_cfg.h": "#ifndef GIZMO_CFG_H\n#define GIZMO_CFG_H\n#define GIZMO_IMPL 1\n#endif\n",
        "gizmo.cpp": (
            '#include "gizmo.h"\n#include "gizmo_cfg.h"\n#include "gizmo_detail.h"\nint gizmo() { return detail_value(); }\n'
        ),
    },
}


def _write_tree(root, files):
    """Write ``files`` under ``root`` and plant the ``.git`` marker.

    The marker is what ``copy_example_workspace`` plants for the same reason:
    without it ``find_git_root`` walks out to the pytest tmpdir's ancestors.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)
    git_dir = root / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return root


def _mm_headers(cxx, workspace, *sources):
    """Headers ``g++ -MM`` names across ``sources``, by basename.

    The oracle is derived here rather than pinned as a literal so the
    expectation cannot drift away from what the compiler actually does with
    these trees.
    """
    found = set()
    for source in sources:
        proc = subprocess.run(
            [cxx, "-MM", "-I", str(workspace), str(workspace / source)],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )
        assert proc.returncode == 0, proc.stderr
        for token in proc.stdout.replace("\\", " ").split():
            if token.endswith(".h"):
                found.add(os.path.basename(token))
    assert found, f"{cxx} -MM named no headers for {sources}; the oracle is empty"
    return found


class TestGuardBelowTheImpliedSource:
    """A guard the implied source's own state resolves, in a header below it.

    The shipped example only exercises the guard sitting in the implied source.
    Threading the source's converged state into its header scan but not into
    the descent below it still gets that example right, and gets these wrong by
    adding ``impl_no.h`` on top of ``impl_yes.h``. Unioning both branches is the
    failure that a closure-equality assertion catches and an ``in`` assertion
    does not, so these compare the whole set.
    """

    @pytest.mark.parametrize("shape", sorted(_GIZMO_TREES))
    @uth.requires_functional_compiler
    def test_the_closure_is_exactly_what_the_compiler_reads(self, tmp_path, shape):
        workspace = _write_tree(tmp_path / shape, _GIZMO_TREES[shape])
        cxx = _functional_cxx()
        expected = _mm_headers(cxx, workspace, "main.cpp", "gizmo.cpp") | {"main.cpp", "gizmo.cpp"}
        assert "impl_yes.h" in expected and "impl_no.h" not in expected, expected
        assert closure(workspace, "main.cpp") == expected

    @pytest.mark.parametrize("shape", sorted(_GIZMO_TREES))
    @uth.requires_functional_compiler
    def test_the_wrong_branch_is_the_one_that_gets_added(self, tmp_path, shape):
        """Names the failure signature a half-applied fix produces.

        The equality assertion above already rejects it, but only by set
        difference; this says what the difference looks like. Thread the
        source's converged state into one of the two walk sites and not the
        other and the closure keeps ``impl_yes.h`` and gains ``impl_no.h`` --
        a union of both branches rather than a swap between them.
        """
        workspace = _write_tree(tmp_path / shape, _GIZMO_TREES[shape])
        found = closure(workspace, "main.cpp")
        assert "impl_yes.h" in found
        assert "impl_no.h" not in found, f"both branches present: {sorted(found)}"

    @uth.requires_functional_compiler
    def test_the_oracle_and_the_walk_resolve_the_same_compiler(self, tmp_path):
        """Otherwise the comparison above is between two different toolchains.

        These trees compile under any C++ dialect, so a mismatch would not
        announce itself as an error -- it would silently compare hunter's
        preprocessing against a different compiler's ``-MM``.
        """
        workspace = _write_tree(tmp_path / "compiler_agreement", _GIZMO_TREES["in_the_source"])
        with hunter_over(workspace) as (_hunter, args):
            walk_cxx = args.CXX
        assert shutil.which(walk_cxx) == shutil.which(_functional_cxx())

    @pytest.mark.parametrize("shape", sorted(_GIZMO_TREES))
    def test_the_walk_says_nothing_about_the_guard(self, tmp_path, shape):
        """``GIZMO_IMPL`` is object-like, so getting it wrong is silent.

        Armed by ``test_the_walk_still_reports_a_genuinely_unevaluable_condition``
        above, which proves this harness does report when there is something to
        report.
        """
        workspace = _write_tree(tmp_path / shape, _GIZMO_TREES[shape])
        proc = run_filelist(workspace, "-v", "main.cpp")
        assert proc.returncode == 0, proc.stderr
        assert _unevaluable_lines(proc) == []


_TWO_STATE_TREE = {
    "main.cpp": '#include "w.h"\nint main() { return w_value(); }\n',
    # Included from both TUs and preprocessed differently by each: main.cpp
    # has never defined W_IMPL, w.cpp defines it before including this.
    "w.h": ('#ifndef W_H\n#define W_H\n#ifdef W_IMPL\n#include "w_impl_detail.h"\n#endif\nint w_value();\n#endif\n'),
    "w.cpp": (
        "#define W_IMPL 1\n"
        '#include "w.h"\n'
        '#include "wcfg.h"\n'
        "#if W_LEVEL >= 2\n"
        '#include "w_hi.h"\n'
        "#else\n"
        '#include "w_lo.h"\n'
        "#endif\n"
        "int w_value() { return w_extra() + w_detail(); }\n"
    ),
    "wcfg.h": "#ifndef WCFG_H\n#define WCFG_H\n#define W_LEVEL 2\n#endif\n",
    "w_hi.h": "#ifndef W_HI_H\n#define W_HI_H\ninline int w_extra() { return 2; }\n#endif\n",
    "w_lo.h": "#ifndef W_LO_H\n#define W_LO_H\ninline int w_extra() { return 1; }\n#endif\n",
    "w_impl_detail.h": (
        "#ifndef W_IMPL_DETAIL_H\n#define W_IMPL_DETAIL_H\ninline int w_detail() { return 10; }\n#endif\n"
    ),
}


class TestHeaderSeenAtTwoMacroStates:
    """A header both TUs include, at states that disagree about its content.

    ``w.h`` is the root TU's own include and the implied source's, and only the
    implied source has defined ``W_IMPL``, so the two readings of ``w.h``
    differ. Giving each source its own converged state means a file already in
    the closure can be reached again under a second state; re-keying and
    re-descending on that second arrival is the non-terminating shape, which is
    why the key is computed inside the already-processed guard rather than
    ahead of it. The closure must therefore be the union over both readings --
    ``w_impl_detail.h`` is in it because ``w.cpp`` sees it -- while still
    choosing one side of ``w.cpp``'s own ``W_LEVEL`` guard.

    Corpus contributed by the fix's author; the oracle is re-derived here from
    the probed compiler rather than carried over with it.
    """

    @uth.requires_functional_compiler
    def test_the_closure_is_the_union_over_both_readings(self, tmp_path):
        workspace = _write_tree(tmp_path / "two_state", _TWO_STATE_TREE)
        cxx = _functional_cxx()
        expected = _mm_headers(cxx, workspace, "main.cpp", "w.cpp") | {"main.cpp", "w.cpp"}
        assert {"w_impl_detail.h", "w_hi.h"} <= expected and "w_lo.h" not in expected, expected
        assert closure(workspace, "main.cpp") == expected

    @uth.requires_functional_compiler
    def test_the_walk_terminates(self, tmp_path):
        """Run out of process so a non-terminating walk fails rather than hangs.

        ``run_filelist`` carries a timeout; an in-process call would take the
        whole pytest worker down with it.
        """
        workspace = _write_tree(tmp_path / "two_state_cli", _TWO_STATE_TREE)
        proc = run_filelist(workspace, "main.cpp")
        assert proc.returncode == 0, proc.stderr
        assert "w.h" in _stdout_basenames(proc)


# Closures that already agree with the compiler. Each was checked against
# ``g++ -MM`` before being pinned, so these are compiler-correct rows rather
# than a snapshot of current behaviour; entries hunter adds that ``-MM`` cannot
# report -- the entry file itself, and implied sources -- are noted per row.
# The absence side of each row is the load-bearing half: a walk that resolves
# guards by unioning both branches, or that re-walks an implied source under a
# second macro state, adds a header here rather than removing one.
_BOUNDARY_ROWS = {
    # The false branch is guarded by a compiler built-in, so windows_header.h
    # must stay out.
    "conditional_includes": ("main.cpp", {"linux_header.h", "main.cpp"}),
    # linux_extra.h / windows_extra.h are the branches not taken.
    "computed_include": ("main.cpp", {"default_extra.h", "main.cpp"}),
    # debug.h is the branch not taken; this example exists for exactly the
    # macro-state-keyed caching the fix perturbs.
    "macro_state_dependency": ("sample.cpp", {"feature.h", "release.h", "sample.cpp"}),
    # optional_feature.h is the __has_include branch not taken.
    "has_include": ("main.cpp", {"main.cpp", "stdheader_extras.h"}),
    # the_code_lin.cpp is an implied source reached through a header, i.e. the
    # same hunter branch as the defect, already correct today.
    "magicsourceinheader": (
        "main.cpp",
        {"another_header.hpp", "main.cpp", "some_header.hpp", "the_code_lin.cpp"},
    ),
    # A version guard resolved through magicflags convergence, which already
    # converges; it must keep the answer it has.
    "version_dependent_api": ("test_main.cpp", {"api_config.h", "test_main.cpp", "version.h"}),
    "hunter_macro_propagation": ("app.cpp", {"app.cpp", "config.h", "renderer.h"}),
    "movingheaders": ("main.cpp", {"main.cpp", "someheader.hpp"}),
}


class TestClosureBoundaryRows:
    """Shipped closures that must not move, in either direction.

    These are the rows a fix is graded against for over-reach. A change that
    gives every implied source its own converged macro state touches every
    example with an implied source, not only the broken one, so the rows that
    are already right have to be watched as closely as the row that is wrong.
    """

    @pytest.mark.parametrize("example", sorted(_BOUNDARY_ROWS))
    def test_the_closure_is_unchanged(self, example):
        entry, expected = _BOUNDARY_ROWS[example]
        assert closure(example_path(example), entry) == expected
