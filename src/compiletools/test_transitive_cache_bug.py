"""
Test for the transitive header dependency cache bug.

This test reproduces the bug where the _include_list_cache can return stale/incorrect
results when processing multiple source files that share headers with traditional
#ifndef include guards.

Bug Description:
- The _include_list_cache is keyed on (content_hash, macro_key)
- When a header with #ifndef guard is processed, the guard macro gets defined
- This can cause subsequent cache lookups to return empty include lists
- Result: transitive dependencies are missing from the dependency list

Real-world scenario (game engine themed, sanitized from production code):
- task_scheduler.cpp → task_scheduler.hpp → core/event_handler.hpp → core/memory_buffer.hpp
- The memory_buffer.hpp dependency (which has //#PKG-CONFIG=zlib) was missing
- This caused build failure: zlib not linked, undefined reference to compress functions
"""

import os
import pytest
import configargparse
import compiletools.test_base as tb
import compiletools.headerdeps
import compiletools.magicflags
import compiletools.hunter
import compiletools.apptools
import compiletools.testhelper as uth


class TestTransitiveCacheBug(tb.BaseCompileToolsTestCase):
    """Tests for the cache bug in transitive header dependency discovery."""

    def _get_sample_file(self, *parts):
        """Helper to get path to file in transitive_cache_bug sample directory."""
        return os.path.join(uth.samplesdir(), "transitive_cache_bug", *parts)

    def _get_engine_file(self, *parts):
        """Helper to get path to file in the engine sample directory."""
        return os.path.join(uth.samplesdir(), "transitive_cache_bug", "engine", *parts)

    def test_game_engine_transitive_deps(self):
        """
        Test the game engine sample code that mirrors the real-world bug.

        The dependency chain is:
        task_scheduler.cpp → task_scheduler.hpp → event_handler.hpp → memory_buffer.hpp

        Where:
        - task_scheduler.hpp uses #pragma once
        - event_handler.hpp uses #ifndef guard (THE KEY TO THE BUG)
        - memory_buffer.hpp uses #pragma once and has //#PKG-CONFIG=zlib

        All 3 headers should appear in task_scheduler.cpp's dependencies.
        """
        # Setup
        cap = configargparse.getArgumentParser()
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)

        sample_dir = os.path.join(uth.samplesdir(), "transitive_cache_bug")
        engine_dir = os.path.join(sample_dir, "engine")
        argv = [
            "--headerdeps", "direct",
            "--INCLUDE", sample_dir,
            "--INCLUDE", engine_dir,
            "-q"
        ]
        args = compiletools.apptools.parseargs(cap, argv)

        # Clear module-level caches to start fresh
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

        # Create DirectHeaderDeps instance
        deps = compiletools.headerdeps.DirectHeaderDeps(args)

        # Process task_scheduler.cpp
        task_scheduler_cpp = self._get_engine_file("systems", "task_scheduler.cpp")
        result = deps.process(task_scheduler_cpp, frozenset())
        result_set = set(result)

        # Expected headers in the dependency chain
        expected_headers = {
            self._get_engine_file("systems", "task_scheduler.hpp"),
            self._get_engine_file("core", "event_handler.hpp"),
            self._get_engine_file("core", "background_task.hpp"),
            self._get_engine_file("core", "memory_buffer.hpp"),  # CRITICAL - has //#PKG-CONFIG=zlib
        }

        missing = expected_headers - result_set
        assert not missing, (
            f"Missing transitive dependencies in task_scheduler.cpp:\n"
            f"  Expected: {sorted([os.path.basename(f) for f in expected_headers])}\n"
            f"  Got: {sorted([os.path.basename(f) for f in result_set])}\n"
            f"  Missing: {sorted([os.path.basename(f) for f in missing])}\n"
            f"\nIf memory_buffer.hpp is missing, the bug has regressed."
        )

    def test_game_engine_multi_file_processing(self):
        """
        Process multiple game engine source files in sequence.

        This mirrors how ct-cake processes a real project - many source files
        using the same DirectHeaderDeps instance, which can cause cache pollution.

        Files are processed in alphabetical order (typical make behavior):
        1. audio_system.cpp
        2. event_loop.cpp
        3. input_system.cpp
        4. render_system.cpp
        5. task_scheduler.cpp (LAST - this is where the bug manifests)

        All files include event_handler.hpp which has the #ifndef guard.
        """
        # Setup
        cap = configargparse.getArgumentParser()
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)

        sample_dir = os.path.join(uth.samplesdir(), "transitive_cache_bug")
        engine_dir = os.path.join(sample_dir, "engine")
        argv = [
            "--headerdeps", "direct",
            "--INCLUDE", sample_dir,
            "--INCLUDE", engine_dir,
            "-q"
        ]
        args = compiletools.apptools.parseargs(cap, argv)

        # Clear module-level caches to start fresh
        compiletools.headerdeps.HeaderDepsBase.clear_cache()

        # Use SINGLE DirectHeaderDeps instance for all files (like ct-cake does)
        deps = compiletools.headerdeps.DirectHeaderDeps(args)

        # Process files in alphabetical order (typical make behavior)
        files_to_process = [
            self._get_engine_file("systems", "audio_system.cpp"),
            self._get_engine_file("event_loop.cpp"),
            self._get_engine_file("systems", "input_system.cpp"),
            self._get_engine_file("systems", "render_system.cpp"),
            self._get_engine_file("systems", "task_scheduler.cpp"),  # Process LAST
        ]

        results = {}
        for filepath in files_to_process:
            result = deps.process(filepath, frozenset())
            results[os.path.basename(filepath)] = set(result)

        # Verify task_scheduler.cpp has memory_buffer.hpp
        # This is the critical check - if memory_buffer.hpp is missing, the bug exists
        task_scheduler_deps = results["task_scheduler.cpp"]
        memory_buffer = self._get_engine_file("core", "memory_buffer.hpp")

        assert memory_buffer in task_scheduler_deps, (
            f"CACHE BUG! task_scheduler.cpp is missing memory_buffer.hpp!\n"
            f"  Got dependencies: {sorted([os.path.basename(f) for f in task_scheduler_deps])}\n"
            f"\nmemory_buffer.hpp has //#PKG-CONFIG=zlib - without it, -lz won't be linked.\n"
            f"This is the exact bug that caused 'undefined reference to compressBound'."
        )

        # Verify all other files also have memory_buffer.hpp
        for filename, file_deps in results.items():
            assert memory_buffer in file_deps, (
                f"{filename} missing memory_buffer.hpp dependency"
            )

    def test_hunter_multi_file_processing(self):
        """
        Process multiple files through Hunter layer (like ct-cake does).

        This test uses hunter.header_dependencies() which is the actual code path
        ct-cake uses when generating Makefiles. The Hunter calls magicparser.parse()
        first, then uses the computed macro_state_key with headerdeps.process().
        """
        # Setup with magicflags arguments
        cap = configargparse.getArgumentParser()
        compiletools.headerdeps.add_arguments(cap)
        compiletools.magicflags.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)

        sample_dir = os.path.join(uth.samplesdir(), "transitive_cache_bug")
        engine_dir = os.path.join(sample_dir, "engine")
        argv = [
            "--headerdeps", "direct",
            "--magic", "direct",
            "--INCLUDE", sample_dir,
            "--INCLUDE", engine_dir,
            "-q"
        ]
        args = compiletools.apptools.parseargs(cap, argv)

        # Clear all caches
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        compiletools.magicflags.MagicFlagsBase.clear_cache()

        # Create Hunter with headerdeps and magicflags (like ct-cake does)
        headerdeps = compiletools.headerdeps.create(args)
        magicparser = compiletools.magicflags.create(args, headerdeps)
        hunter = compiletools.hunter.Hunter(args, headerdeps, magicparser)

        # Process files in alphabetical order (like make does)
        files_to_process = [
            self._get_engine_file("systems", "audio_system.cpp"),
            self._get_engine_file("event_loop.cpp"),
            self._get_engine_file("systems", "input_system.cpp"),
            self._get_engine_file("systems", "render_system.cpp"),
            self._get_engine_file("systems", "task_scheduler.cpp"),
        ]

        results = {}
        for filepath in files_to_process:
            # Use hunter.header_dependencies() like ct-cake does
            result = hunter.header_dependencies(filepath)
            results[os.path.basename(filepath)] = set(result)

        # Verify task_scheduler.cpp has memory_buffer.hpp
        task_scheduler_deps = results["task_scheduler.cpp"]
        memory_buffer = self._get_engine_file("core", "memory_buffer.hpp")

        assert memory_buffer in task_scheduler_deps, (
            f"CACHE BUG via Hunter! task_scheduler.cpp is missing memory_buffer.hpp!\n"
            f"  Got dependencies: {sorted([os.path.basename(f) for f in task_scheduler_deps])}\n"
            f"\nThis is the exact bug that causes ct-cake build failures."
        )

    def test_processing_order_independence(self):
        """
        Verify that file processing order doesn't affect dependency discovery.

        Process the same files in two different orders and verify identical results.
        """
        # Setup
        cap = configargparse.getArgumentParser()
        compiletools.headerdeps.add_arguments(cap)
        compiletools.apptools.add_common_arguments(cap)

        sample_dir = os.path.join(uth.samplesdir(), "transitive_cache_bug")
        engine_dir = os.path.join(sample_dir, "engine")
        argv = [
            "--headerdeps", "direct",
            "--INCLUDE", sample_dir,
            "--INCLUDE", engine_dir,
            "-q"
        ]
        args = compiletools.apptools.parseargs(cap, argv)

        task_scheduler = self._get_engine_file("systems", "task_scheduler.cpp")
        audio_system = self._get_engine_file("systems", "audio_system.cpp")

        # Test 1: Process task_scheduler first
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        deps1 = compiletools.headerdeps.DirectHeaderDeps(args)
        result1_task = set(deps1.process(task_scheduler, frozenset()))
        result1_audio = set(deps1.process(audio_system, frozenset()))

        # Test 2: Process audio_system first (different order)
        compiletools.headerdeps.HeaderDepsBase.clear_cache()
        deps2 = compiletools.headerdeps.DirectHeaderDeps(args)
        result2_audio = set(deps2.process(audio_system, frozenset()))
        result2_task = set(deps2.process(task_scheduler, frozenset()))

        # Results should be identical regardless of order
        assert result1_task == result2_task, (
            f"Processing order affects task_scheduler.cpp dependencies!\n"
            f"  Processed first:  {sorted([os.path.basename(f) for f in result1_task])}\n"
            f"  Processed second: {sorted([os.path.basename(f) for f in result2_task])}\n"
            f"  Difference: {sorted([os.path.basename(f) for f in result1_task ^ result2_task])}"
        )

        assert result1_audio == result2_audio, (
            f"Processing order affects audio_system.cpp dependencies!\n"
            f"  Processed first:  {sorted([os.path.basename(f) for f in result2_audio])}\n"
            f"  Processed second: {sorted([os.path.basename(f) for f in result1_audio])}\n"
            f"  Difference: {sorted([os.path.basename(f) for f in result1_audio ^ result2_audio])}"
        )


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
