"""Per-TU subset of cmdline `-D` macros referenced by source.

Given the fixed cmdline `-D` macro list and a TU's transitive content hashes,
returns the subset that the TU actually references via word-boundary identifier
scan. Caller owns file bytes via callback; this module is pure.

The scan is byte-level: no preprocessing, no comment stripping, no
string-literal stripping. A macro identifier that appears only in a
``// ...`` or ``/* ... */`` comment, or only inside a string literal,
of a transitive header still counts as referenced. This is conservative
(over-includes are safe — keys are stricter than necessary, never
weaker), but it can defeat per-app/per-config macro isolation when a
documentation comment in a shared header mentions the macro by name.
See the "Macro Scope Filter" section of README.ct-cake.rst for the
recommended generated-header pattern that sidesteps this entirely.
"""

from collections.abc import Callable, Iterable

import stringzilla as sz

_IDENT_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_MISSING = object()


def _is_ident_byte(b: int) -> bool:
    return b in _IDENT_BYTES


class CmdlineMacroIndex:
    def __init__(
        self,
        cmdline_d_macro_names: frozenset[sz.Str],
        bytes_provider: Callable[[str], bytes],
    ):
        self._cmdline_d_macro_names = cmdline_d_macro_names
        self._bytes_provider = bytes_provider
        # Pre-tokenize each macro name to ASCII bytes once, avoiding N*M
        # redundant `bytes(str(macro_name), "ascii")` conversions per TU
        # (N transitive headers x M cmdline-D macros).
        self._needles: dict[sz.Str, bytes] = {macro: bytes(str(macro), "ascii") for macro in cmdline_d_macro_names}
        self._is_referenced_cache: dict[tuple[str, str], bool] = {}
        self._tu_cache: dict[tuple[str, str, str], frozenset[sz.Str]] = {}
        # Cache (data_bytes, sz.Str_wrapper) per content_hash so that
        # scanning N macros against the same file does not rebuild the
        # SIMD wrapper N times. None signals "empty/unknown content".
        self._haystack_cache: dict[str, tuple[bytes, sz.Str] | None] = {}

    def is_referenced(self, content_hash: str, macro_name: sz.Str) -> bool:
        """True if the file contains macro_name as a C identifier."""
        key = (content_hash, str(macro_name))
        cached = self._is_referenced_cache.get(key)
        if cached is not None:
            return cached
        result = self._scan(content_hash, macro_name)
        self._is_referenced_cache[key] = result
        return result

    def tu_referenced_macros(
        self,
        tu_filename: str,
        tu_content_hash: str,
        dep_hash: str,
        transitive_content_hashes: Iterable[str],
    ) -> frozenset[sz.Str]:
        """Subset of cmdline_d_macro_names referenced by the TU or its headers.

        The TU's own bytes (identified by ``tu_content_hash``) are always scanned
        alongside everything in ``transitive_content_hashes``. The cache key is
        ``(tu_filename, tu_content_hash, dep_hash)`` so that if the same logical
        TU name ever maps to different content within one ``CmdlineMacroIndex``
        lifetime (e.g., a file edited mid-build), the cache cannot return a
        stale answer.
        """
        if not self._cmdline_d_macro_names:
            return frozenset()
        cache_key = (tu_filename, tu_content_hash, dep_hash)
        cached = self._tu_cache.get(cache_key)
        if cached is not None:
            return cached
        hashes = [tu_content_hash, *transitive_content_hashes]
        referenced: set[sz.Str] = set()
        for macro in self._cmdline_d_macro_names:
            for content_hash in hashes:
                if self.is_referenced(content_hash, macro):
                    referenced.add(macro)
                    break
        result = frozenset(referenced)
        self._tu_cache[cache_key] = result
        return result

    def _haystack(self, content_hash: str) -> tuple[bytes, sz.Str] | None:
        cached = self._haystack_cache.get(content_hash, _MISSING)
        if cached is _MISSING:
            data = self._bytes_provider(content_hash)
            entry: tuple[bytes, sz.Str] | None = (data, sz.Str(data)) if data else None
            self._haystack_cache[content_hash] = entry
            return entry
        return cached  # type: ignore[return-value]

    def _scan(self, content_hash: str, macro_name: sz.Str) -> bool:
        haystack_pair = self._haystack(content_hash)
        if haystack_pair is None:
            return False
        data, haystack = haystack_pair
        needle = self._needles.get(macro_name)
        if needle is None:
            # `is_referenced` accepts arbitrary macro names; fall back to an
            # on-the-fly conversion when the caller passes one that wasn't
            # pre-tokenized in __init__.
            needle = bytes(str(macro_name), "ascii")
        needle_len = len(needle)
        data_len = len(data)
        pos = haystack.find(needle, 0)
        while pos != -1:
            before_ok = pos == 0 or not _is_ident_byte(data[pos - 1])
            after_pos = pos + needle_len
            after_ok = after_pos >= data_len or not _is_ident_byte(data[after_pos])
            if before_ok and after_ok:
                return True
            pos = haystack.find(needle, pos + 1)
        return False
