from compiletools.flag_ops import dedup_tokens


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
