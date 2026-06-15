"""Integration tests for file descriptor and filesystem compatibility fixes."""

import os
import resource
from types import SimpleNamespace

import configargparse
import pytest
import stringzilla as sz

import compiletools.wrappedos
from compiletools.build_context import BuildContext
from compiletools.examples_registry import example_file
from compiletools.file_analyzer import (
    _determine_file_reading_strategy,
    add_arguments,
    analyze_file,
    set_analyzer_args,
)
from compiletools.filesystem_utils import (
    get_filesystem_type,
    get_lock_strategy,
    get_lockdir_sleep_interval,
    supports_mmap_safely,
)
from compiletools.global_hash_registry import get_file_hash, load_hashes


def _make_args(**overrides) -> SimpleNamespace:
    """Default analyzer args; overrides win."""
    defaults = dict(
        max_read_size=0,
        verbose=0,
        exemarkers=[],
        testmarkers=[],
        librarymarkers=[],
        use_mmap=True,
        force_mmap=False,
        suppress_fd_warnings=True,
        suppress_filesystem_warnings=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestFilesystemIntegration:
    """Integration tests for filesystem detection."""

    def test_filesystem_detection_on_cwd(self):
        """Test that filesystem detection works on current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        assert isinstance(fstype, str)
        assert len(fstype) > 0

    def test_lock_strategy_for_cwd(self):
        """Test that lock strategy works for current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        strategy = get_lock_strategy(fstype)
        assert strategy in ["fcntl", "lockdir", "cifs", "flock"]

    def test_mmap_safety_for_cwd(self):
        """Test that mmap safety check works for current directory."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        mmap_safe = supports_mmap_safely(fstype)
        assert isinstance(mmap_safe, bool)

    def test_sleep_interval_for_cwd(self):
        """Test that sleep interval calculation works."""
        cwd = os.getcwd()
        fstype = get_filesystem_type(cwd)
        interval = get_lockdir_sleep_interval(fstype)
        assert isinstance(interval, float)
        assert interval > 0


class TestUlimitDetection:
    """Tests for ulimit detection."""

    def test_ulimit_readable(self):
        """Test that ulimit can be read."""
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            assert soft > 0
            assert hard >= soft
        except (OSError, AttributeError):
            pytest.skip("Ulimit not available on this system")


class _CtxBase:
    """Shared scaffolding: ``self.ctx`` is a fresh BuildContext per test."""

    def setup_method(self):
        self.ctx = BuildContext()


class TestFileReadingStrategy(_CtxBase):
    """Tests for file reading strategy selection."""

    def test_default_strategy(self):
        """Test default strategy selection."""
        args = _make_args()

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy in ["mmap", "no_mmap"]

    def test_manual_override_no_use_mmap(self):
        """Test manual override to disable mmap (no_mmap mode)."""
        args = _make_args(use_mmap=False)

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy == "no_mmap"

    def test_manual_override_force_mmap(self):
        """Test manual override to force mmap mode."""
        args = _make_args(force_mmap=True)

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy == "mmap"

    def test_low_ulimit_auto_no_mmap(self):
        """Test that very low ulimit (< 100) automatically triggers no_mmap mode."""
        # This test simulates the behavior, but can't actually change ulimit
        # The real test is that pytest -n auto works with ulimit 20
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            # If we're running with low ulimit, verify no_mmap is used
            if soft < 100:
                args = _make_args()

                set_analyzer_args(args, self.ctx)
                strategy = _determine_file_reading_strategy(self.ctx)
                assert strategy == "no_mmap", f"Expected no_mmap with ulimit {soft}, got {strategy}"
        except (OSError, AttributeError):
            pytest.skip("Cannot read ulimit on this system")


class TestFileAnalyzerArguments:
    """Tests for the module-level add_arguments function."""

    def test_add_arguments_no_use_mmap(self):
        """Test that --no-use-mmap argument works."""
        cap = configargparse.ArgumentParser()
        add_arguments(cap)

        args = cap.parse_args(["--no-use-mmap"])
        assert args.use_mmap is False

    def test_add_arguments_force_mmap(self):
        """Test that --force-mmap argument works."""
        cap = configargparse.ArgumentParser()
        add_arguments(cap)

        args = cap.parse_args(["--force-mmap"])
        assert args.force_mmap is True

    def test_add_arguments_suppress_warnings(self):
        """Test that warning suppression arguments work."""
        cap = configargparse.ArgumentParser()
        add_arguments(cap)

        args = cap.parse_args(["--suppress-fd-warnings", "--suppress-filesystem-warnings"])
        assert args.suppress_fd_warnings is True
        assert args.suppress_filesystem_warnings is True


class TestFileReadingWithRealFiles(_CtxBase):
    """Tests that verify file reading strategies work with real sample files."""

    def test_no_mmap_mode_reads_real_file(self):
        """Test that no_mmap mode actually reads and analyzes real files correctly."""
        # Use a simple sample file
        sample_file = example_file("simple/helloworld_cpp.cpp")
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file), f"Sample file not found: {sample_file}"

        # Load hash registry
        load_hashes(context=self.ctx)
        content_hash = get_file_hash(sample_file, self.ctx)

        # Configure no_mmap mode
        args = _make_args(exemarkers=["int main"], use_mmap=False)

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy == "no_mmap"

        # Analyze the file - this exercises the no_mmap read path in _load_file_text
        result = analyze_file(content_hash, self.ctx)

        # Verify the analysis worked correctly
        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any("iostream" in str(inc["filename"]) for inc in result.includes)
        assert result.bytes_analyzed > 0

    def test_mmap_mode_reads_real_file(self):
        """Test that mmap mode reads and analyzes real files correctly."""
        sample_file = example_file("simple/helloworld_cpp.cpp")
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file)

        load_hashes(context=self.ctx)
        content_hash = get_file_hash(sample_file, self.ctx)

        args = _make_args(exemarkers=["int main"], force_mmap=True)

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy == "mmap"

        result = analyze_file(content_hash, self.ctx)

        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any("iostream" in str(inc["filename"]) for inc in result.includes)

    def test_analyze_file_detaches_retained_strs(self, monkeypatch):
        """Wiring (A7): analyze_file must detach retained Str views so the cached
        result does not pin the whole decoded-file buffer.

        Captures the parent buffer produced by ``_load_file_text`` and asserts no
        retained Str in the returned result still lies inside it. Every retained
        token is a slice-view into ``str_text`` until ``_detach_file_analysis_result``
        rebuilds it; an un-wired ``analyze_file`` therefore leaves views pinning
        the buffer (RED), a wired one detaches them all (GREEN).
        """
        import compiletools.file_analyzer as fa

        sample_file = compiletools.wrappedos.realpath(example_file("simple/helloworld_cpp.cpp"))
        assert os.path.exists(sample_file)

        load_hashes(context=self.ctx)
        content_hash = get_file_hash(sample_file, self.ctx)

        captured = {}
        real_load = fa._load_file_text

        def _capturing_load(*a, **k):
            str_text, bytes_analyzed, was_truncated = real_load(*a, **k)
            captured["lo"] = str_text.address
            captured["hi"] = str_text.address + str_text.nbytes
            return str_text, bytes_analyzed, was_truncated

        monkeypatch.setattr(fa, "_load_file_text", _capturing_load)

        args = _make_args(exemarkers=["int main"], use_mmap=False)
        set_analyzer_args(args, self.ctx)
        result = analyze_file(content_hash, self.ctx)

        lo, hi = captured["lo"], captured["hi"]

        def walk(obj):
            if isinstance(obj, sz.Str):
                yield obj
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from walk(v)
            elif isinstance(obj, (list, tuple, set, frozenset)):
                for v in obj:
                    yield from walk(v)

        strs = []
        for field in (
            result.includes,
            result.magic_flags,
            result.defines,
            result.system_headers,
            result.quoted_headers,
            result.conditional_macros,
        ):
            strs.extend(walk(field))
        if result.include_guard is not None:
            strs.extend(walk(result.include_guard))
        for d in result.directives:
            for v in (d.condition, d.macro_name, d.macro_value):
                if v is not None:
                    strs.extend(walk(v))

        # Sanity: there is at least one retained Str to check (helloworld has an
        # #include), so a passing assertion is not vacuous.
        assert strs, "expected retained Str fields in the analysis result"
        offenders = [str(s) for s in strs if lo <= s.address < hi]
        assert offenders == [], f"cached result still pins file buffer: {offenders}"

    def test_analyze_file_computes_block_comment_spans_once(self, monkeypatch):
        """Perf contract: block-comment spans are computed ONCE per analyze_file.

        ``find_block_comment_spans`` is a full forward pass over the whole file.
        The bulk finders and the per-item consumers (``_extract_includes`` per
        include, ``_extract_module_declarations`` per line) must all share a
        single precomputed span list rather than each recomputing it. An
        un-threaded analyze_file calls it many times (RED, O(k) full scans); a
        threaded one calls it exactly once (GREEN).
        """
        import compiletools.file_analyzer as fa

        sample_file = compiletools.wrappedos.realpath(example_file("simple/helloworld_cpp.cpp"))
        assert os.path.exists(sample_file)

        load_hashes(context=self.ctx)
        content_hash = get_file_hash(sample_file, self.ctx)

        calls = {"n": 0}
        real = fa.find_block_comment_spans

        def _counting(str_text):
            calls["n"] += 1
            return real(str_text)

        monkeypatch.setattr(fa, "find_block_comment_spans", _counting)

        args = _make_args(exemarkers=["int main"], use_mmap=False)
        set_analyzer_args(args, self.ctx)
        analyze_file(content_hash, self.ctx)

        assert calls["n"] == 1, f"block-comment spans recomputed {calls['n']} times; expected 1"

    def test_no_use_mmap_mode_reads_real_file(self):
        """Test that --no-use-mmap mode reads and analyzes real files correctly."""
        sample_file = example_file("simple/helloworld_cpp.cpp")
        sample_file = compiletools.wrappedos.realpath(sample_file)
        assert os.path.exists(sample_file)

        load_hashes(context=self.ctx)
        content_hash = get_file_hash(sample_file, self.ctx)

        args = _make_args(exemarkers=["int main"], use_mmap=False)

        set_analyzer_args(args, self.ctx)
        strategy = _determine_file_reading_strategy(self.ctx)
        assert strategy == "no_mmap"

        result = analyze_file(content_hash, self.ctx)

        assert result.line_count > 0
        assert len(result.includes) > 0
        assert any("iostream" in str(inc["filename"]) for inc in result.includes)
