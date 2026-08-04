"""gather_inputs: the impure boundary. Tests use the real parser and a
temp conf, mirroring _parseargs_in_temp_repo's setup, but stop at the
parsed-namespace stage (no substitutions())."""

import os
from types import SimpleNamespace

import pytest

import compiletools.apptools as apptools
import compiletools.apptools_pkgconfig
import compiletools.hunter
import compiletools.testhelper as uth
from compiletools.apptools_pkgconfig import compute_pkg_config_path
from compiletools.build_context import BuildContext
from compiletools.build_inputs import gather_inputs


@pytest.fixture
def parsers_reset():
    """Wipe the configargparse parser cache around tests that go through
    the parser end-to-end."""
    uth.reset()
    yield
    uth.reset()


def _parsed_args(extra_argv=(), register_link_args=True):
    uth.create_temp_ct_conf(os.getcwd())
    with uth.TempConfigContext(tempdir=os.getcwd()) as temp_config_name:
        argv = ["--config=" + temp_config_name, *extra_argv]
        cap = apptools.create_parser("gather test", argv=argv)
        compiletools.hunter.add_arguments(cap)
        if register_link_args:
            apptools.add_link_arguments(cap)
        with uth.ParserContext():
            args = cap.parse_args(args=argv)
            apptools._flatten_variables(args)
            apptools._strip_quotes(args)
            return args


def _minimal_args(**overrides):
    """A post-parse-shaped namespace with only the attrs gather_inputs
    reads unconditionally. hasattr-driven fields stay absent unless a
    test supplies them."""
    ns = SimpleNamespace(
        verbose=0,
        quiet=0,
        variant="gcc.debug",
        CXXFLAGS="-fPIC -g -Wall",
        CFLAGS="-fPIC -g -Wall",
        INCLUDE="",
        pkg_config=[],
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class TestGatherInputs:
    def test_registered_slots_reflect_cap(self, parsers_reset):
        with uth.TempDirContext():
            args = _parsed_args(register_link_args=False)
            inputs = gather_inputs(args, BuildContext())
            assert "LDFLAGS" not in inputs.registered_slots
            assert "CXXFLAGS" in inputs.registered_slots

    def test_registered_slots_prefer_sticky_tuple_over_post_finalize_hasattr(self):
        """4d4cfd6d bug class: _finalize_flag_state materializes
        args.LDFLAGS = "" for downstream consumers even when the CAP never
        registered LDFLAGS, so bare hasattr disagrees with the CAP's real
        registration on a post-finalize namespace. _registered_flag_slots
        is the sticky, authoritative record and must win over hasattr."""
        with uth.TempDirContext():
            args = _minimal_args(
                _registered_flag_slots=("CPPFLAGS", "CFLAGS", "CXXFLAGS"),
                LDFLAGS="",
            )
            inputs = gather_inputs(args, BuildContext())
            assert inputs.registered_slots == frozenset({"CPPFLAGS", "CFLAGS", "CXXFLAGS"})

    def test_gitroot_and_identity_are_gathered(self, parsers_reset):
        with uth.TempDirContext():
            args = _parsed_args()
            inputs = gather_inputs(args, BuildContext())
            assert inputs.gitroot == os.getcwd()
            assert inputs.compiler_identity

    def test_pkg_config_results_gathered_per_package(self, parsers_reset, pkgconfig_env):
        with uth.TempDirContext():
            args = _parsed_args(extra_argv=["--pkg-config=nested"])
            inputs = gather_inputs(args, BuildContext())
            packages = dict(inputs.pkg_config_results)
            assert "nested" in packages or any("testpkg" in " ".join(r.cflags) for r in packages.values())

    def test_gather_twice_is_equal(self, parsers_reset, pkgconfig_env):
        with uth.TempDirContext():
            args = _parsed_args(extra_argv=["--pkg-config=nested"])
            context = BuildContext()
            assert gather_inputs(args, context) == gather_inputs(args, context)


@pytest.mark.usefixtures("parsers_reset")
class TestSlotSentinelMapping:
    """unsupplied-vs-empty modeling: cppflags/ldflags map sentinels to
    None (defer to the pure stage's CXX fallback), explicit empty string
    to (); cflags/cxxflags map sentinels to () (no fallback)."""

    @pytest.mark.parametrize(
        ("slot", "raw", "field", "expected"),
        [
            ("CPPFLAGS", apptools._UNSUPPLIED_USE_CXXFLAGS, "cppflags", None),
            ("CPPFLAGS", "unsupplied", "cppflags", None),
            ("CPPFLAGS", "", "cppflags", ()),
            ("CPPFLAGS", "-DX -DY", "cppflags", ("-DX", "-DY")),
            ("LDFLAGS", apptools._UNSUPPLIED_USE_CXXFLAGS, "ldflags", None),
            ("LDFLAGS", "", "ldflags", ()),
            ("LDFLAGS", "-lm", "ldflags", ("-lm",)),
            ("CFLAGS", "unsupplied", "cflags", ()),
            ("CXXFLAGS", "unsupplied", "cxxflags", ()),
        ],
    )
    def test_sentinel_and_empty_mapping(self, slot, raw, field, expected):
        with uth.TempDirContext():
            args = _minimal_args(**{slot: raw})
            inputs = gather_inputs(args, BuildContext())
            assert getattr(inputs, field) == expected
            assert slot in inputs.registered_slots

    def test_absent_slots_map_to_none_and_are_unregistered(self):
        with uth.TempDirContext():
            args = _minimal_args()
            inputs = gather_inputs(args, BuildContext())
            assert inputs.cppflags is None
            assert inputs.ldflags is None
            assert inputs.registered_slots == frozenset({"CFLAGS", "CXXFLAGS"})

    def test_verbose_is_quiet_adjusted_without_clamping(self):
        with uth.TempDirContext():
            args = _minimal_args(verbose=0, quiet=2)
            inputs = gather_inputs(args, BuildContext())
            assert inputs.verbose == -2


@pytest.mark.usefixtures("parsers_reset")
class TestPkgConfigGathering:
    def _fake_batch(self, cflags_by_pkg, libs_by_pkg, calls):
        def fake(packages, option):
            calls.append((tuple(packages), option))
            table = cflags_by_pkg if option == "--cflags" else libs_by_pkg
            return {pkg: table.get(pkg, "") for pkg in packages}

        return fake

    def test_cflags_are_filtered_before_reaching_inputs(self, monkeypatch):
        """Carry-forward: filter_pkg_config_cflags runs in gather -- -I
        becomes -isystem, default system include paths are dropped."""
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/usr/include -I/opt/x/include -DBAR"}, {"foo": "-lfoo"}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"], LDFLAGS="")
            inputs = gather_inputs(args, BuildContext())
            packages = dict(inputs.pkg_config_results)
            assert packages["foo"].cflags == ("-isystem", "/opt/x/include", "-DBAR")
            assert packages["foo"].libs == ("-lfoo",)

    def test_libs_not_queried_when_ldflags_unregistered(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {"foo": "-lfoo"}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"])
            inputs = gather_inputs(args, BuildContext())
            assert dict(inputs.pkg_config_results)["foo"].libs == ()
            assert all(option != "--libs" for _pkgs, option in calls)

    def test_queries_are_memoized_on_context(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"])
            context = BuildContext()
            first = gather_inputs(args, context)

            def explode(packages, option):
                raise AssertionError("memoized query must not re-run pkg-config")

            monkeypatch.setattr(compiletools.apptools_pkgconfig, "_batch_pkg_config", explode)
            second = gather_inputs(args, context)
            assert first.pkg_config_results == second.pkg_config_results

    def test_prepend_and_append_pkg_config_merge_in_declaration_order(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({}, {}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(
                pkg_config=["base"],
                prepend_pkg_config=["first"],
                append_pkg_config=["last base"],
            )
            inputs = gather_inputs(args, BuildContext())
            assert tuple(pkg for pkg, _r in inputs.pkg_config_results) == ("first", "base", "last")


@pytest.mark.usefixtures("parsers_reset")
class TestProjectMacros:
    def test_value_is_escaped_in_gather(self):
        with uth.TempDirContext():
            args = _minimal_args(projectversion='v"1\\', projectname="myapp")
            inputs = gather_inputs(args, BuildContext())
            assert inputs.project_version == 'v\\"1\\\\'
            assert inputs.project_name == "myapp"

    def test_injection_suppressed_when_flag_name_already_in_a_slot(self):
        """Carry-forward: the original suppresses injection on a substring
        match of the flag NAME, not an exact token; gather replicates by
        nulling the field."""
        with uth.TempDirContext():
            args = _minimal_args(
                projectversion="1.2.3",
                projectname="myapp",
                CXXFLAGS='-fPIC -DCT_PROJECT_VERSION="9.9" -DCT_PROJECT_NAME="other"',
            )
            inputs = gather_inputs(args, BuildContext())
            assert inputs.project_version is None
            assert inputs.project_name is None

    def test_projectversioncmd_takes_first_word_of_output(self):
        with uth.TempDirContext():
            args = _minimal_args(projectversioncmd="echo 9.9.9 trailing")
            inputs = gather_inputs(args, BuildContext())
            assert inputs.project_version == "9.9.9"

    def test_no_opt_in_yields_none(self):
        with uth.TempDirContext():
            inputs = gather_inputs(_minimal_args(), BuildContext())
            assert inputs.project_version is None
            assert inputs.project_name is None

    def test_deprecation_warning_fires_once_per_context(self, capsys):
        """The project-macro deprecation warning is re-homed to gather
        (inventory row 14): fires on the raw opt-in even when D1
        suppression later nulls the value, and the context latch makes a
        second gather silent."""
        with uth.TempDirContext():
            context = BuildContext()
            args = _minimal_args(
                projectversion="1.2.3",
                CXXFLAGS='-fPIC -DCT_PROJECT_VERSION="9.9"',
            )
            inputs = gather_inputs(args, context)
            assert inputs.project_version is None, "Precondition: D1 suppression must fire."
            assert "DEPRECATED" in capsys.readouterr().err
            gather_inputs(args, context)
            assert "DEPRECATED" not in capsys.readouterr().err

    def test_no_deprecation_warning_without_opt_in(self, capsys):
        with uth.TempDirContext():
            gather_inputs(_minimal_args(), BuildContext())
            assert "DEPRECATED" not in capsys.readouterr().err


class TestIncludePathsGathering:
    """include_paths must model the two old-pipeline INCLUDE-widening
    steps: --prepend/--append-INCLUDE xxpend merging, and the gitroot
    extension for target-registering CAPs (Task 14 differential fix)."""

    def test_append_and_prepend_include_merge_into_paths(self):
        with uth.TempDirContext():
            args = _minimal_args(
                INCLUDE="/base/inc",
                prepend_include=["/pre/inc"],
                append_include=["/app/inc"],
            )
            inputs = gather_inputs(args, BuildContext())
            assert inputs.include_paths == ("/pre/inc", "/base/inc", "/app/inc")

    def test_xxpend_include_skips_elements_already_present(self):
        with uth.TempDirContext():
            args = _minimal_args(
                INCLUDE="/base/inc",
                prepend_include=["/base/inc"],
                append_include=["/base/inc"],
            )
            inputs = gather_inputs(args, BuildContext())
            assert inputs.include_paths == ("/base/inc",)

    def test_gitroot_extends_include_paths_for_target_registering_cap(self):
        """The four target attrs (filename/static/dynamic/tests) mark a
        cake-shaped CAP; --git-root then folds the cwd gitroot in, exactly
        like _extend_includes_using_git_root."""
        with uth.TempDirContext():
            args = _minimal_args(git_root=True, filename=[])
            inputs = gather_inputs(args, BuildContext())
            assert inputs.include_paths == (os.getcwd(),)

    def test_no_target_attrs_means_no_gitroot_extension(self):
        with uth.TempDirContext():
            args = _minimal_args(git_root=True)
            inputs = gather_inputs(args, BuildContext())
            assert inputs.include_paths == ()

    def test_gitroot_already_in_include_is_not_duplicated(self):
        with uth.TempDirContext():
            args = _minimal_args(git_root=True, filename=[], INCLUDE=os.getcwd())
            inputs = gather_inputs(args, BuildContext())
            assert inputs.include_paths == (os.getcwd(),)


class TestComputePkgConfigPath:
    """Pure merge extracted from _setup_pkg_config_overrides_locked."""

    def test_empty_inputs_produce_none(self):
        assert compute_pkg_config_path("", None, None, [], []) is None

    def test_existing_only_round_trips(self):
        existing = os.pathsep.join(["/a", "/b"])
        assert compute_pkg_config_path(existing, None, None, [], []) == existing

    def test_prepend_promotes_existing_entry_to_front(self):
        existing = os.pathsep.join(["/a", "/b"])
        result = compute_pkg_config_path(existing, ["/b"], None, [], [])
        assert result == os.pathsep.join(["/b", "/a"])

    def test_append_forces_existing_entry_to_end(self):
        existing = os.pathsep.join(["/a", "/b"])
        result = compute_pkg_config_path(existing, None, ["/a"], [], [])
        assert result == os.pathsep.join(["/b", "/a"])

    def test_priority_order_prepend_candidates_existing_append(self):
        result = compute_pkg_config_path("/mid", ["/pre"], ["/post"], ["/cwd"], ["/root"])
        assert result == os.pathsep.join(["/pre", "/cwd", "/root", "/mid", "/post"])

    def test_higher_priority_source_wins_within_prepend_group(self):
        """prepend_paths arrive [low conf, ..., high conf, CLI]; the merge
        reverses so the highest-priority source lands leftmost."""
        result = compute_pkg_config_path("", ["/low", "/high", "/cli"], None, [], [])
        assert result == os.pathsep.join(["/cli", "/high", "/low"])

    def test_duplicate_candidate_entries_are_deduplicated(self):
        result = compute_pkg_config_path("", ["/x"], None, ["/x"], ["/x"])
        assert result == "/x"
