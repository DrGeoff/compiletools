import os

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
            try:
                utils.to_bool(value)
                assert False, f"Expected ValueError for {value}"
            except ValueError:
                pass  # Expected


class TestRemoveMount:
    def test_remove_mount_unix_path(self):
        """Test remove_mount with Unix-style paths"""
        assert utils.remove_mount("/home/user/file.txt") == "home/user/file.txt"
        assert utils.remove_mount("/") == ""
        assert utils.remove_mount("/file.txt") == "file.txt"

    def test_remove_mount_invalid_path(self):
        """Test remove_mount with non-absolute path raises error"""
        try:
            utils.remove_mount("relative/path")
            assert False, "Expected ValueError for relative path"
        except ValueError:
            pass  # Expected


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


