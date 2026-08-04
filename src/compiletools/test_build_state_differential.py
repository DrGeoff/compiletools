"""Old pipeline vs pure core over real parses.

Divergences are the DELIVERABLE of this suite during Branch 1: each
narrowed assertion below corresponds to a blessed-divergence record
(D1..D4) in docs/superpowers/specs/2026-08-04-functional-build-state-design.md.
An UNLISTED divergence is a bug in the core -- fix the core, do not
xfail it.

The harness registers the full cake-shaped parser surface (hunter +
output-directory + target + link args) so the gitroot INCLUDE widening,
project-macro, and cas-dir paths are all live in both pipelines.
"""

import os
import stat

import pytest

import compiletools.apptools as apptools
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext
from compiletools.build_inputs import gather_inputs
from compiletools.build_state import compute_build_state


@pytest.fixture
def parsers_reset():
    uth.reset()
    yield
    uth.reset()


def _old_and_new(extra_argv=(), register_link_args=True, *, explicit_config=True):
    """Run the real parseargs (old pipeline) AND gather+compute (new core)
    from one argv/conf setup; return (args, state, raw).

    explicit_config=False omits the injected --config=<tempfile>. An
    explicit --config makes configutils.extract_variant's impliedvariant()
    short-circuit ALL variant axis/composite discovery -- resolve_variant's
    explicit_config branch returns empty axes unconditionally, treating the
    config path as the sole source of truth (by design: "the path itself is
    authoritative"). That is correct default behavior for the CASES table
    (deterministic CC/CXX, no axis-file dependence), but it also makes any
    --variant token on extra_argv inert, so a case that must genuinely
    exercise extends-based composite resolution has to skip the injection.
    """
    uth.create_temp_ct_conf(os.getcwd())

    def _run(argv):
        cap = apptools.create_parser("differential", argv=argv)
        compiletools.hunter.add_arguments(cap)
        apptools.add_output_directory_arguments(cap, variant="unsupplied")
        apptools.add_target_arguments_ex(cap)
        if register_link_args:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            context = BuildContext()
            args = apptools.parseargs(cap, list(argv), context=context)
            # Re-parse for the new core's input (parseargs mutated args).
            cap2 = apptools.create_parser("differential2", argv=argv)
            compiletools.hunter.add_arguments(cap2)
            apptools.add_output_directory_arguments(cap2, variant="unsupplied")
            apptools.add_target_arguments_ex(cap2)
            if register_link_args:
                apptools.add_link_arguments(cap2)
            raw = cap2.parse_args(args=list(argv))
            apptools._flatten_variables(raw)
            apptools._strip_quotes(raw)
            state = compute_build_state(gather_inputs(raw, context))
            return args, state, raw

    if explicit_config:
        with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
            return _run(["--config=" + temp_config_name, *extra_argv])
    return _run(list(extra_argv))


# pytest.param cannot carry a usefixtures mark (pytest rejects it at
# collection), so pkg-config cases carry a needs_pkgconfig flag and the
# _case fixture pulls pkgconfig_env dynamically instead.
CASES = [
    pytest.param((), True, False, id="plain"),
    pytest.param(("--separate-flags-CPP-CXX",), True, False, id="separate-flags"),
    pytest.param(("--pkg-config=nested",), True, True, id="pkgconfig"),
    pytest.param(("--pkg-config=nested",), False, True, id="pkgconfig-three-slot"),
    pytest.param(("--prepend-CXXFLAGS=-DFROMPRE", "--append-CXXFLAGS=-DFROMAPP"), True, False, id="xxpend"),
    pytest.param(("--append-CXXFLAGS=-DBOTH", "--prepend-CXXFLAGS=-DBOTH"), True, False, id="xxpend-both-lists"),
    pytest.param(("--append-CXXFLAGS=-DA -DB",), True, False, id="xxpend-multi-token-element"),
    pytest.param(("--INCLUDE=/opt/ct-diff/include",), True, False, id="include"),
    pytest.param(("--append-INCLUDE=/opt/ct-diff/extra",), True, False, id="append-include"),
    pytest.param(("--project-version=1.2.3",), True, False, id="project-version"),
    pytest.param(("--project-name=myapp",), True, False, id="project-name"),
    pytest.param(("--variant=gcc,debug",), True, False, id="variant-commas"),
    pytest.param(("--bindir=mybin/sub/",), True, False, id="bindir-explicit"),
    pytest.param(("--LDFLAGS=-lm -ldl",), True, False, id="ldflags-explicit"),
    pytest.param(("--CPPFLAGS=-DFOO",), True, False, id="cppflags-explicit"),
    pytest.param(("--separate-flags-CPP-CXX", "--CPPFLAGS=-DFOO"), True, False, id="separate-plus-cpp"),
    pytest.param(("--quiet", "--quiet"), True, False, id="quiet"),
    pytest.param(("--cas-objdir=relcas/obj",), True, False, id="cas-objdir-relative"),
]


@pytest.fixture
def _case(request, extra_argv, link_args, needs_pkgconfig):
    if needs_pkgconfig:
        request.getfixturevalue("pkgconfig_env")
    return extra_argv, link_args


@pytest.mark.usefixtures("parsers_reset")
@pytest.mark.parametrize("extra_argv,link_args,needs_pkgconfig", CASES)
class TestDifferential:
    def test_flag_tokens_agree(self, _case):
        extra_argv, link_args = _case
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(extra_argv, link_args)
            assert tuple(args.CXXFLAGS_tokens) == state.tokens.cxx
            assert tuple(args.CPPFLAGS_tokens) == state.tokens.cpp
            assert tuple(args.CFLAGS_tokens) == state.tokens.c
            assert tuple(args.LDFLAGS_tokens) == state.tokens.ld

    def test_names_agree(self, _case):
        extra_argv, link_args = _case
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(extra_argv, link_args)
            assert args.variant == state.names.variant
            assert args.bindir == state.names.bindir
            assert args.cas_objdir == state.names.cas_objdir
            assert args.cas_pchdir == state.names.cas_pchdir
            assert args.cas_pcmdir == state.names.cas_pcmdir
            assert args.cas_exedir == state.names.cas_exedir


@pytest.mark.usefixtures("parsers_reset")
class TestBlessedDivergences:
    """Each test pins one appendix entry: the narrowed assertion states
    both the old and the new behavior so a change on EITHER side of the
    divergence fails loudly and forces a spec-appendix revisit."""

    def test_project_macro_suppression_is_all_slot_and_pre_substitution(self):
        """Blessed divergence D1: a -DCT_PROJECT_* name present in one raw
        slot suppresses injection into ALL three compile slots in the new
        core; the old pipeline suppressed per-slot, injecting into the
        others."""
        macro = '-DCT_PROJECT_VERSION="1.2.3"'
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(
                ("--project-version=1.2.3", "--CFLAGS=-fPIC -g -Wall -DCT_PROJECT_VERSION=old")
            )
            # Both agree the pre-seeded slot is not re-injected.
            assert tuple(args.CFLAGS_tokens) == state.tokens.c
            assert macro not in state.tokens.c
            # Old injected into the unseeded slots; new suppressed everywhere.
            assert macro in args.CXXFLAGS_tokens
            assert macro not in state.tokens.cxx
            assert tuple(t for t in args.CXXFLAGS_tokens if t != macro) == state.tokens.cxx
            assert tuple(t for t in args.CPPFLAGS_tokens if t != macro) == state.tokens.cpp

    def test_project_macro_arriving_via_append_shifts_position_only(self):
        """Blessed divergence D1 (append-arrival facet): when the macro
        NAME reaches a slot only through --append-CPPFLAGS, the old
        pipeline suppressed that one slot post-xxpend (unify re-merged the
        macro in anyway, at tail position); gather reads the pre-xxpend raw
        slots, finds no hit, and injects at the project-macros stage. The
        final token SET is identical; only the injected macro's position
        differs."""
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--append-CPPFLAGS=-DCT_PROJECT_VERSION=xx", "--project-version=1.2.3"))
            assert '-DCT_PROJECT_VERSION="1.2.3"' in state.tokens.cpp
            assert "-DCT_PROJECT_VERSION=xx" in state.tokens.cpp
            for slot, old in (
                ("cpp", args.CPPFLAGS_tokens),
                ("cxx", args.CXXFLAGS_tokens),
                ("c", args.CFLAGS_tokens),
            ):
                assert sorted(old) == sorted(getattr(state.tokens, slot)), slot

    def test_xxpend_dedup_is_token_exact_not_substring(self):
        """Blessed divergence D2: old _do_xxpend suppressed an appended
        element when it appeared as a SUBSTRING of the raw slot string
        (-W suppressed by -Wall); stage_xxpend dedups exact tokens only."""
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--append-CXXFLAGS=-W",))
            assert "-Wall" in state.tokens.cxx
            assert "-W" in state.tokens.cxx
            assert "-W" not in args.CXXFLAGS_tokens
            assert tuple(args.CXXFLAGS_tokens) == tuple(t for t in state.tokens.cxx if t != "-W")

    def test_projectversioncmd_write_back_is_dropped(self):
        """Blessed divergence D3: the old pipeline cached the cmd output
        into args.projectversion; gather never mutates the namespace. The
        emitted tokens are identical."""
        with uth.TempDirContext():
            args, state, raw = _old_and_new(("--project-version-cmd=echo 9.9.9 trailing",))
            assert tuple(args.CXXFLAGS_tokens) == state.tokens.cxx
            assert '-DCT_PROJECT_VERSION="9.9.9"' in state.tokens.cxx
            assert args.projectversion == "9.9.9"
            assert raw.projectversion is None

    def test_wild_b_dash_b_token_lands_in_ld_tokens(self, tmp_path, monkeypatch):
        """Blessed divergence D4: the old pipeline kept -B<dir> OUT of
        LDFLAGS (side-channel args._wild_b_search_dir, injected per link
        rule by build_backend); the new core carries it in state.tokens.ld
        plus an EnsureLinkerSymlinkDir effect."""
        fake_wild = tmp_path / "wild"
        fake_wild.write_text("#!/bin/sh\nexit 0\n")
        fake_wild.chmod(fake_wild.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--variant=gcc,debug,wild-B",))
            search_dir = args._wild_b_search_dir
            assert search_dir and search_dir.endswith(".ct-wild-ld")
            assert not any(t.startswith("-B") for t in args.LDFLAGS_tokens)
            assert state.tokens.ld == tuple(args.LDFLAGS_tokens) + (f"-B{search_dir}",)


@pytest.mark.usefixtures("parsers_reset")
class TestDifferentialConfShapes:
    """Conf-file shapes the CASES table cannot express: composite variants
    with extends, and a subproject ct.conf.d layer."""

    def test_extends_composite_variant_agrees(self):
        """A composite conf using extends = ... must resolve to the same
        variant name, flags, and cas dirs in both pipelines.

        Composite filename must match the CANONICALIZED multi-token variant
        name (configutils.resolve_variant's composite lookup only fires for
        len(canonical_tokens) > 1): --variant=gcc,debug,myext canonicalizes
        to "gcc.debug.myext" (gcc/debug sort by canonical order, the unknown
        "myext" token trails), so the file is named accordingly. Must also
        run with explicit_config=False -- an injected --config=<tempfile>
        (the _old_and_new default) short-circuits ALL axis/composite
        discovery via impliedvariant(), which would make this precondition
        vacuous on both pipelines equally rather than exercising extends.
        """
        with uth.TempDirContext():
            confdir = os.path.join(os.getcwd(), "ct.conf.d")
            os.makedirs(confdir)
            with open(os.path.join(confdir, "gcc.debug.myext.conf"), "w") as f:
                f.write("extends = gcc, debug\nappend-CXXFLAGS = -DFROM_COMPOSITE\n")
            args, state, _raw = _old_and_new(("--variant=gcc,debug,myext",), explicit_config=False)
            assert args.variant == state.names.variant
            assert tuple(args.CXXFLAGS_tokens) == state.tokens.cxx
            assert "-DFROM_COMPOSITE" in state.tokens.cxx, (
                "Precondition: the composite's append must actually land, or this test is vacuous."
            )
            assert args.cas_objdir == state.names.cas_objdir

    def test_subproject_conf_layer_agrees(self):
        """A nested ct.conf.d (cwd tier) contributing flags must reach both
        pipelines identically."""
        with uth.TempDirContext():
            confdir = os.path.join(os.getcwd(), "ct.conf.d")
            os.makedirs(confdir)
            with open(os.path.join(confdir, "ct.conf"), "w") as f:
                f.write("append-CPPFLAGS = -DFROM_SUBPROJECT\n")
            args, state, _raw = _old_and_new(())
            assert "-DFROM_SUBPROJECT" in state.tokens.cpp, (
                "Precondition: the cwd-tier conf must actually contribute, or this test is vacuous."
            )
            assert tuple(args.CPPFLAGS_tokens) == state.tokens.cpp
            assert args.variant == state.names.variant
