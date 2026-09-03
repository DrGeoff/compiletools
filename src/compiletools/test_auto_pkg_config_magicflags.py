"""End-to-end --auto-pkg-config through MagicFlags.parse.

pkgconfig_infer is unit-tested exhaustively in test_pkgconfig_infer.py; this
module pins the wiring: a plain ``#include`` in a translation unit parsed by
``MagicFlagsBase.parse`` must end up as the same ``//#PKG-CONFIG=...`` magic
flag the author would have written by hand, and every explicit-flag coverage
rule (F1) plus the ambiguity/map/conf surfaces must behave identically through
the full parse pipeline.

Fixtures: ``pkgconfig_env`` points PKG_CONFIG_PATH at the shared
examples-features/pkgs directory (autoinfer/umbrella/fam fixtures); the
autouse conftest fixture keeps ``--auto-pkg-config`` off elsewhere, so every
test here opts in with ``auto_pkg_config_on`` (env CT_AUTO_PKG_CONFIG=1) or
explicit argv. ``pkgconfig_infer``'s process-global caches are cleared around
every test; MagicFlags' diagnostic dedup sets are BuildContext-owned session
state, and every parse here constructs a fresh BuildContext, so they reset
with each new parser rather than through clear_cache().
"""

import os
import shutil

import pytest
import stringzilla as sz

import compiletools.compiler_macros
import compiletools.magicflags
import compiletools.pkgconfig_infer
import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext

TU_AUTOINFER = "#include <autoinfer/api.h>\nint main() { return 0; }\n"


@pytest.fixture(autouse=True)
def _clear_auto_pkg_config_caches():
    """Isolation for the process-global caches inference reads and writes.

    ``pkgconfig_infer.clear_cache()`` resets the PKG_CONFIG_PATH-keyed
    include-dir index so each test sees its own env value;
    ``MagicFlagsBase.clear_cache()`` clears the shared LRU caches. The
    verbose-note dedup sets the ambiguity tests assert on are owned by the
    BuildContext each parser constructs (per-build-session state, like
    ``auto_pkg_config_memo``), so per-test isolation holds without clearing
    them here.
    """
    compiletools.pkgconfig_infer.clear_cache()
    compiletools.magicflags.MagicFlagsBase.clear_cache()
    yield
    compiletools.pkgconfig_infer.clear_cache()
    compiletools.magicflags.MagicFlagsBase.clear_cache()


class TestAutoPkgConfig(tb.BaseCompileToolsTestCase):
    """The magicflags.parse integration matrix for auto-pkg-config."""

    def _write_tu(self, text, name="main.cpp"):
        return str(uth.write_sources({name: text})[name])

    def _make_parser(self, extraargs=(), extralines=None, flavour=None, context=None):
        """Build a MagicFlags parser the way ``tb.create_magic_parser`` does,
        but with extra conf-file lines (for the xxpend-accumulation test).
        The parser class matters: only ``_ComposingArgumentParser`` +
        ``_AccumulatingConfigFileParser`` (what ``apptools.create_parser``
        builds in production) let conf-file ``append-AUTO-PKG-CONFIG-MAP``
        accumulate with the CLI token -- stock configargparse drops the conf
        value when the same append key appears on the CLI. *context* lets a
        test share one BuildContext across parsers; the diagnostic dedup
        sets are session state on it."""
        import compiletools.apptools
        import compiletools.headerdeps
        import compiletools.magicflags

        argv = []
        if flavour:
            argv.extend([f"--magic={flavour}", f"--headerdeps={flavour}"])
        argv.extend(extraargs)
        temp_config_name = uth.create_temp_config(self._tmpdir, extralines=extralines)
        argv = ["--config=" + temp_config_name] + argv
        cap = compiletools.apptools.create_parser("TestAutoPkgConfig", argv=argv)
        compiletools.apptools.add_common_arguments(cap, argv=argv)
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        ctx = context if context is not None else BuildContext()
        args = compiletools.apptools.parseargs(cap, argv, context=ctx)
        headerdeps = compiletools.headerdeps.create(args, context=ctx)
        return compiletools.magicflags.create(args, headerdeps, context=ctx)

    def _parse(self, text, extraargs=(), extralines=None, flavour=None, name="main.cpp"):
        tu = self._write_tu(text, name=name)
        return tu, self._make_parser(extraargs, extralines, flavour).parse(tu)

    @staticmethod
    def _strings(result, key):
        return [str(token) for token in result.get(sz.Str(key), [])]

    @staticmethod
    def _projection(result):
        return {str(k): [str(v) for v in values] for k, values in result.items()}

    # ------------------------------------------------------------- F2: off

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env")
    def test_off_flag_disables_inference(self, monkeypatch):
        """--no-auto-pkg-config: plain #include, no inferred PKG-CONFIG, and
        the inference layer is never even entered (stubbed to explode)."""

        def _must_not_run(*args, **kwargs):
            raise AssertionError("pkgconfig_infer.infer ran despite --no-auto-pkg-config")

        monkeypatch.setattr(compiletools.pkgconfig_infer, "infer", _must_not_run)
        tu, result = self._parse(TU_AUTOINFER, extraargs=["--no-auto-pkg-config"])
        assert "PKG-CONFIG" not in result, f"expected no inferred packages for {tu}"

    # ---------------------------------------------- F4 + F5: on, both flavours

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_on_infers_package(self):
        """Plain <autoinfer/api.h> include: inference annotates the TU with
        //#PKG-CONFIG=autoinfer and the usual pkg-config processing turns the
        package's Cflags/Libs into real flag tokens in the parse result.
        direct flavour only: the cpp flavour must preprocess the TU before
        inference has landed the package's -isystem, so <autoinfer/api.h> is
        genuinely unresolvable at that point (covered separately by
        test_cpp_flavour_annotated_tu below)."""
        _, result = self._parse(TU_AUTOINFER)
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"]
        assert "-lautoinfer" in self._strings(result, "LDFLAGS")
        for slot in ("CPPFLAGS", "CXXFLAGS"):
            tokens = self._strings(result, slot)
            for index, token in enumerate(tokens):
                if token == "-isystem":
                    assert tokens[index + 1].endswith("autoinfer-include"), (
                        f"-isystem pair does not name the autoinfer include dir in {slot}: {tokens}"
                    )
                    break
            else:
                pytest.fail(f"no -isystem <autoinfer-include> pair in {slot}: {tokens}")

    # ------------------------------------------------- F1: explicit coverage

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_explicit_pkg_config_annotation_not_duplicated(self):
        """An explicit //#PKG-CONFIG=autoinfer covers the header: exactly one
        package, exactly one -lautoinfer, no second entry from inference.
        direct flavour: same cpp-preprocess precondition as
        test_on_infers_package."""
        _, result = self._parse("//#PKG-CONFIG=autoinfer\n#include <autoinfer/api.h>\nint main() { return 0; }\n")
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"]
        assert self._strings(result, "LDFLAGS").count("-lautoinfer") == 1

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_cpp_flavour_annotated_tu_unchanged_by_inference(self):
        """cpp flavour with the package known up front (conf pkg-config): the
        preprocess command carries the package's -isystem, so the TU resolves,
        and the explicit annotation covers the header (F1) — inference adds
        nothing. Pins that the cpp pipeline (preprocessed-text magic scan)
        sees the same F1 no-op the direct pipeline does."""
        _, result = self._parse(
            "//#PKG-CONFIG=autoinfer\n#include <autoinfer/api.h>\nint main() { return 0; }\n",
            extraargs=["--magic=cpp", "--headerdeps=cpp", "--pkg-config=autoinfer"],
        )
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"]
        assert self._strings(result, "LDFLAGS").count("-lautoinfer") == 1

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_explicit_cli_pkg_config_covers_header(self):
        """--pkg-config=autoinfer already builds the package: inference must
        not add a //#PKG-CONFIG for the same header on top."""
        _, result = self._parse(TU_AUTOINFER, extraargs=["--pkg-config=autoinfer"])
        assert "PKG-CONFIG" not in result, "explicit CLI package must cover the include"

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_explicit_ldflags_cover_header(self):
        """A hand-written //#LDFLAGS=-lautoinfer covers the include via the
        lib stem: no inference, no inferred PKG-CONFIG."""
        text = "//#LDFLAGS=-lautoinfer\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert self._strings(result, "LDFLAGS") == ["-lautoinfer"], "precondition: explicit LDFLAGS parsed"
        assert "PKG-CONFIG" not in result

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_explicit_isystem_covers_header(self):
        """An explicit -isystem naming the package's include dir covers the
        include (its header lives under that dir): no inference."""
        include_dir = os.path.realpath(os.path.join(uth.cakedir(), "examples-features", "pkgs", "autoinfer-include"))
        text = f"//#CXXFLAGS=-isystem {include_dir}\n#include <autoinfer/api.h>\nint main() {{ return 0; }}\n"
        _, result = self._parse(text)
        assert "-isystem" in self._strings(result, "CXXFLAGS"), "precondition: explicit -isystem parsed"
        assert "PKG-CONFIG" not in result

    # ------------------------- F1: declared package the environment lacks

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_declared_missing_package_is_not_substituted(self, capsys):
        """``autoinfer-vendored/api.h`` is a vendored copy living inside the
        umbrella's exported include dir, and no ``autoinfer-vendored.pc``
        exists. Baseline: inference attributes it to autoinfer-umbrella and
        ``-lumbrella`` joins the link line. Declaring the package the header
        really belongs to must stop the substitution rather than redirect it,
        so the TU keeps only what its author asked for and the compile fails
        on the missing header instead of linking a foreign library."""
        include = "#include <autoinfer-vendored/api.h>\nint main() { return 0; }\n"

        _, baseline = self._parse(include)
        assert self._strings(baseline, "PKG-CONFIG") == ["autoinfer-umbrella"]
        assert "-lumbrella" in self._strings(baseline, "LDFLAGS")

        _, result = self._parse("//#PKG-CONFIG=autoinfer-vendored\n" + include)
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer-vendored"]
        assert "-lumbrella" not in self._strings(result, "LDFLAGS")
        assert "declared package 'autoinfer-vendored' is unavailable" in capsys.readouterr().err

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_unavailable_declaration_hints_about_unrelated_inferences_at_verbose(self, capsys):
        """Namespace matching cannot cover a header spelled unlike its
        package, so an inference made while a declaration is unsatisfied is
        still hinted at verbose >= 1 -- naming both the inferred package and
        the missing declaration. Verbose-gated, unlike the per-header
        declared-missing warning: the inference still succeeded, so the
        blanket note is a hint, not an actionable failure."""
        text = "//#PKG-CONFIG=nosuchpackage\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text, extraargs=["--verbose"])
        assert "autoinfer" in self._strings(result, "PKG-CONFIG"), "precondition: the sibling header still infers"
        err = capsys.readouterr().err
        assert "inferred autoinfer" in err
        assert "nosuchpackage" in err

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_unrelated_inference_hint_is_silent_at_verbose_0(self, capsys):
        """The must-not-leak control for the hint gating: the blanket
        "inferred X while declared Y is unavailable" note does not print at
        verbose 0 (only the per-header declared-missing failure does), while
        the inference itself still lands the package."""
        text = "//#PKG-CONFIG=nosuchpackage\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert "autoinfer" in self._strings(result, "PKG-CONFIG"), "precondition: the sibling header still infers"
        err = capsys.readouterr().err
        assert "are unavailable" not in err

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_available_declaration_produces_no_unavailable_warning(self, capsys):
        """The must-not-move control: a declaration pkg-config *can* satisfy
        emits neither warning, so the noise is confined to the degraded
        state."""
        text = "//#PKG-CONFIG=autoinfer-fam\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert "autoinfer" in self._strings(result, "PKG-CONFIG")
        err = capsys.readouterr().err
        assert "is unavailable" not in err
        assert "are unavailable" not in err

    # ------------------------------------ diagnostic dedup: per BuildContext

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_diagnostic_dedup_is_per_build_context(self, capsys):
        """The inference-diagnostic dedup sets are BuildContext-owned session
        state, not module globals: two parsers sharing one context share the
        dedup (the second parse of the same TU is silent) and a fresh
        context re-reports. Pinning the ownership keeps
        MagicFlagsBase.clear_cache() honest about what it can reset -- the
        sets die with their context, exactly like auto_pkg_config_memo."""
        text = "//#PKG-CONFIG=autoinfer-vendored\n#include <autoinfer-vendored/api.h>\nint main() { return 0; }\n"
        tu = self._write_tu(text)
        marker = "declared package 'autoinfer-vendored' is unavailable"

        shared = BuildContext()
        first = self._make_parser(context=shared)
        first.parse(tu)
        assert marker in capsys.readouterr().err, "precondition: the declared-missing warning prints"

        second = self._make_parser(context=shared)
        second.parse(tu)
        assert marker not in capsys.readouterr().err, "a second parser on one session must dedup the warning silent"

        fresh = self._make_parser()
        fresh.parse(tu)
        assert marker in capsys.readouterr().err, "a fresh session (fresh BuildContext) must re-report the warning"

    # --------------------------------------- F3: preprocessor conditional gates

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_conditional_disabled_not_inferred(self):
        """An include compiled out by #if 0 is not active: no candidates, no
        inferred PKG-CONFIG."""
        text = "#if 0\n#include <autoinfer/api.h>\n#endif\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert "PKG-CONFIG" not in result

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_conditional_enabled_by_cmdline_macro_is_inferred(self):
        """#ifdef X with X supplied via //#CPPFLAGS=-DX: the include is active
        and inference fires -- the same flags a hand-written annotation gives."""
        text = "//#CPPFLAGS=-DX\n#ifdef X\n#include <autoinfer/api.h>\n#endif\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"]

    # ------------------------------------------------- F2: non-system include

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_quoted_include_never_inferred(self):
        """auto-pkg-config candidates are system-style includes only; the
        same spelling in quotes stays out of inference entirely."""
        text = '#include "autoinfer/api.h"\nint main() { return 0; }\n'
        _, result = self._parse(text)
        assert "PKG-CONFIG" not in result

    # ------------------------------------------------- F1: project headers out

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_project_header_not_inferred(self):
        """A header the project itself provides (resolvable through --include)
        is a project header: dropped before any package attribution."""
        os.makedirs(os.path.join(os.getcwd(), "include", "autoinfer"), exist_ok=True)
        with open(os.path.join(os.getcwd(), "include", "autoinfer", "api.h"), "w") as handle:
            handle.write("#pragma once\ninline int project_shadow(void) { return 1; }\n")
        _, result = self._parse(TU_AUTOINFER, extraargs=[f"--include={os.path.join(os.getcwd(), 'include')}"])
        assert "PKG-CONFIG" not in result, "a project header must never gain a pkg-config annotation"

    # ------------------------------------------- F6: order independence, sorted

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_multi_package_annotation_sorted(self):
        """Two packages from one TU (reverse-alphabetical include order): the
        result names them sorted (alphabetical) and deduplicated."""
        text = "#include <umbrella/u.h>\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer", "autoinfer-umbrella"]

    # ------------------------------- verbose label: inferred vs explicit

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_inferred_package_verbose_label_marks_inferred(self, capsys):
        """The inferred package rides the ordinary PKG-CONFIG processing path
        carrying the inferred marker, so the verbose>=9 per-package trace
        labels it 'Magic PKG-CONFIG (inferred) = ...' -- and, with no
        annotation anywhere in the TU, no plain-labelled trace appears."""
        _, result = self._parse(TU_AUTOINFER, extraargs=["-vvvvvvvvv"])
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"], "precondition: autoinfer was inferred"
        out = capsys.readouterr().out
        assert "Magic PKG-CONFIG (inferred) = autoinfer:" in out
        assert "Magic PKG-CONFIG = autoinfer:" not in out

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_explicit_package_verbose_label_not_marked_inferred(self, capsys):
        """The must-not-leak control: a hand-written //#PKG-CONFIG=autoinfer
        prints the plain 'Magic PKG-CONFIG = ...' trace at the same verbosity
        -- the (inferred) marker never lands on an explicit annotation (the
        explicit flag covers the header, so inference adds nothing here)."""
        text = "//#PKG-CONFIG=autoinfer\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text, extraargs=["-vvvvvvvvv"])
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer"], "precondition: the annotation parsed"
        out = capsys.readouterr().out
        assert "Magic PKG-CONFIG = autoinfer:" in out
        assert "Magic PKG-CONFIG (inferred)" not in out

    # --------------------------------------------- F2: ambiguity warn/strict/map

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("auto_pkg_config_on")
    def test_ambiguity_warn_strict_and_map(self, pkgconfig_env, tmp_path, monkeypatch, capsys):
        """fam/x.h against a pc dir holding only the -a/-b family members:
        warn keeps parsing and explains the pin; the map pins one package;
        --pkg-config-errors=error aborts the parse with SystemExit."""
        pcdir = tmp_path / "pc"
        pcdir.mkdir()
        for name in ("autoinfer-fam-a.pc", "autoinfer-fam-b.pc"):
            shutil.copy(os.path.join(pkgconfig_env, name), pcdir / name)
        shutil.copytree(os.path.join(pkgconfig_env, "autoinfer-fam-include"), pcdir / "autoinfer-fam-include")
        monkeypatch.setenv("PKG_CONFIG_PATH", str(pcdir))

        # Warn (verbose>=1): no package, and a diagnostic naming candidates and the pin.
        _, result = self._parse("#include <fam/x.h>\nint main() { return 0; }\n", extraargs=["-v"])
        assert "PKG-CONFIG" not in result
        err = capsys.readouterr().err
        assert "ambiguous" in err
        assert "autoinfer-fam-a" in err and "autoinfer-fam-b" in err
        assert "auto-pkg-config-map=fam/x.h=" in err

        # Map wins over the resolvers.
        _, mapped = self._parse(
            "#include <fam/x.h>\nint main() { return 0; }\n",
            extraargs=["--auto-pkg-config-map=fam/x.h=autoinfer-fam-a"],
        )
        assert self._strings(mapped, "PKG-CONFIG") == ["autoinfer-fam-a"]

        # Strict aborts the parse rather than silently dropping the header.
        with pytest.raises(SystemExit):
            self._parse("#include <fam/x.h>\nint main() { return 0; }\n", extraargs=["--pkg-config-errors=error"])

    # ------------------------------------------------- F2: user ignore globs

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_ignore_glob_suppresses_inference(self):
        """--auto-pkg-config-ignore=autoinfer/*: the spelling is never a
        candidate, so nothing is inferred for it."""
        _, result = self._parse(TU_AUTOINFER, extraargs=["--auto-pkg-config-ignore=autoinfer/*"])
        assert "PKG-CONFIG" not in result

    # ------------------------------- F9: xxpend accumulation across conf + CLI

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_map_accumulates_across_conf_and_cli(self):
        """A conf-file append-AUTO-PKG-CONFIG-MAP entry and a CLI one both
        reach the same header map. Each entry is provably effective: without
        it fam/x.h resolves (tie-break) to autoinfer-fam or stays ambiguous,
        and umbrella/u.h resolves to autoinfer-umbrella -- neither is the
        pinned value."""
        text = "#include <umbrella/u.h>\n#include <fam/x.h>\nint main() { return 0; }\n"
        _, result = self._parse(
            text,
            extraargs=["--append-AUTO-PKG-CONFIG-MAP=umbrella/u.h=autoinfer"],
            extralines=["append-AUTO-PKG-CONFIG-MAP = fam/x.h=autoinfer-fam-a"],
        )
        assert self._strings(result, "PKG-CONFIG") == ["autoinfer", "autoinfer-fam-a"]

    # ------------------------------------------------- F5: memo across TUs

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_memo_resolves_once_across_translation_units(self, monkeypatch):
        """Two TUs naming the same header through one parser (one
        BuildContext memo): the pc-index resolution runs once and the second
        parse reuses the memoised verdict; both parse results agree."""
        real_resolve = compiletools.pkgconfig_infer.resolve_via_pc_index
        seen = []

        def counting_resolve(header, header_path, index):
            seen.append(str(header))
            return real_resolve(header, header_path, index)

        monkeypatch.setattr(compiletools.pkgconfig_infer, "resolve_via_pc_index", counting_resolve)
        parser = self._make_parser()
        first_tu = self._write_tu(TU_AUTOINFER)
        second_tu = self._write_tu("#include <autoinfer/api.h>\nint other() { return 1; }\n", name="other.cpp")
        first = parser.parse(first_tu)
        second = parser.parse(second_tu)
        assert len(seen) == 1, f"resolver consulted {len(seen)}x for the same header: {seen} ({first_tu})"
        assert self._strings(first, "PKG-CONFIG") == ["autoinfer"]
        assert self._strings(second, "PKG-CONFIG") == ["autoinfer"]

    # --------------------------- language-aware built-in include-dir probe

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_builtin_include_dirs_probed_with_language_matching_compiler(self, monkeypatch):
        """The TU's main file picks the compiler whose built-in angle-bracket
        search list inference probes: a .c TU is probed with the config's CC
        (mirroring the compile rule's CC + cflags dispatch) and a .cpp TU with
        CXX. The recorder delegates to the real probe, so each parse must
        still infer autoinfer -- the CC dispatch must not cost the C
        translation unit its package."""
        real_builtin_include_dirs = compiletools.compiler_macros.builtin_include_dirs
        probed = []

        def recording_builtin_include_dirs(compiler_path, verbose=0):
            probed.append(str(compiler_path))
            return real_builtin_include_dirs(compiler_path, verbose)

        monkeypatch.setattr(compiletools.compiler_macros, "builtin_include_dirs", recording_builtin_include_dirs)
        parser = self._make_parser()
        # Distinct TU contents: the structured-data cache keys on content
        # hash, so two byte-identical TUs through one parser would collide
        # on the cache key and the second parse would restore state that was
        # never populated for its own path (see the memo test above).
        c_tu = self._write_tu(TU_AUTOINFER, name="main.c")
        cpp_tu = self._write_tu("#include <autoinfer/api.h>\nint other() { return 1; }\n", name="other.cpp")
        c_result = parser.parse(c_tu)
        cpp_result = parser.parse(cpp_tu)
        assert probed == [parser._args.CC, parser._args.CXX], (
            f"built-in probe must follow the TU language: probed={probed}, "
            f"CC={parser._args.CC!r}, CXX={parser._args.CXX!r}"
        )
        assert self._strings(c_result, "PKG-CONFIG") == ["autoinfer"]
        assert self._strings(cpp_result, "PKG-CONFIG") == ["autoinfer"]

    # ------------------------------------------------- F6: never raise

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_inference_failure_never_breaks_explicit_flags(self, monkeypatch):
        """An inference-internal crash is swallowed to a verbose>=1 note
        (here silent, the only place silence is contractual): the TU's
        explicit flags parse exactly as if --auto-pkg-config did not exist."""

        def _explode(*args, **kwargs):
            raise RuntimeError("simulated inference crash")

        monkeypatch.setattr(compiletools.pkgconfig_infer, "infer", _explode)
        text = "//#LDFLAGS=-lfoo\n//#CXXFLAGS=-DFOO\n#include <autoinfer/api.h>\nint main() { return 0; }\n"
        _, result = self._parse(text)
        assert self._strings(result, "LDFLAGS") == ["-lfoo"]
        assert "-DFOO" in self._strings(result, "CXXFLAGS")
        assert "PKG-CONFIG" not in result

    # ------------------------------------------------- F7: determinism

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("pkgconfig_env", "auto_pkg_config_on")
    def test_deterministic_across_repeated_parses(self):
        """The same TU parsed twice through one parser produces the identical
        flag dict -- no process-global state leaks between parses."""
        tu = self._write_tu(TU_AUTOINFER)
        parser = self._make_parser()
        first = self._projection(parser.parse(tu))
        parser.clear_cache()
        second = self._projection(parser.parse(tu))
        assert first == second

    # ------------------------------------- F8: test-XML framework detection

    @uth.requires_functional_compiler
    @pytest.mark.usefixtures("auto_pkg_config_on")
    def test_inferred_package_feeds_test_framework_detection(self, monkeypatch, capsys):
        """A site-local gtest package: the inferred //#PKG-CONFIG=gtest feeds
        test_framework detection exactly like the annotation -- the gtest
        framework is recognised and no ambiguity warning is emitted."""
        import compiletools.test_framework

        include_dir = os.path.join(os.getcwd(), "inc", "gtest")
        os.makedirs(include_dir, exist_ok=True)
        with open(os.path.join(include_dir, "gtest.h"), "w") as handle:
            handle.write("#pragma once\n// gtest stand-in\n")
        with open(os.path.join(os.getcwd(), "gtest.pc"), "w") as handle:
            handle.write(
                "Name: gtest\nDescription: auto-pkg-config test-XML fixture\nVersion: 1.0\n"
                f"Cflags: -I{os.path.join(os.getcwd(), 'inc')}\nLibs: -lgtest\n"
            )
        monkeypatch.setenv("PKG_CONFIG_PATH", os.getcwd())

        _, result = self._parse("#include <gtest/gtest.h>\nint main() { return 0; }\n")
        packages = self._strings(result, "PKG-CONFIG")
        assert packages == ["gtest"]

        detected = compiletools.test_framework.detect_framework(["gtest/gtest.h"], "main.cpp", packages)
        assert detected is not None and detected.id == "gtest"
        pkg_only = compiletools.test_framework.detect_framework([], "main.cpp", packages)
        assert pkg_only is not None and pkg_only.id == "gtest"
        assert "multiple test frameworks" not in capsys.readouterr().err
