import stringzilla as sz

from compiletools.cmdline_macro_index import CmdlineMacroIndex


class _CountingProvider:
    def __init__(self, byte_dict: dict[str, bytes]):
        self._byte_dict = byte_dict
        self.call_count = 0
        self.calls: list[str] = []

    def __call__(self, content_hash: str) -> bytes:
        self.call_count += 1
        self.calls.append(content_hash)
        return self._byte_dict[content_hash]


def _index_with(byte_dict: dict[str, bytes], names: list[str]) -> tuple[CmdlineMacroIndex, _CountingProvider]:
    provider = _CountingProvider(byte_dict)
    cmdline = frozenset(sz.Str(n) for n in names)
    return CmdlineMacroIndex(cmdline, provider), provider


def test_identifier_exact_match():
    idx, _ = _index_with({"h": b"int FOO = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_substring_does_not_match():
    idx, _ = _index_with({"h": b"int FOOD = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is False


def test_identifier_with_trailing_underscore_does_not_match():
    idx, _ = _index_with({"h": b"int FOO_X = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is False


def test_identifier_with_leading_underscore_does_not_match():
    idx, _ = _index_with({"h": b"int MY_FOO = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is False


def test_identifier_at_start_of_buffer():
    idx, _ = _index_with({"h": b"FOO = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_at_end_of_buffer():
    idx, _ = _index_with({"h": b"int x = FOO"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_in_string_literal_matches():
    idx, _ = _index_with({"h": b'printf("%s", "FOO");'}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_in_comment_matches():
    idx, _ = _index_with({"h": b"// FOO is unused"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_in_ifdef_matches():
    idx, _ = _index_with({"h": b"#ifdef FOO\n#endif"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True


def test_identifier_in_value_position_matches():
    idx, _ = _index_with({"h": b"const char* x = APP_NAME;"}, ["APP_NAME"])
    assert idx.is_referenced("h", sz.Str("APP_NAME")) is True


def test_identifier_with_digit_suffix_does_not_match():
    idx, _ = _index_with({"h": b"int FOO2 = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is False


def test_is_referenced_caches_result():
    idx, provider = _index_with({"h": b"int FOO = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True
    assert idx.is_referenced("h", sz.Str("FOO")) is True
    assert provider.call_count == 1


def test_tu_referenced_macros_unions_transitive():
    byte_dict = {
        "hash_tu": b"int x = FOO;",
        "hash_h1": b"int uses = BAR;",
        "hash_h2": b"int unrelated = 1;",
    }
    idx, _ = _index_with(byte_dict, ["FOO", "BAR", "BAZ"])
    result = idx.tu_referenced_macros("tu.cpp", "depABC", ["hash_tu", "hash_h1", "hash_h2"])
    assert result == frozenset({sz.Str("FOO"), sz.Str("BAR")})


def test_tu_referenced_macros_caches_by_dep_hash():
    byte_dict = {
        "hash_tu": b"int x = FOO;",
        "hash_h1": b"int y = BAR;",
    }
    idx, provider = _index_with(byte_dict, ["FOO", "BAR"])
    idx.tu_referenced_macros("tu.cpp", "depABC", ["hash_tu", "hash_h1"])
    calls_after_first = provider.call_count
    idx.tu_referenced_macros("tu.cpp", "depABC", ["hash_tu", "hash_h1"])
    assert provider.call_count == calls_after_first


def test_tu_referenced_macros_empty_cmdline_d_short_circuits():
    byte_dict = {"hash_tu": b"int x = FOO;", "hash_h1": b"int y = BAR;"}
    idx, provider = _index_with(byte_dict, [])
    result = idx.tu_referenced_macros("tu.cpp", "depABC", ["hash_tu", "hash_h1"])
    assert result == frozenset()
    assert provider.call_count == 0


def test_no_match_when_macro_name_absent():
    idx, _ = _index_with({"h": b"int x = 1;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is False


def test_multiple_occurrences_one_real_one_substring():
    idx, _ = _index_with({"h": b"int FOOD = FOO;"}, ["FOO"])
    assert idx.is_referenced("h", sz.Str("FOO")) is True
