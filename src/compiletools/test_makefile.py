import io
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import compiletools.makefile
import compiletools.testhelper as uth
import compiletools.utils
from compiletools.makefile import Rule


class TestRule:
    """Test Rule class directly."""

    def test_rule_write_basic(self):
        rule = Rule(target="foo.o", prerequisites="foo.cpp foo.h", recipe="g++ -c foo.cpp -o foo.o")
        buf = io.StringIO()
        rule.write(buf)
        output = buf.getvalue()
        assert "foo.o: foo.cpp foo.h" in output
        assert "g++ -c foo.cpp -o foo.o" in output

    def test_rule_write_phony(self):
        rule = Rule(target="clean", prerequisites="", recipe="rm -f *.o", phony=True)
        buf = io.StringIO()
        rule.write(buf)
        output = buf.getvalue()
        assert ".PHONY:" in output
        assert "clean" in output

    def test_rule_write_no_recipe(self):
        rule = Rule(target="all", prerequisites="foo bar")
        buf = io.StringIO()
        rule.write(buf)
        output = buf.getvalue()
        assert "all: foo bar" in output

    def test_rule_write_order_only_prereqs(self):
        rule = Rule(target="foo.o", prerequisites="foo.cpp", order_only_prerequisites="bin/", recipe="g++ -c foo.cpp")
        buf = io.StringIO()
        rule.write(buf)
        output = buf.getvalue()
        assert "| bin/" in output

    def test_rule_equality_and_hash(self):
        r1 = Rule(target="foo.o", prerequisites="foo.cpp")
        r2 = Rule(target="foo.o", prerequisites="bar.cpp")
        assert r1 == r2
        assert hash(r1) == hash(r2)

    def test_rule_repr(self):
        """Cover Rule.__repr__ (line 45)."""
        r = Rule(target="t", prerequisites="p")
        result = repr(r)
        assert "Rule" in result
        assert "'t'" in result

    def test_rule_str(self):
        """Cover Rule.__str__ (line 48)."""
        r = Rule(target="t", prerequisites="p")
        result = str(r)
        assert "'t'" in result
        assert "'p'" in result


def _make_args(**overrides):
    """Create a minimal args namespace for MakefileCreator unit tests."""
    defaults = dict(
        file_locking=False,
        verbose=0,
        objdir="/tmp/test_obj",
        filename=[],
        tests=[],
        static=[],
        dynamic=[],
        makefilename="Makefile",
        build_only_changed=None,
        serialisetests=False,
        TESTPREFIX="",
        CC="gcc",
        CXX="g++",
        CFLAGS="-O2",
        CXXFLAGS="-O2",
        LD="g++",
        LDFLAGS="",
        sleep_interval_lockdir=None,
        sleep_interval_cifs=0.01,
        sleep_interval_flock_fallback=0.01,
        lock_warn_interval=30,
        lock_cross_host_timeout=300,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestMakefileCreatorUnit:
    """Unit tests for MakefileCreator methods without needing a full build."""

    def test_detect_os_type_linux(self):
        """Cover _detect_os_type linux branch (line 302)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            result = mc._detect_os_type()
            # Running on linux, should return "linux"
            assert result == "linux"

    def test_detect_os_type_darwin(self):
        """Cover _detect_os_type darwin/bsd branch (lines 303-304)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            with patch("platform.system", return_value="Darwin"):
                result = mc._detect_os_type()
                assert result == "bsd"

    def test_detect_os_type_unknown(self):
        """Cover _detect_os_type default branch (lines 305-307)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            with patch("platform.system", return_value="Windows"):
                result = mc._detect_os_type()
                assert result == "linux"

    def test_validate_umask_warning(self, capsys):
        """Cover _validate_umask_for_file_locking warning (line 318)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=True, verbose=1)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            # Set restrictive umask to trigger warning
            old_umask = os.umask(0o077)
            try:
                mc._validate_umask_for_file_locking()
            finally:
                os.umask(old_umask)
            captured = capsys.readouterr()
            assert "restrictive umask" in captured.err

    def test_validate_umask_no_warning_permissive(self, capsys):
        """No warning with permissive umask."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=True, verbose=1)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            old_umask = os.umask(0o002)
            try:
                mc._validate_umask_for_file_locking()
            finally:
                os.umask(old_umask)
            captured = capsys.readouterr()
            assert "restrictive umask" not in captured.err

    def test_wrap_compile_without_file_locking(self):
        """Cover _wrap_compile_with_lock non-shared path (line 391)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=False)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            result = mc._wrap_compile_with_lock("gcc -c foo.c", "$@")
            assert result == "gcc -c foo.c -o $@"

    def test_wrap_compile_with_lockdir_strategy(self):
        """Cover _wrap_compile_with_lock lockdir branch (lines 398-400)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=True, sleep_interval_lockdir=0.05)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc._filesystem_type = "nfs"
            result = mc._wrap_compile_with_lock("gcc -c foo.c", "$@")
            assert "ct-lock-helper" in result
            assert "--strategy=lockdir" in result
            assert "CT_LOCK_SLEEP_INTERVAL=0.05" in result

    def test_wrap_compile_with_cifs_strategy(self):
        """Cover _wrap_compile_with_lock cifs branch (lines 401-402)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=True, sleep_interval_cifs=0.02)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc._filesystem_type = "cifs"
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="cifs"):
                result = mc._wrap_compile_with_lock("gcc -c foo.c", "$@")
                assert "--strategy=cifs" in result
                assert "CT_LOCK_SLEEP_INTERVAL_CIFS=0.02" in result

    def test_wrap_compile_with_flock_strategy(self):
        """Cover _wrap_compile_with_lock flock branch (lines 403-404)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(file_locking=True, sleep_interval_flock_fallback=0.03)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc._filesystem_type = "ext4"
            with patch("compiletools.filesystem_utils.get_lock_strategy", return_value="flock"):
                result = mc._wrap_compile_with_lock("gcc -c foo.c", "$@")
                assert "--strategy=flock" in result
                assert "CT_LOCK_SLEEP_INTERVAL_FLOCK=0.03" in result

    def test_get_lockdir_sleep_interval_user_override(self):
        """Cover _get_lockdir_sleep_interval user override (lines 429-430)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(sleep_interval_lockdir=0.99)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc._filesystem_type = "nfs"
            assert mc._get_lockdir_sleep_interval() == 0.99

    def test_get_lockdir_sleep_interval_auto(self):
        """Cover _get_lockdir_sleep_interval auto-detect (lines 432-433)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(sleep_interval_lockdir=None)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc._filesystem_type = "nfs"
            result = mc._get_lockdir_sleep_interval()
            assert isinstance(result, float)

    def test_get_locking_recipe_prefix_and_suffix(self):
        """Cover deprecated _get_locking_recipe_prefix/suffix (lines 417, 438)."""
        from compiletools.makefile import MakefileCreator

        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            assert mc._get_locking_recipe_prefix() == ""
            assert mc._get_locking_recipe_suffix() == ""

    def test_create_all_rule_with_tests(self):
        """Cover _create_all_rule with tests (line 444)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(tests=["test_foo.cpp"])
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            rule = mc._create_all_rule()
            assert "runtests" in rule.prerequisites

    def test_create_all_rule_without_tests(self):
        """Cover _create_all_rule without tests."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(tests=[])
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            rule = mc._create_all_rule()
            assert "runtests" not in rule.prerequisites
            assert "build" in rule.prerequisites

    def test_create_cp_rule_same_dir(self):
        """Cover _create_cp_rule returning None (line 488)."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_dir.return_value = "/some/dir"
            # When output is in same dir, should return None
            result = mc._create_cp_rule("/some/dir/myexe")
            assert result is None

    def test_create_cp_rule_different_dir(self):
        """Cover _create_cp_rule returning a Rule (line 490)."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_dir.return_value = "/exe/dir"
            result = mc._create_cp_rule("/obj/dir/myexe")
            assert result is not None
            assert result.target == "/exe/dir/myexe"
            assert "cp -f" in result.recipe
            assert "2>/dev/null" not in result.recipe
            assert "||true" not in result.recipe

    def test_create_test_rules(self):
        """Cover _create_test_rules (lines 497-519)."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args(TESTPREFIX="valgrind", verbose=1)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_pathname.return_value = "/bin/test_foo"
            rules = mc._create_test_rules(["/src/test_foo.cpp"])
            # Should have runtests phony + one test rule
            assert len(rules) == 2
            targets = [r.target for r in rules]
            assert "runtests" in targets
            # test rule should have valgrind and echo
            test_rule = next(r for r in rules if r.target != "runtests")
            assert "valgrind" in test_rule.recipe
            assert "@echo" in test_rule.recipe

    def test_create_tests_not_parallel_rule(self):
        """Cover _create_tests_not_parallel_rule (line 523)."""
        rule = compiletools.makefile.MakefileCreator._create_tests_not_parallel_rule()
        assert rule.target == ".NOTPARALLEL"
        assert rule.prerequisites == "runtests"

    def test_gather_root_sources(self):
        """Cover _gather_root_sources (lines 529-540)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(
            static=["a.cpp"],
            dynamic=["b.cpp"],
            filename=["c.cpp"],
            tests=["d.cpp"],
        )
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            sources = mc._gather_root_sources()
            assert "a.cpp" in sources
            assert "b.cpp" in sources
            assert "c.cpp" in sources
            assert "d.cpp" in sources

    def test_detect_filesystem_type_verbose(self, capsys):
        """Cover _detect_filesystem_type verbose print (line 293)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(verbose=3)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            with patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"):
                result = mc._detect_filesystem_type()
            assert result == "ext4"
            captured = capsys.readouterr()
            assert "Detected filesystem type: ext4" in captured.out

    def test_ct_lock_helper_missing_exits(self):
        """Cover ct-lock-helper not found error (lines 233-243)."""

        args = _make_args(file_locking=True, verbose=0)
        with (
            patch("shutil.which", return_value=None),
            patch("compiletools.filesystem_utils.get_filesystem_type", return_value="ext4"),
            patch("compiletools.namer.Namer") as MockNamer,
        ):
            MockNamer.return_value = None
            import pytest

            with pytest.raises(RuntimeError):
                compiletools.makefile.MakefileCreator(args, hunter=None)

    def test_link_rule_verbose(self):
        """Cover verbose recipe in _create_link_rule (line 125)."""
        from unittest.mock import MagicMock

        from compiletools.makefile import LinkRuleCreator

        args = _make_args(verbose=1)
        namer = MagicMock()
        namer.object_pathname.return_value = "/obj/foo.o"
        namer.compute_dep_hash.return_value = "abc123"
        hunter = MagicMock()
        hunter.macro_state_hash.return_value = "hash1"
        hunter.header_dependencies.return_value = []
        hunter.magicflags.return_value = {}

        creator = LinkRuleCreator(args, namer, hunter)
        rule = creator._create_link_rule(
            outputname="myexe",
            completesources=["foo.cpp"],
            linker="g++",
        )
        assert "+@echo" in rule.recipe
        assert "myexe" in rule.recipe

    def test_uptodate_no_makefile(self):
        """Cover _uptodate when Makefile doesn't exist (lines 339-340)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(makefilename="/tmp/nonexistent_makefile_xyz", verbose=8)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            assert mc._uptodate() is False

    def test_uptodate_changed_args(self, tmp_path):
        """Cover _uptodate when args changed (lines 344-353)."""
        from compiletools.makefile import MakefileCreator

        makefile_path = str(tmp_path / "Makefile")
        # Write a Makefile with different args
        with open(makefile_path, "w") as f:
            f.write("# Makefile generated by old_args\n")

        args = _make_args(makefilename=makefile_path, verbose=8)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            assert mc._uptodate() is False

    def test_uptodate_matching_args_no_sources(self, tmp_path):
        """Cover _uptodate when args match and no files changed (lines 354-377)."""
        from compiletools.makefile import MakefileCreator

        args = _make_args(makefilename=str(tmp_path / "Makefile"), verbose=10)
        makefile_path = str(tmp_path / "Makefile")
        expected_header = "# Makefile generated by " + str(args)
        with open(makefile_path, "w") as f:
            f.write(expected_header + "\n")

        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.hunter = None
            # No sources to gather, so nothing to check
            with patch.object(mc, "_gather_root_sources", return_value=[]):
                assert mc._uptodate() is True

    def test_uptodate_file_newer(self, tmp_path):
        """Cover _uptodate when a dependency is newer (lines 361-367)."""
        import time
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args(makefilename=str(tmp_path / "Makefile"), verbose=8)
        makefile_path = str(tmp_path / "Makefile")
        expected_header = "# Makefile generated by " + str(args)
        with open(makefile_path, "w") as f:
            f.write(expected_header + "\n")

        # Create a source file that is "newer" than makefile
        src_file = str(tmp_path / "foo.cpp")
        with open(src_file, "w") as f:
            f.write("int main() {}")
        # Touch source to make it newer
        time.sleep(0.01)
        os.utime(src_file, None)

        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.hunter = MagicMock()
            mc.hunter.required_files.return_value = [src_file]
            with patch.object(mc, "_gather_root_sources", return_value=["foo.cpp"]):
                # The source file should be newer
                with patch("compiletools.wrappedos.getmtime") as mock_getmtime:
                    mock_getmtime.side_effect = lambda f: 100.0 if f == makefile_path else 200.0
                    assert mc._uptodate() is False

    def test_uptodate_file_older(self, tmp_path):
        """Cover _uptodate when dependencies are older (lines 368-377)."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args(makefilename=str(tmp_path / "Makefile"), verbose=10)
        makefile_path = str(tmp_path / "Makefile")
        expected_header = "# Makefile generated by " + str(args)
        with open(makefile_path, "w") as f:
            f.write(expected_header + "\n")

        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.hunter = MagicMock()
            mc.hunter.required_files.return_value = ["/some/file.cpp"]
            with patch.object(mc, "_gather_root_sources", return_value=["main.cpp"]):
                with patch("compiletools.wrappedos.getmtime") as mock_getmtime:
                    mock_getmtime.side_effect = lambda f: 200.0 if f == makefile_path else 100.0
                    assert mc._uptodate() is True

    def test_clean_rules_have_error_ignore_prefix(self):
        """Clean and realclean recipes should use - prefix to ignore errors."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args()
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_dir.return_value = "/exe/dir"
            mc.namer.object_dir.return_value = "/obj/dir"
            mc.objects = {"obj1.o", "obj2.o"}
            rules = mc._create_clean_rules(["target1", "target2"])
            clean_rule = next(r for r in rules if r.target == "clean")
            realclean_rule = next(r for r in rules if r.target == "realclean")
            assert clean_rule.recipe.startswith("-")
            assert realclean_rule.recipe.startswith("-")

    def test_create_test_rules_serialize_fallback(self):
        """When serialize_fallback=True, test rules should chain via order-only deps."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args(TESTPREFIX="", verbose=0)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_pathname.side_effect = lambda tt: f"/bin/{os.path.basename(tt)}"
            rules = mc._create_test_rules(
                ["/src/test_a.cpp", "/src/test_b.cpp", "/src/test_c.cpp"],
                serialize_fallback=True,
            )
            # First test should have no order-only dep; subsequent should chain
            test_rules = [r for r in rules if r.target != "runtests"]
            assert test_rules[0].order_only_prerequisites is None
            assert test_rules[1].order_only_prerequisites == test_rules[0].target
            assert test_rules[2].order_only_prerequisites == test_rules[1].target

    def test_create_test_rules_no_serialize(self):
        """Without serialize_fallback, test rules should have no order-only deps."""
        from unittest.mock import MagicMock

        from compiletools.makefile import MakefileCreator

        args = _make_args(TESTPREFIX="", verbose=0)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.namer = MagicMock()
            mc.namer.executable_pathname.side_effect = lambda tt: f"/bin/{os.path.basename(tt)}"
            rules = mc._create_test_rules(
                ["/src/test_a.cpp", "/src/test_b.cpp"],
                serialize_fallback=False,
            )
            test_rules = [r for r in rules if r.target != "runtests"]
            for rule in test_rules:
                assert rule.order_only_prerequisites is None

    def test_write_includes_makeflags_and_shell(self, tmp_path):
        """MakefileCreator.write() should include MAKEFLAGS and SHELL directives."""
        from compiletools.makefile import MakefileCreator

        makefile_path = str(tmp_path / "Makefile")
        args = _make_args(makefilename=makefile_path)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.rules = {}
            mc.write(makefile_path)

        with open(makefile_path) as f:
            content = f.read()
        assert "MAKEFLAGS += -rR" in content
        assert ".DELETE_ON_ERROR:" in content
        # SHELL depends on /bin/bash existence, just check it doesn't crash

    @patch("os.path.isfile", return_value=False)
    def test_write_omits_shell_without_bash(self, _mock_isfile, tmp_path):
        """MakefileCreator.write() should omit SHELL when /bin/bash doesn't exist."""
        from compiletools.makefile import MakefileCreator

        makefile_path = str(tmp_path / "Makefile")
        args = _make_args(makefilename=makefile_path)
        with patch.object(MakefileCreator, "__init__", lambda self, *a, **kw: None):
            mc = MakefileCreator.__new__(MakefileCreator)
            mc.args = args
            mc.rules = {}
            mc.write(makefile_path)

        with open(makefile_path) as f:
            content = f.read()
        assert "SHELL" not in content


class TestMakefile:
    def setup_method(self):
        uth.reset()

    def _create_makefile_and_make(self, tempdir):
        origdir = uth.ctdir()
        print("origdir=" + origdir)
        print(tempdir)
        samplesdir = uth.samplesdir()
        print("samplesdir=" + samplesdir)

        with uth.DirectoryContext(tempdir), uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
            relativepaths = [
                "numbers/test_direct_include.cpp",
                "factory/test_factory.cpp",
                "simple/helloworld_c.c",
                "simple/helloworld_cpp.cpp",
                "dottypaths/dottypaths.cpp",
            ]
            realpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
            with uth.ParserContext():  # Clear any existing parsers before calling main()
                compiletools.makefile.main(["--config=" + temp_config_name] + realpaths)

            filelist = os.listdir(".")
            makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
            cmd = ["make", "-f"] + makefilename
            subprocess.check_output(cmd, universal_newlines=True)

            # Check that an executable got built for each cpp
            actual_exes = set()
            for root, _dirs, files in os.walk(tempdir):
                for ff in files:
                    if compiletools.utils.is_executable(os.path.join(root, ff)):
                        actual_exes.add(ff)
                        print(root + " " + ff)

            expected_exes = {os.path.splitext(os.path.split(filename)[1])[0] for filename in relativepaths}
            assert expected_exes == actual_exes

    @uth.requires_functional_compiler
    def test_makefile(self):
        with uth.TempDirContextNoChange() as tempdir1:
            self._create_makefile_and_make(tempdir1)

    @uth.requires_functional_compiler
    def test_static_library(self):
        _test_library("--static")

    @uth.requires_functional_compiler
    def test_dynamic_library(self):
        _test_library("--dynamic")

    @uth.requires_functional_compiler
    def test_file_locking_propagates_compiler_errors(self):
        """Test that compiler errors fail the build when using --file-locking.

        Regression test for bug where set -e was missing from locking recipes,
        causing compiler failures to be silently ignored.
        """
        with uth.TempDirContextWithChange() as tempdir:
            # Create a source file with intentional syntax error
            bad_source = os.path.join(tempdir, "test_syntax_error.cpp")
            with open(bad_source, "w") as f:
                f.write("""
// ct-exemarker
int main() {
    this_is_a_syntax_error;  // Intentional error
    return 0;
}
""")

            with uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
                with uth.ParserContext():
                    # Generate Makefile with --file-locking enabled
                    compiletools.makefile.main(["--config=" + temp_config_name, bad_source, "--file-locking"])

                # Find generated Makefile
                filelist = os.listdir(".")
                makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
                assert makefilename, "Makefile should have been generated"

                # Verify Makefile uses ct-lock-helper for error propagation
                with open(makefilename[0]) as f:
                    makefile_content = f.read()
                    assert "ct-lock-helper" in makefile_content, (
                        "Makefile should use ct-lock-helper (which has set -euo pipefail)"
                    )

                # Attempt to build - this MUST fail
                cmd = ["make", "-f"] + makefilename
                result = subprocess.run(cmd, capture_output=True, text=True)

                # Verify build failed (non-zero exit code)
                assert result.returncode != 0, "Build should fail with non-zero exit code when compiler errors occur"

                # Verify error message is visible
                combined_output = result.stdout + result.stderr
                assert "error" in combined_output.lower(), "Compiler error message should be visible in output"

    def teardown_method(self):
        uth.reset()


def _test_library(static_dynamic):
    """Manually specify what files to turn into the static (or dynamic)
    library and test linkage
    """
    samplesdir = uth.samplesdir()

    with uth.TempDirContextWithChange() as tempdir, uth.TempConfigContext(tempdir=tempdir) as temp_config_name:
        exerelativepath = "numbers/test_library.cpp"
        librelativepaths = [
            "numbers/get_numbers.cpp",
            "numbers/get_int.cpp",
            "numbers/get_double.cpp",
        ]
        exerealpath = os.path.join(samplesdir, exerelativepath)
        librealpaths = [os.path.join(samplesdir, filename) for filename in librelativepaths]
        argv = ["--config=" + temp_config_name, exerealpath, static_dynamic] + librealpaths
        compiletools.makefile.main(argv)

        # Figure out the name of the makefile and run make
        filelist = os.listdir(".")
        makefilename = [ff for ff in filelist if ff.startswith("Makefile")]
        cmd = ["make", "-f"] + makefilename
        subprocess.check_output(cmd, universal_newlines=True)
