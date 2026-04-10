import os

import pytest
import stringzilla as sz

import compiletools.testhelper as uth
import compiletools.utils as utils


class TestIsFuncs:
    def test_is_header(self):
        assert utils.is_header("myfile.h")
        assert utils.is_header("/home/user/myfile.h")
        assert utils.is_header("myfile.H")
        assert utils.is_header("My File.H")
        assert utils.is_header("myfile.inl")
        assert utils.is_header("myfile.hh")
        assert utils.is_header("myfile.hxx")
        assert utils.is_header("myfile.hpp")
        assert utils.is_header("/home/user/myfile.hpp")
        assert utils.is_header("myfile.with.dots.hpp")
        assert utils.is_header("/home/user/myfile.with.dots.hpp")
        assert utils.is_header("myfile_underscore.h")
        assert utils.is_header("myfile-hypen.h")
        assert utils.is_header("myfile.h")

        assert not utils.is_header("myfile.c")
        assert not utils.is_header("myfile.cc")
        assert not utils.is_header("myfile.cpp")
        assert not utils.is_header("/home/user/myfile")

    def test_is_source(self):
        assert utils.is_source("myfile.c")
        assert utils.is_source("myfile.cc")
        assert utils.is_source("myfile.cpp")
        assert utils.is_source("/home/user/myfile.cpp")
        assert utils.is_source("/home/user/myfile.with.dots.cpp")
        assert utils.is_source("myfile.C")
        assert utils.is_source("myfile.CC")
        assert utils.is_source("My File.c")
        assert utils.is_source("My File.cpp")
        assert utils.is_source("myfile.cxx")

        assert not utils.is_source("myfile.h")
        assert not utils.is_source("myfile.hh")
        assert not utils.is_source("myfile.hpp")
        assert not utils.is_source("/home/user/myfile.with.dots.hpp")

    def test_is_c_source(self):
        # Test that .c files are identified as C source
        assert utils.is_c_source("myfile.c")
        assert utils.is_c_source("/path/to/myfile.c")

        # Test that .C files are NOT identified as C source (they're C++)
        assert not utils.is_c_source("myfile.C")
        assert not utils.is_c_source("/path/to/myfile.C")

        # Test that other extensions are not C source
        assert not utils.is_c_source("myfile.cpp")
        assert not utils.is_c_source("myfile.cxx")
        assert not utils.is_c_source("myfile.h")

    def test_is_cpp_source(self):
        # Test that common C++ extensions are identified as C++ source
        assert utils.is_cpp_source("myfile.cpp")
        assert utils.is_cpp_source("myfile.cxx")
        assert utils.is_cpp_source("myfile.cc")
        assert utils.is_cpp_source("myfile.c++")

        # Test that .C (uppercase) is identified as C++ source
        assert utils.is_cpp_source("myfile.C")
        assert utils.is_cpp_source("/path/to/myfile.C")

        # Test that .c (lowercase) is NOT identified as C++ source
        assert not utils.is_cpp_source("myfile.c")
        assert not utils.is_cpp_source("/path/to/myfile.c")

        # Test that headers are not C++ source
        assert not utils.is_cpp_source("myfile.h")
        assert not utils.is_cpp_source("myfile.hpp")


class TestImpliedSource:
    def test_implied_source_nonexistent_file(self):
        assert utils.implied_source("nonexistent_file.hpp") is None

    def test_implied_source(self):
        relativefilename = "dottypaths/d2/d2.hpp"
        basename = os.path.splitext(relativefilename)[0]
        expected = os.path.join(uth.samplesdir(), basename + ".cpp")
        result = utils.implied_source(os.path.join(uth.samplesdir(), relativefilename))
        assert expected == result


class TestToBool:
    def test_to_bool_true_values(self):
        """Test that various true values are converted correctly"""
        true_values = ["yes", "y", "true", "t", "1", "on", "YES", "True", "ON"]
        for value in true_values:
            assert utils.to_bool(value) is True, f"Expected True for {value}"

    def test_to_bool_false_values(self):
        """Test that various false values are converted correctly"""
        false_values = ["no", "n", "false", "f", "0", "off", "NO", "False", "OFF"]
        for value in false_values:
            assert utils.to_bool(value) is False, f"Expected False for {value}"

    def test_to_bool_invalid_values(self):
        """Test that invalid values raise ValueError"""
        invalid_values = ["maybe", "invalid", "2", ""]
        for value in invalid_values:
            with pytest.raises(ValueError):
                utils.to_bool(value)


class TestRemoveMount:
    def test_remove_mount_unix_path(self):
        """Test remove_mount with Unix-style paths"""
        assert utils.remove_mount("/home/user/file.txt") == "home/user/file.txt"
        assert utils.remove_mount("/") == ""
        assert utils.remove_mount("/file.txt") == "file.txt"

    def test_remove_mount_invalid_path(self):
        """Test remove_mount with non-absolute path raises error"""
        with pytest.raises(ValueError):
            utils.remove_mount("relative/path")


class TestOrderedUnique:
    def test_ordered_unique_basic(self):
        result = utils.ordered_unique([5, 4, 3, 2, 1])
        assert len(result) == 5
        assert 3 in result
        assert 6 not in result
        assert result == [5, 4, 3, 2, 1]

    def test_ordered_unique_duplicates(self):
        # Test deduplication while preserving order
        result = utils.ordered_unique(["five", "four", "three", "two", "one", "four", "two"])
        expected = ["five", "four", "three", "two", "one"]
        assert result == expected
        assert len(result) == 5
        assert "four" in result
        assert "two" in result

    def test_ordered_union(self):
        # Test union functionality
        list1 = ["a", "b", "c"]
        list2 = ["c", "d", "e"]
        list3 = ["e", "f", "g"]
        result = utils.ordered_union(list1, list2, list3)
        expected = ["a", "b", "c", "d", "e", "f", "g"]
        assert result == expected

    def test_ordered_difference(self):
        # Test difference functionality
        source = ["a", "b", "c", "d", "e"]
        subtract = ["b", "d"]
        result = utils.ordered_difference(source, subtract)
        expected = ["a", "c", "e"]
        assert result == expected


class TestMergeLdflagsTopoSort:
    """Test constraint-based topological sorting of -l flags across files."""

    def test_single_file_preserves_order(self):
        """Single file's -l ordering should be preserved exactly."""
        per_file = [["-llibnext", "-llibbase"]]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result == ["-llibnext", "-llibbase"]

    def test_two_files_conflicting_discovery_order(self):
        """The exact bug scenario: file one only needs libbase,
        file two needs libnext before libbase. Naive concat produces
        [-llibbase, -llibnext] which is wrong for static linking."""
        per_file = [
            ["-llibbase"],
            ["-llibnext", "-llibbase"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        idx_next = result.index("-llibnext")
        idx_base = result.index("-llibbase")
        assert idx_next < idx_base, f"libnext must precede libbase, got {result}"

    def test_three_level_chain(self):
        """A -> B -> C dependency chain across files."""
        per_file = [
            ["-la", "-lb"],
            ["-lb", "-lc"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result.index("-la") < result.index("-lb")
        assert result.index("-lb") < result.index("-lc")

    def test_diamond_dependency(self):
        """Diamond: top depends on left and right, both depend on bottom."""
        per_file = [
            ["-ltop", "-lleft", "-lbottom"],
            ["-ltop", "-lright", "-lbottom"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result.index("-ltop") < result.index("-lleft")
        assert result.index("-ltop") < result.index("-lright")
        assert result.index("-lleft") < result.index("-lbottom")
        assert result.index("-lright") < result.index("-lbottom")

    def test_non_l_flags_preserved(self):
        """-L and -pthread etc. should pass through unchanged."""
        per_file = [
            ["-L/usr/lib", "-pthread", "-llibnext", "-llibbase"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert "-L/usr/lib" in result
        assert "-pthread" in result
        assert result.index("-llibnext") < result.index("-llibbase")

    def test_non_l_flags_deduplicated(self):
        """Same -L from two files should appear once."""
        per_file = [
            ["-L/usr/lib", "-llibbase"],
            ["-L/usr/lib", "-llibnext"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result.count("-L/usr/lib") == 1

    def test_separate_l_form(self):
        """Handle -l as separate token: ['-l', 'next', '-l', 'base']."""
        per_file = [
            ["-l", "next", "-l", "base"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        # Should contain -lnext before -lbase (combined form in output)
        assert result.index("-lnext") < result.index("-lbase")

    def test_empty_input(self):
        assert utils.merge_ldflags_with_topo_sort([]) == []

    def test_single_empty_file(self):
        assert utils.merge_ldflags_with_topo_sort([[]]) == []

    def test_soft_cycle_resolved_by_breaking_soft_edges(self):
        """An all-soft cycle should be resolved by breaking soft edges,
        not by raising an error.  Soft constraints are hints from
        per-file flag ordering, not explicit intent."""
        per_file = [
            ["-la", "-lb"],
            ["-lb", "-lc"],
            ["-lc", "-la"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        # All libs present in deterministic alphabetical order (no hard
        # constraints, so tie-breaking decides).
        assert result == ["-la", "-lb", "-lc"]

    def test_hard_cycle_raises_error(self):
        """A purely hard cycle should raise ValueError with cycle path."""
        per_file = [
            ["-la", "-lb"],
            ["-lb", "-lc"],
            ["-lc", "-la"],
        ]
        hard_orderings = [("a", "b"), ("b", "c"), ("c", "a")]
        with pytest.raises(ValueError, match=r"a -> b -> c -> a"):
            utils.merge_ldflags_with_topo_sort(
                per_file, hard_orderings=hard_orderings,
            )

    def test_hard_cycle_error_shows_source_files(self, tmp_path):
        """When source_files are provided, the error should show paths
        relative to their common root."""
        per_file = [
            ["-la", "-lb"],
            ["-lb", "-lc"],
            ["-lc", "-la"],
        ]
        hard_orderings = [("a", "b"), ("b", "c"), ("c", "a")]
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "foo.cpp").touch()
        (src_dir / "bar.cpp").touch()
        (src_dir / "baz.cpp").touch()
        source_files = [
            str(src_dir / "foo.cpp"),
            str(src_dir / "bar.cpp"),
            str(src_dir / "baz.cpp"),
        ]
        with pytest.raises(ValueError) as exc_info:
            utils.merge_ldflags_with_topo_sort(
                per_file,
                source_files=source_files,
                hard_orderings=hard_orderings,
                hard_ordering_sources=source_files,
            )
        msg = str(exc_info.value)
        assert f"Root: {src_dir}/" in msg
        assert "foo.cpp" in msg
        assert "bar.cpp" in msg
        assert "baz.cpp" in msg

    def test_mixed_cycle_breaks_soft_edges(self):
        """A cycle with both hard and soft edges should be resolved by
        dropping the soft edges, preserving all hard orderings."""
        per_file = [
            ["-la", "-lb"],     # soft: a→b
            ["-lb", "-lc"],     # soft: b→c
            ["-lc", "-la"],     # soft: c→a (closes the cycle)
        ]
        hard_orderings = [("a", "b")]   # hard: a must precede b
        result = utils.merge_ldflags_with_topo_sort(
            per_file, hard_orderings=hard_orderings,
        )
        # Hard ordering a→b preserved
        assert result.index("-la") < result.index("-lb")
        # All libs present
        assert "-la" in result
        assert "-lb" in result
        assert "-lc" in result

    def test_multiple_disjoint_soft_cycles(self):
        """Two independent non-mutual soft cycles should both be resolved."""
        per_file = [
            ["-la", "-lb"], ["-lb", "-lc"], ["-lc", "-la"],  # cycle 1
            ["-lx", "-ly"], ["-ly", "-lz"], ["-lz", "-lx"],  # cycle 2
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert len(result) == 6
        for lib in ["-la", "-lb", "-lc", "-lx", "-ly", "-lz"]:
            assert lib in result

    def test_deterministic_output(self):
        """Same input must always produce same output (CA cache requirement)."""
        per_file = [
            ["-lc", "-lb"],
            ["-la", "-lb"],
        ]
        result1 = utils.merge_ldflags_with_topo_sort(per_file)
        result2 = utils.merge_ldflags_with_topo_sort(per_file)
        assert result1 == result2

    def test_deterministic_output_with_soft_cycle_breaking(self):
        """Cycle-breaking path must also produce deterministic output."""
        per_file = [
            ["-la", "-lb"], ["-lb", "-lc"], ["-lc", "-la"],
        ]
        result1 = utils.merge_ldflags_with_topo_sort(per_file)
        result2 = utils.merge_ldflags_with_topo_sort(per_file)
        assert result1 == result2

    def test_no_l_flags_passthrough(self):
        """If no -l flags exist, non-l flags should just be deduplicated."""
        per_file = [
            ["-L/usr/lib", "-pthread"],
            ["-L/other/lib"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert "-L/usr/lib" in result
        assert "-L/other/lib" in result
        assert "-pthread" in result

    def test_stringzilla_str_input(self):
        """Function should handle stringzilla Str inputs."""
        per_file = [
            [sz.Str("-llibnext"), sz.Str("-llibbase")],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result == ["-llibnext", "-llibbase"]

    def test_l_flags_deduplicated(self):
        """Same -l lib from multiple files should appear only once."""
        per_file = [
            ["-llibbase"],
            ["-llibbase"],
        ]
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert result.count("-llibbase") == 1

    # --- Hard/soft edge distinction tests ---

    def test_opposite_hard_orderings_is_cycle(self):
        """Two multi-package annotations with opposite orderings is a
        genuine contradiction — both files explicitly declare intent.
        PKG-CONFIG=libssh2 numa vs PKG-CONFIG=numa libssh2."""
        per_file = [
            ["-lssh2", "-lnuma"],
            ["-lnuma", "-lssh2"],
        ]
        hard_orderings = [("ssh2", "numa"), ("numa", "ssh2")]
        with pytest.raises(ValueError, match="Cyclic library dependency"):
            utils.merge_ldflags_with_topo_sort(
                per_file, hard_orderings=hard_orderings,
            )

    def test_overlapping_transitive_deps_not_cycle(self):
        """Two single-package files whose pkg-config output lists shared
        transitive deps in different orders should NOT be treated as a
        cycle — these are soft constraints from discovery, not intent."""
        per_file = [
            ["-lssh2", "-lssl", "-lcrypto"],   # pkg-config --libs libssh2
            ["-lnuma", "-lcrypto", "-lssl"],    # pkg-config --libs numa
        ]
        # No hard_orderings: both files are single-package
        result = utils.merge_ldflags_with_topo_sort(per_file)
        assert "-lssh2" in result
        assert "-lnuma" in result
        assert "-lssl" in result
        assert "-lcrypto" in result

    def test_single_pkg_vs_multi_pkg_no_conflict(self):
        """A single-package file (libssh2) does not constrain ordering
        relative to a multi-package file (numa libssh2)."""
        per_file = [
            ["-lssh2"],                # single: PKG-CONFIG=libssh2
            ["-lnuma", "-lssh2"],      # multi: PKG-CONFIG=numa libssh2
        ]
        hard_orderings = [("numa", "ssh2")]
        result = utils.merge_ldflags_with_topo_sort(
            per_file, hard_orderings=hard_orderings,
        )
        assert result.index("-lnuma") < result.index("-lssh2")

    def test_single_file_hard_order_respected(self):
        """When only one file constrains the order between two packages
        (hard ordering) and other files have just one of the two,
        the explicit ordering must be respected."""
        per_file = [
            ["-lnuma", "-lssh2"],   # PKG-CONFIG=numa libssh2
            ["-lnuma"],             # PKG-CONFIG=numa
            ["-lssh2"],             # PKG-CONFIG=libssh2
        ]
        hard_orderings = [("numa", "ssh2")]
        result = utils.merge_ldflags_with_topo_sort(
            per_file, hard_orderings=hard_orderings,
        )
        assert result.index("-lnuma") < result.index("-lssh2")

    def test_genuine_three_pkg_cycle_with_hard_orderings(self):
        """file A: PKG-CONFIG=libssh2 numa, file B: PKG-CONFIG=numa libz libssh2.
        Hard cycle ssh2→numa→z→ssh2 — truly irreconcilable."""
        per_file = [
            ["-lssh2", "-lnuma"],
            ["-lnuma", "-lz", "-lssh2"],
        ]
        hard_orderings = [("ssh2", "numa"), ("numa", "z"), ("z", "ssh2")]
        with pytest.raises(ValueError, match="Cyclic library dependency"):
            utils.merge_ldflags_with_topo_sort(
                per_file, hard_orderings=hard_orderings,
            )

    def test_soft_contradiction_resolved_even_with_hard_elsewhere(self):
        """A soft mutual pair is cancelled even when unrelated hard
        orderings exist."""
        per_file = [
            ["-la", "-lb"],     # soft: a→b
            ["-lb", "-la"],     # soft: b→a (contradicts above)
            ["-lx", "-ly"],     # soft: x→y
        ]
        hard_orderings = [("x", "y")]   # hard: x must precede y
        result = utils.merge_ldflags_with_topo_sort(
            per_file, hard_orderings=hard_orderings,
        )
        # a<->b is soft-soft mutual, cancelled — no error
        assert "-la" in result
        assert "-lb" in result
        # x→y hard ordering respected
        assert result.index("-lx") < result.index("-ly")

    def test_hard_soft_cancellation_with_same_expanded_names(self):
        """When hard and soft constraints use the same (macro-expanded)
        library name, mutual edges should cancel correctly even when
        surrounded by unrelated packages in various positions.

        The contested pair (foo-O2, bar) appears at different positions
        across files — not always first — to verify cancellation doesn't
        rely on positional assumptions.

        Regression test: before the fix, _collect_hard_orderings() used
        unexpanded names (e.g. foo-LIB_SUFFIX) while soft constraints
        used expanded names (e.g. foo-O2), preventing cancellation and
        causing false cyclic dependency errors."""
        per_file = [
            # file A: contested pair in the middle, unrelated on both sides
            ["-lnuma", "-lfoo-O2", "-lbar", "-lssl"],
            # file B: contested pair reversed, preceded by crypto
            ["-lcrypto", "-lbar", "-lfoo-O2", "-lzip"],
            # file C: only unrelated libs — pure noise
            ["-lnuma", "-lcrypto", "-lssl"],
            # file D: contested libs not adjacent (no direct soft edge),
            # with zip between them
            ["-lfoo-O2", "-lzip", "-lbar"],
        ]
        # Hard ordering with the same expanded name (foo-O2, not foo-LIB_SUFFIX)
        hard_orderings = [("foo-O2", "bar")]
        result = utils.merge_ldflags_with_topo_sort(
            per_file, hard_orderings=hard_orderings,
        )
        # Hard wins: foo-O2 must precede bar, soft reverse is cancelled
        assert result.index("-lfoo-O2") < result.index("-lbar")
        # All libs present
        for lib in ("-lnuma", "-lfoo-O2", "-lbar", "-lcrypto", "-lzip", "-lssl"):
            assert lib in result, f"{lib} missing from result"
