"""Where ``//#READMACROS=`` is declared changes which branch a gate resolves to.

A function-like version gate whose operands come from a ``//#READMACROS=``
header resolves correctly when the declaration sits in the same file as the
``#if``, and resolves to the **wrong branch** when the declaration sits in the
header that defines the gate macro one include away. Nothing is printed at any
verbosity, so the failure is wrong compile flags with no diagnostic.

The declaration site is the only variable between the two shapes here: same
external header, same macro body, same gate expression, same include chain.

``TestReadmacrosSuppliedFunctionLikeGate`` in ``test_function_like_macros.py``
covers the working half only -- it declares ``//#READMACROS=`` in the file
holding the ``#if``. That is why the whole family passes while the shape below
mislinks.

Sources live in ``examples-features/readmacros_declaration_site/``, one
subdirectory per declaration site. The trees use a *relative*
``-isystem extlib/include`` so they can ship verbatim; each test copies the
tree it needs into the per-test tempdir and parses from there (the base class
already chdirs into it), which keeps the shipped tree pristine and gives the
relative flag its anchor.
"""

import os
import shutil
import subprocess

import stringzilla as sz

import compiletools.apptools
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.testhelper import requires_functional_compiler

_EXAMPLE = "readmacros_declaration_site"

# Subdirectory of the example per declaration site: "gate" declares
# //#READMACROS= in gate.hpp (holding the #if), "definer" declares it in
# extver.hpp (beside the macro body).
_SHAPE_DIRS = {"gate": "declared_beside_gate", "definer": "declared_in_definer"}


class TestReadmacrosDeclarationSite(tb.BaseCompileToolsTestCase):
    def setup_method(self):
        super().setup_method()
        compiletools.magicflags.MagicFlagsBase.clear_cache()
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

    def _copy_shape(self, declared_in: str, dest: str) -> str:
        """Copy one declaration-site tree into ``dest``; returns ``dest``."""
        src = uth.example_file(f"{_EXAMPLE}/{_SHAPE_DIRS[declared_in]}")
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return dest

    def _parse(self, declared_in: str, capsys):
        """Return (cxxflags, unevaluable-condition lines) for one shape."""
        self._copy_shape(declared_in, self._tmpdir)
        parser = tb.create_magic_parser(["--magic=direct", "-v"], tempdir=self._tmpdir, context=BuildContext())
        parser.clear_cache()
        result = parser.parse(os.path.join(self._tmpdir, "main.cpp"))
        cxxflags = " ".join(str(flag) for flag in result.get(sz.Str("CXXFLAGS"), []))
        warnings = [line for line in capsys.readouterr().err.splitlines() if "cannot evaluate" in line]
        return cxxflags, warnings

    def test_control_a_declaration_beside_the_gate_resolves_correctly(self, capsys):
        """The shape the existing suite covers. Passes today.

        Without this control the failing test below could be read as the gate
        itself being beyond the evaluator, rather than the declaration site
        being what decides the answer.
        """
        cxxflags, _warnings = self._parse("gate", capsys)

        assert "-DPICKED_NEW" in cxxflags, cxxflags
        assert "-DPICKED_OLD" not in cxxflags, cxxflags

    def test_a_declaration_in_the_defining_header_resolves_to_the_wrong_branch(self, capsys):
        """The defect. Moving ``//#READMACROS=`` one include away flips the gate.

        The operands never reach the gate's evaluation, so every clause of
        EXTLIB_AT_LEAST reads its version macros as 0, the whole expression is
        false, and the negated gate takes the arm the compiler does not take.
        """
        cxxflags, _warnings = self._parse("definer", capsys)

        assert "-DPICKED_NEW" in cxxflags, cxxflags
        assert "-DPICKED_OLD" not in cxxflags, cxxflags

    def test_the_wrong_branch_is_taken_with_nothing_on_stderr(self, capsys):
        """The second obligation: be right, or be loud. Today it is neither.

        Separate from the assertion above because they fail for one cause but
        pin two different promises. A fix that made the gate merely *audible*
        would satisfy this one and still leave the flags wrong; a fix that
        resolved the gate satisfies both.
        """
        cxxflags, warnings = self._parse("definer", capsys)

        if "-DPICKED_NEW" in cxxflags:
            return  # resolved correctly, so there is nothing to report
        assert warnings, (
            "the gate resolved to the branch the compiler does not take and said "
            f"nothing about it; flags were {cxxflags!r}"
        )

    def _run_oracle(self, root: str) -> str:
        """Preprocess the tree's shipped oracle.cpp; returns the stdout."""
        compiler = compiletools.apptools.get_functional_cxx_compiler()
        assert compiler is not None
        proc = subprocess.run(
            [
                str(compiler),
                "-E",
                "-P",
                "-I",
                root,
                "-I",
                os.path.join(root, "extlib", "include"),
                os.path.join(root, "oracle.cpp"),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    @requires_functional_compiler
    def test_the_compiler_takes_the_branch_these_tests_expect(self):
        """Pins ``-DPICKED_NEW`` against a real preprocessor, not an assumption."""
        root = self._copy_shape("definer", os.path.join(self._tmpdir, "definer"))

        stdout = self._run_oracle(root)

        assert "int picked = 2;" in stdout, stdout
        assert "int picked = 1;" not in stdout, stdout

    @requires_functional_compiler
    def test_control_the_declaration_site_does_not_change_the_compiler_answer(self):
        """Proves the two shapes differ only for compiletools.

        The compiler ignores ``//#READMACROS=`` entirely -- it is a comment --
        so both trees must preprocess identically. If this ever fails, the
        fixture has grown a second variable and the comparison above is no
        longer about the declaration site.
        """
        outputs = []
        for declared_in in ("gate", "definer"):
            root = self._copy_shape(declared_in, os.path.join(self._tmpdir, declared_in))
            outputs.append(self._run_oracle(root).strip())

        assert outputs[0] == outputs[1], outputs
