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
from compiletools.apptools_argparse import _fix_variable_handling_method
from compiletools.build_context import BuildContext
from compiletools.build_inputs import gather_inputs
from compiletools.build_state import compute_build_state


@pytest.fixture
def parsers_reset():
    uth.reset()
    yield
    uth.reset()


def _old_and_new(extra_argv=(), register_link_args=True, *, explicit_config=True, confdir=None):
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

    confdir=None (default) writes the project ct.conf and the temp
    --config file at os.getcwd(), matching every existing caller. A case
    invoked from a SUBDIR of the gitroot (monkeypatch.chdir'd before
    calling this) must pass confdir=<gitroot> explicitly -- writing the
    conf at the subdir would put it at the wrong tier and, since both
    pipelines read the same cwd, wouldn't even surface as a divergence.

    The raw (new-core) namespace mirrors apptools.parseargs's append-mode
    branch: when --variable-handling-method=append is in play, a bare
    cap2.parse_args() never reroutes a FLAG_ENV_VAR_NAMES env var into
    APPEND_*, because configargparse only supports "override" semantics for
    environment-sourced values. This helper replicates parseargs's
    `_fix_variable_handling_method` reparse + `_stash_private_attrs` on the
    raw namespace for harness fidelity -- without it, the "append" CASES
    row is vacuous whenever no FLAG_ENV_VAR_NAMES member is actually set in
    os.environ, and even when one is set, the raw namespace would silently
    diverge from the old pipeline for a reason that has nothing to do with
    the core under test.
    """
    if confdir is None:
        confdir = os.getcwd()
    uth.create_temp_ct_conf(confdir)

    def _run(argv):
        cap2 = apptools.create_parser("differential2", argv=argv)
        compiletools.hunter.add_arguments(cap2)
        apptools.add_output_directory_arguments(cap2, variant="unsupplied")
        apptools.add_target_arguments_ex(cap2)
        if register_link_args:
            apptools.add_link_arguments(cap2)
        with uth.ParserContext():
            context = BuildContext()
            # Parse the new core's input and gather+compute FIRST, from a
            # pristine environment -- before the old pipeline's parseargs
            # (below) mutates PKG_CONFIG_PATH. Reading the environment
            # only after that mutation would let gather's PKG_CONFIG_PATH
            # read accidentally depend on the old merge already having run
            # (and being a fixed point under re-application), instead of
            # genuinely reproducing it from scratch.
            raw = cap2.parse_args(args=list(argv))
            apptools._stash_private_attrs(raw, cap2, context, list(argv))
            # Mirrors apptools.parseargs's append-mode branch: configargparse
            # only supports "override" for environment-sourced values, so a
            # bare cap2.parse_args() never reroutes a FLAG_ENV_VAR_NAMES env
            # var (CPPFLAGS/CFLAGS/CXXFLAGS/LDFLAGS/INCLUDE) into APPEND_*.
            # Without this the new core's raw namespace would silently
            # disagree with the old pipeline whenever both an env var AND
            # --variable-handling-method=append are in play -- harness
            # fidelity, not production behavior.
            if raw.variable_handling_method == "append":
                raw = _fix_variable_handling_method(cap2, list(argv), getattr(raw, "verbose", 0))
                apptools._stash_private_attrs(raw, cap2, context, list(argv))
            apptools._flatten_variables(raw)
            apptools._strip_quotes(raw)
            state = compute_build_state(gather_inputs(raw, context))

            cap = apptools.create_parser("differential", argv=argv)
            compiletools.hunter.add_arguments(cap)
            apptools.add_output_directory_arguments(cap, variant="unsupplied")
            apptools.add_target_arguments_ex(cap)
            if register_link_args:
                apptools.add_link_arguments(cap)
            args = apptools.parseargs(cap, list(argv), context=context)
            return args, state, raw

    if explicit_config:
        with uth.TempConfigContext(tempdir=confdir) as temp_config_name:
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
    pytest.param(("--variable-handling-method=append",), True, False, id="append-mode"),
    pytest.param(('--CPPFLAGS=-DMSG="hello world"',), True, False, id="quoted-value"),
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
class TestDifferentialToolchainVariants:
    def test_clang_variant_agrees(self):
        """clang toolchain: flags/names parity (guarded: requires clang++ on
        PATH; skip otherwise)."""
        import shutil as _sh

        if not _sh.which("clang++"):
            pytest.skip("clang++ not on PATH")
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--variant=clang,debug",))
            assert tuple(args.CXXFLAGS_tokens) == state.tokens.cxx
            assert tuple(args.LDFLAGS_tokens) == state.tokens.ld
            assert args.variant == state.names.variant

    def test_clang_variant_agrees_including_wild_rewrite(self):
        """The live -fuse-ld=wild -> --ld-path=wild rewrite when the wild
        axis rides along a clang variant (guarded: requires clang++ AND
        wild on PATH; skip otherwise, naming which prerequisite is
        missing). Kept as its own test, separate from
        test_clang_variant_agrees, so a missing `wild` binary skips only
        this sub-case instead of silently dropping it after that test's
        earlier asserts already ran and passed."""
        import shutil as _sh

        if not _sh.which("clang++"):
            pytest.skip("clang++ not on PATH")
        if not _sh.which("wild"):
            pytest.skip("wild linker not on PATH")
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--variant=clang,debug,wild",))
            assert "--ld-path=wild" in state.tokens.ld
            assert tuple(args.LDFLAGS_tokens) == state.tokens.ld


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
class TestRawStringAndExeNameSplit:
    """Consumers outside apptools' own pipeline that still read
    args.CPPFLAGS/CFLAGS/CXXFLAGS/LDFLAGS as raw strings (see the Task 5
    audit) rely on the raw string agreeing with the token form for
    unquoted token streams, and on CPP/LD staying resolved by the old
    pipeline since BuildState does not model them."""

    def test_raw_string_form_agrees_for_unquoted_tokens(self):
        """populate_args writes shlex.join'ed strings. For token streams
        with no shell-active characters the round-trip is the identity, so
        raw strings agree; quoted-value cases are covered by the
        quoted-value CASES row at the token level (raw strings there may
        legitimately differ in quoting style -- that difference is confined
        to display)."""
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(())
            import shlex as _shlex

            assert _shlex.split(args.CXXFLAGS) == list(state.tokens.cxx)
            assert _shlex.split(args.LDFLAGS) == list(state.tokens.ld)

    def test_cpp_and_ld_exe_names_stay_on_the_old_path(self):
        """BuildState does not model CPP/LD executable names; the old
        pipeline's _substitute_CXX_for_missing owns them through the swap.
        Pin that the old pipeline still resolves them so the swap task
        cannot silently drop the responsibility."""
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(())
            assert args.CPP not in (None, apptools._UNSUPPLIED_USE_CXX), "old pipeline must resolve CPP"
            assert args.LD not in (None, apptools._UNSUPPLIED_USE_CXX), "old pipeline must resolve LD"
            assert not hasattr(state, "cpp_exe"), "BuildState must not grow exe names silently"


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

    def test_subdir_invocation_anchors_relative_cas_dirs(self):
        """Relative --cas-objdir invoked from a SUBDIR of the gitroot must
        anchor to the gitroot identically in both pipelines (the resolver's
        anchoring gate in apptools_argparse.resolve_cas_directory_arguments
        vs its port in build_inputs._anchored_cas_dir).

        No current CASES/blessed-divergence case reaches this branch -- all
        of them run with cwd == gitroot, where the anchoring gate
        (realpath(gitroot) != cwd) never fires.

        Uses a raw ``os.chdir`` into the subdir, not ``monkeypatch.chdir``:
        ``uth.TempDirContext.__exit__`` restores the outer cwd and rmtree's
        the tmpdir INSIDE this ``with`` block, before the test function
        returns; a ``monkeypatch.chdir`` fixture only undoes at test
        teardown (after the block already exited) and would try to chdir
        back into the already-deleted subdir, raising FileNotFoundError
        (the exact ordering hazard documented on
        ``conftest.py``'s hermetic-git-env fixture). ``TempDirContext``
        itself restores the pre-``with`` cwd on exit, so no manual restore
        of the outer chdir is needed here either (mirrors ``test_base.py``'s
        ``os.chdir(tempdir)`` inside a ``with uth.TempDirContext()``).
        """
        with uth.TempDirContext():
            # TempDirContext's tmpdir has no ancestor .git, so find_git_root
            # falls back to returning the queried directory itself -- from
            # the subdir below, that would make gitroot == cwd and the
            # anchoring gate vacuous. Plant a real .git marker at this level
            # (directory + HEAD, the same convention as
            # testhelper.copy_example_workspace / test_relative_cas_dir_bug)
            # so find_git_root resolves HERE from the subdir.
            gitroot = os.getcwd()
            git_dir = os.path.join(gitroot, ".git")
            os.mkdir(git_dir)
            with open(os.path.join(git_dir, "HEAD"), "w") as f:
                f.write("ref: refs/heads/main\n")
            subdir = os.path.join(gitroot, "sub")
            os.makedirs(subdir)
            os.chdir(subdir)
            # confdir=gitroot: _old_and_new's default writes the project
            # ct.conf and the temp --config file at os.getcwd() (the
            # subdir), the wrong tier for a gitroot-level project conf.
            args, state, _raw = _old_and_new(("--cas-objdir=relcas/obj",), confdir=gitroot)
            assert args.cas_objdir == state.names.cas_objdir
            assert not args.cas_objdir.startswith(subdir), (
                f"Precondition failed: anchoring did not fire — cas_objdir {args.cas_objdir!r} resolved under the subdir cwd."
            )
            assert args.cas_objdir.startswith(os.path.join(gitroot, "relcas")), (
                f"Precondition failed: expected gitroot-anchored relcas path, got {args.cas_objdir!r}."
            )

    def test_pkg_config_path_value_parity(self, pkgconfig_env):
        """The value the new core would SetEnv must equal what the old
        pipeline actually wrote to the environment."""
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--pkg-config=nested",))
            del args
            old_env_value = os.environ.get("PKG_CONFIG_PATH")
            assert state.pkg_config_path == old_env_value, (
                f"new core would set {state.pkg_config_path!r}; old pipeline left {old_env_value!r}"
            )

    def test_append_mode_env_var_reroutes_identically(self, monkeypatch):
        """--variable-handling-method=append with a real CPPFLAGS env var:
        the old pipeline reparses via _fix_variable_handling_method so the
        env value APPENDS to conf/CLI values instead of overriding. The new
        core must see the same final tokens."""
        monkeypatch.setenv("CPPFLAGS", "-DFROMENV")
        with uth.TempDirContext():
            args, state, _raw = _old_and_new(("--variable-handling-method=append",))
            assert "-DFROMENV" in args.CPPFLAGS_tokens, (
                "Precondition failed: the env var never reached the old pipeline's CPPFLAGS."
            )
            assert tuple(args.CPPFLAGS_tokens) == state.tokens.cpp
            assert tuple(args.CXXFLAGS_tokens) == state.tokens.cxx
