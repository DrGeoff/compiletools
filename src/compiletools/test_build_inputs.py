"""gather_inputs: the impure boundary. Tests use the real parser and a
temp conf, mirroring _parseargs_in_temp_repo's setup, but stop at the
parsed-namespace stage (no build-state computation)."""

import os
from types import SimpleNamespace

import pytest

import compiletools.apptools as apptools
import compiletools.apptools_pkgconfig
import compiletools.hunter
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.apptools_pkgconfig import compute_pkg_config_path
from compiletools.build_context import BuildContext
from compiletools.build_inputs import _query_pkg_config, gather_inputs


@pytest.fixture(autouse=True)
def _clear_pkg_config_cache():
    compiletools.apptools_pkgconfig.clear_cache()
    yield
    compiletools.apptools_pkgconfig.clear_cache()


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

    def test_registered_slots_hasattr_is_authoritative_after_populate(self):
        """4d4cfd6d bug class, closed structurally: populate_args never
        materializes slot attrs, so hasattr IS the CAP registration on
        every namespace shape — an unregistered LDFLAGS stays absent
        through populate and re-gather."""
        from compiletools.build_apply import populate_args
        from compiletools.build_state import BuildInputs, compute_build_state

        with uth.TempDirContext():
            args = _minimal_args()
            state = compute_build_state(BuildInputs(registered_slots=frozenset({"CFLAGS", "CXXFLAGS"})))
            populate_args(args, state)
            assert not hasattr(args, "LDFLAGS")
            inputs = gather_inputs(args, BuildContext())
            assert "LDFLAGS" not in inputs.registered_slots
            assert "CXXFLAGS" in inputs.registered_slots

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


@pytest.mark.usefixtures("parsers_reset")
class TestRawSlotsAreGatherInput:
    """The raw slot attrs are gather's alone: populate_args never writes
    them, so the live attr is the pre-parse raw value on every pass and
    a re-gather rebuilds from the same base by construction."""

    def test_regather_after_populate_reads_the_same_raw_base(self):
        """Populate with a state carrying derived tokens, then re-gather:
        the inputs must reflect the untouched raw attr, not the state."""
        import dataclasses

        from compiletools.build_apply import populate_args
        from compiletools.build_state import compute_build_state

        with uth.TempDirContext():
            args = _minimal_args(CXXFLAGS="-DRAW")
            first = gather_inputs(args, BuildContext())
            populate_args(args, compute_build_state(dataclasses.replace(first, cxxflags=("-DDERIVED",))))
            second = gather_inputs(args, BuildContext())
            assert second.cxxflags == first.cxxflags == ("-DRAW",)

    def test_live_sentinel_flows_through_sentinel_mapping(self):
        """Slot attrs can hold the unsupplied sentinels (the parse-time
        default); they must map to unsupplied on every pass."""
        with uth.TempDirContext():
            args = _minimal_args(CPPFLAGS=apptools._UNSUPPLIED_USE_CXXFLAGS)
            inputs = gather_inputs(args, BuildContext())
            assert inputs.cppflags is None

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

    def test_latched_namespace_skips_the_quiet_decrement(self):
        """parseargs folds --quiet into args.verbose exactly once and sets
        _quiet_applied; a re-gather over that namespace must read the
        already-decremented args.verbose as-is, not subtract quiet again."""
        with uth.TempDirContext():
            args = _minimal_args(verbose=-2, quiet=2, _quiet_applied=True)
            inputs = gather_inputs(args, BuildContext())
            assert inputs.verbose == -2


@pytest.mark.usefixtures("parsers_reset")
class TestFlagTokenizeAttribution:
    """An unbalanced quote in a flag-slot value must surface as an
    attributed FlagTokenizeError, not a bare shlex ValueError escaping
    gather_inputs (coverage-gaps Task 1)."""

    @pytest.mark.parametrize("slot", ["CPPFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"])
    def test_unbalanced_quote_in_a_slot_is_attributed_to_that_slot(self, slot):
        with uth.TempDirContext():
            args = _minimal_args(**{slot: '-DFOO="bar'})
            with pytest.raises(compiletools.utils.FlagTokenizeError, match=slot):
                gather_inputs(args, BuildContext())

    def test_error_names_the_offending_value_verbatim(self):
        with uth.TempDirContext():
            args = _minimal_args(CXXFLAGS='-DFOO="bar')
            with pytest.raises(compiletools.utils.FlagTokenizeError) as excinfo:
                gather_inputs(args, BuildContext())
            assert '-DFOO="bar' in str(excinfo.value)

    def test_prepend_variant_is_attributed_as_prepend_slot(self):
        with uth.TempDirContext():
            args = _minimal_args(prepend_cxxflags=['-DFOO="bar'])
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="prepend-CXXFLAGS"):
                gather_inputs(args, BuildContext())

    def test_append_variant_is_attributed_as_append_slot(self):
        with uth.TempDirContext():
            args = _minimal_args(append_ldflags=['-Wl,"bad'])
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="append-LDFLAGS"):
                gather_inputs(args, BuildContext())

    def test_well_formed_prepend_and_append_values_are_unaffected(self):
        """Control: the new attribution path must not reject valid input."""
        with uth.TempDirContext():
            args = _minimal_args(prepend_cxxflags=["-DOK=1"], append_ldflags=["-lm"])
            inputs = gather_inputs(args, BuildContext())
            assert inputs.prepend_cxxflags == ("-DOK=1",)
            assert inputs.append_ldflags == ("-lm",)


@pytest.mark.usefixtures("parsers_reset")
class TestTask9IncludeAndProjectMacroTokenizeAttribution:
    """coverage-gaps Task 9: INCLUDE and project-version/name-cmd used to
    silently degrade a malformed quote (whitespace .split() / str.split())
    instead of raising. Mutation guard: reverting
    _include_paths_with_gitroots' tokenize_flags_or_raise calls back to a
    plain " ".join(...).split() makes every test in this class fail --
    the malformed-quote tests stop raising, and
    test_quoted_space_containing_include_path_survives_as_one_path starts
    failing because the quoted path shreds again.
    """

    def test_include_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(INCLUDE='/opt/"unterminated')
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="INCLUDE"):
                gather_inputs(args, BuildContext())

    def test_prepend_include_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(prepend_include=['/opt/"unterminated'])
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="prepend-INCLUDE"):
                gather_inputs(args, BuildContext())

    def test_append_include_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(append_include=['/opt/"unterminated'])
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="append-INCLUDE"):
                gather_inputs(args, BuildContext())

    def test_quoted_space_containing_include_path_survives_as_one_path(self):
        """Behavior improvement: a quoted --INCLUDE path with a space now
        parses as ONE path instead of shredding into fragments with
        literal quote characters (the old plain .split() behavior)."""
        with uth.TempDirContext():
            args = _minimal_args(INCLUDE='"/opt/has space/include" /opt/plain')
            inputs = gather_inputs(args, BuildContext())
            assert "/opt/has space/include" in inputs.include_paths
            assert "/opt/plain" in inputs.include_paths
            assert not any('"' in p for p in inputs.include_paths)

    def test_project_version_cmd_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(projectversioncmd='echo "unterminated')
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="project-version-cmd"):
                gather_inputs(args, BuildContext())

    def test_project_name_cmd_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(projectnamecmd='echo "unterminated')
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="project-name-cmd"):
                gather_inputs(args, BuildContext())

    def test_pkg_config_cli_spec_with_unbalanced_quote_is_attributed(self):
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=['zlib "unterminated'])
            with pytest.raises(compiletools.utils.FlagTokenizeError, match="pkg-config"):
                gather_inputs(args, BuildContext())


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

    def test_arming_strict_mode_reprobes_a_memoized_warn_result(self, monkeypatch):
        """The per-context memo must not outlive the policy it was filled
        under. ``set_pkg_config_errors`` clears the module-level memos in
        ``apptools_pkgconfig``, but it cannot reach this one, so a warn-mode
        empty result served after strict mode is armed bypasses enforcement
        entirely.
        """
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"])
            context = BuildContext()
            gather_inputs(args, context)
            assert len(calls) == 1

            compiletools.apptools_pkgconfig.set_pkg_config_errors("error")
            gather_inputs(args, context)
            assert len(calls) == 2

    def test_a_libs_needing_gather_is_not_served_the_libs_free_memo(self, monkeypatch):
        """want_libs decides whether the memo entry carries libs at all.
        A namespace with no LDFLAGS slot queries --cflags only and stores
        libs=(); without want_libs in the key that empty tuple is then
        served to a later gather over the same context that DOES register
        LDFLAGS, so the package's link flags never reach the build.
        """
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {"foo": "-lfoo"}, calls),
        )
        with uth.TempDirContext():
            context = BuildContext()
            narrow = gather_inputs(_minimal_args(pkg_config=["foo"]), context)
            assert dict(narrow.pkg_config_results)["foo"].libs == (), "Precondition: no LDFLAGS slot, no libs query."

            widened = gather_inputs(_minimal_args(pkg_config=["foo"], LDFLAGS=""), context)
            assert dict(widened.pkg_config_results)["foo"].libs == ("-lfoo",)

    def test_malformed_libs_output_degrades_with_warning_at_verbose_1(self, monkeypatch, capsys):
        """coverage-gaps Task 10: raw --libs pkg-config subprocess output is
        never re-quoted by filter_pkg_config_cflags (unlike --cflags), so it
        used to reach split_command_cached directly with no try/except -- an
        unbalanced quote in a broken .pc file raised a bare ValueError and
        killed the build. It must instead degrade to a whitespace split
        with a verbose>=1 warning naming the package."""
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {"foo": '-lfoo "unterminated'}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"], LDFLAGS="", verbose=1)
            inputs = gather_inputs(args, BuildContext())
            libs = dict(inputs.pkg_config_results)["foo"].libs
            assert libs == ("-lfoo", '"unterminated')
            error_output = capsys.readouterr().err
            assert "foo" in error_output

    def test_malformed_libs_output_is_silent_at_verbose_0(self, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(
            compiletools.apptools_pkgconfig,
            "_batch_pkg_config",
            self._fake_batch({"foo": "-I/opt/x/include"}, {"foo": '-lfoo "unterminated'}, calls),
        )
        with uth.TempDirContext():
            args = _minimal_args(pkg_config=["foo"], LDFLAGS="", verbose=0)
            inputs = gather_inputs(args, BuildContext())
            libs = dict(inputs.pkg_config_results)["foo"].libs
            assert libs == ("-lfoo", '"unterminated'), "Must still degrade even when silent."
            error_output = capsys.readouterr().err
            assert error_output == ""

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


class TestQueryPkgConfigEnvRestore:
    """_query_pkg_config's set/restore dance around ``_batch_pkg_config``'s
    global-environment read, exercised through the REAL ``_batch_pkg_config``
    (real pkg-config subprocess, real .pc files from the pkgconfig_env
    fixture) rather than a monkeypatched stand-in. Every other pkg-config
    test in this module mocks ``_batch_pkg_config`` -- the set/restore
    dance around a mock never runs the real global-env read it guards
    against, so it is untested by everything else here."""

    def test_restores_a_previously_set_value(self, pkgconfig_env, monkeypatch):
        monkeypatch.setenv("PKG_CONFIG_PATH", "/prior/pkg/config/path")
        _query_pkg_config(["nested"], pkgconfig_env, want_libs=False, verbose=0, context=BuildContext())
        assert os.environ["PKG_CONFIG_PATH"] == "/prior/pkg/config/path"

    def test_restores_to_unset_when_originally_unset(self, pkgconfig_env, monkeypatch):
        monkeypatch.delenv("PKG_CONFIG_PATH", raising=False)
        _query_pkg_config(["nested"], pkgconfig_env, want_libs=False, verbose=0, context=BuildContext())
        assert "PKG_CONFIG_PATH" not in os.environ


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

    def test_explicit_value_beats_cmd_for_both_macros(self):
        """_project_macro_value's `if not value and cmd:` branch: the
        explicit value must win over the *cmd -- and win without the cmd
        ever running. Each cmd is a script that both echoes a distinct
        wrong value AND touches a sentinel file, so a precedence flip is
        caught two ways: the wrong value would surface, and the sentinel
        would prove the cmd was invoked at all."""
        with uth.TempDirContext():
            version_sentinel = os.path.join(os.getcwd(), "version-cmd-ran")
            name_sentinel = os.path.join(os.getcwd(), "name-cmd-ran")
            version_script = os.path.join(os.getcwd(), "version_cmd.sh")
            name_script = os.path.join(os.getcwd(), "name_cmd.sh")
            with open(version_script, "w") as f:
                f.write(f"#!/bin/sh\ntouch {version_sentinel}\necho 9.9.9\n")
            with open(name_script, "w") as f:
                f.write(f"#!/bin/sh\ntouch {name_sentinel}\necho otherapp\n")
            os.chmod(version_script, 0o755)
            os.chmod(name_script, 0o755)

            args = _minimal_args(
                projectversion="1.2.3",
                projectversioncmd=f"sh {version_script}",
                projectname="myapp",
                projectnamecmd=f"sh {name_script}",
            )
            inputs = gather_inputs(args, BuildContext())
            assert inputs.project_version == "1.2.3"
            assert inputs.project_name == "myapp"
            assert not os.path.exists(version_sentinel), "projectversioncmd must not run when projectversion is set"
            assert not os.path.exists(name_sentinel), "projectnamecmd must not run when projectname is set"


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

    def test_higher_priority_source_wins_within_append_group(self):
        """append_paths arrive in the same [low conf, ..., high conf, CLI]
        order as prepend_paths, and the documented reversal in
        _merged_pkg_config_path_entries is symmetric for append: the
        highest-priority source still lands leftmost, this time within the
        appended tail. Only single-element append lists were previously
        exercised (test_append_forces_existing_entry_to_end), so the
        within-group ordering for a multi-entry append list had zero
        coverage."""
        result = compute_pkg_config_path("", None, ["/low", "/high", "/cli"], [], [])
        assert result == os.pathsep.join(["/cli", "/high", "/low"])

    def test_duplicate_candidate_entries_are_deduplicated(self):
        result = compute_pkg_config_path("", ["/x"], None, ["/x"], ["/x"])
        assert result == "/x"
