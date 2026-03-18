"""Tests for tree module."""

from compiletools.tree import InTree, depth_first_traverse, dicts, flatten, tree


def test_dicts():
    t = tree()
    t["a"]["b"]["c"]
    t["a"]["d"]
    result = dicts(t)
    assert result == {"a": {"b": {"c": {}}, "d": {}}}


def test_flatten():
    t = tree()
    t["a"]["b"]["c"]
    t["a"]["d"]
    assert flatten(t) == {"a", "b", "c", "d"}


def test_depth_first_traverse_pre():
    t = tree()
    t["a"]["b"]
    t["a"]["c"]
    visited = []
    depth_first_traverse(t, pre_traverse_function=lambda key: visited.append(key))
    assert visited[0] == "a"
    assert set(visited[1:]) == {"b", "c"}


def test_depth_first_traverse_post():
    t = tree()
    t["a"]["b"]
    visited = []
    depth_first_traverse(t, post_traverse_function=lambda key: visited.append(key))
    assert visited == ["b", "a"]


def test_depth_first_traverse_named_args():
    t = tree()
    t["root"]["child"]
    results = []

    def visitor(key, value, depth):
        results.append((key, depth))

    depth_first_traverse(t, pre_traverse_function=visitor)
    assert results == [("root", 0), ("child", 1)]


def test_in_tree_present():
    t = tree()
    t["a"]["b"]["c"]
    in_tree = InTree(t)
    assert in_tree("b") is True


def test_in_tree_absent():
    t = tree()
    t["a"]["b"]
    in_tree = InTree(t)
    assert in_tree("z") is False
