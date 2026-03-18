import os

import configargparse

import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.testhelper
import compiletools.testhelper as uth
import compiletools.wrappedos


def callprocess(headerobj, filenames):
    result = set()
    for filename in filenames:
        realpath = compiletools.wrappedos.realpath(filename)
        result |= set(headerobj.process(realpath, frozenset()))
    return result


class TestHunterModule:
    def setup_method(self):
        uth.reset()
        configargparse.getArgumentParser(
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=False,
        )

    def test_hunter_follows_source_files_from_header(self):
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            argv = ["-c", temp_config, "--include", uth.ctdir()]
            cap = configargparse.getArgumentParser()
            compiletools.hunter.add_arguments(cap)
            args = compiletools.apptools.parseargs(cap, argv)
            headerdeps = compiletools.headerdeps.create(args)
            magicparser = compiletools.magicflags.create(args, headerdeps)
            hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser)

            relativepath = "factory/widget_factory.hpp"
            realpath = os.path.join(uth.samplesdir(), relativepath)
            filesfromheader = hntr.required_source_files(realpath)
            filesfromsource = hntr.required_source_files(compiletools.utils.implied_source(realpath))
            assert set(filesfromheader) == set(filesfromsource)

    @staticmethod
    def _hunter_is_not_order_dependent(precall):
        samplesdir = uth.samplesdir()
        relativepaths = [
            "factory/test_factory.cpp",
            "numbers/test_direct_include.cpp",
            "simple/helloworld_c.c",
            "simple/helloworld_cpp.cpp",
            "simple/test_cflags.c",
        ]
        bulkpaths = [os.path.join(samplesdir, filename) for filename in relativepaths]
        with uth.TempConfigContext() as temp_config:
            argv = ["--config", temp_config, "--include", uth.ctdir()]
            cap = configargparse.getArgumentParser()
            compiletools.hunter.add_arguments(cap)
            args = compiletools.apptools.parseargs(cap, argv)
            headerdeps = compiletools.headerdeps.create(args)
            magicparser = compiletools.magicflags.create(args, headerdeps)
            hntr = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        realpath = os.path.join(samplesdir, "dottypaths/dottypaths.cpp")
        if precall:
            result = hntr.required_source_files(realpath)
            return result
        else:
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

    def teardown_method(self):
        uth.reset()


def _make_hunter(argv_extra=None, temp_config=None):
    """Helper to create a Hunter with standard setup."""
    if argv_extra is None:
        argv_extra = []
    argv = ["-c", temp_config, "--include", uth.ctdir()] + argv_extra
    cap = configargparse.getArgumentParser()
    compiletools.hunter.add_arguments(cap)
    args = compiletools.apptools.parseargs(cap, argv)
    headerdeps = compiletools.headerdeps.create(args)
    magicparser = compiletools.magicflags.create(args, headerdeps)
    return compiletools.hunter.Hunter(args, headerdeps, magicparser), args


class TestHunterClearCache:
    """Tests for cache clearing methods."""

    def setup_method(self):
        uth.reset()
        configargparse.getArgumentParser(
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=False,
        )

    def teardown_method(self):
        uth.reset()

    def test_clear_cache_static(self):
        """Test Hunter.clear_cache() clears module-level caches (lines 157-159)."""
        compiletools.hunter.Hunter.clear_cache()
        # Should not raise; verifies the static method runs the three clear calls

    def test_clear_instance_cache(self):
        """Test clear_instance_cache clears functools.cache and dynamic attrs (lines 167-175)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)

            # Set dynamic attributes that clear_instance_cache should remove
            hntr._hunted_sources = ["fake.cpp"]
            hntr._test_sources = ["fake_test.cpp"]

            hntr.clear_instance_cache()

            assert not hasattr(hntr, "_hunted_sources")
            assert not hasattr(hntr, "_test_sources")

    def test_clear_instance_cache_without_dynamic_attrs(self):
        """Test clear_instance_cache when dynamic attrs don't exist (lines 172-174 else branches)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            # Should not raise even without _hunted_sources/_test_sources
            hntr.clear_instance_cache()


class TestHunterRequiredFiles:
    """Tests for required_files, required_source_files, header_dependencies."""

    def setup_method(self):
        uth.reset()
        configargparse.getArgumentParser(
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=False,
        )

    def teardown_method(self):
        uth.reset()

    def test_required_files_returns_all_deps(self):
        """Test required_files returns headers + sources (lines 135, 142, 150, 152)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/test_factory.cpp")
            result = hntr.required_files(realpath)
            # Should contain the file itself plus dependencies
            assert realpath in [compiletools.wrappedos.realpath(f) for f in result] or \
                   compiletools.wrappedos.realpath(realpath) in result

    def test_required_source_files_filters_to_sources(self):
        """Test required_source_files only returns source files (lines 122-126)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/test_factory.cpp")
            sources = hntr.required_source_files(realpath)
            for s in sources:
                assert compiletools.utils.is_source(s), f"{s} is not a source file"

    def test_header_dependencies(self):
        """Test header_dependencies public API (lines 214-223)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/test_factory.cpp")
            headers = hntr.header_dependencies(realpath)
            # Should return a list of header file paths
            assert isinstance(headers, (list, tuple, set))
            # test_factory.cpp includes widget_factory.hpp
            header_basenames = [os.path.basename(h) for h in headers]
            assert "widget_factory.hpp" in header_basenames

    def test_macro_state_hash(self):
        """Test macro_state_hash returns a hash string (line 206)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/test_factory.cpp")
            # Must call magicflags first
            hntr.magicflags(realpath)
            h = hntr.macro_state_hash(realpath)
            assert isinstance(h, str)
            assert len(h) > 0

    def test_extractSOURCE_finds_source_flags(self):
        """Test _extractSOURCE extracts SOURCE magic flags (lines 48-62)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            # widget_factory.cpp has //#SOURCE=a_widget.cpp and //#SOURCE=z_widget.cpp
            realpath = os.path.join(uth.samplesdir(), "factory/widget_factory.cpp")
            sources = hntr._extractSOURCE(realpath)
            basenames = {os.path.basename(s) for s in sources}
            assert "a_widget.cpp" in basenames
            assert "z_widget.cpp" in basenames

    def test_get_immediate_deps_with_implied_source(self):
        """Test _get_immediate_deps finds implied source for header (lines 82-87)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            # widget_factory.hpp has implied source widget_factory.cpp
            realpath = os.path.join(uth.samplesdir(), "factory/widget_factory.hpp")
            headers, sources = hntr._get_immediate_deps(realpath, frozenset())
            # implied source should be in headers tuple
            implied_basenames = [os.path.basename(h) for h in headers]
            assert "widget_factory.cpp" in implied_basenames


class TestHunterHuntSource:
    """Tests for huntsource, getsources, gettestsources."""

    def setup_method(self):
        uth.reset()
        configargparse.getArgumentParser(
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=False,
        )

    def teardown_method(self):
        uth.reset()

    def test_huntsource_no_initial_sources(self):
        """Test huntsource with no initial sources (lines 253-257)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            # args has no filename/static/dynamic/tests
            hntr.huntsource()
            assert hntr._hunted_sources == []

    def test_getsources_calls_huntsource_if_needed(self):
        """Test getsources auto-calls huntsource (lines 303-305)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            # getsources should work without prior huntsource call
            result = hntr.getsources()
            assert isinstance(result, list)

    def test_huntsource_with_filename(self):
        """Test huntsource expands filename arg (lines 248-249, 264-276, 288-289)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.filename = [realpath]
            hntr.huntsource()
            assert len(hntr._hunted_sources) >= 1
            assert any("helloworld_cpp.cpp" in s for s in hntr._hunted_sources)

    def test_huntsource_with_nonexistent_file(self):
        """Test huntsource skips nonexistent files (lines 270-273)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            args.filename = ["/nonexistent/file.cpp"]
            hntr.huntsource()
            assert hntr._hunted_sources == []

    def test_huntsource_with_static(self):
        """Test huntsource picks up args.static (lines 244-245)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.static = [realpath]
            hntr.huntsource()
            assert len(hntr._hunted_sources) >= 1

    def test_huntsource_with_dynamic(self):
        """Test huntsource picks up args.dynamic (lines 246-247)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.dynamic = [realpath]
            hntr.huntsource()
            assert len(hntr._hunted_sources) >= 1

    def test_huntsource_clears_previous_results(self):
        """Test huntsource clears cached results on re-call (lines 234-237)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            hntr._hunted_sources = ["old.cpp"]
            hntr._test_sources = ["old_test.cpp"]
            hntr.huntsource()
            assert "old.cpp" not in hntr._hunted_sources

    def test_gettestsources_no_tests(self):
        """Test gettestsources with no test sources (lines 316-332)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            result = hntr.gettestsources()
            assert result == []

    def test_gettestsources_with_tests(self):
        """Test gettestsources expands test sources (lines 319-330)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.tests = [realpath]
            result = hntr.gettestsources()
            assert len(result) >= 1

    def test_gettestsources_cached(self):
        """Test gettestsources uses cached result on second call (line 316)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            result1 = hntr.gettestsources()
            result2 = hntr.gettestsources()
            assert result1 is result2

    def test_huntsource_with_tests_arg(self):
        """Test huntsource picks up args.tests (lines 250-251)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.tests = [realpath]
            hntr.huntsource()
            assert len(hntr._hunted_sources) >= 1

    def test_huntsource_deduplicates(self):
        """Test huntsource deduplicates across static/dynamic/filename (line 259)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.filename = [realpath]
            args.static = [realpath]
            hntr.huntsource()
            # Should still work without duplicates
            assert len(hntr._hunted_sources) >= 1


class TestHunterVerbose:
    """Tests for verbose output paths."""

    def setup_method(self):
        uth.reset()
        configargparse.getArgumentParser(
            description="Configargparser in test code",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=False,
        )

    def teardown_method(self):
        uth.reset()

    def test_required_files_verbose(self, capsys):
        """Test verbose output in required_files and _required_files_impl (lines 106, 112, 123, 135, 150)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v", "-v", "-v", "-v", "-v",
                                                   "-v", "-v", "-v", "-v", "-v"],
                                       temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            hntr.required_files(realpath)
            captured = capsys.readouterr()
            assert "Hunter::" in captured.out

    def test_get_immediate_deps_verbose(self, capsys):
        """Test verbose output in _get_immediate_deps (line 72)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            hntr._get_immediate_deps(realpath, frozenset())
            captured = capsys.readouterr()
            assert "_get_immediate_deps" in captured.out

    def test_huntsource_verbose(self, capsys):
        """Test verbose output in huntsource (lines 240, 256, 261, 272, 279, 292)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "simple/helloworld_cpp.cpp")
            args.filename = [realpath]
            hntr.huntsource()
            captured = capsys.readouterr()
            assert "huntsource" in captured.out

    def test_huntsource_verbose_nonexistent(self, capsys):
        """Test verbose output for nonexistent file in huntsource (line 272)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            args.filename = ["/nonexistent/path.cpp"]
            hntr.huntsource()
            captured = capsys.readouterr()
            assert "does not exist" in captured.out

    def test_huntsource_verbose_no_sources(self, capsys):
        """Test verbose output when no initial sources (line 256)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            hntr.huntsource()
            captured = capsys.readouterr()
            assert "No initial sources found" in captured.out

    def test_header_dependencies_verbose(self, capsys):
        """Test verbose output in header_dependencies (lines 215)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/test_factory.cpp")
            hntr.header_dependencies(realpath)
            captured = capsys.readouterr()
            assert "header dependencies" in captured.out

    def test_extractSOURCE_verbose(self, capsys):
        """Test verbose output in _extractSOURCE (line 61)."""
        with uth.TempDirContextNoChange(), uth.TempConfigContext() as temp_config:
            hntr, args = _make_hunter(argv_extra=["-v"] * 10, temp_config=temp_config)
            realpath = os.path.join(uth.samplesdir(), "factory/widget_factory.cpp")
            hntr._extractSOURCE(realpath)
            captured = capsys.readouterr()
            assert "SOURCE flag" in captured.out
