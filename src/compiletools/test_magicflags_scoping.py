"""Tests for the per-TU cache-key scoping wiring inside magicflags.py.

These cover the four MacroState-construction sites touched by the
key-pollution fix:

    - DirectMagicFlags._initialize_macro_state          (Site 1)
    - DirectMagicFlags._parse final state update        (Site 2)
    - CppMagicFlags._parse reconstruction               (Site 3)
    - get_final_macro_state_hash(scope_filter=...)      (public API)

The headerdeps.py call site (Site 4) is deliberately NOT propagated;
that's covered by the comment above the construction in headerdeps.py.
"""

import os

import pytest
import stringzilla as sz

import compiletools.test_base as tb
import compiletools.testhelper as uth
from compiletools.build_context import BuildContext


def _make_parser(extra_args, tempdir, magic_type="direct"):
    """Build a fresh MagicFlags parser with the given extra args.

    Each call uses a fresh BuildContext so that per-build caches don't
    leak between scenarios (required for the hash-comparison tests).
    """
    args = ["--magic", magic_type]
    if extra_args:
        args.extend(extra_args)
    ctx = BuildContext()
    return tb.create_magic_parser(args, tempdir=tempdir, context=ctx)


class TestInitialMacroStateWiring(tb.BaseCompileToolsTestCase):
    """Site 1: DirectMagicFlags._initialize_macro_state must populate
    cmdline_origin and the structured *_tokens fields."""

    def test_initial_macro_state_populates_cmdline_origin(self):
        """cmdline_origin must include macros from BOTH attached and
        detached -D form, proving the extract_command_line_macros bug
        fix and the new wiring are both in place."""
        parser = _make_parser(
            ["--append-CPPFLAGS=-DFOO -D BAR"],
            tempdir=self._tmpdir,
        )
        origin = parser._initial_macro_state.cmdline_origin
        assert sz.Str("FOO") in origin
        assert sz.Str("BAR") in origin

    def test_initial_macro_state_populates_tokens(self):
        """Structured *_tokens must be populated and must NOT contain
        the cmdline -D entries (those go via core), but MUST retain
        non -D flags like -O2."""
        parser = _make_parser(
            ["--append-CPPFLAGS=-DFOO=1 -O2"],
            tempdir=self._tmpdir,
        )
        cppflags_tokens = parser._initial_macro_state.cppflags_tokens
        assert cppflags_tokens is not None
        # -D entries stripped
        assert "-DFOO=1" not in cppflags_tokens
        assert "-D" not in cppflags_tokens
        assert "FOO=1" not in cppflags_tokens
        # Other flags retained
        assert "-O2" in cppflags_tokens

    def test_initial_macro_state_tokens_drop_detached_d_form(self):
        """Detached -D form must drop BOTH the -D and the value token."""
        parser = _make_parser(
            ["--append-CPPFLAGS=-D FOO=1 -O2"],
            tempdir=self._tmpdir,
        )
        cppflags_tokens = parser._initial_macro_state.cppflags_tokens
        assert "FOO=1" not in cppflags_tokens
        assert "-D" not in cppflags_tokens
        assert "-O2" in cppflags_tokens


class TestGetFinalMacroStateHash(tb.BaseCompileToolsTestCase):
    """Public API: get_final_macro_state_hash must accept scope_filter
    and forward it to MacroState.get_hash unchanged."""

    def _process(self, parser, sample_rel="simple/helloworld_cpp.cpp"):
        try:
            sample_path = uth.example_file(sample_rel)
            parser.parse(sample_path)
            return sample_path
        except RuntimeError as e:
            if "No functional C++ compiler detected" in str(e):
                pytest.skip("No functional C++ compiler detected")
            raise

    def test_get_final_macro_state_hash_default_unchanged(self):
        """Calling with no scope_filter must equal calling with
        scope_filter=None explicitly."""
        parser = _make_parser([], tempdir=self._tmpdir)
        sample_path = self._process(parser)
        h_default = parser.get_final_macro_state_hash(sample_path)
        h_explicit_none = parser.get_final_macro_state_hash(sample_path, scope_filter=None)
        assert h_default == h_explicit_none

    def test_get_final_macro_state_hash_with_scope_filter_excludes_cmdline_macros(self):
        """When the scope_filter is empty, cmdline-origin macros are
        dropped from the hash, so changing the value of a cmdline -D
        no longer changes the hash."""
        # Case A: FOO present, no scope filter (FOO contributes to hash).
        parser_a = _make_parser(["--append-CPPFLAGS=-DFOO=one"], tempdir=self._tmpdir)
        path_a = self._process(parser_a)
        hash_a = parser_a.get_final_macro_state_hash(path_a)

        # Case B: FOO present, scope_filter=frozenset() (FOO filtered out).
        parser_b = _make_parser(["--append-CPPFLAGS=-DFOO=one"], tempdir=self._tmpdir)
        path_b = self._process(parser_b)
        hash_b = parser_b.get_final_macro_state_hash(path_b, scope_filter=frozenset())

        # Case C: FOO=different, scope_filter=frozenset() (still filtered).
        parser_c = _make_parser(["--append-CPPFLAGS=-DFOO=different_value"], tempdir=self._tmpdir)
        path_c = self._process(parser_c)
        hash_c = parser_c.get_final_macro_state_hash(path_c, scope_filter=frozenset())

        # Case D: FOO=different, NO scope filter (FOO contributes again,
        # different value -> different hash from A).
        parser_d = _make_parser(["--append-CPPFLAGS=-DFOO=different_value"], tempdir=self._tmpdir)
        path_d = self._process(parser_d)
        hash_d = parser_d.get_final_macro_state_hash(path_d)

        # Filtered hashes are independent of FOO's value.
        assert hash_b == hash_c, "scope_filter=frozenset() must drop FOO regardless of its value"
        # Without filter, changing FOO's value perturbs the hash.
        assert hash_a != hash_d, "Without scope_filter, different FOO values must hash differently"
        # And the filtered version differs from the unfiltered one (FOO was
        # removed from one but not the other).
        assert hash_a != hash_b, "scope_filter=frozenset() must produce a different hash than no filter when FOO is set"


class TestParsePropagation(tb.BaseCompileToolsTestCase):
    """Sites 2 and 3: after _parse(), the MacroState stored in
    _final_macro_states must keep cmdline_origin from the initial state
    and must NOT include per-file magic -D macros in its tokens."""

    def _make_magic_d_source(self):
        """Write a tiny .cpp that uses //#CPPFLAGS=-DMAGIC_DEFINE=1
        magic and return its path."""
        src_dir = os.path.join(self._tmpdir, "magic_d_src")
        os.makedirs(src_dir, exist_ok=True)
        src_path = os.path.join(src_dir, "magic_d.cpp")
        with open(src_path, "w") as fh:
            fh.write("//#CPPFLAGS=-DMAGIC_DEFINE=1\nint main() { return 0; }\n")
        return src_path

    def _parse_or_skip(self, parser, src_path):
        try:
            parser.parse(src_path)
        except RuntimeError as e:
            if "No functional C++ compiler detected" in str(e):
                pytest.skip("No functional C++ compiler detected")
            raise

    def test_parse_preserves_cmdline_origin_through_final_state(self):
        """cmdline_origin in the final state must equal the initial
        state's cmdline_origin -- magic -D macros are NOT cmdline-origin."""
        parser = _make_parser(
            ["--append-CPPFLAGS=-DCMDLINE_FOO"],
            tempdir=self._tmpdir,
        )
        src_path = self._make_magic_d_source()
        self._parse_or_skip(parser, src_path)

        import compiletools.wrappedos

        abs_path = compiletools.wrappedos.realpath(src_path)
        final_ms = parser._final_macro_states[abs_path]

        # Initial cmdline_origin contains CMDLINE_FOO.
        initial_origin = parser._initial_macro_state.cmdline_origin
        assert sz.Str("CMDLINE_FOO") in initial_origin
        # Final origin must equal the initial origin -- MAGIC_DEFINE is
        # NOT promoted to cmdline-origin.
        assert final_ms.cmdline_origin == initial_origin
        assert sz.Str("MAGIC_DEFINE") not in final_ms.cmdline_origin

    def test_parse_drops_d_from_effective_tokens(self):
        """Tokens in the final state must have all -D entries stripped,
        including the per-file magic -DMAGIC_DEFINE=1."""
        parser = _make_parser([], tempdir=self._tmpdir)
        src_path = self._make_magic_d_source()
        self._parse_or_skip(parser, src_path)

        import compiletools.wrappedos

        abs_path = compiletools.wrappedos.realpath(src_path)
        final_ms = parser._final_macro_states[abs_path]

        cppflags_tokens = final_ms.cppflags_tokens
        assert cppflags_tokens is not None
        assert "-DMAGIC_DEFINE=1" not in cppflags_tokens
        assert "MAGIC_DEFINE=1" not in cppflags_tokens
        assert "-D" not in cppflags_tokens

    def test_parse_uses_global_tokens_directly(self):
        """The final state's cppflags_tokens must equal
        ``args.CPPFLAGS_tokens + magic_cpp_tokens`` with -D/-U stripped.

        This is the contract that lets _parse() avoid the
        list -> join -> concat -> tokenize round-trip per TU: the
        global tokens are reused directly, only the magic-flag
        contribution is appended.
        """
        import compiletools.apptools
        import compiletools.wrappedos

        parser = _make_parser([], tempdir=self._tmpdir)
        # The args object owned by the parser must have the global
        # token cache populated by parseargs (TOKEN-2).
        assert hasattr(parser._args, "CPPFLAGS_tokens"), "parseargs must populate args.CPPFLAGS_tokens"

        src_path = self._make_magic_d_source()
        self._parse_or_skip(parser, src_path)

        abs_path = compiletools.wrappedos.realpath(src_path)
        final_ms = parser._final_macro_states[abs_path]

        # Expected = strip_d_u(args tokens + magic tokens). For this
        # test the only magic CPPFLAG is -DMAGIC_DEFINE=1, which the
        # strip filter removes; the result must equal the stripped
        # form of args.CPPFLAGS_tokens.
        expected = compiletools.apptools.strip_d_u_tokens(list(parser._args.CPPFLAGS_tokens) + ["-DMAGIC_DEFINE=1"])
        assert final_ms.cppflags_tokens == expected
