import compiletools.apptools_canonicalize
import compiletools.flag_ops
from compiletools.flag_ops import (
    dedup_include_paths_to_append,
    dedup_tokens,
    extract_d_macros,
    extract_include_paths_from_tokens,
    strip_d_u_tokens,
    system_include_paths_from_tokens,
)


def test_prefix_map_stems_match_apptools_canonicalize_prefixes():
    """Drift guard: the pure core's prefix-map family list must stay
    identical to the canonicalization layer's -- stage_prefix_map's skip
    check and canonicalize_for_cache_key must agree on what counts as a
    prefix-map flag."""
    assert compiletools.flag_ops._PREFIX_MAP_STEMS == compiletools.apptools_canonicalize._PREFIX_MAP_FLAG_PREFIXES


class TestDedupTokens:
    def test_plain_duplicate_removed_first_occurrence_wins(self):
        assert dedup_tokens(("-O2", "-Wall", "-O2")) == ("-O2", "-Wall")

    def test_detached_and_attached_include_forms_are_one_flag(self):
        assert dedup_tokens(("-I", "/x", "-I/x", "-Wall")) == ("-I", "/x", "-Wall")

    def test_different_paths_survive(self):
        assert dedup_tokens(("-I/x", "-I/y")) == ("-I/x", "-I/y")

    def test_idempotent(self):
        toks = ("-I", "/x", "-DFOO", "-DFOO", "-L/l", "-L", "/l")
        once = dedup_tokens(toks)
        assert dedup_tokens(once) == once

    def test_returns_tuple(self):
        assert isinstance(dedup_tokens(["-O2"]), tuple)


class TestExtractDMacros:
    def test_attached_with_value(self):
        assert extract_d_macros(("-DFOO=bar",)) == {"FOO": "bar"}

    def test_attached_without_value_defaults_to_one(self):
        assert extract_d_macros(("-DFOO",)) == {"FOO": "1"}

    def test_detached_forms(self):
        assert extract_d_macros(("-D", "FOO=1", "-D", "BAR")) == {"FOO": "1", "BAR": "1"}

    def test_value_keeps_embedded_equals(self):
        assert extract_d_macros(("-DKV=a=b",)) == {"KV": "a=b"}

    def test_last_occurrence_wins(self):
        assert extract_d_macros(("-DFOO=1", "-DFOO=2")) == {"FOO": "2"}

    def test_non_d_tokens_and_u_ignored(self):
        assert extract_d_macros(("-Wall", "-UFOO", "-I/x", "-O2")) == {}

    def test_dangling_detached_d_ignored(self):
        assert extract_d_macros(("-Wall", "-D")) == {}

    def test_form_consistency_with_strip_d_u_tokens(self):
        """Every -D form extract_d_macros collects must be a form
        strip_d_u_tokens removes -- a macro collected here but left in
        the stripped tokens (or vice versa) would put it in one macro
        universe but not the other, defeating cache-key scoping."""
        tokens = ("-DA=1", "-D", "B=2", "-DC", "-D", "D", "-Wall")
        collected = set(extract_d_macros(tokens))
        assert collected == {"A", "B", "C", "D"}
        assert strip_d_u_tokens(list(tokens)) == ["-Wall"]


class TestSystemIncludePathsFromTokens:
    def test_attached_and_detached_i(self):
        assert system_include_paths_from_tokens(("-I/a", "-I", "/b")) == ["/a", "/b"]

    def test_isystem_forms(self):
        assert system_include_paths_from_tokens(("-isystem", "/a", "-isystem/b")) == ["/a", "/b"]

    def test_order_preserved_first_wins(self):
        assert system_include_paths_from_tokens(("-I/b", "-I/a", "-I/b")) == ["/b", "/a"]

    def test_non_include_tokens_ignored(self):
        assert system_include_paths_from_tokens(("-Wall", "-DFOO", "-L/lib")) == []

    def test_dangling_flag_ignored(self):
        assert system_include_paths_from_tokens(("-Wall", "-I")) == []


class TestExtractIncludePathsFromTokens:
    def test_attached_and_detached_i(self):
        assert extract_include_paths_from_tokens(("-I/a", "-I", "/b")) == {"/a", "/b"}

    def test_isystem_attached_not_recognized(self):
        # -isystem is a different flag family (system include search path,
        # not project -I) and must not be treated as a -I entry.
        assert extract_include_paths_from_tokens(("-isystem/opt/inc",)) == set()

    def test_isystem_detached_not_recognized(self):
        assert extract_include_paths_from_tokens(("-isystem", "/opt/inc")) == set()

    def test_l_attached_not_recognized(self):
        assert extract_include_paths_from_tokens(("-L/opt/lib",)) == set()

    def test_l_detached_not_recognized(self):
        assert extract_include_paths_from_tokens(("-L", "/opt/lib")) == set()

    def test_non_include_tokens_ignored(self):
        assert extract_include_paths_from_tokens(("-Wall", "-DFOO")) == set()

    def test_dangling_i_ignored(self):
        assert extract_include_paths_from_tokens(("-Wall", "-I")) == set()


class TestDedupIncludePathsToAppend:
    """Regression guard: a path reachable only via a different flag family
    (-isystem, -L) must not be mistaken for an already-present -I entry --
    otherwise a caller that wants the path on the project -I search list
    would silently lose the append."""

    def test_isystem_attached_does_not_suppress_append(self):
        existing = ("-isystem/opt/inc",)
        assert dedup_include_paths_to_append(existing, ["/opt/inc"]) == ["-I", "/opt/inc"]

    def test_isystem_detached_does_not_suppress_append(self):
        existing = ("-isystem", "/opt/inc")
        assert dedup_include_paths_to_append(existing, ["/opt/inc"]) == ["-I", "/opt/inc"]

    def test_l_attached_does_not_suppress_append(self):
        existing = ("-L/opt/lib",)
        assert dedup_include_paths_to_append(existing, ["/opt/lib"]) == ["-I", "/opt/lib"]

    def test_l_detached_does_not_suppress_append(self):
        existing = ("-L", "/opt/lib")
        assert dedup_include_paths_to_append(existing, ["/opt/lib"]) == ["-I", "/opt/lib"]

    def test_attached_i_is_deduped(self):
        existing = ("-I/opt/inc",)
        assert dedup_include_paths_to_append(existing, ["/opt/inc"]) == []

    def test_detached_i_is_deduped(self):
        existing = ("-I", "/opt/inc")
        assert dedup_include_paths_to_append(existing, ["/opt/inc"]) == []

    def test_multiple_paths_appended_with_duplicate_input_self_deduped(self):
        # The loop accumulates its own seen-set as it appends, so a path
        # repeated within new_paths must only be appended once.
        result = dedup_include_paths_to_append((), ["/a", "/b", "/a"])
        assert result == ["-I", "/a", "-I", "/b"]
