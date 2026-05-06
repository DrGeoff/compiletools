"""Per-TU subset of cmdline `-D` macros referenced by source.

Given the fixed cmdline `-D` macro list and a TU's transitive content hashes,
returns the subset that the TU actually references via word-boundary identifier
scan. Caller owns file bytes via callback; this module is pure.
"""

from collections.abc import Callable, Iterable

import stringzilla as sz

_IDENT_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


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
        self._needles: dict[sz.Str, bytes] = {
            macro: bytes(str(macro), "ascii") for macro in cmdline_d_macro_names
        }
        self._is_referenced_cache: dict[tuple[str, str], bool] = {}
        self._tu_cache: dict[tuple[str, str, str], frozenset[sz.Str]] = {}

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

    def _scan(self, content_hash: str, macro_name: sz.Str) -> bool:
        data = self._bytes_provider(content_hash)
        if not data:
            return False
        needle = self._needles.get(macro_name)
        if needle is None:
            # `is_referenced` accepts arbitrary macro names; fall back to an
            # on-the-fly conversion when the caller passes one that wasn't
            # pre-tokenized in __init__.
            needle = bytes(str(macro_name), "ascii")
        haystack = sz.Str(data)
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
