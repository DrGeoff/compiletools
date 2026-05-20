import os
import tempfile

import configargparse

import compiletools.apptools
import compiletools.headerdeps
import compiletools.hunter
import compiletools.magicflags
import compiletools.namer
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


def _make_namer(description):
    """Build (args, namer) for the bundled ``gcc.debug`` variant.

    The args/namer share a single BuildContext, mirroring the production
    create_parser → parseargs → Namer flow.
    """
    config_dir = os.path.join(uth.cakedir(), "ct.conf.d")
    config_files = [os.path.join(config_dir, "gcc.debug.conf")]
    cap = configargparse.ArgumentParser(
        conflict_handler="resolve",
        description=description,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        default_config_files=config_files,
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
    )
    argv = ["--no-git-root"]
    compiletools.apptools.add_common_arguments(cap=cap, argv=argv, variant="gcc.debug")
    compiletools.namer.Namer.add_arguments(cap=cap, argv=argv, variant="gcc.debug")
    ctx = BuildContext()
    args = compiletools.apptools.parseargs(cap, argv, context=ctx)
    namer = compiletools.namer.Namer(args, argv=argv, variant="gcc.debug", context=ctx)
    return args, namer


def test_executable_pathname():
    uth.reset()
    try:
        _args, namer = _make_namer("TestNamer")
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
            _args, namer = _make_namer("TestNamer")

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
            _args, namer = _make_namer("TestNamer")

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
            _args, namer = _make_namer("TestNamer")

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


def test_object_pathname_is_sharded_by_file_hash():
    """``object_pathname`` returns ``<objdir>/<file_hash[:2]>/<basename>_<...>.o``.

    Sharding splits writes/renames across 256 directory inodes so that
    no single parent dir grows past its filesystem's per-directory
    sweet spot. The bucket key is the leading 2 hex chars of the
    per-source ``file_hash`` (the high-entropy field already in the
    filename), so every artifact for one source — ``.o``, ``.lock``,
    ``.lockdir``, ``.tmp`` — collapses into the same bucket as a
    side-effect of being derived from the same target path string.

    Identical layout on local and shared caches; the cost is sub-µs
    per ``object_pathname`` and bucket dirs are created lazily by the
    build, so an empty private cache stays empty until the first
    compile lands.
    """
    uth.reset()

    try:
        with tempfile.TemporaryDirectory() as srcdir:
            src = uth.write_sources(
                {"shardme.cpp": "int main() { return 0; }\n"},
                target_dir=srcdir,
            )
            source_file = str(src["shardme.cpp"])

            args, namer = _make_namer("TestNamerSharding")

            dep_hash = namer.compute_dep_hash([])
            obj_path = namer.object_pathname(source_file, "0123456789abcdef", dep_hash)

            from compiletools.trim_cache import parse_object_filename

            obj_filename = os.path.basename(obj_path)
            parsed = parse_object_filename(obj_filename)
            assert parsed is not None, f"object filename should still parse: {obj_filename}"
            _basename, file_hash, _dep, _macro = parsed
            expected_bucket = file_hash[:2]

            bucket_dir = os.path.basename(os.path.dirname(obj_path))
            assert bucket_dir == expected_bucket, (
                f"Expected sharded bucket dir {expected_bucket!r} (file_hash[:2]); "
                f"got {bucket_dir!r}. Full path: {obj_path}"
            )

            objdir_root = os.path.dirname(os.path.dirname(obj_path))
            assert objdir_root == args.cas_objdir, (
                f"Bucket dir should sit directly under args.cas_objdir={args.cas_objdir!r}; "
                f"got grandparent={objdir_root!r}"
            )

            # object_dir(sourcefilename) with no file_hash must still return the
            # bare objdir so realclean()/clean() can rmtree the whole cache root.
            assert namer.object_dir() == args.cas_objdir
            # And the explicit form must return the same bucket dir as
            # object_pathname picks, so build_backend can use it for
            # ``order_only_deps`` without recomputing the join.
            assert namer.object_dir(source_file, file_hash) == os.path.join(args.cas_objdir, expected_bucket)
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
        with tempfile.TemporaryDirectory() as srcdir:
            src = uth.write_sources(
                {
                    "main.cpp": "//#SOURCE=helper.cpp\nint main() { return 0; }\n",
                    "helper.cpp": "void helper() {}\n",
                },
                target_dir=srcdir,
            )
            main_file = str(src["main.cpp"])
            helper_file = str(src["helper.cpp"])

            def create_hunter_with_cppflags(cppflags_value):
                """Create a Hunter instance with specific CPPFLAGS."""
                cap = configargparse.ArgumentParser(
                    conflict_handler="resolve",
                    args_for_setting_config_path=["-c", "--config"],
                    ignore_unknown_config_file_keys=True,
                )
                compiletools.hunter.add_arguments(cap)
                argv = [f"--append-CPPFLAGS={cppflags_value}", "-q"]
                ctx = BuildContext()
                args = compiletools.apptools.parseargs(cap, argv, context=ctx)
                hdeps = compiletools.headerdeps.create(args, context=ctx)
                magic = compiletools.magicflags.create(args, hdeps, context=ctx)
                return compiletools.hunter.Hunter(args, hdeps, magic, context=ctx), magic

            # Config 1: trigger _extractSOURCE via required_source_files
            hunter1, magic1 = create_hunter_with_cppflags("-I/opt/libfoo/v1/include")
            sources1 = hunter1.required_source_files(main_file)
            assert any(f.endswith("helper.cpp") for f in sources1), (
                f"Hunter should expand //#SOURCE=helper.cpp, got: {sources1}"
            )
            hash1_main = magic1.get_final_macro_state_hash(main_file)
            hash1_helper = magic1.get_final_macro_state_hash(helper_file)

            # Clear class-level caches between configurations
            compiletools.hunter.Hunter.clear_cache()

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
        import compiletools.test_base as tb

        # Create a source file in a temp directory
        with tempfile.TemporaryDirectory() as srcdir:
            src = uth.write_sources(
                {"test_objname.cpp": "int main() { return 0; }\n"},
                target_dir=srcdir,
            )
            source_file = str(src["test_objname.cpp"])

            with tempfile.TemporaryDirectory() as tmpdir:
                # Build two magicflag parsers with different CPPFLAGS
                parser1 = tb.create_magic_parser(
                    ["--magic=direct", "--append-CPPFLAGS=-I/opt/libfoo/v1/include"],
                    tempdir=tmpdir,
                    context=BuildContext(),
                )
                parser1.parse(source_file)
                hash1 = parser1.get_final_macro_state_hash(source_file)

                parser2 = tb.create_magic_parser(
                    ["--magic=direct", "--append-CPPFLAGS=-I/opt/libfoo/v2/include"],
                    tempdir=tmpdir,
                    context=BuildContext(),
                )
                parser2.parse(source_file)
                hash2 = parser2.get_final_macro_state_hash(source_file)

                assert hash1 != hash2, f"Different CPPFLAGS must produce different macro_state_hash: {hash1} vs {hash2}"

            # Reset parser state before creating namer's parser
            uth.reset()

            # Setup namer
            _args, namer = _make_namer("TestNamerCPPFLAGS")

            dep_hash = namer.compute_dep_hash([])

            obj1 = namer.object_pathname(source_file, hash1, dep_hash)
            obj2 = namer.object_pathname(source_file, hash2, dep_hash)

            assert obj1 != obj2, f"Different CPPFLAGS must produce different object paths: {obj1} vs {obj2}"

    finally:
        uth.reset()


def test_cas_exe_pathname_is_sharded_by_link_key_hash():
    """``cas_exe_pathname`` returns ``<cas-exedir>/<linkkey[:2]>/<basename>_<linkkey>.exe``.

    Mirrors ``object_pathname``'s sharding rationale (256 buckets to keep
    per-directory inode counts manageable when the cache is shared across
    many builds) but keys on the *linkkey* — a hash of the link
    command's content-relevant inputs.
    """
    uth.reset()

    try:
        args, namer = _make_namer("TestCasExe")

        link_key = "abcd1234ef567890abcd1234ef567890"  # pragma: allowlist secret
        exe_path = namer.cas_exe_pathname("/some/where/foo.cpp", link_key)

        # Bucket dir is the leading 2 chars of the link key.
        bucket = os.path.basename(os.path.dirname(exe_path))
        assert bucket == link_key[:2], f"expected bucket {link_key[:2]!r}, got {bucket!r} from {exe_path}"

        # Filename is ``<basename>_<linkkey>.exe``.
        assert os.path.basename(exe_path) == f"foo_{link_key}.exe"

        # Bucket lives directly under args.cas_exedir.
        cas_exedir_root = os.path.dirname(os.path.dirname(exe_path))
        assert cas_exedir_root == args.cas_exedir, (
            f"bucket should sit directly under args.cas_exedir={args.cas_exedir!r}; got grandparent={cas_exedir_root!r}"
        )

        # cas_exe_dir() with no key returns the bare cache root (used by
        # trim-cache or rmtree of the whole tree).
        assert namer.cas_exe_dir() == args.cas_exedir
        assert namer.cas_exe_dir(link_key) == os.path.join(args.cas_exedir, link_key[:2])
    finally:
        uth.reset()


def test_cas_exedir_default_path_includes_variant():
    """The default --cas-exedir lives at <git_root>/cas-exedir/<variant> so
    parallel-variant builds don't trample each other's caches."""
    uth.reset()

    try:
        args, _namer = _make_namer("TestCasExeDirDefault")

        # Default path must end in <variant>; matches the sibling cache dirs
        # (--cas-objdir / --cas-pchdir / --cas-pcmdir) which all variant-suffix.
        assert args.cas_exedir.endswith(os.path.join("cas-exedir", "gcc.debug")), (
            f"unexpected default cas_exedir: {args.cas_exedir!r}"
        )
    finally:
        uth.reset()


def test_create_link_rule_returns_cas_link_plus_publish_pair():
    """Production link path emits two rules: cas-link to <cas-exedir>/...exe
    and a `symlink` rule that materialises bin/<name> via hard link
    (with symlink fallback). Downstream rules continue to reference
    bin/<name> via Namer.executable_pathname; the symlink rule's output
    IS that path so test/build deps resolve correctly."""
    import tempfile

    import compiletools.testhelper as uth
    from compiletools.build_backend import BuildBackend

    class _ConcreteBackend(BuildBackend):
        @staticmethod
        def name():
            return "test-concrete"

        @staticmethod
        def build_filename():
            return "Concretefile"

        def generate(self, graph, output=None):
            raise NotImplementedError

        def _execute_build(self, target):
            raise NotImplementedError

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            args = uth.make_backend_args(tmpdir, filename=["/src/main.cpp"])
            hunter = uth.make_mock_hunter(
                sources=["/src/main.cpp"],
                per_file_magicflags={"/src/main.cpp": {}},
            )
            backend = _ConcreteBackend.__new__(_ConcreteBackend)
            backend.args = args
            backend.hunter = hunter
            backend.namer = uth.make_mock_namer(args)
            backend.context = BuildContext()
            backend._anchor_root = ""

            rules = backend._create_link_rule("/src/main.cpp")
            assert len(rules) == 2, f"expected [link, symlink], got {[r.rule_type for r in rules]}"

            link_rule, symlink_rule = rules
            assert link_rule.rule_type == "link"
            assert link_rule.output.startswith(args.cas_exedir), (
                f"link output should live under cas-exedir, got {link_rule.output}"
            )
            assert link_rule.output.endswith(".exe")

            assert symlink_rule.rule_type == "symlink"
            # Output is the user-facing exename (what downstream test/build
            # rules reference).
            assert symlink_rule.output == backend.namer.executable_pathname("/src/main.cpp")
            # Symlink rule consumes the cas-exe as its only input.
            assert symlink_rule.inputs == [link_rule.output]
            # Publish recipe is now ``ct-cas-publish`` (I1/I2): a Python
            # helper that does atomic link+rename, falls back to symlink
            # ONLY on EXDEV, surfaces other errors visibly, and writes
            # the C4 sidecar manifest.
            cmd = symlink_rule.command
            assert cmd is not None
            assert cmd[0] == "ct-cas-publish", f"expected publish via ct-cas-publish, got: {cmd}"
            # Required flags: --cas-path / --user-path. Optional --source-realpath.
            assert "--cas-path" in cmd
            assert "--user-path" in cmd
            assert link_rule.output in cmd  # the cas path
            assert symlink_rule.output in cmd  # the user-facing path
            # Source realpath sidecar is included for trim-bucketing.
            assert "--source-realpath" in cmd
    finally:
        uth.reset()


def test_create_link_rule_legacy_shape_when_backend_self_manages_exe():
    """Backends that manage their own exe placement (cmake/bazel)
    override ``_self_manages_exe_placement`` to return True;
    ``_create_link_rule`` then emits a single classical link rule whose
    output IS the user-facing bin/<name> path (no compiletools-side
    cas-exedir wrapping)."""
    import tempfile

    import compiletools.testhelper as uth
    from compiletools.build_backend import BuildBackend

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            args = uth.make_backend_args(tmpdir, filename=["/src/main.cpp"])
            hunter = uth.make_mock_hunter(
                sources=["/src/main.cpp"],
                per_file_magicflags={"/src/main.cpp": {}},
            )

            class _LegacyBackend(BuildBackend):
                @classmethod
                def _self_manages_exe_placement(cls):
                    return True

                @staticmethod
                def name():
                    return "test-legacy"

                @staticmethod
                def build_filename():
                    return "Legacyfile"

                def generate(self, graph, output=None):
                    raise NotImplementedError

                def _execute_build(self, target):
                    raise NotImplementedError

            backend = _LegacyBackend.__new__(_LegacyBackend)
            backend.args = args
            backend.hunter = hunter
            backend.namer = uth.make_mock_namer(args)
            backend.context = BuildContext()
            backend._anchor_root = ""

            rules = backend._create_link_rule("/src/main.cpp")
            assert len(rules) == 1, f"legacy shape should be a single rule, got {len(rules)}"
            (rule,) = rules
            assert rule.rule_type == "link"
            # Output is the user-facing path; no cas-exedir routing.
            assert rule.output == backend.namer.executable_pathname("/src/main.cpp")
            assert args.cas_exedir not in rule.output
    finally:
        uth.reset()


def _make_minimal_link_backend(tmpdir, *, sources=None, env=None):
    """Build a minimal _ConcreteBackend wired to call _create_link_rule
    so the link-key payload contents can be exercised in isolation.
    Used by C3 / C5 tests.
    """
    import compiletools.testhelper as uth
    from compiletools.build_backend import BuildBackend

    sources = sources or ["/src/main.cpp"]

    class _ConcreteBackend(BuildBackend):
        @staticmethod
        def name():
            return "test-c3"

        @staticmethod
        def build_filename():
            return "Concretefile"

        def generate(self, graph, output=None):
            raise NotImplementedError

        def _execute_build(self, target):
            raise NotImplementedError

    args = uth.make_backend_args(tmpdir, filename=sources)
    hunter = uth.make_mock_hunter(
        sources=sources,
        per_file_magicflags={s: {} for s in sources},
    )
    backend = _ConcreteBackend.__new__(_ConcreteBackend)
    backend.args = args
    backend.hunter = hunter
    backend.namer = uth.make_mock_namer(args)
    backend.context = BuildContext()
    backend._anchor_root = ""
    return backend


def _make_workspace_link_backend(root, *, ldflags=""):
    """Build a link-key backend whose paths are rooted under *root*."""
    source = os.path.join(root, "src", "main.cpp")
    backend = _make_minimal_link_backend(root, sources=[source])
    backend._anchor_root = root
    backend.args.LDFLAGS = ldflags
    uth.finalize_flag_state(backend.args)
    backend._object_pathname_for_source = lambda _source: os.path.join(root, "obj", "main.o")  # type: ignore[method-assign]
    return backend, source


def test_link_key_changes_with_source_date_epoch(monkeypatch):
    """C3: SOURCE_DATE_EPOCH affects build-id baked into the binary.
    Two builds at different epochs MUST produce different cas-exe
    paths so the cache doesn't bake a stale build-id.
    """
    import tempfile

    import compiletools.testhelper as uth

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_minimal_link_backend(tmpdir)

            monkeypatch.setenv("SOURCE_DATE_EPOCH", "1")
            rules1 = backend._create_link_rule("/src/main.cpp")

            monkeypatch.setenv("SOURCE_DATE_EPOCH", "2")
            rules2 = backend._create_link_rule("/src/main.cpp")

            assert rules1[0].output != rules2[0].output, (
                "SOURCE_DATE_EPOCH must participate in the link-key payload — "
                "otherwise the cached binary bakes the wrong build-id"
            )
    finally:
        uth.reset()


def test_link_key_changes_with_library_path(monkeypatch):
    """C3: LIBRARY_PATH (and LD_LIBRARY_PATH) at link time changes
    which libfoo.so the linker resolves -lfoo against. Different
    resolution → different binary content → must differ in cache key.
    """
    import tempfile

    import compiletools.testhelper as uth

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _make_minimal_link_backend(tmpdir)

            monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
            monkeypatch.setenv("LIBRARY_PATH", "/opt/v1/lib")
            rules1 = backend._create_link_rule("/src/main.cpp")

            monkeypatch.setenv("LIBRARY_PATH", "/opt/v2/lib")
            rules2 = backend._create_link_rule("/src/main.cpp")

            assert rules1[0].output != rules2[0].output, "LIBRARY_PATH must participate in the link-key payload"
    finally:
        uth.reset()


def test_link_key_canonicalizes_workspace_rooted_ld_extra(monkeypatch, tmp_path):
    """Workspace-rooted command-line LDFLAGS should not split link CAS keys.

    The emitted command already rewrites ``-Wl,-rpath,<gitroot>/lib`` to the
    configured prefix-map target. The cache-key payload must use the same
    workspace-stable representation rather than hashing the raw checkout path.
    """
    uth.reset()
    try:
        for var in ("SOURCE_DATE_EPOCH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "LD_PRELOAD"):
            monkeypatch.delenv(var, raising=False)

        ws1 = str(tmp_path / "alice")
        ws2 = str(tmp_path / "bob")
        backend1, source1 = _make_workspace_link_backend(ws1, ldflags=f"-Wl,-rpath,{ws1}/lib")
        backend2, source2 = _make_workspace_link_backend(ws2, ldflags=f"-Wl,-rpath,{ws2}/lib")

        rule1 = backend1._create_link_rule(source1)[0]
        rule2 = backend2._create_link_rule(source2)[0]

        assert rule1.command is not None
        assert rule2.command is not None
        assert "-Wl,-rpath,./lib" in rule1.command
        assert "-Wl,-rpath,./lib" in rule2.command
        assert os.path.basename(rule1.output) == os.path.basename(rule2.output)
    finally:
        uth.reset()


def test_link_key_canonicalizes_workspace_rooted_extra_link_argv(monkeypatch, tmp_path):
    """Library-search argv synthesized from the workspace bindir is portable."""
    uth.reset()
    try:
        for var in ("SOURCE_DATE_EPOCH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "LD_PRELOAD"):
            monkeypatch.delenv(var, raising=False)

        ws1 = str(tmp_path / "alice")
        ws2 = str(tmp_path / "bob")
        backend1, source1 = _make_workspace_link_backend(ws1)
        backend2, source2 = _make_workspace_link_backend(ws2)

        rule1 = backend1._create_link_rule(source1, library_outputs=[os.path.join(ws1, "bin", "libdep.a")])[0]
        rule2 = backend2._create_link_rule(source2, library_outputs=[os.path.join(ws2, "bin", "libdep.a")])[0]

        assert os.path.basename(rule1.output) == os.path.basename(rule2.output)
    finally:
        uth.reset()


def test_shared_library_key_canonicalizes_workspace_rooted_ld_extra(monkeypatch, tmp_path):
    """Shared-library CAS keys use canonicalized command-line LDFLAGS too."""
    uth.reset()
    try:
        for var in ("SOURCE_DATE_EPOCH", "LD_LIBRARY_PATH", "LIBRARY_PATH", "LD_PRELOAD"):
            monkeypatch.delenv(var, raising=False)

        ws1 = str(tmp_path / "alice")
        ws2 = str(tmp_path / "bob")
        backend1, source1 = _make_workspace_link_backend(ws1, ldflags=f"-Wl,-rpath,{ws1}/lib")
        backend2, source2 = _make_workspace_link_backend(ws2, ldflags=f"-Wl,-rpath,{ws2}/lib")
        backend1.args.dynamic = [source1]
        backend2.args.dynamic = [source2]

        rule1 = backend1._create_shared_library_rule()[0]
        rule2 = backend2._create_shared_library_rule()[0]

        assert rule1.command is not None
        assert rule2.command is not None
        assert "-Wl,-rpath,./lib" in rule1.command
        assert "-Wl,-rpath,./lib" in rule2.command
        assert os.path.basename(rule1.output) == os.path.basename(rule2.output)
    finally:
        uth.reset()


def test_link_key_differs_for_same_basename_different_bindir(monkeypatch):
    """C5: two ct-cake invocations in the same gitroot but with
    different bindirs (e.g. ``bin/blank`` vs ``out/blank`` — or any
    case where ``$ORIGIN``-relative RPATH semantics would change)
    must produce different cache keys. The ``bindir_basename`` defence
    is too weak — both ``bin/blank`` and ``out/blank`` share basename
    ``blank``. The new payload field is ``canonical_bindir``: the full
    canonicalised bindir, which differs between the two cases.
    """
    import tempfile

    import compiletools.testhelper as uth

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend1 = _make_minimal_link_backend(tmpdir)
            # Force the bindir on backend1 to e.g. <tmp>/bin/blank
            backend1.namer.executable_dir = lambda: os.path.join(tmpdir, "bin", "blank")  # type: ignore[method-assign]
            rules1 = backend1._create_link_rule("/src/main.cpp")

            backend2 = _make_minimal_link_backend(tmpdir)
            # bindir is a SIBLING with the SAME BASENAME — exactly the
            # collision case bindir_basename was supposed to defend.
            backend2.namer.executable_dir = lambda: os.path.join(tmpdir, "out", "blank")  # type: ignore[method-assign]
            rules2 = backend2._create_link_rule("/src/main.cpp")

            assert rules1[0].output != rules2[0].output, (
                "C5: distinct bindirs sharing the same basename must hash differently. "
                "bindir_basename was a useless defence; the real defence is canonical_bindir."
            )
    finally:
        uth.reset()


def test_static_lib_key_changes_with_ar_identity(monkeypatch, tmp_path):
    """C3: binutils 2.30 vs 2.40 ``ar`` produce different archive
    formats (BSD vs SysV symbol tables, compressed debug sections).
    A cache shared across runners with different binutils must not
    silently mix formats.
    """
    import tempfile

    import compiletools.testhelper as uth

    uth.reset()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Two distinct stub `ar` binaries with different mtime/size.
            ar1 = tmp_path / "ar_v1"
            ar1.write_text("#!/bin/sh\nexit 0\n" + "x" * 100)
            ar1.chmod(0o755)
            ar2 = tmp_path / "ar_v2"
            ar2.write_text("#!/bin/sh\nexit 0\n" + "y" * 200)
            ar2.chmod(0o755)

            backend = _make_minimal_link_backend(tmpdir, sources=["/src/lib.cpp"])
            backend.args.static = ["/src/lib.cpp"]

            monkeypatch.setattr(backend.args, "AR", str(ar1), raising=False)
            rules1 = backend._create_static_library_rule()

            monkeypatch.setattr(backend.args, "AR", str(ar2), raising=False)
            rules2 = backend._create_static_library_rule()

            assert rules1[0].output != rules2[0].output, (
                "AR identity must participate in the static-library cache key — "
                "binutils version determines archive format"
            )
    finally:
        uth.reset()
