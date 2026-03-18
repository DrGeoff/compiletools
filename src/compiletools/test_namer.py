import os
import tempfile

import configargparse

import compiletools.apptools
import compiletools.configutils
import compiletools.namer
import compiletools.testhelper as uth


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

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create temp files
            src_file = os.path.join(tmpdir, "test.cpp")
            h1_file = os.path.join(tmpdir, "foo.h")
            h2_file = os.path.join(tmpdir, "bar.h")
            h3_file = os.path.join(tmpdir, "baz.h")

            with open(src_file, "wb") as f:
                f.write(b"int main() { return 0; }")
            with open(h1_file, "wb") as f:
                f.write(b"#define FOO 1")
            with open(h2_file, "wb") as f:
                f.write(b"#define BAR 2")
            with open(h3_file, "wb") as f:
                f.write(b"#define BAZ 3")

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
            assert obj2.endswith(".o"), f"Object should end with .o: {obj2}"
            obj_without_ext = obj2[:-2]  # Remove .o

            # Split and find the hashes by their known lengths from the end
            # Format: {basename}_{file_hash_12}_{dep_hash_14}_{macro_hash_16}.o
            # Last 16 chars before extension is macro hash
            # Previous 14 chars is dep hash
            # Previous 12 chars is file hash
            # Everything before is basename (may contain underscores)
            assert len(obj_without_ext) >= 1 + 12 + 1 + 14 + 1 + 16, f"Object name too short: {obj2}"

            # Extract from right to left
            macro_hash = obj_without_ext[-16:]
            dep_hash_extracted = obj_without_ext[-17 - 14 : -17]
            file_hash_extracted = obj_without_ext[-18 - 14 - 12 : -18 - 14]

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
        uth.reset()


def test_dep_hash_xor_properties():
    """Verify dependency hash uses correct XOR algorithm with proper properties."""
    uth.reset()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            h1_file = os.path.join(tmpdir, "foo.h")
            h2_file = os.path.join(tmpdir, "bar.h")

            with open(h1_file, "wb") as f:
                f.write(b"#define FOO 1")
            with open(h2_file, "wb") as f:
                f.write(b"#define BAR 2")

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
            assert hash_single != "00000000000000", "Hash of real file should not be zero"
            assert hash_ab != "00000000000000", "Hash of multiple files should not be zero"

            # Test 4: XOR identity with empty list
            hash_empty = namer.compute_dep_hash([])
            assert hash_empty == "00000000000000", "Empty dependency list should give zero hash"

            # Test 5: Hash is valid hex
            assert len(hash_ab) == 14, "Hash should be 14 characters"
            try:
                int(hash_ab, 16)
            except ValueError:
                assert False, f"Hash must be valid hex: {hash_ab}"

    finally:
        uth.reset()


def test_dep_hash_handles_missing_generated_headers():
    """Verify compute_dep_hash handles missing files (generated headers) gracefully."""
    uth.reset()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create real header
            h1_file = os.path.join(tmpdir, "real.h")
            with open(h1_file, "wb") as f:
                f.write(b"#define REAL 1")

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

            missing_gen = os.path.join(tmpdir, "generated.h")

            # Test: Mix real and missing files - should not raise FileNotFoundError
            deps_with_missing = [h1_file, missing_gen]
            hash_before = namer.compute_dep_hash(deps_with_missing)

            assert len(hash_before) == 14, "Should return valid hash despite missing file"
            assert hash_before != "00000000000000", "Hash should include real file"

            # When generated file appears, hash should change
            with open(missing_gen, "w") as f:
                f.write("#define GENERATED 1")

            hash_after = namer.compute_dep_hash([h1_file, missing_gen])
            assert hash_before != hash_after, "Hash must change when generated file appears"

            # Verify both hashes are valid
            assert len(hash_after) == 14, "Hash after generation should be valid"
            assert hash_after != "00000000000000", "Hash should not be zero"

            # Test with only missing files
            missing_only = [missing_gen + "_nonexistent"]
            hash_missing_only = namer.compute_dep_hash(missing_only)
            assert hash_missing_only == "00000000000000", "Hash of only missing files should be zero"

    finally:
        uth.reset()


@uth.requires_functional_compiler
def test_source_magic_produces_different_hash_with_different_flags():
    """Regression: //#SOURCE= expanded files must get different macro_state_hash when flags differ.

    This guards against a regression in hunter._extractSOURCE where calling
    get_structured_data() instead of magicflags() would skip _parse() and leave
    _final_macro_states unpopulated for SOURCE-expanded files.
    """
    uth.reset()

    try:
        import compiletools.headerdeps
        import compiletools.hunter
        import compiletools.magicflags
        import compiletools.preprocessing_cache

        src = uth.write_sources(
            {
                "main.cpp": "//#SOURCE=helper.cpp\nint main() { return 0; }\n",
                "helper.cpp": "void helper() {}\n",
            }
        )
        main_file = str(src["main.cpp"])
        helper_file = str(src["helper.cpp"])

        def create_hunter_with_cppflags(cppflags_value):
            """Create a Hunter instance with specific CPPFLAGS."""
            cap = configargparse.getArgumentParser()
            compiletools.hunter.add_arguments(cap)
            argv = [f"--append-CPPFLAGS={cppflags_value}", "-q"]
            args = compiletools.apptools.parseargs(cap, argv)
            hdeps = compiletools.headerdeps.create(args)
            magic = compiletools.magicflags.create(args, hdeps)
            return compiletools.hunter.Hunter(args, hdeps, magic), magic

        # Config 1: trigger _extractSOURCE via required_source_files
        hunter1, magic1 = create_hunter_with_cppflags("-I/opt/libfoo/v1/include")
        sources1 = hunter1.required_source_files(main_file)
        assert any(f.endswith("helper.cpp") for f in sources1), (
            f"Hunter should expand //#SOURCE=helper.cpp, got: {sources1}"
        )
        hash1_main = magic1.get_final_macro_state_hash(main_file)
        hash1_helper = magic1.get_final_macro_state_hash(helper_file)

        # Clear caches between configurations
        compiletools.hunter.Hunter.clear_cache()
        compiletools.preprocessing_cache.clear_cache()

        # Config 2: same files, different CPPFLAGS
        hunter2, magic2 = create_hunter_with_cppflags("-I/opt/libfoo/v2/include")
        hunter2.required_source_files(main_file)
        hash2_main = magic2.get_final_macro_state_hash(main_file)
        hash2_helper = magic2.get_final_macro_state_hash(helper_file)

        assert hash1_main != hash2_main, (
            f"Different CPPFLAGS must produce different hash for main: {hash1_main} vs {hash2_main}"
        )
        assert hash1_helper != hash2_helper, (
            f"Different CPPFLAGS must produce different hash for SOURCE-expanded helper: "
            f"{hash1_helper} vs {hash2_helper}"
        )

    finally:
        uth.reset()


@uth.requires_functional_compiler
def test_different_cppflags_produce_different_object_names():
    """Full chain: different CPPFLAGS -> different macro_state_hash -> different object names."""
    uth.reset()

    try:
        import compiletools.preprocessing_cache
        import compiletools.test_base as tb

        # Create a source file
        src = uth.write_sources({"test_objname.cpp": "int main() { return 0; }\n"})
        source_file = str(src["test_objname.cpp"])

        with tempfile.TemporaryDirectory() as tmpdir:
            # Build two magicflag parsers with different CPPFLAGS
            parser1 = tb.create_magic_parser(
                ["--magic=direct", "--append-CPPFLAGS=-I/opt/libfoo/v1/include"], tempdir=tmpdir
            )
            parser1.parse(source_file)
            hash1 = parser1.get_final_macro_state_hash(source_file)

            compiletools.preprocessing_cache.clear_cache()
            parser2 = tb.create_magic_parser(
                ["--magic=direct", "--append-CPPFLAGS=-I/opt/libfoo/v2/include"], tempdir=tmpdir
            )
            parser2.parse(source_file)
            hash2 = parser2.get_final_macro_state_hash(source_file)

            assert hash1 != hash2, f"Different CPPFLAGS must produce different macro_state_hash: {hash1} vs {hash2}"

        # Reset parser state before creating namer's parser
        uth.reset()

        # Setup namer
        config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
        config_files = [os.path.join(config_dir, "gcc.debug.conf")]
        cap = configargparse.getArgumentParser(
            description="TestNamerCPPFLAGS",
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

        dep_hash = namer.compute_dep_hash([])

        obj1 = namer.object_pathname(source_file, hash1, dep_hash)
        obj2 = namer.object_pathname(source_file, hash2, dep_hash)

        assert obj1 != obj2, f"Different CPPFLAGS must produce different object paths: {obj1} vs {obj2}"

    finally:
        uth.reset()
