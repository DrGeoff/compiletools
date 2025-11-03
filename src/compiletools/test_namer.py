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
