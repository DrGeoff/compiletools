import os

import configargparse
import pytest

import compiletools.apptools
import compiletools.apptools_pkgconfig as pkgconfig
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper
import compiletools.testhelper as uth
import compiletools.utils
import compiletools.wrappedos
from compiletools.build_context import BuildContext


@pytest.fixture(autouse=True)
def _reset_parser_state():
    """Wipe global configargparse parser cache around every test in this
    module. Each class historically also constructed a throwaway
    ArgumentParser in setup_method to re-seed the registry; preserved here
    in the fixture for behavioural equivalence."""
    uth.reset()
    configargparse.ArgumentParser(
        conflict_handler="resolve",
        description="Configargparser in test code",
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    yield
    uth.reset()


def callprocess(headerobj, filenames):
    result = set()
    for filename in filenames:
        realpath = compiletools.wrappedos.realpath(filename)
        result |= set(headerobj.process(realpath, frozenset()))
    return result


class TestHunterModule:
    def test_hunter_follows_source_files_from_header(self):
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            argv = ["-c", temp_config, "--include", uth.ctdir()]
            cap = configargparse.ArgumentParser(
                conflict_handler="resolve",
                args_for_setting_config_path=["-c", "--config"],
                ignore_unknown_config_file_keys=True,
            )
            compiletools.hunter.add_arguments(cap)
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

            relativepath = "factory/widget_factory.hpp"
            realpath = uth.example_file(relativepath)
            filesfromheader = hntr.required_source_files(realpath)
            filesfromsource = hntr.required_source_files(compiletools.utils.implied_source(realpath))
            assert set(filesfromheader) == set(filesfromsource)

    @staticmethod
    def _hunter_is_not_order_dependent(precall):
        relativepaths = [
            "factory/test_factory.cpp",
            "numbers/test_direct_include.cpp",
            "simple/helloworld_c.c",
            "simple/helloworld_cpp.cpp",
            "simple/test_cflags.c",
        ]
        bulkpaths = [uth.example_file(filename) for filename in relativepaths]
        with uth.TempConfigContext() as temp_config:
            argv = ["--config", temp_config, "--include", uth.ctdir()]
            cap = configargparse.ArgumentParser(
                conflict_handler="resolve",
                args_for_setting_config_path=["-c", "--config"],
                ignore_unknown_config_file_keys=True,
            )
            compiletools.hunter.add_arguments(cap)
            ctx = BuildContext()
            args = compiletools.apptools.parseargs(cap, argv, context=ctx)
            headerdeps = compiletools.headerdeps.create(args, context=ctx)
            magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
            hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx)

        realpath = uth.example_file("dottypaths/dottypaths.cpp")
        if precall:
            result = hntr.required_source_files(realpath)
            return result
        for filename in bulkpaths:
            hntr.required_source_files(filename)
        result = hntr.required_source_files(realpath)
        return result

    def test_hunter_is_not_order_dependent(self):
        with uth.TempDirContextNoChange():
            result2 = self._hunter_is_not_order_dependent(True)
            result1 = self._hunter_is_not_order_dependent(False)
            result3 = self._hunter_is_not_order_dependent(False)
            result4 = self._hunter_is_not_order_dependent(True)

            assert set(result1) == set(result2)
            assert set(result3) == set(result2)
            assert set(result4) == set(result2)


@pytest.fixture
def hunter_factory():
    """Yield a `factory(argv_extra=None) -> (hunter, args)` callable
    that runs `_make_hunter` inside a TempDirContextNoChange +
    TempConfigContext. Per-test scope: a fresh temp dir + config file
    for every test that requests this fixture."""
    with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:

        def factory(argv_extra=None):
            return _make_hunter(argv_extra=argv_extra, temp_config=temp_config)

        yield factory


def _make_hunter(argv_extra=None, temp_config=None):
    """Helper to create a Hunter with standard setup."""
    if argv_extra is None:
        argv_extra = []
    argv = ["-c", temp_config, "--include", uth.ctdir()] + argv_extra
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    compiletools.hunter.add_arguments(cap)
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    headerdeps = compiletools.headerdeps.create(args, context=ctx)
    magicparser = compiletools.magicflags.create(args, headerdeps, context=ctx)
    return compiletools.hunter.Hunter(args, headerdeps, magicparser, context=ctx), args


class TestHunterClearCache:
    """Tests for cache clearing methods."""

    def test_clear_cache_static(self):
        """Test Hunter.clear_cache() actually empties the shared module-level caches.

        ``Hunter.clear_cache`` delegates to ``MagicFlagsBase.clear_cache``,
        which clears the shared ``split_command_cached_sz`` LRU. Assert the
        observable postcondition (cache empty) rather than merely that the
        call returns.
        """
        import stringzilla as sz

        compiletools.utils.split_command_cached_sz.cache_clear()
        compiletools.utils.split_command_cached_sz(sz.Str("-DFOO=1 -DBAR=2"))
        assert compiletools.utils.split_command_cached_sz.cache_info().currsize > 0

        compiletools.hunter.Hunter.clear_cache()

        assert compiletools.utils.split_command_cached_sz.cache_info().currsize == 0

    def test_clear_instance_cache(self, hunter_factory):
        """Test clear_instance_cache clears functools.cache and dynamic attrs (lines 167-175)."""
        hntr, _args = hunter_factory()

        # Set dynamic attributes that clear_instance_cache should remove
        hntr._hunted_sources = ["fake.cpp"]
        hntr._test_sources = ["fake_test.cpp"]

        hntr.clear_instance_cache()

        assert not hasattr(hntr, "_hunted_sources")
        assert not hasattr(hntr, "_test_sources")

    def test_clear_instance_cache_without_dynamic_attrs(self, hunter_factory):
        """Test clear_instance_cache when dynamic attrs don't exist (lines 172-174 else branches)."""
        hntr, _args = hunter_factory()
        # Should not raise even without _hunted_sources/_test_sources
        hntr.clear_instance_cache()


class TestHunterRequiredFiles:
    """Tests for required_files, required_source_files, header_dependencies."""

    def test_required_files_returns_all_deps(self, hunter_factory):
        """Test required_files returns headers + sources (lines 135, 142, 150, 152)."""
        hntr, _args = hunter_factory()
        realpath = uth.example_file("factory/test_factory.cpp")
        result = hntr.required_files(realpath)
        # Should contain the file itself plus dependencies
        assert (
            realpath in [compiletools.wrappedos.realpath(f) for f in result]
            or compiletools.wrappedos.realpath(realpath) in result
        )

    def test_required_source_files_filters_to_sources(self, hunter_factory):
        """Test required_source_files only returns source files (lines 122-126)."""
        hntr, _args = hunter_factory()
        realpath = uth.example_file("factory/test_factory.cpp")
        sources = hntr.required_source_files(realpath)
        for s in sources:
            assert compiletools.utils.is_source(s), f"{s} is not a source file"

    def test_header_dependencies(self, hunter_factory):
        """Test header_dependencies public API (lines 214-223)."""
        hntr, _args = hunter_factory()
        realpath = uth.example_file("factory/test_factory.cpp")
        headers = hntr.header_dependencies(realpath)
        # Should return a list of header file paths
        assert isinstance(headers, (list, tuple, set))
        # test_factory.cpp includes widget_factory.hpp
        header_basenames = [os.path.basename(h) for h in headers]
        assert "widget_factory.hpp" in header_basenames

    def test_macro_state_hash(self, hunter_factory):
        """Test macro_state_hash returns a hash string (line 206)."""
        hntr, _args = hunter_factory()
        realpath = uth.example_file("factory/test_factory.cpp")
        # Must call magicflags first
        hntr.magicflags(realpath)
        h = hntr.macro_state_hash(realpath)
        assert isinstance(h, str)
        assert len(h) > 0

    def test_extractSOURCE_finds_source_flags(self, hunter_factory):
        """Test _extractSOURCE extracts SOURCE magic flags (lines 48-62)."""
        hntr, _args = hunter_factory()
        # widget_factory.cpp has //#SOURCE=a_widget.cpp and //#SOURCE=z_widget.cpp
        realpath = uth.example_file("factory/widget_factory.cpp")
        sources = hntr._extractSOURCE(realpath)
        basenames = {os.path.basename(s) for s in sources}
        assert "a_widget.cpp" in basenames
        assert "z_widget.cpp" in basenames

    def test_get_immediate_deps_with_implied_source(self, hunter_factory):
        """Test _get_immediate_deps finds implied source for header (lines 82-87)."""
        hntr, _args = hunter_factory()
        # widget_factory.hpp has implied source widget_factory.cpp
        realpath = uth.example_file("factory/widget_factory.hpp")
        headers, _sources = hntr._get_immediate_deps(realpath, frozenset())
        # implied source should be in headers tuple
        implied_basenames = [os.path.basename(h) for h in headers]
        assert "widget_factory.cpp" in implied_basenames


class TestHunterHuntSource:
    """Tests for huntsource, getsources, gettestsources."""

    def test_huntsource_no_initial_sources(self, hunter_factory):
        """Test huntsource with no initial sources (lines 253-257)."""
        hntr, _args = hunter_factory()
        # args has no filename/static/dynamic/tests
        hntr.huntsource()
        assert hntr._hunted_sources == []

    def test_getsources_calls_huntsource_if_needed(self, hunter_factory):
        """Test getsources auto-calls huntsource (lines 303-305)."""
        hntr, _args = hunter_factory()
        # getsources should work without prior huntsource call
        result = hntr.getsources()
        assert isinstance(result, list)

    def test_huntsource_with_filename(self, hunter_factory):
        """Test huntsource expands filename arg (lines 248-249, 264-276, 288-289)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.filename = [realpath]
        hntr.huntsource()
        assert len(hntr._hunted_sources) >= 1
        assert any("helloworld_cpp.cpp" in s for s in hntr._hunted_sources)

    def test_huntsource_with_nonexistent_file(self, hunter_factory):
        """Test huntsource skips nonexistent files (lines 270-273)."""
        hntr, args = hunter_factory()
        args.filename = ["/nonexistent/file.cpp"]
        hntr.huntsource()
        assert hntr._hunted_sources == []

    def test_huntsource_with_static(self, hunter_factory):
        """Test huntsource picks up args.static (lines 244-245)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.static = [realpath]
        hntr.huntsource()
        assert len(hntr._hunted_sources) >= 1

    def test_huntsource_with_dynamic(self, hunter_factory):
        """Test huntsource picks up args.dynamic (lines 246-247)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.dynamic = [realpath]
        hntr.huntsource()
        assert len(hntr._hunted_sources) >= 1

    def test_huntsource_clears_previous_results(self, hunter_factory):
        """Test huntsource clears cached results on re-call (lines 234-237)."""
        hntr, _args = hunter_factory()
        hntr._hunted_sources = ["old.cpp"]
        hntr._test_sources = ["old_test.cpp"]
        hntr.huntsource()
        assert "old.cpp" not in hntr._hunted_sources

    def test_gettestsources_no_tests(self, hunter_factory):
        """Test gettestsources with no test sources (lines 316-332)."""
        hntr, _args = hunter_factory()
        result = hntr.gettestsources()
        assert result == []

    def test_gettestsources_with_tests(self, hunter_factory):
        """Test gettestsources expands test sources (lines 319-330)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.tests = [realpath]
        result = hntr.gettestsources()
        assert len(result) >= 1

    def test_gettestsources_cached(self, hunter_factory):
        """Test gettestsources uses cached result on second call (line 316)."""
        hntr, _args = hunter_factory()
        result1 = hntr.gettestsources()
        result2 = hntr.gettestsources()
        assert result1 is result2

    def test_huntsource_with_tests_arg(self, hunter_factory):
        """Test huntsource picks up args.tests (lines 250-251)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.tests = [realpath]
        hntr.huntsource()
        assert len(hntr._hunted_sources) >= 1

    def test_huntsource_deduplicates(self, hunter_factory):
        """Test huntsource deduplicates across static/dynamic/filename (line 259)."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.filename = [realpath]
        args.static = [realpath]
        hntr.huntsource()
        # Should still work without duplicates
        assert len(hntr._hunted_sources) >= 1


class TestHunterVerbose:
    """Tests for verbose output paths."""

    def test_required_files_verbose(self, capsys, hunter_factory):
        """Test verbose output in required_files and _required_files_impl (lines 106, 112, 123, 135, 150)."""
        hntr, _args = hunter_factory(argv_extra=["-v", "-v", "-v", "-v", "-v", "-v", "-v", "-v", "-v", "-v"])
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        hntr.required_files(realpath)
        captured = capsys.readouterr()
        assert "Hunter::" in captured.out

    def test_get_immediate_deps_verbose(self, capsys, hunter_factory):
        """Test verbose output in _get_immediate_deps (line 72)."""
        hntr, _args = hunter_factory(argv_extra=["-v"] * 10)
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        hntr._get_immediate_deps(realpath, frozenset())
        captured = capsys.readouterr()
        assert "_get_immediate_deps" in captured.out

    def test_huntsource_verbose(self, capsys, hunter_factory):
        """Test verbose output in huntsource (lines 240, 256, 261, 272, 279, 292)."""
        hntr, args = hunter_factory(argv_extra=["-v"] * 10)
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.filename = [realpath]
        hntr.huntsource()
        captured = capsys.readouterr()
        assert "huntsource" in captured.out

    def test_huntsource_verbose_nonexistent(self, capsys, hunter_factory):
        """Test verbose output for nonexistent file in huntsource (line 272)."""
        hntr, args = hunter_factory(argv_extra=["-v"] * 10)
        args.filename = ["/nonexistent/path.cpp"]
        hntr.huntsource()
        captured = capsys.readouterr()
        assert "does not exist" in captured.out

    def test_huntsource_verbose_no_sources(self, capsys, hunter_factory):
        """Test verbose output when no initial sources (line 256)."""
        hntr, _args = hunter_factory(argv_extra=["-v"] * 10)
        hntr.huntsource()
        captured = capsys.readouterr()
        assert "No initial sources found" in captured.out

    def test_header_dependencies_verbose(self, capsys, hunter_factory):
        """Test verbose output in header_dependencies (lines 215)."""
        hntr, _args = hunter_factory(argv_extra=["-v"] * 10)
        realpath = uth.example_file("factory/test_factory.cpp")
        hntr.header_dependencies(realpath)
        captured = capsys.readouterr()
        assert "header dependencies" in captured.out

    def test_extractSOURCE_verbose(self, capsys, hunter_factory):
        """Test verbose output in _extractSOURCE (line 61)."""
        hntr, _args = hunter_factory(argv_extra=["-v"] * 10)
        realpath = uth.example_file("factory/widget_factory.cpp")
        hntr._extractSOURCE(realpath)
        captured = capsys.readouterr()
        assert "SOURCE flag" in captured.out


@pytest.fixture
def strict_pkg_config():
    """Restore the process-local pkg-config failure policy (and its memos)
    around a test that parses ``--pkg-config-errors=error``."""
    pkgconfig.clear_cache()
    yield
    pkgconfig.clear_cache()


class TestStrictPkgConfigIsFatalDuringExpansion:
    """``--pkg-config-errors=error`` must terminate the run at every
    verbosity. ``huntsource``/``gettestsources`` wrap the expansion in a
    deliberately broad ``except Exception`` that prints a warning and keeps
    going; a strict-mode pkg-config failure reaching that handler would
    downgrade an enforcement policy to a warning and hand the caller a
    source list built from a package whose flags are missing."""

    @staticmethod
    def _source_with_missing_package(tmp_path, name):
        files = uth.write_sources(
            {f"{name}.cpp": f"//#PKG-CONFIG=compiletools-missing-{name}\nint main() {{ return 0; }}\n"},
            target_dir=str(tmp_path),
        )
        return str(files[f"{name}.cpp"])

    @pytest.mark.parametrize("verbosity", [0, 2])
    def test_getsources_terminates_instead_of_warning(
        self, capsys, tmp_path, hunter_factory, strict_pkg_config, verbosity
    ):
        """The whole point of the finding: at ``-vv`` the strict branch used
        to raise ``PkgConfigError``, which ``huntsource`` caught and turned
        into a warning, so ``getsources()`` returned normally."""
        source = self._source_with_missing_package(tmp_path, f"getsources_v{verbosity}")
        argv_extra = ["--magic", "direct", "--pkg-config-errors=error"] + ["-v"] * verbosity
        hntr, args = hunter_factory(argv_extra=argv_extra)
        args.filename = [source]

        with pytest.raises(SystemExit) as excinfo:
            hntr.getsources()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Error expanding source" not in captured.err, (
            f"strict pkg-config was downgraded to a warning at verbosity {verbosity}: {captured.err!r}"
        )
        assert "--pkg-config-errors=warn" in captured.err
        assert "Traceback" not in captured.err

    @pytest.mark.parametrize("verbosity", [0, 2])
    def test_gettestsources_terminates_instead_of_warning(
        self, capsys, tmp_path, hunter_factory, strict_pkg_config, verbosity
    ):
        source = self._source_with_missing_package(tmp_path, f"gettestsources_v{verbosity}")
        argv_extra = ["--magic", "direct", "--pkg-config-errors=error"] + ["-v"] * verbosity
        hntr, args = hunter_factory(argv_extra=argv_extra)
        args.tests = [source]

        with pytest.raises(SystemExit) as excinfo:
            hntr.gettestsources()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Error expanding test source" not in captured.err, (
            f"strict pkg-config was downgraded to a warning at verbosity {verbosity}: {captured.err!r}"
        )
        assert "--pkg-config-errors=warn" in captured.err

    def test_huntsource_does_not_swallow_a_pkg_config_error_from_deeper(self, capsys, hunter_factory, monkeypatch):
        """Defence in depth: magicflags is not the only thing the expansion
        calls, so the broad handler itself must refuse the enforcement class
        rather than relying on the one call site raising SystemExit."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.filename = [realpath]

        def raise_pkg_config_error(_filename):
            raise pkgconfig.PkgConfigError("pkg-config package 'deep' not found")

        monkeypatch.setattr(hntr, "required_source_files", raise_pkg_config_error)

        with pytest.raises(SystemExit) as excinfo:
            hntr.huntsource()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Error expanding source" not in captured.err
        assert "deep" in captured.err

    def test_gettestsources_does_not_swallow_a_pkg_config_error_from_deeper(self, capsys, hunter_factory, monkeypatch):
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.tests = [realpath]

        def raise_pkg_config_error(_filename):
            raise pkgconfig.PkgConfigError("pkg-config package 'deep' not found")

        monkeypatch.setattr(hntr, "required_source_files", raise_pkg_config_error)

        with pytest.raises(SystemExit) as excinfo:
            hntr.gettestsources()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        assert "Error expanding test source" not in captured.err
        assert "deep" in captured.err

    def test_an_ordinary_expansion_failure_is_still_only_a_warning(self, capsys, hunter_factory, monkeypatch):
        """The strict-mode carve-out must not widen the broad handler into
        a fatal one for the OSError/parse-error class it exists for."""
        hntr, args = hunter_factory()
        realpath = uth.example_file("simple/helloworld_cpp.cpp")
        args.filename = [realpath]

        def raise_os_error(_filename):
            raise OSError("disk gremlins")

        monkeypatch.setattr(hntr, "required_source_files", raise_os_error)

        hntr.huntsource()

        captured = capsys.readouterr()
        assert "Error expanding source" in captured.err
        assert hntr._hunted_sources == [compiletools.wrappedos.realpath(realpath)]
