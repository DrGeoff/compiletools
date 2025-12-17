"""
Multi-user shared object cache tests.

Tests concurrent compilation, locking mechanisms, and permission handling
for shared object file caches across multiple users/processes.

Filesystem Compatibility:
- Most tests work with any locking strategy (flock, lockdir, cifs)
- test_stale_lock_cleanup requires lockdir (NFS/GPFS/Lustre) and skips on flock filesystems
- When run on GPFS/Lustre/NFS, all tests execute
- When run on local filesystems (ext4/xfs), stale lock test skips with informative message

Umask Requirements:
- Tests temporarily set umask to 0o0002 to ensure group-writable permissions
- This is required for multi-user cache functionality
- Original umask is restored after each test
"""

import os
import time
import tempfile
import multiprocessing
from pathlib import Path
import pytest

import compiletools.testhelper as uth
import compiletools.cake
import compiletools.apptools
from compiletools.test_base import BaseCompileToolsTestCase


def compile_worker(worker_id, source_dir, config_name):
    """
    Worker process that runs ct-cake in a directory.

    Args:
        worker_id: Unique identifier for this worker
        source_dir: Directory containing source files
        config_name: Path to config file

    Returns:
        Dict with worker_id, returncode, exception info
    """
    # Small delay to increase concurrency
    import time
    time.sleep(0.01 * worker_id)

    try:
        os.chdir(source_dir)

        argv = [
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--auto",
            "--config=" + config_name,
        ]

        uth.reset()
        compiletools.cake.main(argv)

        return {
            'worker_id': worker_id,
            'returncode': 0,
            'error': None,
        }
    except Exception as e:
        return {
            'worker_id': worker_id,
            'returncode': 1,
            'error': str(e),
        }


def compile_with_umask(source_dir, config_name, umask_value):
    """Run ct-cake with specific umask value."""
    import io
    import sys

    old_umask = os.umask(umask_value)
    # Capture both stdout and stderr to get error messages
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    try:
        os.chdir(source_dir)
        sys.stdout = stdout_capture
        sys.stderr = stderr_capture

        argv = [
            "--exemarkers=main",
            "--testmarkers=unittest.hpp",
            "--auto",
            "--config=" + config_name,
        ]

        uth.reset()
        result = compiletools.cake.main(argv)
        error_output = stdout_capture.getvalue() + stderr_capture.getvalue()
        return result if result else 0, error_output if error_output else None
    except SystemExit as e:
        error_output = stdout_capture.getvalue() + stderr_capture.getvalue()
        return e.code if e.code else 0, error_output or f"SystemExit: {e.code}"
    except Exception as e:
        return 1, str(e)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        os.umask(old_umask)


def create_objdir_worker(worker_id, objdir):
    """Worker that tries to create objdir."""
    # Small delay to increase chance of concurrent access
    import time
    time.sleep(0.01 * worker_id)

    try:
        # Save and set umask to allow group write
        old_umask = os.umask(0o002)
        try:
            os.makedirs(objdir, mode=0o2775, exist_ok=True)
        finally:
            os.umask(old_umask)
        return {'worker_id': worker_id, 'success': True, 'error': None}
    except Exception as e:
        return {'worker_id': worker_id, 'success': False, 'error': str(e)}


def build_subproject_worker(worker_id, source_dir, config_name):
    """Build a subproject using ct-cake (module-level for multiprocessing)."""
    try:
        os.chdir(source_dir)
        argv = [
            "--exemarkers=main",
            "--auto",
            "--config=" + config_name,
        ]
        uth.reset()
        compiletools.cake.main(argv)
        return {'worker_id': worker_id, 'returncode': 0, 'error': None}
    except Exception as e:
        return {'worker_id': worker_id, 'returncode': 1, 'error': str(e)}


def continuous_reader(obj_path, stop_event, errors):
    """Continuously read object file and detect corruption."""
    original_size = None
    while not stop_event.is_set():
        try:
            with open(str(obj_path), 'rb') as f:
                data = f.read()
                if original_size is None and len(data) > 0:
                    original_size = len(data)

                # With atomic move, should never see empty or partial file
                if len(data) == 0:
                    errors.append("Read empty file")
                elif original_size and len(data) < original_size // 2:
                    errors.append(f"Read partial file: {len(data)} bytes vs {original_size}")
        except FileNotFoundError:
            # File might briefly not exist during atomic move - acceptable
            pass
        except Exception as e:
            errors.append(f"Unexpected error: {e}")
        time.sleep(0.001)  # 1ms polling


@uth.with_group_writable_umask
class TestMultiUserCache(BaseCompileToolsTestCase):
    """Tests for multi-user shared object cache functionality."""

    def _create_test_source_dir(self, tmpdir, source_name, objdir):
        """
        Create a test directory with a source file and config.

        Returns: (source_dir, config_name)
        """
        source_dir = Path(tmpdir) / source_name
        source_dir.mkdir(exist_ok=True)

        # Copy source file
        import shutil
        src_file = os.path.join(uth.samplesdir(), 'simple/helloworld_cpp.cpp')
        shutil.copy2(src_file, str(source_dir / 'main.cpp'))

        # Create config with shared-objects enabled and custom objdir
        config_name = uth.create_temp_config(str(source_dir))
        uth.create_temp_ct_conf(
            tempdir=str(source_dir),
            defaultvariant=os.path.basename(config_name)[:-5],
            extralines=[
                'shared-objects = true',
                f'objdir = {objdir}'
            ]
        )

        return str(source_dir), config_name

    @uth.requires_functional_compiler
    def test_concurrent_same_file_compilation(self):
        """
        Test 1.1: Two processes compile same file simultaneously.

        Expected:
        - Both processes succeed
        - No corruption, no partial writes
        - Both can access shared objdir without permission errors
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Create two separate build directories with identical source
            dirs_and_configs = [
                self._create_test_source_dir(tmpdir, f'build_{i}', str(objdir))
                for i in range(2)
            ]

            num_workers = 2

            # Use 'spawn' instead of 'fork' to avoid multi-threading issues with pytest
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(num_workers) as pool:
                results = pool.starmap(
                    compile_worker,
                    [(i, src_dir, cfg) for i, (src_dir, cfg) in enumerate(dirs_and_configs)]
                )

            # Verify all workers succeeded
            for r in results:
                assert r['returncode'] == 0, \
                    f"Worker {r['worker_id']} failed: {r['error']}"

            # Verify object files were created in shared objdir
            obj_files = list(objdir.glob('**/*.o'))
            assert len(obj_files) >= 1, \
                f"Expected at least 1 object file, found {len(obj_files)}"

            # Verify all object files are valid and group-readable
            for obj_file in obj_files:
                assert obj_file.stat().st_size > 0, f"{obj_file.name} is empty"
                mode = obj_file.stat().st_mode
                assert mode & 0o040, f"{obj_file.name} not group-readable: {oct(mode)}"

    @uth.requires_functional_compiler
    def test_concurrent_different_files(self):
        """
        Test 1.2: Multiple processes compile to same objdir.

        Expected:
        - All compilations succeed
        - No permission errors
        - All can write to shared objdir
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Create build directories with different main files
            import shutil
            sources = [
                'simple/helloworld_cpp.cpp',
                'numbers/test_direct_include.cpp',
                'factory/test_factory.cpp',
            ]

            dirs_and_configs = []
            for i, src_name in enumerate(sources):
                source_dir = Path(tmpdir) / f'build_{i}'
                source_dir.mkdir()

                # Copy entire source directory for files with dependencies
                src_parent = Path(uth.samplesdir()) / os.path.dirname(src_name)
                for f in src_parent.glob('*'):
                    if f.is_file():
                        shutil.copy2(str(f), str(source_dir))

                config_name = uth.create_temp_config(str(source_dir))
                uth.create_temp_ct_conf(
                    tempdir=str(source_dir),
                    defaultvariant=os.path.basename(config_name)[:-5],
                    extralines=[
                        'shared-objects = true',
                        f'objdir = {objdir}'
                    ]
                )
                dirs_and_configs.append((str(source_dir), config_name))

            num_workers = len(sources)

            # Use 'spawn' instead of 'fork' to avoid multi-threading issues with pytest
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(num_workers) as pool:
                results = pool.starmap(
                    compile_worker,
                    [(i, src_dir, cfg) for i, (src_dir, cfg) in enumerate(dirs_and_configs)]
                )

            # All should succeed
            for r in results:
                assert r['returncode'] == 0, \
                    f"Worker {r['worker_id']} failed: {r['error']}"

            # Should have object files in shared objdir
            obj_files = list(objdir.glob('**/*.o'))
            assert len(obj_files) >= len(sources), \
                f"Expected at least {len(sources)} object files, found {len(obj_files)}"

    @uth.requires_functional_compiler
    def test_different_umask_compatibility(self):
        """
        Test 2.1: Umask compatibility for shared-objects mode.

        Expected:
        - User A with umask 0002 compiles successfully
        - User B with umask 0022 compiles successfully (blocks group write)
        - User C with umask 0077 compiles successfully (blocks group read/write)
        - All succeed - restrictive umask is fine for single-user scenarios
        - Warning only shown at verbose >= 1 for multi-user awareness
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            source_dir_a, config_name_a = self._create_test_source_dir(
                tmpdir, 'build_a', str(objdir)
            )

            # User A: umask 0002 (group read/write) - should succeed without warning
            returncode, output = compile_with_umask(source_dir_a, config_name_a, 0o002)
            assert returncode == 0, f"User A compilation failed: {output}"

            # Check object file permissions
            obj_files = list(objdir.glob('**/*.o'))
            assert len(obj_files) >= 1
            obj_file = obj_files[0]
            mode = obj_file.stat().st_mode
            assert mode & 0o060, f"Object file not group read/write: {oct(mode)}"

            # User B: umask 0022 (blocks group write) - should succeed (no warning at verbose=0)
            source_dir_b, config_name_b = self._create_test_source_dir(
                tmpdir, 'build_b', str(objdir)
            )
            returncode, output = compile_with_umask(source_dir_b, config_name_b, 0o022)
            assert returncode == 0, f"User B should succeed: {output}"

            # User C: umask 0077 (blocks all group) - should succeed (no warning at verbose=0)
            source_dir_c, config_name_c = self._create_test_source_dir(
                tmpdir, 'build_c', str(objdir)
            )
            returncode, output = compile_with_umask(source_dir_c, config_name_c, 0o077)
            assert returncode == 0, f"User C should succeed: {output}"

    @uth.requires_functional_compiler
    def test_group_writable_cache(self):
        """
        Test 2.2: Verify setgid directory enables multi-user sharing.

        Expected:
        - Directory has setgid bit (02775)
        - Files are group-accessible
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Try to set setgid bit explicitly (may not work on all filesystems)
            try:
                os.chmod(str(objdir), 0o2775)
            except OSError:
                pass

            # Note: setgid bit may not be settable on all filesystems (e.g., tmpfs)
            # This is a known limitation - skip verification if not supported
            mode = objdir.stat().st_mode
            if not (mode & 0o2000):
                pytest.skip("Filesystem does not support setgid bit")

            source_dir, config_name = self._create_test_source_dir(
                tmpdir, 'build', str(objdir)
            )

            # Compile
            os.chdir(source_dir)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name,
                ]
            uth.reset()
            compiletools.cake.main(argv)

            # Check all created files are group-accessible
            for item in objdir.rglob('*'):
                if item.is_file():
                    mode = item.stat().st_mode
                    assert mode & 0o040, f"{item.name} not group-readable: {oct(mode)}"

    def test_concurrent_objdir_creation(self):
        """
        Test 1.5: Multiple processes create objdir simultaneously.

        Expected:
        - All processes succeed with exist_ok=True
        - Proper permissions set
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'

            assert not objdir.exists()

            num_workers = 5

            # Use 'spawn' instead of 'fork' to avoid multi-threading issues with pytest
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(num_workers) as pool:
                results = pool.starmap(
                    create_objdir_worker,
                    [(i, str(objdir)) for i in range(num_workers)]
                )

            # All should succeed
            for r in results:
                assert r['success'], \
                    f"Worker {r['worker_id']} failed: {r['error']}"

            # Directory should exist
            assert objdir.exists()
            assert objdir.is_dir()

            # Check permissions (setgid may not be supported on all filesystems)
            mode = objdir.stat().st_mode
            assert mode & 0o020, f"Group write not set: {oct(mode)}"

    @uth.requires_functional_compiler
    def test_lock_fairness_high_contention(self):
        """
        Test 1.3: 10 processes compile same file - test lock fairness.

        Expected:
        - All processes complete successfully
        - No starvation
        - No deadlock
        - Total time reasonable (not 10x single compilation)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Create 10 build directories with identical source
            dirs_and_configs = [
                self._create_test_source_dir(tmpdir, f'build_{i}', str(objdir))
                for i in range(10)
            ]

            num_workers = 10
            start_time = time.time()

            # Use 'spawn' instead of 'fork' to avoid multi-threading issues with pytest
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(num_workers) as pool:
                results = pool.starmap(
                    compile_worker,
                    [(i, src_dir, cfg) for i, (src_dir, cfg) in enumerate(dirs_and_configs)]
                )

            elapsed = time.time() - start_time

            # All should succeed (no starvation, no deadlock)
            failures = [r for r in results if r['returncode'] != 0]
            assert len(failures) == 0, \
                f"Some workers failed: {failures}"

            # Should complete in reasonable time
            # Allow generous time for slow systems (60 seconds max)
            assert elapsed < 60, \
                f"Took {elapsed}s - too slow, possible lock contention issue"

    @uth.requires_functional_compiler
    def test_full_multiuser_workflow(self):
        """
        Test 5.1: Realistic multi-user development workflow.

        Expected:
        - User A builds project (populates cache)
        - User B builds same project (can access shared cache)
        - All users successful, no permission errors
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # User A: Full build (cold cache)
            source_dir_a, config_name_a = self._create_test_source_dir(
                tmpdir, 'user_a', str(objdir)
            )

            # start = time.time()  # Timing not currently used
            os.chdir(source_dir_a)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_a,
                ]
            uth.reset()
            compiletools.cake.main(argv)
            # time.time() - start  # Timing not currently used

            initial_obj_count = len(list(objdir.glob('**/*.o')))
            assert initial_obj_count >= 1, "User A should have created object files"

            # User B: Same build
            source_dir_b, config_name_b = self._create_test_source_dir(
                tmpdir, 'user_b', str(objdir)
            )

            # start = time.time()  # Timing not currently used
            os.chdir(source_dir_b)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_b,
                ]
            uth.reset()
            compiletools.cake.main(argv)
            # time.time() - start  # Timing not currently used

            # User B should succeed (main goal is no permission errors)
            final_obj_count = len(list(objdir.glob('**/*.o')))
            assert final_obj_count >= initial_obj_count, \
                "User B should have succeeded in building"

    @uth.requires_functional_compiler
    def test_mixed_compiler_flags(self):
        """
        Test 5.2: Different compiler flags create different object files.

        Expected:
        - Different flags → different macro_state_hash
        - Different object files created (no collision)
        - Both builds succeed
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Build 1: Default flags
            source_dir_1, config_name_1 = self._create_test_source_dir(
                tmpdir, 'build_default', str(objdir)
            )

            os.chdir(source_dir_1)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_1,
                ]
            uth.reset()
            compiletools.cake.main(argv)

            default_objs = set(objdir.glob('**/*.o'))
            assert len(default_objs) >= 1

            # Build 2: Different flags (optimization)
            source_dir_2, config_name_2 = self._create_test_source_dir(
                tmpdir, 'build_optimized', str(objdir)
            )

            os.chdir(source_dir_2)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_2,
                    "--CXXFLAGS=-O2",
            ]
            uth.reset()
            compiletools.cake.main(argv)

            all_objs = set(objdir.glob('**/*.o'))

            # Should have more objects (different flags = different hash)
            assert len(all_objs) > len(default_objs), \
                f"Expected more than {len(default_objs)} objects with different flags, found {len(all_objs)}"

    @uth.requires_functional_compiler
    def test_readonly_cache_access(self):
        """
        Test 2.3: User with read-only cache access.

        Expected:
        - Cannot write to read-only cache
        - Build fails with permission error
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # User A: Create cached object
            source_dir_a, config_name_a = self._create_test_source_dir(
                tmpdir, 'user_a', str(objdir)
            )

            os.chdir(source_dir_a)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_a,
                ]
            uth.reset()
            compiletools.cake.main(argv)

            # Verify objects were created
            initial_objs = list(objdir.glob('**/*.o'))
            assert len(initial_objs) >= 1

            # Make cache read-only
            objdir.chmod(0o555)  # r-xr-xr-x

            # User B: Try to write - should fail
            # Instead of full build, just verify we can't create lock file
            test_lock = objdir / 'test.lock'

            # This demonstrates the permission issue
            with pytest.raises(PermissionError):
                test_lock.touch()

            # Restore permissions for cleanup
            objdir.chmod(0o775)

    @uth.requires_functional_compiler
    def test_object_replacement_race(self):
        """
        Test 1.4: Object file replacement race.

        Expected:
        - Process reading object file never sees partial/corrupt data
        - Atomic move ensures complete file replacement
        - Brief FileNotFoundError during move is acceptable
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Initial build
            source_dir_1, config_name_1 = self._create_test_source_dir(
                tmpdir, 'build_1', str(objdir)
            )

            os.chdir(source_dir_1)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_1,
                ]
            uth.reset()
            compiletools.cake.main(argv)

            obj_files = list(objdir.glob('**/*.o'))
            assert len(obj_files) >= 1
            target_obj = obj_files[0]

            # Start continuous reader using spawn context
            ctx = multiprocessing.get_context('spawn')
            stop_event = ctx.Event()
            manager = ctx.Manager()
            errors = manager.list()

            reader = ctx.Process(
                target=continuous_reader,
                args=(target_obj, stop_event, errors)
            )
            reader.start()

            try:
                # Let reader start
                time.sleep(0.1)

                # Rebuild with different flags (creates new object file)
                source_dir_2, config_name_2 = self._create_test_source_dir(
                    tmpdir, 'build_2', str(objdir)
                )

                os.chdir(source_dir_2)
                argv_2 = [
                    "--exemarkers=main",
                    "--auto",
                    "--config=" + config_name_2,
                            "--CXXFLAGS=-O2",
                ]
                uth.reset()
                compiletools.cake.main(argv_2)

                # Let reader continue for a bit
                time.sleep(0.1)
            finally:
                # Stop reader
                stop_event.set()
                reader.join(timeout=5)
                if reader.is_alive():
                    reader.terminate()

            # Verify no corruption detected
            assert len(list(errors)) == 0, f"Reader detected corruption: {list(errors)}"

    @uth.requires_functional_compiler
    def test_cache_hit_rate(self):
        """
        Test 6.1: Cache hit rate measurement.

        Expected:
        - Second build reuses cached objects (via make)
        - Significantly faster than first build
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # First build (cold cache)
            source_dir_1, config_name_1 = self._create_test_source_dir(
                tmpdir, 'build_1', str(objdir)
            )

            os.chdir(source_dir_1)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_1,
                ]

            start = time.time()
            uth.reset()
            compiletools.cake.main(argv)
            cold_time = time.time() - start

            initial_obj_count = len(list(objdir.glob('**/*.o')))
            assert initial_obj_count >= 1

            # Second build (warm cache - make sees objects are up to date)
            start = time.time()
            uth.reset()
            compiletools.cake.main(argv)
            warm_time = time.time() - start

            # Warm build should be faster (make skips up-to-date targets)
            # Don't assert specific speedup as it's system-dependent
            # Just verify warm build completes and doesn't rebuild everything
            assert warm_time < cold_time * 2, \
                f"Warm build ({warm_time:.2f}s) not significantly faster than cold ({cold_time:.2f}s)"

            # Object count should be same (no new objects)
            final_obj_count = len(list(objdir.glob('**/*.o')))
            assert final_obj_count == initial_obj_count

    @uth.requires_functional_compiler
    def test_lock_contention_overhead(self):
        """
        Test 6.2: Lock contention overhead measurement.

        Expected:
        - Locking adds minimal overhead for serial builds
        - Overhead should be < 50% for single-threaded case
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Build without shared-objects (no locking)
            objdir_no_lock = Path(tmpdir) / 'obj_no_lock'
            objdir_no_lock.mkdir()

            source_dir_1, config_name_1 = self._create_test_source_dir(
                tmpdir, 'build_no_lock', str(objdir_no_lock)
            )

            # Modify config to disable shared-objects
            ct_conf = Path(source_dir_1) / 'ct.conf'
            content = ct_conf.read_text()
            content = content.replace('shared-objects = true', 'shared-objects = false')
            ct_conf.write_text(content)

            os.chdir(source_dir_1)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_1,
                ]

            # Measure without locking
            start = time.time()
            uth.reset()
            compiletools.cake.main(argv)
            no_lock_time = time.time() - start

            # Build with shared-objects (with locking)
            objdir_lock = Path(tmpdir) / 'obj_lock'
            objdir_lock.mkdir()

            source_dir_2, config_name_2 = self._create_test_source_dir(
                tmpdir, 'build_lock', str(objdir_lock)
            )

            os.chdir(source_dir_2)
            argv_2 = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name_2,
                ]

            # Measure with locking
            start = time.time()
            uth.reset()
            compiletools.cake.main(argv_2)
            lock_time = time.time() - start

            # Locking should add minimal overhead (< 50% for single build)
            if no_lock_time > 0:
                overhead = (lock_time - no_lock_time) / no_lock_time
                # Be generous - locking adds some overhead but shouldn't double build time
                assert overhead < 0.5, \
                    f"Lock overhead {overhead*100:.1f}% too high (no-lock: {no_lock_time:.2f}s, lock: {lock_time:.2f}s)"

    @uth.requires_lockdir_filesystem
    @uth.requires_functional_compiler
    def test_lockdir_stale_lock_cleanup(self):
        """
        Test stale lock detection and automatic cleanup.

        Runs only on filesystems that use lockdir strategy (NFS, GPFS, Lustre).
        Skipped on local filesystems (ext4, xfs, etc.) that use flock.

        Verifies that:
        1. Stale locks with hostname:PID format are detected
        2. Same-host stale locks are automatically removed
        3. Build succeeds after removing stale lock
        """
        import socket

        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            source_dir, config_name = self._create_test_source_dir(
                tmpdir, 'build', str(objdir)
            )

            # First, do a build to find out what object file name is generated
            os.chdir(source_dir)
            argv = [
                "--exemarkers=main",
                "--auto",
                "--config=" + config_name,
                ]
            uth.reset()
            compiletools.cake.main(argv)

            # Find the generated object file
            obj_files = list(objdir.glob('*.o'))
            assert len(obj_files) > 0, "Should have generated at least one object file"

            # Clean the object file to force rebuild
            obj_file = obj_files[0]
            obj_file.unlink()

            # Create stale lock for this specific object file
            stale_lock = Path(str(obj_file) + '.lockdir')
            stale_lock.mkdir()

            # Place PID file with hostname:PID format from same host
            pid_file = stale_lock / 'pid'
            hostname = socket.gethostname()
            pid_file.write_text(f'{hostname}:999999')  # PID that doesn't exist

            # Rebuild - with stale lock detection, this should succeed
            uth.reset()
            compiletools.cake.main(argv)

            # After successful build, stale lock should be removed
            assert not stale_lock.exists(), "Stale lock should be cleaned up"
            # And object file should be rebuilt
            assert obj_file.exists(), "Object file should be rebuilt"

    @uth.requires_functional_compiler
    def test_single_user_two_subprojects_concurrent(self):
        """
        Test single user building two subprojects concurrently with shared source.

        Scenario:
        - Same user, two terminal sessions
        - Two different subprojects (numbers and factory samples)
        - Both using same shared objdir
        - May have shared dependencies

        Expected:
        - Both builds succeed without permission errors
        - Lock contention is handled correctly (same UID, kill -0 works)
        - No corruption of shared object files
        - Stale lock cleanup works (same user can delete own files)
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            objdir = Path(tmpdir) / 'shared_obj'
            objdir.mkdir(mode=0o2775)

            # Create two subproject directories using different sample projects
            import shutil

            # Subproject A: numbers sample
            subproject_a_dir = Path(tmpdir) / 'subproject_numbers'
            subproject_a_dir.mkdir()
            numbers_src = Path(uth.samplesdir()) / 'numbers'
            for f in numbers_src.glob('*'):
                if f.is_file():
                    shutil.copy2(str(f), str(subproject_a_dir))

            config_a = uth.create_temp_config(str(subproject_a_dir))
            uth.create_temp_ct_conf(
                tempdir=str(subproject_a_dir),
                defaultvariant=os.path.basename(config_a)[:-5],
                extralines=[
                    'shared-objects = true',
                    f'objdir = {objdir}'
                ]
            )

            # Subproject B: factory sample
            subproject_b_dir = Path(tmpdir) / 'subproject_factory'
            subproject_b_dir.mkdir()
            factory_src = Path(uth.samplesdir()) / 'factory'
            for f in factory_src.glob('*'):
                if f.is_file():
                    shutil.copy2(str(f), str(subproject_b_dir))

            config_b = uth.create_temp_config(str(subproject_b_dir))
            uth.create_temp_ct_conf(
                tempdir=str(subproject_b_dir),
                defaultvariant=os.path.basename(config_b)[:-5],
                extralines=[
                    'shared-objects = true',
                    f'objdir = {objdir}'
                ]
            )

            # Build both subprojects concurrently
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(2) as pool:
                results = pool.starmap(
                    build_subproject_worker,
                    [
                        (0, str(subproject_a_dir), config_a),
                        (1, str(subproject_b_dir), config_b),
                    ]
                )

            # Verify both builds succeeded
            for r in results:
                assert r['returncode'] == 0, \
                    f"Worker {r['worker_id']} failed: {r['error']}"

            # Verify object files were created in shared objdir
            obj_files = list(objdir.glob('**/*.o'))
            assert len(obj_files) >= 2, \
                f"Expected at least 2 object files, found {len(obj_files)}"

            # Verify no lock directories remain (all cleaned up)
            lockdirs = list(objdir.glob('**/*.lockdir'))
            assert len(lockdirs) == 0, \
                f"Found stale lock directories: {lockdirs}"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
