"""Test for shared object cache with low mtime resolution filesystems.

This test exposes a bug where header dependency changes are not detected
when timestamps have coarse granularity (FAT32, some NFS, fast builds).

The bug occurs because:
1. Object file naming only includes source file hash and macro state hash
2. Header dependency content hashes are NOT included in object filename
3. Make relies on mtime comparison which fails when timestamps are equal

This is a real issue on:
- FAT32 filesystems (2-second mtime resolution)
- Some NFS configurations (1-second resolution)
- Fast incremental builds completing in < 1 second
- CI/CD systems with shared build caches
"""

import os
import time
import shutil
import tempfile
import subprocess
from pathlib import Path

import compiletools.testhelper as uth
from compiletools.test_base import BaseCompileToolsTestCase


def set_mtime_to_second(path, timestamp=None):
    """Set file mtime rounded to whole seconds (simulating FAT32/NFS).

    Args:
        path: File path to modify
        timestamp: Optional timestamp to use, otherwise uses current time rounded down

    Returns:
        The timestamp that was set (as integer seconds)
    """
    if timestamp is None:
        timestamp = int(time.time())
    else:
        timestamp = int(timestamp)

    os.utime(path, (timestamp, timestamp))
    return timestamp


@uth.with_group_writable_umask
class TestSharedCacheLowMtimeResolution(BaseCompileToolsTestCase):
    """Tests for shared object cache behavior on low mtime resolution filesystems."""

    def _create_worktree_test_env(self, tmpdir, shared_objdir):
        """Create git worktree test environment with shared objdir.

        Returns:
            Tuple of (main_repo_dir, worktree_dir, config_name)
        """
        tmpdir = Path(tmpdir)
        shared_objdir = Path(shared_objdir)

        # Main repo directory
        main_repo = tmpdir / "main_repo"
        main_repo.mkdir()

        # Copy hunter_macro_propagation sample (includes config.h -> renderer.h chain)
        sample_dir = Path(uth.samplesdir()) / "hunter_macro_propagation"
        for src_file in ["app.cpp", "config.h", "renderer.h"]:
            shutil.copy(sample_dir / src_file, main_repo / src_file)

        # Initialize git repo
        subprocess.run(['git', 'init'], cwd=main_repo, capture_output=True, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=main_repo, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=main_repo, check=True)
        subprocess.run(['git', 'add', '.'], cwd=main_repo, capture_output=True, check=True)
        subprocess.run(['git', 'commit', '-m', 'Initial'], cwd=main_repo, capture_output=True, check=True)

        # Create worktree for User B
        worktree_dir = tmpdir / "user_b_worktree"
        result = subprocess.run(
            ['git', 'worktree', 'add', str(worktree_dir), 'HEAD'],
            cwd=main_repo,
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"Failed to create worktree: {result.stderr}"

        # Create config with shared objdir and fixed project version
        config_name = uth.create_temp_config(str(main_repo))
        uth.create_temp_ct_conf(
            tempdir=str(main_repo),
            defaultvariant=os.path.basename(config_name)[:-5],
            extralines=[
                'shared-objects = true',
                f'objdir = {shared_objdir}'
            ]
        )

        # Copy config to worktree
        shutil.copy(main_repo / "ct.conf", worktree_dir / "ct.conf")
        shutil.copy(config_name, worktree_dir / os.path.basename(config_name))

        return str(main_repo), str(worktree_dir), config_name

    def _run_cake_build(self, work_dir, config_name):
        """Run ct-cake in the given directory."""
        import compiletools.cake

        os.chdir(work_dir)
        argv = [
            "--exemarkers=main",
            "--auto",
            "--config=" + config_name,
                        "--project-version=test-1.0.0",  # Fixed version for same macro hash
        ]
        uth.reset()
        compiletools.cake.main(argv)

    @uth.requires_functional_compiler
    def test_header_change_not_detected_with_equal_mtime(self):
        """Test that header changes are missed when object and header mtimes are equal.

        Scenario:
        1. User A compiles app.cpp (includes config.h -> renderer.h)
        2. Object file created with mtime T
        3. User B edits renderer.h in worktree, mtime set to T (same second)
        4. User B builds - Make sees object_mtime >= header_mtime
        5. BUG: Make skips rebuild, User B gets stale object

        This simulates:
        - FAT32 filesystems (2-second resolution)
        - NFS with coarse timestamps
        - Fast builds completing in < 1 second
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            shared_objdir = Path(tmpdir) / "shared_obj"
            shared_objdir.mkdir(mode=0o2775)

            main_repo, worktree, config_name = self._create_worktree_test_env(
                tmpdir, shared_objdir
            )

            # Pick a specific timestamp for our simulation
            base_timestamp = int(time.time())

            # Set all source files to the same timestamp
            for repo_dir in [main_repo, worktree]:
                for src_file in ["app.cpp", "config.h", "renderer.h"]:
                    set_mtime_to_second(os.path.join(repo_dir, src_file), base_timestamp)

            # User A: Initial build
            self._run_cake_build(main_repo, config_name)

            # Capture object files BEFORE header change
            obj_files_before = list(shared_objdir.glob("*.o"))
            assert len(obj_files_before) == 1, f"Expected 1 object file, found {len(obj_files_before)}"

            obj_file_before = obj_files_before[0]
            obj_name_before = obj_file_before.name
            obj_size_before = obj_file_before.stat().st_size
            assert obj_size_before > 0, "Object file should not be empty"

            # CRITICAL: Set object mtime to same timestamp as sources
            # This simulates low-resolution filesystem or fast build
            set_mtime_to_second(obj_file_before, base_timestamp)
            obj_mtime = os.path.getmtime(obj_file_before)

            # User B: Edit renderer.h
            renderer_h = Path(worktree) / "renderer.h"
            original_content = renderer_h.read_text()

            # Modify the file
            with open(renderer_h, 'a') as f:
                f.write("\nvoid new_function();  // This change will be missed!\n")

            modified_content = renderer_h.read_text()
            assert original_content != modified_content, "File should be modified"

            # CRITICAL: Set renderer.h mtime to SAME timestamp as object
            # On low-resolution FS, this happens when edit is in same second as build
            set_mtime_to_second(renderer_h, base_timestamp)
            header_mtime = os.path.getmtime(renderer_h)

            # Verify timestamps are equal
            assert int(obj_mtime) == int(header_mtime), \
                "Object and header mtimes should be equal"

            # Commit the change in worktree
            subprocess.run(['git', 'add', 'renderer.h'], cwd=worktree, capture_output=True, check=True)
            subprocess.run(['git', 'commit', '-m', 'Edit renderer.h'], cwd=worktree, capture_output=True, check=True)

            # Verify content actually changed
            hash_main = subprocess.run(
                ['git', 'hash-object', 'renderer.h'],
                cwd=main_repo,
                capture_output=True,
                text=True
            ).stdout.strip()
            hash_worktree = subprocess.run(
                ['git', 'hash-object', 'renderer.h'],
                cwd=worktree,
                capture_output=True,
                text=True
            ).stdout.strip()
            assert hash_main != hash_worktree, "renderer.h content should differ"

            # User B: Build after editing renderer.h
            self._run_cake_build(worktree, config_name)

            # Capture object files AFTER rebuild
            obj_files_after = list(shared_objdir.glob("*.o"))

            # The test: Was a NEW object created with different name?
            new_obj_files = [f for f in obj_files_after if f.name != obj_name_before]

            if not new_obj_files:
                # BUG NOT FIXED: Still using old object name
                assert False, (
                    f"BUG: No new object created despite header change!\n"
                    f"Old object: {obj_name_before}\n"
                    f"All objects: {[f.name for f in obj_files_after]}\n"
                    f"Expected: New object with different dependency hash\n"
                )

            # SUCCESS: New object created
            assert len(new_obj_files) == 1, f"Expected 1 new object, found {len(new_obj_files)}"
            new_obj = new_obj_files[0]

            # Verify new object has different hash in middle position (dep_hash changed)
            old_parts = obj_name_before.replace('.o', '').split('_')
            new_parts = new_obj.name.replace('.o', '').split('_')

            # Both should have new format (4 parts) since our fix is already applied
            assert len(old_parts) == 4, f"Should have 4 parts: {old_parts}"
            assert len(new_parts) == 4, f"Should have 4 parts: {new_parts}"

            assert new_parts[0] == old_parts[0], "Basename should match"
            assert new_parts[1] == old_parts[1], "File hash should match (source unchanged)"
            # The key test: dep_hash (middle position) should be DIFFERENT
            assert new_parts[2] != old_parts[2], f"Dep hash should differ: {old_parts[2]} vs {new_parts[2]}"
            assert len(new_parts[2]) == 14, f"Dep hash should be 14 chars: {new_parts[2]}"
            assert new_parts[2] != '00000000000000', "Dep hash should not be zero (has dependencies)"

            # Additional verification: Ensure hashes are valid hex (algorithm correctness)
            try:
                int(old_parts[2], 16)
                int(new_parts[2], 16)
            except ValueError as e:
                assert False, f"Dep hash must be valid hex: {e}"

            # Both objects should coexist in shared cache
            assert len(obj_files_after) == 2, f"Expected both old and new objects, found {len(obj_files_after)}"
            assert obj_file_before in obj_files_after, "Old object should still exist"
