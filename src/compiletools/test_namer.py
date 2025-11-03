import os
import tempfile
import configargparse
import compiletools.testhelper as uth
import compiletools.namer
import compiletools.configutils
import compiletools.apptools


def test_executable_pathname():
    uth.reset()
    
    try:
        config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
        config_files = [os.path.join(config_dir, "gcc.debug.conf")]
        cap = configargparse.getArgumentParser(
            description="TestNamer",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        argv = ["--no-git-root"]
        compiletools.apptools.add_common_arguments(cap=cap, argv=argv, variant="gcc.debug")
        compiletools.namer.Namer.add_arguments(cap=cap, argv=argv, variant="gcc.debug")
        args = compiletools.apptools.parseargs(cap, argv)
        namer = compiletools.namer.Namer(args, argv=argv, variant="gcc.debug")
        exename = namer.executable_pathname("/home/user/code/my.cpp")
        assert exename == "bin/gcc.debug/my"
    finally:
        uth.reset()


def test_object_name_with_dependencies():
    """Test that object naming includes dependency hash."""
    uth.reset()

    # Create real temp files (avoid FileNotFoundError from get_file_hash)
    src_file = None
    h1_file = None
    h2_file = None
    h3_file = None

    try:
        # Create temp source file
        src_fd, src_file = tempfile.mkstemp(suffix='.cpp')
        os.write(src_fd, b"int main() { return 0; }")
        os.close(src_fd)

        # Create temp header files
        h1_fd, h1_file = tempfile.mkstemp(suffix='.h')
        os.write(h1_fd, b"#define FOO 1")
        os.close(h1_fd)

        h2_fd, h2_file = tempfile.mkstemp(suffix='.h')
        os.write(h2_fd, b"#define BAR 2")
        os.close(h2_fd)

        h3_fd, h3_file = tempfile.mkstemp(suffix='.h')
        os.write(h3_fd, b"#define BAZ 3")
        os.close(h3_fd)

        # Setup namer
        config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
        config_files = [os.path.join(config_dir, "gcc.debug.conf")]
        cap = configargparse.getArgumentParser(
            description="TestNamer",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        argv = ["--no-git-root"]
        compiletools.apptools.add_common_arguments(cap=cap, argv=argv, variant="gcc.debug")
        compiletools.namer.Namer.add_arguments(cap=cap, argv=argv, variant="gcc.debug")
        args = compiletools.apptools.parseargs(cap, argv)
        namer = compiletools.namer.Namer(args, argv=argv, variant="gcc.debug")

        # Test with no dependencies
        dep_hash_empty = namer.compute_dep_hash([])
        assert dep_hash_empty == "00000000000000", f"Empty dep hash should be all zeros, got {dep_hash_empty}"

        obj1 = namer.object_name(src_file, "0123456789abcdef", dep_hash_empty)
        assert "_00000000000000_" in obj1, f"Empty dep hash not in object name: {obj1}"

        # Test with dependencies
        deps = [h1_file, h2_file]
        dep_hash = namer.compute_dep_hash(deps)
        assert len(dep_hash) == 14, f"Dep hash should be 14 chars, got {len(dep_hash)}"

        obj2 = namer.object_name(src_file, "0123456789abcdef", dep_hash)

        # Verify format: basename_12chars_14chars_16chars.o
        # Note: basename might contain underscores, so we match from the end
        assert obj2.endswith('.o'), f"Object should end with .o: {obj2}"
        obj_without_ext = obj2[:-2]  # Remove .o

        # Split and find the hashes by their known lengths from the end
        # Format: {basename}_{file_hash_12}_{dep_hash_14}_{macro_hash_16}.o
        # Last 16 chars before extension is macro hash
        # Previous 14 chars is dep hash
        # Previous 12 chars is file hash
        # Everything before is basename (may contain underscores)
        assert len(obj_without_ext) >= 1+12+1+14+1+16, f"Object name too short: {obj2}"

        # Extract from right to left
        macro_hash = obj_without_ext[-16:]
        dep_hash_extracted = obj_without_ext[-17-14:-17]
        file_hash_extracted = obj_without_ext[-18-14-12:-18-14]

        assert len(file_hash_extracted) == 12, f"file hash should be 12 chars: {file_hash_extracted}"
        assert len(dep_hash_extracted) == 14, f"dep hash should be 14 chars (MIDDLE): {dep_hash_extracted}"
        assert len(macro_hash) == 16, f"macro hash should be 16 chars: {macro_hash}"
        assert macro_hash == "0123456789abcdef", f"macro hash mismatch: {macro_hash}"
        assert dep_hash_extracted == dep_hash, f"dep hash mismatch: {dep_hash_extracted} vs {dep_hash}"

        # Test order independence (XOR is commutative + sorting)
        deps_reversed = list(reversed(deps))
        dep_hash_reversed = namer.compute_dep_hash(deps_reversed)
        assert dep_hash == dep_hash_reversed, "Dep hash should be order-independent"

        # Test different dependencies produce different hash
        deps_different = [h3_file]
        dep_hash_different = namer.compute_dep_hash(deps_different)
        assert dep_hash != dep_hash_different, "Different deps should produce different hash"

    finally:
        # Cleanup temp files
        for f in [src_file, h1_file, h2_file, h3_file]:
            if f and os.path.exists(f):
                os.unlink(f)
        uth.reset()


def test_dep_hash_xor_properties():
    """Verify dependency hash uses correct XOR algorithm with proper properties."""
    uth.reset()

    h1_file = None
    h2_file = None

    try:
        # Create test files
        h1_fd, h1_file = tempfile.mkstemp(suffix='.h')
        os.write(h1_fd, b"#define FOO 1")
        os.close(h1_fd)

        h2_fd, h2_file = tempfile.mkstemp(suffix='.h')
        os.write(h2_fd, b"#define BAR 2")
        os.close(h2_fd)

        # Setup namer
        config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
        config_files = [os.path.join(config_dir, "gcc.debug.conf")]
        cap = configargparse.getArgumentParser(
            description="TestNamer",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        argv = ["--no-git-root"]
        compiletools.apptools.add_common_arguments(cap=cap, argv=argv, variant="gcc.debug")
        compiletools.namer.Namer.add_arguments(cap=cap, argv=argv, variant="gcc.debug")
        args = compiletools.apptools.parseargs(cap, argv)
        namer = compiletools.namer.Namer(args, argv=argv, variant="gcc.debug")

        # Test 1: XOR commutativity A⊕B = B⊕A (order-independent via sorting)
        hash_ab = namer.compute_dep_hash([h1_file, h2_file])
        hash_ba = namer.compute_dep_hash([h2_file, h1_file])
        assert hash_ab == hash_ba, "XOR must be order-independent (commutative + sorted)"

        # Test 2: Deduplication (A⊕A should equal A after deduplication)
        hash_single = namer.compute_dep_hash([h1_file])
        hash_dup = namer.compute_dep_hash([h1_file, h1_file])
        assert hash_single == hash_dup, "Duplicates should be removed before XOR"

        # Test 3: Non-zero for real files
        assert hash_single != '00000000000000', "Hash of real file should not be zero"
        assert hash_ab != '00000000000000', "Hash of multiple files should not be zero"

        # Test 4: XOR identity with empty list
        hash_empty = namer.compute_dep_hash([])
        assert hash_empty == '00000000000000', "Empty dependency list should give zero hash"

        # Test 5: Hash is valid hex
        assert len(hash_ab) == 14, "Hash should be 14 characters"
        try:
            int(hash_ab, 16)
        except ValueError:
            assert False, f"Hash must be valid hex: {hash_ab}"

    finally:
        # Cleanup temp files
        for f in [h1_file, h2_file]:
            if f and os.path.exists(f):
                os.unlink(f)
        uth.reset()


def test_dep_hash_handles_missing_generated_headers():
    """Verify compute_dep_hash handles missing files (generated headers) gracefully."""
    uth.reset()

    h1_file = None
    tmpdir = None

    try:
        # Create real header
        h1_fd, h1_file = tempfile.mkstemp(suffix='.h')
        os.write(h1_fd, b"#define REAL 1")
        os.close(h1_fd)

        # Setup namer
        config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
        config_files = [os.path.join(config_dir, "gcc.debug.conf")]
        cap = configargparse.getArgumentParser(
            description="TestNamer",
            formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
            default_config_files=config_files,
            args_for_setting_config_path=["-c", "--config"],
            ignore_unknown_config_file_keys=True,
        )
        argv = ["--no-git-root"]
        compiletools.apptools.add_common_arguments(cap=cap, argv=argv, variant="gcc.debug")
        compiletools.namer.Namer.add_arguments(cap=cap, argv=argv, variant="gcc.debug")
        args = compiletools.apptools.parseargs(cap, argv)
        namer = compiletools.namer.Namer(args, argv=argv, variant="gcc.debug")

        # Create temp directory for "generated" file
        tmpdir = tempfile.mkdtemp()
        missing_gen = os.path.join(tmpdir, 'generated.h')

        # Test: Mix real and missing files - should not raise FileNotFoundError
        deps_with_missing = [h1_file, missing_gen]
        hash_before = namer.compute_dep_hash(deps_with_missing)

        assert len(hash_before) == 14, "Should return valid hash despite missing file"
        assert hash_before != '00000000000000', "Hash should include real file"

        # When generated file appears, hash should change
        with open(missing_gen, 'w') as f:
            f.write('#define GENERATED 1')

        hash_after = namer.compute_dep_hash([h1_file, missing_gen])
        assert hash_before != hash_after, "Hash must change when generated file appears"

        # Verify both hashes are valid
        assert len(hash_after) == 14, "Hash after generation should be valid"
        assert hash_after != '00000000000000', "Hash should not be zero"

        # Test with only missing files
        missing_only = [missing_gen + '_nonexistent']
        hash_missing_only = namer.compute_dep_hash(missing_only)
        assert hash_missing_only == '00000000000000', "Hash of only missing files should be zero"

    finally:
        if h1_file and os.path.exists(h1_file):
            os.unlink(h1_file)
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        uth.reset()
