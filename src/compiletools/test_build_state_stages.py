"""Table-driven unit tests for the pure build-state stages.

Every test constructs BuildInputs literals -- no tempdirs, parsers,
conf files, or compilers.
"""

from compiletools.build_inputs import BuildInputs, PkgConfigResult
from compiletools.build_state import (
    EnsureLinkerSymlinkDir,
    TokenState,
    stage_dedup,
    stage_defaults,
    stage_include_paths,
    stage_pkg_config_flags,
    stage_prefix_map,
    stage_project_macros,
    stage_resolve_names,
    stage_unify,
    stage_wild_linker,
    stage_xxpend,
)

ALL_SLOTS = frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"})


def _inputs(**kw):
    kw.setdefault("registered_slots", ALL_SLOTS)
    return BuildInputs(**kw)


class TestStageDefaults:
    def test_slots_copied_from_inputs(self):
        ts = stage_defaults(_inputs(cppflags=("-DX",), cxxflags=("-O2",)))
        assert ts.cpp == ("-DX",)
        assert ts.cxx == ("-O2",)

    def test_unsupplied_cppflags_falls_back_to_cxxflags(self):
        ts = stage_defaults(_inputs(cppflags=None, cxxflags=("-O2", "-Wall")))
        assert ts.cpp == ("-O2", "-Wall")

    def test_explicitly_empty_cppflags_stays_empty(self):
        ts = stage_defaults(_inputs(cppflags=(), cxxflags=("-O2", "-Wall")))
        assert ts.cpp == ()

    def test_unsupplied_ldflags_falls_back_to_cxxflags_when_registered(self):
        ts = stage_defaults(_inputs(ldflags=None, cxxflags=("-O2",)))
        assert ts.ld == ("-O2",)

    def test_explicitly_empty_ldflags_stays_empty_when_registered(self):
        ts = stage_defaults(_inputs(ldflags=(), cxxflags=("-O2",)))
        assert ts.ld == ()

    def test_unregistered_ldflags_stays_empty(self):
        ts = stage_defaults(
            _inputs(registered_slots=frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"}), ldflags=None, cxxflags=("-O2",))
        )
        assert ts.ld == ()


class TestStageXxpend:
    def test_prepend_goes_leftmost_append_rightmost(self):
        inputs = _inputs(prepend_cxxflags=("-P",), append_cxxflags=("-A",))
        ts = stage_xxpend(inputs, TokenState(cxx=("-O2",)))
        assert ts.cxx == ("-P", "-O2", "-A")

    def test_already_present_token_not_duplicated(self):
        inputs = _inputs(prepend_cxxflags=("-O2",))
        ts = stage_xxpend(inputs, TokenState(cxx=("-O2",)))
        assert ts.cxx == ("-O2",)

    def test_other_slots_untouched(self):
        inputs = _inputs(prepend_cxxflags=("-P",))
        ts = stage_xxpend(inputs, TokenState(cpp=("-DX",), cxx=()))
        assert ts.cpp == ("-DX",)


class TestStageIncludePaths:
    def test_appends_detached_pairs_to_compile_slots_only(self):
        inputs = _inputs(include_paths=("/inc",))
        ts = stage_include_paths(inputs, TokenState(cpp=("-DX",), ld=("-lm",)))
        assert ts.cpp == ("-DX", "-I", "/inc")
        assert ts.c == ("-I", "/inc")
        assert ts.cxx == ("-I", "/inc")
        assert ts.ld == ("-lm",)

    def test_already_present_path_skipped_per_slot(self):
        inputs = _inputs(include_paths=("/inc",))
        ts = stage_include_paths(inputs, TokenState(cpp=("-I/inc",)))
        assert ts.cpp == ("-I/inc",)
        assert ts.c == ("-I", "/inc")

    def test_no_include_paths_is_identity(self):
        ts = TokenState(cpp=("-DX",))
        assert stage_include_paths(_inputs(), ts) is ts


class TestStageProjectMacros:
    def test_version_and_name_appended_as_single_tokens(self):
        inputs = _inputs(project_version="1.2.3", project_name="myproj")
        ts = stage_project_macros(inputs, TokenState())
        assert '-DCT_PROJECT_VERSION="1.2.3"' in ts.cpp
        assert '-DCT_PROJECT_NAME="myproj"' in ts.cxx
        assert ts.ld == ()

    def test_none_means_no_macro(self):
        ts = stage_project_macros(_inputs(), TokenState(cpp=("-DX",)))
        assert ts.cpp == ("-DX",)

    def test_existing_macro_not_doubled(self):
        tok = '-DCT_PROJECT_VERSION="1.2.3"'
        inputs = _inputs(project_version="1.2.3")
        ts = stage_project_macros(inputs, TokenState(cpp=(tok,), c=(tok,), cxx=(tok,)))
        assert ts.cpp.count(tok) == 1


def _zlib_inputs(**kw):
    return _inputs(
        pkg_config_results=(("zlib", PkgConfigResult(cflags=("-I/z",), libs=("-lz",))),),
        **kw,
    )


class TestStagePkgConfigFlags:
    def test_cflags_to_compile_slots_libs_to_ld(self):
        ts = stage_pkg_config_flags(_zlib_inputs(), TokenState())
        assert ts.cpp == ("-I/z",) and ts.c == ("-I/z",) and ts.cxx == ("-I/z",)
        assert ts.ld == ("-lz",)

    def test_unregistered_ldflags_never_receives_libs(self):
        inputs = _inputs(
            registered_slots=frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"}),
            pkg_config_results=(("zlib", PkgConfigResult(cflags=("-I/z",), libs=("-lz",))),),
        )
        ts = stage_pkg_config_flags(inputs, TokenState())
        assert ts.ld == ()

    def test_package_order_preserved(self):
        inputs = _inputs(
            pkg_config_results=(
                ("a", PkgConfigResult(cflags=(), libs=("-la",))),
                ("b", PkgConfigResult(cflags=(), libs=("-lb",))),
            )
        )
        ts = stage_pkg_config_flags(inputs, TokenState())
        assert ts.ld == ("-la", "-lb")


class TestStageUnify:
    def test_unifies_cpp_and_cxx_with_dedup(self):
        ts = stage_unify(_inputs(), TokenState(cpp=("-DX", "-O2"), cxx=("-O2", "-Wall")))
        assert ts.cpp == ts.cxx == ("-DX", "-O2", "-Wall")

    def test_separate_mode_is_identity(self):
        ts_in = TokenState(cpp=("-DX",), cxx=("-O2",))
        assert stage_unify(_inputs(separate_flags=True), ts_in) is ts_in

    def test_idempotent(self):
        once = stage_unify(_inputs(), TokenState(cpp=("-DX",), cxx=("-O2",)))
        assert stage_unify(_inputs(), once) == once


class TestStagePrefixMap:
    def test_injects_into_cxx_and_c_when_gitroot_known(self):
        inputs = _inputs(gitroot="/repo")
        ts = stage_prefix_map(inputs, TokenState(cxx=("-O2",), c=("-O1",)))
        assert ts.cxx == ("-O2", "-ffile-prefix-map=/repo=.")
        assert ts.c == ("-O1", "-ffile-prefix-map=/repo=.")
        assert ts.cpp == ()

    def test_no_gitroot_is_identity(self):
        ts_in = TokenState(cxx=("-O2",))
        assert stage_prefix_map(_inputs(), ts_in) is ts_in

    def test_user_prefix_map_skips_that_slot_only(self):
        inputs = _inputs(gitroot="/repo")
        ts = stage_prefix_map(inputs, TokenState(cxx=("-fdebug-prefix-map=/a=b",), c=()))
        assert ts.cxx == ("-fdebug-prefix-map=/a=b",)
        assert ts.c == ("-ffile-prefix-map=/repo=.",)


class TestStageWildLinker:
    def test_clang_driver_rewrites_fuse_ld_wild(self):
        inputs = _inputs(link_driver_is_clang=True)
        ts, effects = stage_wild_linker(inputs, TokenState(ld=("-fuse-ld=wild", "-lm")))
        assert ts.ld == ("--ld-path=wild", "-lm")
        assert effects == ()

    def test_gcc_driver_keeps_fuse_ld_wild(self):
        ts, effects = stage_wild_linker(_inputs(), TokenState(ld=("-fuse-ld=wild",)))
        assert ts.ld == ("-fuse-ld=wild",)
        assert effects == ()

    def test_wild_b_appends_bdir_and_emits_effect(self):
        inputs = _inputs(wild_b_selected=True, gitroot="/repo")
        ts, effects = stage_wild_linker(inputs, TokenState())
        assert ts.ld == ("-B/repo/.ct-wild-ld",)
        assert effects == (EnsureLinkerSymlinkDir(directory="/repo/.ct-wild-ld", link_name="ld", target="wild"),)

    def test_no_wild_selection_is_identity(self):
        ts_in = TokenState(ld=("-lm",))
        ts, effects = stage_wild_linker(_inputs(), ts_in)
        assert ts is ts_in and effects == ()


ORDER = ("gcc", "clang", "debug", "release")


class TestStageDedup:
    def test_all_slots_deduped(self):
        ts = stage_dedup(
            _inputs(),
            TokenState(cpp=("-DX", "-DX"), c=("-I/a", "-I", "/a"), cxx=("-O2",), ld=("-lm", "-lm")),
        )
        assert ts.cpp == ("-DX",)
        assert ts.c == ("-I/a",)
        assert ts.ld == ("-lm",)


class TestStageResolveNames:
    def test_variant_canonicalized_and_deduped(self):
        ns = stage_resolve_names(_inputs(variant_raw="debug,gcc,gcc", canonical_order=ORDER))
        assert ns.variant == "gcc.debug"

    def test_bindir_defaults_to_bin_variant(self):
        ns = stage_resolve_names(_inputs(variant_raw="gcc.debug", canonical_order=ORDER))
        assert ns.bindir == "bin/gcc.debug"

    def test_explicit_bindir_is_normalized(self):
        ns = stage_resolve_names(_inputs(variant_raw="gcc.debug", canonical_order=ORDER, bindir_raw="./out//x/./y"))
        assert ns.bindir == "out/x/y"

    def test_cas_dir_gets_variant_suffix_once(self):
        inputs = _inputs(variant_raw="gcc.debug", canonical_order=ORDER, cas_objdir_raw="/cas/obj")
        ns = stage_resolve_names(inputs)
        assert ns.cas_objdir == "/cas/obj/gcc.debug"
        again = stage_resolve_names(
            _inputs(variant_raw="gcc.debug", canonical_order=ORDER, cas_objdir_raw=ns.cas_objdir)
        )
        assert again.cas_objdir == "/cas/obj/gcc.debug"

    def test_empty_cas_objdir_raw_passes_through_unchanged(self):
        ns = stage_resolve_names(_inputs(variant_raw="gcc.debug", canonical_order=ORDER, cas_objdir_raw=""))
        assert ns.cas_objdir == ""

    def test_unsupplied_cas_dirs_derive_gitroot_anchored_defaults(self):
        """None raws derive <gitroot>/cas-<kind>dir/<variant>, mirroring
        resolve_cas_directory_arguments (Task 14 differential fix)."""
        ns = stage_resolve_names(_inputs(variant_raw="gcc.debug", canonical_order=ORDER, gitroot="/repo"))
        assert ns.cas_objdir == "/repo/cas-objdir/gcc.debug"
        assert ns.cas_pchdir == "/repo/cas-pchdir/gcc.debug"
        assert ns.cas_pcmdir == "/repo/cas-pcmdir/gcc.debug"
        assert ns.cas_exedir == "/repo/cas-exedir/gcc.debug"

    def test_empty_bindir_raw_passes_through_unchanged(self):
        ns = stage_resolve_names(_inputs(variant_raw="gcc.debug", canonical_order=ORDER, bindir_raw=""))
        assert ns.bindir == ""
