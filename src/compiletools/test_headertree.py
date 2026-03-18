"""Tests for headertree style classes."""

from unittest.mock import Mock

import compiletools.headertree as headertree
import compiletools.tree as tree


def _make_args(strip_git_root=False, verbose=0):
    args = Mock()
    args.strip_git_root = strip_git_root
    args.verbose = verbose
    return args


def _make_simple_tree():
    """Create: root.h -> child.h -> grandchild.h"""
    t = tree.tree()
    t["root.h"]["child.h"]["grandchild.h"]
    return t


class TestFlatStyle:
    def test_prints_all_nodes(self, capsys):
        t = _make_simple_tree()
        headertree.FlatStyle(t, _make_args())
        out = capsys.readouterr().out
        assert "root.h" in out
        assert "child.h" in out
        assert "grandchild.h" in out


class TestDepthStyle:
    def test_prints_with_depth_indicator(self, capsys):
        t = _make_simple_tree()
        headertree.DepthStyle(t, _make_args())
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        # root at depth 0 has no indicator prefix
        assert lines[0] == "root.h"
        # child at depth 1 has one indicator
        assert lines[1] == "--child.h"
        # grandchild at depth 2 has two indicators
        assert lines[2] == "----grandchild.h"

    def test_custom_indicator(self, capsys):
        t = tree.tree()
        t["a.h"]["b.h"]
        headertree.DepthStyle(t, _make_args(), indicator=">>")
        out = capsys.readouterr().out
        assert ">>b.h" in out


class TestDotStyle:
    def test_dot_output(self, capsys):
        t = tree.tree()
        t["main.h"]["sub.h"]
        headertree.DotStyle(t, _make_args())
        out = capsys.readouterr().out
        assert 'digraph "main.h"' in out
        assert '"main.h"->"sub.h"' in out
        assert out.strip().endswith("}")


class TestTreeStyle:
    def test_tree_output(self, capsys):
        t = _make_simple_tree()
        headertree.TreeStyle(t, _make_args(verbose=0))
        out = capsys.readouterr().out
        assert "root.h" in out
        assert "child.h" in out
        assert "grandchild.h" in out

    def test_tree_verbose_header(self, capsys):
        t = tree.tree()
        t["a.h"]
        headertree.TreeStyle(t, _make_args(verbose=1))
        out = capsys.readouterr().out
        assert "cumulative" in out

    def test_tree_multiple_children(self, capsys):
        t = tree.tree()
        t["root.h"]["a.h"]
        t["root.h"]["b.h"]
        t["root.h"]["c.h"]
        headertree.TreeStyle(t, _make_args())
        out = capsys.readouterr().out
        assert "a.h" in out
        assert "b.h" in out
        assert "c.h" in out

    def test_tree_deep_nesting(self, capsys):
        """Exercise depth > 1 path for internal tree chars."""
        t = tree.tree()
        t["a"]["b"]["c"]["d"]
        headertree.TreeStyle(t, _make_args())
        out = capsys.readouterr().out
        assert "d" in out
