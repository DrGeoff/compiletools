import pytest
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


@pytest.mark.parametrize(
    ("source", "macro", "expected"),
    [
        pytest.param(b"int FOO = 1;", "FOO", True, id="exact-match"),
        pytest.param(b"int FOOD = 1;", "FOO", False, id="substring-does-not-match"),
        pytest.param(b"int FOO_X = 1;", "FOO", False, id="trailing-underscore-does-not-match"),
        pytest.param(b"int MY_FOO = 1;", "FOO", False, id="leading-underscore-does-not-match"),
        pytest.param(b"FOO = 1;", "FOO", True, id="start-of-buffer"),
        pytest.param(b"int x = FOO", "FOO", True, id="end-of-buffer"),
        pytest.param(b'printf("%s", "FOO");', "FOO", True, id="string-literal"),
        pytest.param(b"// FOO is unused", "FOO", True, id="comment"),
        pytest.param(b"#ifdef FOO\n#endif", "FOO", True, id="ifdef"),
        pytest.param(b"const char* x = APP_NAME;", "APP_NAME", True, id="value-position"),
        pytest.param(b"int FOO2 = 1;", "FOO", False, id="digit-suffix-does-not-match"),
        pytest.param(b"int x = 1;", "FOO", False, id="absent"),
        pytest.param(b"int FOOD = FOO;", "FOO", True, id="substring-and-real-match"),
    ],
)
def test_identifier_reference_detection(source, macro, expected):
    idx, _ = _index_with({"h": source}, [macro])
    assert idx.is_referenced("h", sz.Str(macro)) is expected


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
    result = idx.tu_referenced_macros("tu.cpp", "hash_tu", "depABC", ["hash_h1", "hash_h2"])
    assert result == frozenset({sz.Str("FOO"), sz.Str("BAR")})


def test_tu_referenced_macros_scans_tu_bytes_when_transitive_empty():
    byte_dict = {"hash_tu": b"int x = FOO;"}
    idx, provider = _index_with(byte_dict, ["FOO"])
    result = idx.tu_referenced_macros("tu.cpp", "hash_tu", "depABC", [])
    assert result == frozenset({sz.Str("FOO")})
    assert "hash_tu" in provider.calls


def test_tu_referenced_macros_caches_per_tu_state():
    byte_dict = {
        "hash_tu": b"int x = FOO;",
        "hash_h1": b"int y = BAR;",
    }
    idx, provider = _index_with(byte_dict, ["FOO", "BAR"])
    idx.tu_referenced_macros("tu.cpp", "hash_tu", "depABC", ["hash_h1"])
    calls_after_first = provider.call_count
    # Same (tu_filename, tu_content_hash, dep_hash) -> cache hit, no new provider calls.
    idx.tu_referenced_macros("tu.cpp", "hash_tu", "depABC", ["hash_h1"])
    assert provider.call_count == calls_after_first


def test_tu_referenced_macros_empty_cmdline_d_short_circuits():
    byte_dict = {"hash_tu": b"int x = FOO;", "hash_h1": b"int y = BAR;"}
    idx, provider = _index_with(byte_dict, [])
    result = idx.tu_referenced_macros("tu.cpp", "hash_tu", "depABC", ["hash_h1"])
    assert result == frozenset()
    assert provider.call_count == 0
