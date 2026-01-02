import os
import configargparse

import compiletools.testhelper as uth
import compiletools.utils
import compiletools.findtargets


class TestFindTargetsModule:
    def setup_method(self):
        uth.reset()

    def _find_samples_targets(self, disable_tests, disable_exes=False):
        relativeexpectedexes = {
            "macro_state_dependency/sample.cpp",
            "macro_state_dependency/main.cpp",
            "macro_state_dependency/clean_main.cpp",
            "conditional_includes/main.cpp",
            "cppflags_macros/main.cpp",
            "cppflags_macros/multi_flag_test.cpp",
            "cppflags_macros/compiler_builtin_test.cpp",
            "cppflags_macros/elif_test.cpp",
            "cppflags_macros/advanced_preprocessor_test.cpp",
            "cppflags_macros/nested_macros_test.cpp",
            "dottypaths/dottypaths.cpp",
            "feature_headers/main.cpp",
            "hunter_macro_propagation/app.cpp",
            "isystem_include_bug/main.cpp",
            "ldflags/conditional_ldflags_test.cpp",
            "ldflags/version_dependent_ldflags.cpp",
            "library/main.cpp",
            "lotsofmagic/lotsofmagic.cpp",
            "macro_deps/main.cpp",
            "magic_processing_order/complex_test.cpp",
            "magicinclude/main.cpp",
            "magicpkgconfig/main.cpp",
            "magicpkgconfig_fake/main.cpp",
            "magicsourceinheader/main.cpp",
            "movingheaders/main.cpp",
            "nestedconfig/nc.cpp",
            "nestedconfig/subdir/nc.cpp",
            "pkgconfig/main.cpp",
            "simple/helloworld_c.c",
            "simple/helloworld_cpp.cpp",
            "calculator/main.cpp",
            "duplicate_flags/main.cpp",
            "empty_macro_bug/libs/main.cpp",
            "parse_order_macro_bug/libs/entry_point_1.cpp",
            "parse_order_macro_bug/libs/entry_point_2.cpp",
            "transitive_cache_bug/engine/a-game.cpp",
            "transitive_cache_bug/engine/b-game.cpp",
            "header_guard_bug/main.cpp",
        }
        relativeexpectedtests = {
            "cross_platform/test_source.cpp",
            "factory/test_factory.cpp",
            "magic_processing_order/test_macro_transform.cpp",
            "numbers/test_direct_include.cpp",
            "numbers/test_library.cpp",
            "simple/test_cflags.c",
            "serialise_tests/test_flock_1.cpp",
            "serialise_tests/test_flock_2.cpp",
            "version_dependent_api/test_main.cpp",
            "version_dependent_api/test_main_new.cpp",
            "pkg_config_header_deps/src/test.cpp",
        }

        expectedexes = set()
        if not disable_exes:
            expectedexes = {
                os.path.realpath(os.path.join(uth.samplesdir(), exe))
                for exe in relativeexpectedexes
            }
        expectedtests = set()
        if not disable_tests:
            expectedtests = {
                os.path.realpath(os.path.join(uth.samplesdir(), tt))
                for tt in relativeexpectedtests
            }

        config_files = compiletools.configutils.config_files_from_variant(
            exedir=uth.cakedir(), argv=[]
        )
        cap = configargparse.getArgumentParser(
            description="TestFindTargetsModule",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        compiletools.findtargets.add_arguments(cap)
        argv = ["--shorten"]
        if disable_tests:
            argv.append("--disable-tests")
        if disable_exes:
            argv.append("--disable-exes")
        args = compiletools.apptools.parseargs(cap, argv=argv)
        findtargets = compiletools.findtargets.FindTargets(args, exedir=uth.cakedir())
        executabletargets, testtargets = findtargets(path=uth.cakedir())
        assert expectedexes == set(executabletargets)
        assert expectedtests == set(testtargets)

    def test_samples(self):
        self._find_samples_targets(disable_tests=False)

    def test_disable_tests(self):
        self._find_samples_targets(disable_tests=True)

    def test_tests_only(self):
        self._find_samples_targets(disable_tests=False, disable_exes=True)

    def teardown_method(self):
        uth.reset()
