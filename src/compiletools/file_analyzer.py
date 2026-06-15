"""File analysis module for efficient pattern detection in source files.

This module provides SIMD-optimized file analysis built on StringZilla, which is
a required dependency (imported unconditionally below).
"""

import bisect
import builtins
import mmap
import resource
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from compiletools.build_context import BuildContext

import stringzilla
from stringzilla import Str

import compiletools.filesystem_utils
import compiletools.wrappedos
from compiletools.stringzilla_utils import (
    ends_with_backslash_sz,
    is_alpha_or_underscore_sz,
    join_lines_strip_backslash_sz,
    strip_sz,
)


class MarkerType(Enum):
    """Type of marker found in source file."""

    NONE = 0
    EXE = 1
    TEST = 2
    LIBRARY = 3


def is_position_commented_simd_optimized(
    str_text: "stringzilla.Str",
    pos: int,
    line_byte_offsets: list[int],
    block_comment_spans: "list[tuple[int, int]] | None" = None,
) -> bool:
    """Optimized comment detection using pre-computed line boundaries.

    ``block_comment_spans`` (from :func:`find_block_comment_spans`) is the
    file's block-comment spans precomputed once per scan. When supplied, the
    block-comment test is an O(log n) ``_pos_in_spans`` lookup; when omitted it
    falls back to recomputing the spans, so callers off the hot path keep the
    old single-argument behaviour.
    """
    # Binary search for line start using precomputed line starts
    line_start_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
    line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0

    # Check for single-line comment on current line using StringZilla
    line_prefix_slice = str_text[line_start:pos]
    if line_prefix_slice.find("//") != -1:
        return True

    # Block-comment check: reuse precomputed spans on the hot path, else recompute.
    if block_comment_spans is not None:
        return _pos_in_spans(block_comment_spans, pos)
    return is_inside_block_comment_simd(str_text, pos)


def _skip_quoted_literal(str_text: "stringzilla.Str", start: int, quote: str, n: int) -> int:
    """Return the index just past a string/char literal that opened at ``start``.

    ``start`` points at the first byte *after* the opening quote. Backslash
    escapes are honoured so an escaped quote does not close the literal. If the
    literal is unterminated, ``n`` (end of text) is returned.
    """
    i = start
    delims = "\\" + quote
    while i < n:
        k = str_text.find_first_of(delims, i)
        if k == -1:
            return n
        if str_text[k : k + 1] == "\\":
            i = k + 2  # skip the escaped byte
        else:
            return k + 1  # consumed the closing quote
    return n


def find_block_comment_spans(str_text: "stringzilla.Str") -> list[tuple[int, int]]:
    """Compute block-comment byte ranges in a single forward pass.

    Returns a sorted list of ``(start, end)`` half-open intervals covering every
    ``/* ... */`` block comment, where ``end`` is the index just past the closing
    ``*/`` (or ``len(str_text)`` for an unterminated comment). The scan is
    comment/string aware: a ``/*`` that appears inside a ``//`` line comment or
    inside a string/char literal does NOT open a block comment. This is the
    authoritative source for block-comment membership, replacing the naive
    backwards ``rfind`` that mistook such markers for real comments.
    """
    spans: list[tuple[int, int]] = []
    n = len(str_text)
    i = 0
    while i < n:
        j = str_text.find_first_of("/\"'", i)
        if j == -1:
            break
        ch = str_text[j : j + 1]
        if ch == "/":
            nxt = str_text[j + 1 : j + 2]
            if nxt == "*":
                end = str_text.find("*/", j + 2)
                if end == -1:
                    spans.append((j, n))
                    break
                end += 2
                spans.append((j, end))
                i = end
            elif nxt == "/":
                eol = str_text.find("\n", j + 2)
                i = n if eol == -1 else eol + 1
            else:
                i = j + 1
        else:  # opening of a string (") or char (') literal
            i = _skip_quoted_literal(str_text, j + 1, str(ch), n)
    return spans


def _pos_in_spans(spans: list[tuple[int, int]], pos: int) -> bool:
    """True if ``pos`` falls inside any half-open ``(start, end)`` span.

    ``spans`` must be sorted and non-overlapping (as produced by
    :func:`find_block_comment_spans`), so membership is an O(log n) search.
    """
    lo, hi = 0, len(spans)
    while lo < hi:
        mid = (lo + hi) // 2
        start, end = spans[mid]
        if end <= pos:
            lo = mid + 1
        elif start > pos:
            hi = mid
        else:
            return True
    return False


def is_inside_block_comment_simd(str_text: "stringzilla.Str", pos: int) -> bool:
    """Check if position is inside a multi-line block comment using StringZilla.

    Comment/string aware via :func:`find_block_comment_spans` so a ``/*`` marker
    inside a ``//`` line comment or a string literal is not mistaken for a real
    block comment (fixes the rest-of-file blinding bug).
    """
    return _pos_in_spans(find_block_comment_spans(str_text), pos)


def _is_directive_start(
    str_text: "stringzilla.Str",
    marker_pos: int,
    line_byte_offsets: list[int],
    block_comment_spans: list[tuple[int, int]],
) -> bool:
    """True if a preprocessor marker at ``marker_pos`` begins a real directive.

    A directive (or magic ``//#``) marker is genuine only when it is preceded on
    its physical line by whitespace alone AND it does not fall inside a ``/* */``
    block comment. ``block_comment_spans`` is precomputed once per scan
    (:func:`find_block_comment_spans`) so membership is O(log n) rather than the
    old O(n*m) recompute on every marker.

    The whitespace-only prefix rule is also what rejects a ``#include`` that only
    *looks* like one because it sits inside a string literal (e.g.
    ``const char* s = "#include <x>";``): the ``#`` there has non-whitespace
    bytes before it on the line. Routing every include through this single gate is
    what closed the N9 "ghost header from a string literal" bug that the old,
    separate include scan lacked.
    """
    # Whitespace-only line prefix. A *closed* block comment counts as whitespace
    # per the standard (`/* c */ #if FOO` is a valid directive, A21), so the gaps
    # between the prefix's block-comment spans — not the raw prefix — must be
    # whitespace-only. An UNclosed comment extends past marker_pos and is caught
    # by the block-comment membership test below, so it is not special-cased here.
    line_start_idx = bisect.bisect_right(line_byte_offsets, marker_pos) - 1
    line_start = line_byte_offsets[line_start_idx] if line_start_idx >= 0 else 0
    if marker_pos > line_start:
        cursor = line_start
        # Start at the first span that could overlap [line_start, marker_pos):
        # the last span whose start <= line_start (it may extend in), or the next.
        si = bisect.bisect_right(block_comment_spans, line_start, key=lambda s: s[0]) - 1
        if si < 0:
            si = 0
        for s_start, s_end in block_comment_spans[si:]:
            if s_start >= marker_pos:
                break
            if s_end <= cursor:
                continue
            gap_end = min(s_start, marker_pos)
            if cursor < gap_end and str_text[cursor:gap_end].find_first_not_of(" \t\r\n") != -1:
                return False
            cursor = max(cursor, min(s_end, marker_pos))
        if cursor < marker_pos and str_text[cursor:marker_pos].find_first_not_of(" \t\r\n") != -1:
            return False

    # Not inside a block comment.
    return not _pos_in_spans(block_comment_spans, marker_pos)


def _strip_trailing_comment_sz(s: "stringzilla.Str") -> "stringzilla.Str":
    """Return ``s`` truncated before the first ``//`` or ``/*`` not inside a literal.

    Directive operands, values, and conditions may carry a trailing comment
    (``#define V 100 // note``); the comment is not part of the operand. The scan
    is string/char-literal aware so a ``//`` or ``/*`` inside a literal (e.g. a
    URL ``"http://x"`` or a path) is preserved. The caller is responsible for any
    surrounding whitespace strip.
    """
    n = len(s)
    i = 0
    while i < n:
        j = s.find_first_of("/\"'", i)
        if j == -1:
            break
        ch = s[j : j + 1]
        if ch == "/":
            nxt = s[j + 1 : j + 2]
            if nxt == "/" or nxt == "*":
                return s[:j]
            i = j + 1
        else:  # opening of a string (") or char (') literal
            i = _skip_quoted_literal(s, j + 1, str(ch), n)
    return s


def _include_positions_from_directives(directives: list["PreprocessorDirective"]) -> list[int]:
    """Byte positions of include directives, derived from the cleaned directive list.

    This is the production derivation: continuation lines have already been
    absorbed by :func:`_extract_directives`, so an N13 backslash-continued
    ``#include \\`` <newline> ``"foo.h"`` yields one include position, not a
    phantom on the continuation line. ``#include`` and ``#include_next`` (the
    header-wrapper idiom) both name real dependencies and are parsed by the same
    ``include``-prefix extractor; ``include_next`` keeps its own directive type
    rather than folding into ``include`` — otherwise its operand would carry the
    stray ``_next`` suffix.
    """
    return [d.byte_pos for d in directives if d.directive_type in ("include", "include_next")]


def find_magic_positions_simd_bulk(str_text, line_byte_offsets: list[int], block_comment_spans=None) -> list[int]:
    """Optimized magic position finder using pre-computed line byte offsets.

    Vectorization: Single-pass search avoiding intermediate list allocation.

    ``block_comment_spans`` may be supplied by the caller (``analyze_file`` computes
    it once and threads it to all consumers); recomputed here only when omitted.
    """
    positions = []

    # Precompute block-comment spans once so the per-marker gate is O(log n).
    if block_comment_spans is None:
        block_comment_spans = find_block_comment_spans(str_text)

    # Single-pass search with inline validation
    pos = str_text.find("//#", 0)
    while pos != -1:
        # Reject markers with a non-whitespace line prefix or inside a block comment.
        if not _is_directive_start(str_text, pos, line_byte_offsets, block_comment_spans):
            pos = str_text.find("//#", pos + 3)
            continue

        # Look for KEY=value pattern after //# using StringZilla
        after_hash = pos + 3
        # Find the end of this line using line_byte_offsets
        current_line_idx = bisect.bisect_right(line_byte_offsets, pos) - 1
        if current_line_idx + 1 < len(line_byte_offsets):
            line_end = line_byte_offsets[current_line_idx + 1] - 1  # End before next line starts
        else:
            line_end = len(str_text)  # Last line

        # Use StringZilla slice to find = efficiently
        line_content_slice = str_text[after_hash:line_end]
        equals_pos = line_content_slice.find("=")
        if equals_pos != -1:
            # Extract key part using StringZilla slice
            key_slice = line_content_slice[:equals_pos]

            # Use StringZilla's character set operations for efficient whitespace trimming
            start_pos = key_slice.find_first_not_of(" \t")
            if start_pos != -1:
                end_pos = key_slice.find_last_not_of(" \t")
                trimmed_key = key_slice[start_pos : end_pos + 1]
            else:
                trimmed_key = key_slice[0:0]  # Empty slice

            if len(trimmed_key) > 0:
                # Validate key format using StringZilla character set operations
                if (
                    trimmed_key.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
                    == -1
                ):
                    if is_alpha_or_underscore_sz(trimmed_key, 0):
                        positions.append(pos)

        # Continue search from next position
        pos = str_text.find("//#", pos + 3)

    return positions


def find_directive_positions_simd_bulk(
    str_text, line_byte_offsets: list[int], block_comment_spans=None
) -> dict[str, list[int]]:
    """Optimized directive position finder using pre-computed newline positions.

    Vectorization: Single-pass search without intermediate list allocation.

    This is the single source of directive positions — ``#include`` /
    ``#include_next`` included — so the whitespace-prefix and block-comment gates
    in :func:`_is_directive_start` apply uniformly. The earlier design had a
    separate, weaker include scan that re-introduced the N9 (string-literal ghost)
    and A18 (spaced ``# include``) bugs; folding includes into this finder retired
    it.

    ``block_comment_spans`` may be supplied by the caller (``analyze_file`` computes
    it once and threads it to all consumers); recomputed here only when omitted.
    """
    directive_positions = {}

    # Pre-define common directives for faster lookup
    target_directives = {
        "include",
        "include_next",
        "ifdef",
        "ifndef",
        "define",
        "undef",
        "endif",
        "else",
        "elif",
        "pragma",
        "error",
        "warning",
        "line",
        "if",
    }

    # Precompute block-comment spans once so the per-marker gate is O(log n).
    if block_comment_spans is None:
        block_comment_spans = find_block_comment_spans(str_text)

    # Single-pass search: find and process each # character
    hash_pos = str_text.find("#", 0)
    while hash_pos != -1:
        # Reject markers that are not real directive starts: a non-whitespace
        # line prefix (e.g. inside a string literal) or inside a block comment.
        if not _is_directive_start(str_text, hash_pos, line_byte_offsets, block_comment_spans):
            hash_pos = str_text.find("#", hash_pos + 1)
            continue

        # Extract directive name efficiently
        directive_start = hash_pos + 1
        # Tolerate whitespace between the marker and the keyword: `#   include`
        # and `# if` are valid directives (A18). The retired separate include scan
        # matched only the tight `#include` spelling and missed these.
        directive_start = str_text.find_first_not_of(" \t", directive_start)
        if directive_start == -1:
            hash_pos = str_text.find("#", hash_pos + 1)
            continue

        # Find end of directive name using character set
        directive_end = str_text.find_first_not_of(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", directive_start
        )
        if directive_end == -1:  # Directive takes up rest of string
            directive_end = len(str_text)

        if directive_end > directive_start:
            # Use StringZilla slice for directive name
            directive_slice = str_text[directive_start:directive_end]

            # Check if directive matches any target directive using StringZilla direct comparison
            for target_directive in target_directives:
                # Use StringZilla's efficient string comparison
                if directive_slice == target_directive:
                    if target_directive not in directive_positions:
                        directive_positions[target_directive] = []
                    directive_positions[target_directive].append(hash_pos)
                    break

        # Continue search from next position
        hash_pos = str_text.find("#", hash_pos + 1)

    return directive_positions


def parse_directive_struct(
    dtype: str, pos: int, line_num: int, directive_lines: list["stringzilla.Str"]
) -> "PreprocessorDirective":
    """Parse a directive into structured form using StringZilla operations."""
    full_text_str = join_lines_strip_backslash_sz(directive_lines)

    directive = PreprocessorDirective(
        line_num=line_num, byte_pos=pos, directive_type=dtype, continuation_lines=len(directive_lines) - 1
    )

    # Find start of content after directive
    content_start_pos = full_text_str.find(dtype)
    if content_start_pos == -1:
        return directive
    content_start_pos += len(dtype)

    # Skip whitespace after directive
    content_start_pos = full_text_str.find_first_not_of(" \t", content_start_pos)
    if content_start_pos == -1:
        return directive

    # Drop any trailing comment so it does not leak into the operand/value/condition.
    content_slice = _strip_trailing_comment_sz(full_text_str[content_start_pos:])

    if dtype in ("ifdef", "ifndef", "undef"):
        directive.macro_name = strip_sz(content_slice)

    elif dtype in ("if", "elif"):
        directive.condition = strip_sz(content_slice)

    elif dtype == "define":
        # A macro is function-like ONLY when '(' immediately follows the name
        # with no intervening whitespace (C standard): `#define F(x)` is
        # function-like, `#define F (x)` is an object macro whose value is `(x)`.
        # Splitting on the first whitespace is wrong because a function-like
        # parameter list may itself contain spaces (`#define F(a, b) ...`) (N2).
        paren_pos = content_slice.find("(")
        space_pos = content_slice.find_first_of(" \t")
        if paren_pos != -1 and (space_pos == -1 or paren_pos < space_pos):
            name_end = paren_pos
        else:
            name_end = space_pos

        if name_end == -1:  # bare macro, no params, no value
            directive.macro_name = strip_sz(content_slice)
            directive.macro_value = None
        else:
            directive.macro_name = content_slice[:name_end]
            if paren_pos == name_end:  # function-like: value follows the ')'
                params_end = content_slice.find(")", paren_pos + 1)
                value_start = content_slice.find_first_not_of(" \t", params_end + 1) if params_end != -1 else -1
            else:  # object-like: value follows the whitespace
                value_start = content_slice.find_first_not_of(" \t", name_end)

            if value_start != -1:
                value = strip_sz(content_slice[value_start:])
                directive.macro_value = value if len(value) > 0 else None
            else:
                directive.macro_value = None

    elif dtype == "include":
        directive.condition = strip_sz(content_slice)

    elif dtype == "pragma":
        # Extract pragma name (e.g., "once" from "#pragma once")
        directive.macro_name = strip_sz(content_slice)

    return directive


def _warn_low_ulimit(total_files: int, soft_limit: int, context: "BuildContext"):
    """Warn once about low file descriptor limit."""
    if context.warned_low_ulimit:
        return
    args = context.analyzer_args

    if args and getattr(args, "suppress_fd_warnings", False):
        return

    print(f"Warning: File descriptor limit too low for mmap mode (ulimit -n = {soft_limit})", file=sys.stderr)
    print(f"  Total files: {total_files}, available FDs (90% of limit): {int(soft_limit * 0.9)}", file=sys.stderr)
    print("  Using traditional file I/O instead of mmap to avoid 'Too many open files' errors", file=sys.stderr)
    print("  This is ~0.1-0.2ms slower per file but prevents EMFILE errors", file=sys.stderr)
    print(f"  To use faster mmap mode: ulimit -n {total_files * 2}", file=sys.stderr)
    print("  To suppress this warning: add '--suppress-fd-warnings' flag or config", file=sys.stderr)
    context.warned_low_ulimit = True


def _detach_str(s: "stringzilla.Str") -> "stringzilla.Str":
    """Return a stringzilla.Str that owns its own bytes (pins no larger buffer).

    A slice of a parent Str is a zero-copy *view* that keeps the entire parent
    buffer alive. ``Str(str(s))`` round-trips through an independent Python str,
    allocating a fresh buffer sized to the token alone.
    """
    return Str(str(s))


def _detach_file_analysis_result(result: "FileAnalysisResult") -> None:
    """Detach every retained Str so the cached result stops pinning the file (A7).

    Every Str field is a view into the decoded-text buffer (or a small per-line/
    per-directive join buffer). Because ``FileAnalysisResult`` is cached for the
    whole build, those views would keep every analyzed file's full text resident.
    The retained tokens are tiny next to the source, so copying them out and
    releasing the parents is a net memory win. Mutates ``result`` in place.

    Set/frozenset membership is preserved: ``hash(Str)`` is content-based, so
    rebuilt copies collide with the originals' keys.
    """
    for inc in result.includes:
        for key in ("full_line", "filename"):
            if isinstance(inc.get(key), Str):
                inc[key] = _detach_str(inc[key])
    for mf in result.magic_flags:
        for key in ("full_line", "key", "value"):
            if isinstance(mf.get(key), Str):
                mf[key] = _detach_str(mf[key])
    for d in result.defines:
        for key in ("name", "value"):
            if isinstance(d.get(key), Str):
                d[key] = _detach_str(d[key])
        for key in ("lines", "params"):
            seq = d.get(key)
            if isinstance(seq, list):
                d[key] = [_detach_str(x) if isinstance(x, Str) else x for x in seq]
    result.system_headers = {_detach_str(h) for h in result.system_headers}
    result.quoted_headers = {_detach_str(h) for h in result.quoted_headers}
    result.conditional_macros = frozenset(_detach_str(m) for m in result.conditional_macros)
    if result.include_guard is not None:
        result.include_guard = _detach_str(result.include_guard)
    # directive_by_line shares these objects, so detaching here covers both.
    for directive in result.directives:
        if directive.condition is not None:
            directive.condition = _detach_str(directive.condition)
        if directive.macro_name is not None:
            directive.macro_name = _detach_str(directive.macro_name)
        if directive.macro_value is not None:
            directive.macro_value = _detach_str(directive.macro_value)


def _determine_file_reading_strategy(context: "BuildContext") -> str:
    """Determine which file reading strategy to use for this session.

    Returns:
        'mmap' - Use Str(File(filepath)) directly (best performance)
        'no_mmap' - Use traditional open()/read() (for low ulimit or problematic filesystems)
    """
    if context.file_reading_strategy is not None:
        return context.file_reading_strategy
    args = context.analyzer_args

    # Check for manual overrides first
    if args and not getattr(args, "use_mmap", True):
        strategy = "no_mmap"
        context.file_reading_strategy = strategy
        return strategy

    if args and getattr(args, "force_mmap", False):
        strategy = "mmap"
        context.file_reading_strategy = strategy
        return strategy

    # Get total file count from global hash registry
    try:
        from compiletools.global_hash_registry import get_registry_stats, load_hashes

        load_hashes(context=context)
        stats = get_registry_stats(context=context)
        total_files = stats.get("total_files", 0)
    except (ImportError, AttributeError):
        total_files = 0

    # Query actual OS limit
    try:
        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, AttributeError):
        soft_limit = 1024  # Reasonable fallback

    # If ulimit is dangerously low (< 100), always use no_mmap mode
    if soft_limit < 100:
        if total_files > 0:
            _warn_low_ulimit(total_files, soft_limit, context)
        strategy = "no_mmap"
        context.file_reading_strategy = strategy
        return strategy

    # Compare file count to available fd limit
    safe_fd_limit = int(soft_limit * 0.9)

    if total_files > 0 and total_files > safe_fd_limit:
        _warn_low_ulimit(total_files, soft_limit, context)
        strategy = "no_mmap"
    else:
        strategy = "mmap"

    context.file_reading_strategy = strategy

    return strategy


def set_analyzer_args(args, context: "BuildContext"):
    """Set args for file analysis. Must be called once at build start.

    Args:
        args: Args object containing max_read_size, verbose, exemarkers, testmarkers, librarymarkers
        context: BuildContext where state is stored
    """
    context.analyzer_args = args
    context.file_reading_strategy = None
    _determine_file_reading_strategy(context)


def _load_file_text(filepath: str, file_size: int, max_read_size: int, strategy: str):
    """Load file text into a stringzilla.Str, honoring strategy and truncation.

    Returns:
        tuple: (str_text, bytes_analyzed, was_truncated)
    """
    # Handle empty files - StringZilla cannot memory-map zero-byte files
    if file_size == 0:
        return Str(""), 0, False

    read_entire_file = (max_read_size == 0) or (file_size <= max_read_size)

    if read_entire_file:
        # Read entire file. The strategy only governs the ulimit/resource path
        # (mmap vs read()); filesystem safety is handled by safe_read_text_file.
        str_text = compiletools.filesystem_utils.safe_read_text_file(
            filepath, encoding="utf-8", force_no_mmap=(strategy == "no_mmap")
        )
        return str_text, len(str_text), False

    # Read limited amount using mmap for better performance
    text, bytes_analyzed, was_truncated = read_file_mmap(filepath, max_read_size)
    try:
        str_text = Str(text)
    except UnicodeDecodeError:
        # This shouldn't happen since read_file_mmap decodes with errors='ignore'
        # But if it does, provide useful debugging info
        print(f"ERROR: Failed to create Str from text in {filepath}", file=sys.stderr)
        print(f"  text type: {type(text)}, len: {len(text)}", file=sys.stderr)
        print(f"  First 100 chars: {text[:100]!r}", file=sys.stderr)
        raise
    return str_text, bytes_analyzed, was_truncated


def _compute_line_byte_offsets(str_text) -> list[int]:
    """Build the list of byte offsets where each line begins."""
    line_byte_offsets = [0]  # First line starts at position 0
    pos = str_text.find("\n", 0)
    while pos != -1:
        line_byte_offsets.append(pos + 1)  # Next line starts after newline
        pos = str_text.find("\n", pos + 1)  # Continue from next position
    return line_byte_offsets


def _extract_directives(
    directive_positions: dict[str, list[int]],
    lines: list["stringzilla.Str"],
    line_byte_offsets: list[int],
) -> tuple[list["PreprocessorDirective"], dict[int, "PreprocessorDirective"]]:
    """Extract structured directive records from raw directive positions.

    Honors line continuations (lines ending with backslash). Each line is
    processed only once even if it appears under multiple directive types.

    Positions are processed in strict source (byte) order. ``directive_positions``
    is keyed by type, so its values interleave out of source order; iterating it
    type-by-type would let a later type's continuation line be claimed by an
    earlier type as a phantom standalone directive (e.g. an ``#include`` on a
    ``#define`` continuation line). Flattening and sorting by position first makes
    the ``processed_lines`` filter strictly top-to-bottom and returns the
    directives already in source order.
    """
    directives: list[PreprocessorDirective] = []
    directive_by_line: dict[int, PreprocessorDirective] = {}
    processed_lines: set[int] = set()

    flat = sorted(
        ((pos, dtype) for dtype, positions in directive_positions.items() for pos in positions),
        key=lambda item: item[0],
    )

    for pos, dtype in flat:
        # Use binary search on pre-computed line offsets for O(log n) performance
        line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
        if line_num in processed_lines:
            continue

        # Extract directive with continuations using StringZilla
        directive_lines = []
        current_line = line_num
        while current_line < len(lines):
            line = lines[current_line]
            directive_lines.append(line)  # Already StringZilla.Str from splitlines()
            processed_lines.add(current_line)
            if not ends_with_backslash_sz(line):
                break
            current_line += 1

        # Parse directive
        directive = parse_directive_struct(dtype, pos, line_num, directive_lines)
        directives.append(directive)
        directive_by_line[line_num] = directive

    return directives, directive_by_line


def _extract_includes(
    include_positions: list[int],
    lines: list["stringzilla.Str"],
    line_byte_offsets: list[int],
    str_text: "stringzilla.Str",
    block_comment_spans=None,
) -> list[dict]:
    """Build include records for each #include position.

    ``block_comment_spans`` may be supplied by the caller (``analyze_file`` computes
    it once and threads it to all consumers); recomputed per-position only when omitted.
    """
    includes: list[dict] = []
    if not include_positions:
        return includes

    for pos in include_positions:
        line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
        line = lines[line_num] if line_num < len(lines) else Str("")  # Already Str from splitlines()

        # Splice backslash line-continuations (C++ phase-2): the header token
        # may legitimately sit on a continuation line (N13). Only join when a
        # continuation is actually present so the common single-line case is
        # byte-for-byte unchanged.
        if ends_with_backslash_sz(line):
            inc_lines = [line]
            current_line = line_num + 1
            while current_line < len(lines):
                cont = lines[current_line]
                inc_lines.append(cont)
                if not ends_with_backslash_sz(cont):
                    break
                current_line += 1
            line = join_lines_strip_backslash_sz(inc_lines)

        is_commented = is_position_commented_simd_optimized(
            str_text, pos, line_byte_offsets, block_comment_spans
        )

        # Extract filename and type using StringZilla, replacing regex.
        # Tolerate whitespace between '#' and 'include' (e.g. `#  include`).
        hash_in_line = line.find("#")
        if hash_in_line == -1:
            continue
        name_start = line.find_first_not_of(" \t", hash_in_line + 1)
        if name_start == -1 or line[name_start : name_start + 7] != "include":
            continue

        search_start = name_start + 7  # len('include')

        quote_pos = line.find('"', search_start)
        lt_pos = line.find("<", search_start)

        start_delim_pos = -1
        is_system = False
        end_delim = ""

        if quote_pos != -1 and (lt_pos == -1 or quote_pos < lt_pos):
            start_delim_pos = quote_pos
            end_delim = '"'
            is_system = False
        elif lt_pos != -1:
            start_delim_pos = lt_pos
            end_delim = ">"
            is_system = True

        if start_delim_pos != -1:
            end_delim_pos = line.find(end_delim, start_delim_pos + 1)
            if end_delim_pos != -1:
                filename_slice = line[start_delim_pos + 1 : end_delim_pos]
                includes.append(
                    {
                        "line_num": line_num,
                        "byte_pos": pos,
                        "full_line": line,
                        "filename": filename_slice,
                        "is_system": is_system,
                        "is_commented": is_commented,
                    }
                )

    return includes


def _extract_magic_flags(
    magic_positions: list[int],
    lines: list["stringzilla.Str"],
    line_byte_offsets: list[int],
) -> list[dict]:
    """Build magic-flag records for each //#KEY=value position."""
    magic_flags: list[dict] = []
    if not magic_positions:
        return magic_flags

    for pos in magic_positions:
        line_num = bisect.bisect_right(line_byte_offsets, pos) - 1
        # Use Str("") for the OOB fallback — a bare "" literal makes pyright track
        # ``line`` as ``Str | Literal[""]``, which then propagates LiteralString
        # through slice/split and breaks attribute resolution on the stringzilla
        # surface (find_first_not_of et al.) below.
        line = lines[line_num] if line_num < len(lines) else Str("")

        # Parse magic flag using StringZilla operations
        hash_pos = line.find("//#")
        if hash_pos == -1:
            continue

        after_hash = line[hash_pos + 3 :]  # Skip //#

        # Use StringZilla split for KEY=value parsing
        equals_parts = after_hash.split("=", maxsplit=1)
        if len(equals_parts) != 2:
            continue

        key_part = equals_parts[0]
        value_part = equals_parts[1]

        # Trim whitespace using StringZilla character set operations
        key_start = key_part.find_first_not_of(" \t")
        if key_start == -1:
            continue
        key_end = key_part.find_last_not_of(" \t")
        key_trimmed = key_part[key_start : key_end + 1]

        # Validate key format using StringZilla character set operations
        if len(key_trimmed) == 0 or not is_alpha_or_underscore_sz(key_trimmed, 0):
            continue

        # Use StringZilla to check if all chars are valid (alphanumeric, _, -)
        invalid_pos = key_trimmed.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
        if invalid_pos != -1:
            continue

        # Trim value whitespace
        value_start = value_part.find_first_not_of(" \t")
        if value_start != -1:
            value_end = value_part.find_last_not_of(" \t\r\n")
            value_trimmed = value_part[value_start : value_end + 1]
        else:
            value_trimmed = value_part[0:0]  # Empty Str

        magic_flags.append(
            {
                "line_num": line_num,
                "byte_pos": pos,
                "full_line": line,
                "key": key_trimmed,
                "value": value_trimmed,
            }
        )

    return magic_flags


# C++20 module declarations. Recognized at the start of a logical line
# (after stripping leading whitespace, ignoring lines inside block
# comments). Named modules, partitions (`M:P`, `import :part;`), and
# header units (`import "h";`, `import <h>;`) are all classified below;
# the global module fragment opener (`module;`) is recognized
# syntactically and skipped, since it carries no module name.
def _classify_module_line(rest: "stringzilla.Str"):
    """Classify a single source line as a C++20 module declaration.

    `rest` is the line content (already a stringzilla.Str) AFTER leading
    whitespace has been stripped by the caller, AND verified not to be
    inside a block comment.

    Returns ``(kind, name)`` where ``kind`` is one of
    ``"export_module"``, ``"module"``, ``"import"`` -- or ``(None, None)``
    if the line is not a module declaration we want to record.

    Returns ``("header_import", "<vector>")`` or ``("header_import",
    "\"foo.h\"")`` for header units -- the token form (with brackets or
    quotes) is preserved so build_backend can re-emit it on the
    precompile invocation and on the importer's
    ``-fmodule-file=NAME=PATH`` flag.

    Partition imports (``import :p;``) and header units are classified.
    The global module fragment opener (``module;``) returns
    ``(None, None)`` -- it carries no module name to record.
    """
    s = str(rest)
    n = len(s)
    if n == 0:
        return None, None

    def is_ident_start(c: str) -> bool:
        return c.isalpha() or c == "_"

    def is_ident_cont(c: str) -> bool:
        return c.isalnum() or c == "_"

    def read_ident(j: int):
        if j >= n or not is_ident_start(s[j]):
            return None, j
        k = j + 1
        while k < n and is_ident_cont(s[k]):
            k += 1
        return s[j:k], k

    def read_dotted_ident(j: int):
        first, j = read_ident(j)
        if first is None:
            return None, j
        parts = [first]
        while j < n and s[j] == ".":
            nxt, jj = read_ident(j + 1)
            if nxt is None:
                return None, j
            parts.append(nxt)
            j = jj
        return ".".join(parts), j

    def read_module_spec(j: int, allow_partition_only: bool):
        """Read a module-name spec: ``M[.dotted][:P[.dotted]]`` or ``:P``.

        ``:P`` is only legal when ``allow_partition_only`` is True (the
        ``import :P;`` form inside a module). The leading ``:`` is
        preserved in the returned string so the consumer can tell the
        partition-only form apart from a fully-qualified ``M:P``.
        """
        # Partition-only form: leading `:`, not the `::` scope operator.
        if j < n and s[j] == ":":
            if not allow_partition_only:
                return None, j
            if j + 1 >= n or s[j + 1] == ":":
                return None, j
            part, jj = read_dotted_ident(j + 1)
            if part is None:
                return None, j
            return ":" + part, jj
        # Otherwise: M[.dotted][:P[.dotted]]
        name, j = read_dotted_ident(j)
        if name is None:
            return None, j
        if j < n and s[j] == ":" and (j + 1 >= n or s[j + 1] != ":"):
            part, jj = read_dotted_ident(j + 1)
            if part is not None:
                return name + ":" + part, jj
        return name, j

    def skip_ws(j: int) -> int:
        # C++ treats CR as whitespace; tolerate stray lone CRs (mixed/old-Mac
        # line endings) between tokens so a declaration still classifies.
        while j < n and s[j] in " \t\r":
            j += 1
        return j

    def read_header_unit(j: int):
        """Classify a header-unit import token (``<h>`` or ``"h"``) at ``j``.

        Returns ``("header_import", tok)`` with the token captured verbatim
        (brackets/quotes included) so build_backend can re-emit it on the
        precompile / ``-fmodule-file=`` flags, or ``None`` when ``j`` does not
        open a well-formed ``<...>;`` / ``"...";`` header-name (caller falls
        through to the named-module forms). Shared by plain ``import`` and
        ``export import`` so both record header-unit dependency edges (A1).
        """
        if j >= n or s[j] not in ("<", '"'):
            return None
        closer = ">" if s[j] == "<" else '"'
        close = s.find(closer, j + 1)
        if close == -1:
            return None
        tok = s[j : close + 1]
        k = skip_ws(close + 1)
        if k >= n or s[k] != ";":
            return None
        return "header_import", tok

    word1, i = read_ident(0)
    if word1 is None:
        return None, None

    if word1 == "export":
        i = skip_ws(i)
        word2, i = read_ident(i)
        # Allow `export import :P;` / `export import M;` -- common in
        # primary interface units that re-export a partition or another
        # module. We only need to classify the underlying token kind, so
        # treat `export import` as `import` (the export wrapper doesn't
        # change which other TUs the importer needs).
        if word2 == "import":
            i = skip_ws(i)
            if i >= n:
                return None, None
            hu = read_header_unit(i)
            if hu is not None:
                return hu
            name, i = read_module_spec(i, allow_partition_only=True)
            if name is None:
                return None, None
            i = skip_ws(i)
            if i >= n or s[i] != ";":
                return None, None
            return "import", name
        if word2 != "module":
            return None, None
        i = skip_ws(i)
        name, i = read_module_spec(i, allow_partition_only=False)
        if name is None:
            return None, None
        i = skip_ws(i)
        if i >= n or s[i] != ";":
            return None, None
        return "export_module", name

    if word1 == "module":
        i = skip_ws(i)
        # `module;` is the global module fragment opener -- not a name
        # declaration, ignore. `module NAME;` is an implementation unit.
        if i < n and s[i] == ";":
            return None, None
        name, i = read_module_spec(i, allow_partition_only=False)
        if name is None:
            return None, None
        i = skip_ws(i)
        if i >= n or s[i] != ";":
            return None, None
        return "module", name

    if word1 == "import":
        i = skip_ws(i)
        if i >= n:
            return None, None
        hu = read_header_unit(i)
        if hu is not None:
            return hu
        name, i = read_module_spec(i, allow_partition_only=True)
        if name is None:
            return None, None
        i = skip_ws(i)
        if i >= n or s[i] != ";":
            return None, None
        return "import", name

    return None, None


def _extract_module_declarations(
    str_text: "stringzilla.Str",
    line_byte_offsets: list[int],
    block_comment_spans=None,
) -> dict[str, list[str]]:
    """Find every C++20 module declaration in a source.

    Walks lines, ignores lines whose first non-whitespace position is
    inside a block comment, and delegates per-line classification to
    ``_classify_module_line``.

    Returns a dict with four keys -- ``"export_module"``, ``"module"``,
    ``"import"``, ``"header_import"`` -- mapping to lists of names in
    source order. ``header_import`` entries preserve the token form
    (with ``<...>`` or ``"..."``) so the build backend can re-emit them.

    ``block_comment_spans`` may be supplied by the caller (``analyze_file`` computes
    it once and threads it to all consumers); recomputed per-line only when omitted.
    """
    if block_comment_spans is None:
        block_comment_spans = find_block_comment_spans(str_text)
    result: dict[str, list[str]] = {
        "export_module": [],
        "module": [],
        "import": [],
        "header_import": [],
    }
    n = len(str_text)
    if n == 0 or not line_byte_offsets:
        return result

    line_count = len(line_byte_offsets)
    i = 0
    while i < line_count:
        start = line_byte_offsets[i]
        end = line_byte_offsets[i + 1] if i + 1 < line_count else n
        line = str_text[start:end]
        # CR is C++ whitespace; skip a stray leading lone CR too (mixed line
        # endings) so the declaration after it is reached.
        first_nws = line.find_first_not_of(" \t\r")
        if first_nws == -1:
            i += 1
            continue
        kw_pos = start + first_nws
        if _pos_in_spans(block_comment_spans, kw_pos):
            i += 1
            continue
        rest = line[first_nws:]
        # Splice backslash line-continuations (C++ phase-2): a module name may
        # legitimately sit on a continuation line (N4/A1). Consumed lines are
        # skipped so they are not reprocessed.
        if ends_with_backslash_sz(line):
            decl_lines = [rest]
            j = i + 1
            while j < line_count:
                cstart = line_byte_offsets[j]
                cend = line_byte_offsets[j + 1] if j + 1 < line_count else n
                cont = str_text[cstart:cend]
                decl_lines.append(cont)
                if not ends_with_backslash_sz(cont):
                    break
                j += 1
            rest = join_lines_strip_backslash_sz(decl_lines)
            i = j + 1
        else:
            i += 1
        kind, name = _classify_module_line(rest)
        if kind is not None and name is not None:
            result[kind].append(name)
    return result


def _extract_defines(
    define_positions: list[int],
    lines: list["stringzilla.Str"],
    line_byte_offsets: list[int],
    include_guard: Optional["stringzilla.Str"],
) -> list[dict]:
    """Build define records, excluding the include guard if set."""
    defines: list[dict] = []
    for pos in define_positions:
        line_num = bisect.bisect_right(line_byte_offsets, pos) - 1

        # Get all lines including continuations using StringZilla
        define_lines = []
        current_line = line_num
        while current_line < len(lines):
            line = lines[current_line]
            define_lines.append(line)  # Already StringZilla.Str from splitlines()
            if not ends_with_backslash_sz(line):
                break
            current_line += 1

        # Parse define using StringZilla, replacing regex
        if not define_lines:
            continue

        first_line = define_lines[0]
        define_kw_pos = first_line.find("#define")
        if define_kw_pos == -1:
            continue

        # Find start of macro name
        name_start_pos = first_line.find_first_not_of(" \t", define_kw_pos + 7)
        if name_start_pos == -1:
            continue

        # Join lines for parsing complex defines using StringZilla. Drop any
        # trailing comment so it cannot leak into the macro value (A19); the
        # macro name precedes any comment, so name detection is unaffected.
        full_define_str = _strip_trailing_comment_sz(join_lines_strip_backslash_sz(define_lines))

        # Find macro name part in the joined string
        name_part_start = full_define_str.find_first_not_of(" \t", full_define_str.find("#define") + 7)

        # Find end of name (space or parenthesis)
        paren_pos = full_define_str.find("(", name_part_start)
        space_pos = full_define_str.find_first_of(" \t", name_part_start)

        name_end_pos = -1
        if paren_pos != -1 and (space_pos == -1 or paren_pos < space_pos):
            name_end_pos = paren_pos
        else:
            name_end_pos = space_pos

        if name_end_pos == -1:  # Macro without value
            name = full_define_str[name_part_start:]
            value = None
            is_function_like = False
            params = []
        else:
            name = full_define_str[name_part_start:name_end_pos]

            # Check for function-like macro
            is_function_like = paren_pos == name_end_pos
            if is_function_like:
                params_end_pos = full_define_str.find(")", paren_pos + 1)
                if params_end_pos != -1:
                    params_str = full_define_str[paren_pos + 1 : params_end_pos]
                    params = [strip_sz(p) for p in params_str.split(",")] if params_str else []
                    value_start_pos = full_define_str.find_first_not_of(" \t", params_end_pos + 1)
                else:  # Malformed
                    params = []
                    value_start_pos = -1
            else:
                params = []
                value_start_pos = full_define_str.find_first_not_of(" \t", name_end_pos)

            if value_start_pos != -1:
                value = strip_sz(full_define_str[value_start_pos:])
            else:
                value = None

        # Skip include guard - it's tracked separately and doesn't affect compilation
        if include_guard and name == include_guard:
            continue

        defines.append(
            {
                "line_num": line_num,
                "byte_pos": pos,
                "lines": define_lines,
                "name": name,
                "value": value if value else None,
                "is_function_like": is_function_like,
                "params": params,
            }
        )

    return defines


def _detect_marker_type(
    str_text,
    exe_markers: list,
    test_markers: list,
    library_markers: list,
) -> MarkerType:
    """Detect EXE/TEST/LIBRARY marker type by scanning the source text.

    Priority is intentional: EXE > TEST > LIBRARY. The first matching list
    short-circuits the rest, mirroring the cumulative-flag check in the
    pre-decompose orchestrator.
    """
    if exe_markers:
        for marker in exe_markers:
            if str_text.find(marker) != -1:
                return MarkerType.EXE

    if test_markers:
        for marker in test_markers:
            if str_text.find(marker) != -1:
                return MarkerType.TEST

    if library_markers:
        for marker in library_markers:
            if str_text.find(marker) != -1:
                return MarkerType.LIBRARY

    return MarkerType.NONE


def analyze_file(content_hash: str, context: "BuildContext") -> "FileAnalysisResult":
    """File analysis with per-context caching - content hash based.

    Args:
        content_hash: Git blob hash of file content
        context: BuildContext where cache and args are stored

    Raises:
        FileNotFoundError: If file with given hash not found
        RuntimeError: If analyzer args not set via set_analyzer_args()
    """
    cached = context.analyze_file_cache.get(content_hash)
    if cached is not None:
        return cached

    if context.analyzer_args is None:
        raise RuntimeError("analyze_file: analyzer args not set on context. Call set_analyzer_args() first.")

    args = context.analyzer_args

    # Reverse lookup to get filepath (already realpath from registry)
    from compiletools.global_hash_registry import get_filepath_by_hash

    filepath = get_filepath_by_hash(content_hash, context)

    # Extract parameters from args
    max_read_size = getattr(args, "max_read_size", 0)
    exe_markers = getattr(args, "exemarkers", [])
    test_markers = getattr(args, "testmarkers", [])
    library_markers = getattr(args, "librarymarkers", [])

    file_size = compiletools.wrappedos.getsize(filepath)

    # Determine file reading strategy and read file content
    strategy = _determine_file_reading_strategy(context)
    str_text, bytes_analyzed, was_truncated = _load_file_text(filepath, file_size, max_read_size, strategy)

    # Use StringZilla's splitlines for optimal line processing
    lines = str_text.splitlines()

    # Build line_byte_offsets efficiently in a single pass
    line_byte_offsets = _compute_line_byte_offsets(str_text)

    # Compute block-comment spans ONCE and thread them to every consumer; each
    # is a full forward pass, so recomputing per-finder/per-include/per-line is
    # the dominant redundant cost on this hot path.
    block_comment_spans = find_block_comment_spans(str_text)

    # Find all pattern positions using optimized StringZilla bulk operations.
    magic_positions = find_magic_positions_simd_bulk(str_text, line_byte_offsets, block_comment_spans)
    directive_positions = find_directive_positions_simd_bulk(str_text, line_byte_offsets, block_comment_spans)

    # Extract structured directive information. _extract_directives returns
    # records in source order with continuation lines absorbed, so include and
    # define positions are derived from it (not from the raw, type-keyed
    # directive_positions) to keep phantom continuation directives out of the
    # dependency/define graph.
    directives, directive_by_line = _extract_directives(directive_positions, lines, line_byte_offsets)
    include_positions = _include_positions_from_directives(directives)

    # Extract includes with full information using bulk processing
    includes = _extract_includes(include_positions, lines, line_byte_offsets, str_text, block_comment_spans)

    # Extract magic flags with full information using StringZilla operations
    magic_flags = _extract_magic_flags(magic_positions, lines, line_byte_offsets)

    # Detect include guard first so we can exclude it from defines
    # (directives are already in source order).
    include_guard = detect_include_guard(directives)

    # Extract defines with full information (excluding include guard)
    define_positions = [d.byte_pos for d in directives if d.directive_type == "define"]
    defines = _extract_defines(define_positions, lines, line_byte_offsets, include_guard)

    # Extract unique headers
    system_headers = {inc["filename"] for inc in includes if inc["is_system"]}
    quoted_headers = {inc["filename"] for inc in includes if not inc["is_system"]}

    # Extract macros referenced in conditionals (for cache optimization)
    conditional_macros = _extract_conditional_macros(directives)

    # Detect marker type - check for exe, test, or library markers
    marker_type = _detect_marker_type(str_text, exe_markers, test_markers, library_markers)

    # C++20 module declarations (named modules, partitions, and header
    # units; the global module fragment opener is skipped at the classifier).
    module_decls = _extract_module_declarations(str_text, line_byte_offsets, block_comment_spans)

    result = FileAnalysisResult(
        line_count=len(lines),
        line_byte_offsets=line_byte_offsets,
        include_positions=include_positions,
        magic_positions=magic_positions,
        directive_positions=directive_positions,
        directives=directives,
        directive_by_line=directive_by_line,
        bytes_analyzed=bytes_analyzed,
        was_truncated=was_truncated,
        includes=includes,
        magic_flags=magic_flags,
        defines=defines,
        system_headers=system_headers,
        quoted_headers=quoted_headers,
        content_hash=content_hash,
        include_guard=include_guard,
        conditional_macros=conditional_macros,
        marker_type=marker_type,
        module_exports=tuple(module_decls["export_module"]),
        module_implements=tuple(module_decls["module"]),
        module_imports=tuple(module_decls["import"]),
        module_header_imports=tuple(module_decls["header_import"]),
    )

    # Detach every retained Str slice so the cached result owns its bytes and
    # stops pinning the whole decoded-file buffer for the build lifetime (A7).
    _detach_file_analysis_result(result)

    context.analyze_file_cache[content_hash] = result
    return result


def read_file_mmap(filepath, max_size=0):
    """Use memory-mapped I/O for large files with fallback to traditional reading.

    Args:
        filepath: Path to file to read
        max_size: Maximum bytes to read (0 = entire file)

    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = compiletools.wrappedos.getsize(filepath)

        # Handle empty files (mmap fails on zero-byte files)
        if file_size == 0:
            return "", 0, False

        with builtins.open(filepath, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            if max_size > 0 and max_size < file_size:
                data = mm[:max_size]
                bytes_analyzed = max_size
                was_truncated = True
            else:
                data = mm[:]
                bytes_analyzed = len(data)
                was_truncated = False

            text = data.decode("utf-8", errors="ignore")
            return text, bytes_analyzed, was_truncated

    except (OSError, ValueError):
        # Fallback to traditional reading on any mmap failure
        return read_file_traditional(filepath, max_size)


def read_file_traditional(filepath, max_size=0):
    """Traditional file reading fallback.

    Args:
        filepath: Path to file to read
        max_size: Maximum bytes to read (0 = entire file)

    Returns:
        tuple: (text_content, bytes_analyzed, was_truncated)
    """
    try:
        file_size = compiletools.wrappedos.getsize(filepath)

        # Read in binary so truncation is by BYTES, matching the mmap path
        # (text-mode f.read(max_size) counts characters and would over-read and
        # mis-report bytes_analyzed on multibyte content, A6). Counting bytes
        # directly also avoids re-encoding the whole text just to size it (A22c).
        with builtins.open(filepath, "rb") as f:
            if max_size > 0 and max_size < file_size:
                data = f.read(max_size)
                was_truncated = True
            else:
                data = f.read()
                was_truncated = False

        bytes_analyzed = len(data)
        text = data.decode("utf-8", errors="ignore")
        return text, bytes_analyzed, was_truncated

    except (OSError, ValueError):
        # Return empty content on any error
        return "", 0, False


@dataclass
class PreprocessorDirective:
    """A preprocessor directive with all its content."""

    line_num: int  # Starting line number (0-based)
    byte_pos: int  # Byte position in original file
    directive_type: str  # 'if', 'ifdef', 'ifndef', 'elif', 'else', 'endif', 'define', 'undef', 'include'
    continuation_lines: int  # Number of continuation lines (for multi-line directives)
    condition: Optional["stringzilla.Str"] = None  # The condition expression (for if/ifdef/ifndef/elif)
    macro_name: Optional["stringzilla.Str"] = None  # Macro name (for define/undef/ifdef/ifndef)
    macro_value: Optional["stringzilla.Str"] = None  # Macro value (for define)


def _extract_conditional_macros(directives: list[PreprocessorDirective]) -> frozenset["stringzilla.Str"]:
    """Extract all macro names referenced in conditional directives.

    Returns frozenset of sz.Str macro names from ifdef/ifndef/if/elif conditions.
    Used for cache optimization - files are effectively invariant when none
    of these macros are defined.
    """

    macros = set()

    for directive in directives:
        if directive.directive_type in ("ifdef", "ifndef"):
            if directive.macro_name:
                macros.add(directive.macro_name)
        elif directive.directive_type in ("if", "elif", "include"):
            if directive.condition:
                # Extract identifiers from condition using stringzilla
                cond = directive.condition
                keywords = {"and", "or", "not", "true", "false", "defined"}

                i = 0
                while i < len(cond):
                    # Skip non-identifier chars
                    if not is_alpha_or_underscore_sz(cond, i):
                        i += 1
                        continue

                    # Found start of identifier - vectorized
                    start = i
                    identifier_end = cond.find_first_not_of(
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_", start
                    )
                    i = identifier_end if identifier_end != -1 else len(cond)

                    # Extract identifier
                    identifier = cond[start:i]
                    name = str(identifier)

                    if name not in keywords:
                        macros.add(identifier)

    return frozenset(macros)


def detect_include_guard(directives: list[PreprocessorDirective]) -> Optional["stringzilla.Str"]:
    """Detect include guard macro from preprocessor directives.

    Supports both traditional include guards (#ifndef/#define) and #pragma once.
    Returns the guard macro name as StringZilla.Str or sz.Str("pragma_once") for #pragma once.

    Include guard detection is STRICT to avoid false positives:
    - #pragma once must be among the first 3 directives
    - #ifndef/#define pattern must start at the FIRST directive
    - The matching #endif must be the LAST (or near-last) directive
    - This avoids misidentifying feature flag patterns like:
        #ifndef ENABLE_FEATURE
        #define ENABLE_FEATURE
        #endif
        // ... rest of file
    """
    if not directives:
        return None

    # Check for #pragma once first (must be early in file)
    # Note: pragma directives have macro_name set (e.g., "once")
    for directive in directives[:3]:  # Only first 3 directives
        if directive.directive_type == "pragma":
            # Check macro_name for "once" (how parse_directive_struct stores it)
            if directive.macro_name and str(directive.macro_name) == "once":
                return Str("pragma_once")
            # Also check condition in case it's stored there
            if directive.condition and "once" in str(directive.condition):
                return Str("pragma_once")

    # Check for traditional include guard pattern: #ifndef GUARD followed by #define GUARD
    # STRICT: Must start at the FIRST directive to be a true include guard
    # AND the matching #endif must be the LAST directive (wraps entire file)
    if len(directives) < 3:  # Need at least #ifndef, #define, #endif
        return None

    first_directive = directives[0]
    last_directive = directives[-1]

    # The last directive must be #endif for this to be an include guard
    if last_directive.directive_type != "endif":
        return None

    # The #endif that closes the opening #ifndef must be the LAST directive, so
    # the guard wraps the entire file. Track conditional nesting depth from the
    # start: an early return to depth 0 means the opener was closed before EOF
    # (a feature-flag pattern), not a whole-file include guard (N1).
    depth = 0
    matching_endif_idx = None
    for idx, d in enumerate(directives):
        if d.directive_type in ("if", "ifdef", "ifndef"):
            depth += 1
        elif d.directive_type == "endif":
            depth -= 1
            if depth == 0:
                matching_endif_idx = idx
                break
    if matching_endif_idx != len(directives) - 1:
        return None

    if first_directive.directive_type == "ifndef" and first_directive.macro_name:
        guard_candidate = first_directive.macro_name

        # Look ahead up to 5 positions for the matching #define
        # This handles cases where comments or other directives appear between
        # the #ifndef and the matching #define
        for j in range(1, min(6, len(directives))):
            if (
                directives[j].directive_type == "define"
                and directives[j].macro_name
                and directives[j].macro_name == guard_candidate
            ):
                # guard_candidate is already sz.Str from PreprocessorDirective.macro_name
                return guard_candidate

    return None


@dataclass
class FileAnalysisResult:
    """Complete structured result without text field.

    Provides all information needed by consumers without requiring text reconstruction.
    """

    # Line-level data (for SimplePreprocessor) - required fields first
    line_count: int  # Number of lines in the file
    line_byte_offsets: list[int]  # Byte offset where each line starts

    # Position arrays (for fast lookups) - required fields
    include_positions: list[int]  # Byte positions of #include directives
    magic_positions: list[int]  # Byte positions of //#KEY= patterns
    directive_positions: dict[str, list[int]]  # Byte positions by directive type

    # Preprocessor directives (structured for SimplePreprocessor) - required fields
    directives: list[PreprocessorDirective]  # All directives with full context
    directive_by_line: dict[int, PreprocessorDirective]  # Line number -> directive mapping

    # Metadata - required fields
    bytes_analyzed: int  # Bytes analyzed from file
    was_truncated: bool  # Whether file was truncated

    # Optional fields with defaults come last
    includes: list[dict] = field(default_factory=list)
    # Each include dict contains:
    # {
    #   'line_num': int,                # Line number (0-based)
    #   'byte_pos': int,                # Byte position
    #   'full_line': str,               # Complete include line (str for compatibility)
    #   'filename': stringzilla.Str,    # Extracted filename
    #   'is_system': bool,              # True for <>, False for ""
    #   'is_commented': bool,           # True if in comment
    # }

    magic_flags: list[dict] = field(default_factory=list)
    # Each magic flag dict contains:
    # {
    #   'line_num': int,           # Line number (0-based)
    #   'byte_pos': int,                 # Byte position
    #   'full_line': stringzilla.Str,   # Complete line with //#KEY=value
    #   'key': stringzilla.Str,          # The KEY part
    #   'value': stringzilla.Str,        # The value part
    # }

    defines: list[dict] = field(default_factory=list)
    # Each define dict contains:
    # {
    #   'line_num': int,                        # Starting line number
    #   'byte_pos': int,                        # Byte position
    #   'lines': List[stringzilla.Str],         # All lines including continuations
    #   'name': stringzilla.Str,                # Macro name
    #   'value': Optional[stringzilla.Str],     # Macro value (if any)
    #   'is_function_like': bool,               # True for function-like macros
    #   'params': List[stringzilla.Str],        # Parameters for function-like macros
    # }

    # NOTE: these sets hold stringzilla.Str (from include filenames), NOT plain
    # str. hash(Str) != hash(str) and `Str("x") in {<str>}` is False, so membership
    # queries must use Str keys (all current consumers are Str-consistent). See A14.
    system_headers: set["stringzilla.Str"] = field(default_factory=set)  # Unique system headers found
    quoted_headers: set["stringzilla.Str"] = field(default_factory=set)  # Unique quoted headers found
    content_hash: str = ""  # SHA1 of original content
    include_guard: Optional["stringzilla.Str"] = (
        None  # Include guard macro name (traditional) or sz.Str("pragma_once") for #pragma once
    )
    conditional_macros: frozenset["stringzilla.Str"] = field(
        default_factory=frozenset
    )  # Macros referenced in conditionals (for cache optimization)
    marker_type: MarkerType = MarkerType.NONE  # Type of marker found in file (exe, test, library, or none)

    # C++20 module declarations. See _extract_module_declarations for the
    # forms recognized; only the global module fragment opener (`module;`)
    # is deliberately not surfaced, since it carries no module name.
    module_exports: tuple[str, ...] = ()  # `export module NAME;`
    module_implements: tuple[str, ...] = ()  # `module NAME;` (impl unit)
    module_imports: tuple[str, ...] = ()  # `import NAME;`
    module_header_imports: tuple[str, ...] = ()  # `import <h>;` / `import "h";`

    # Helper method for SimplePreprocessor compatibility
    def get_directive_line_numbers(self) -> dict[str, set[int]]:
        """Get line numbers for each directive type (for SimplePreprocessor).

        Derived from the cleaned ``directives`` list (source-ordered, with
        continuation lines absorbed) rather than the raw ``directive_positions``
        map, so phantom continuation directives never contribute a line number.
        """
        result: dict[str, set[int]] = {}
        for directive in self.directives:
            result.setdefault(directive.directive_type, set()).add(directive.line_num)
        return result


def add_arguments(cap):
    """Add file-analyzer command-line arguments to a parser.

    The module-level ``analyze_file()`` is the canonical entry point for file
    analysis; this function only registers the file-reading-strategy flags
    (matching the module-level ``add_arguments`` convention used by
    ``hunter``/``findtargets``). Safe to call more than once on the same parser.
    Call sites: ``findtargets`` and ``headerdeps``.

    Args:
        cap: ConfigArgParse parser instance
    """
    import compiletools.apptools
    import compiletools.utils

    if compiletools.apptools._parser_has_option(cap, "--use-mmap"):
        return

    # Manual overrides for testing/debugging
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="use-mmap",
        dest="use_mmap",
        default=True,
        help="Use mmap for file reading. Disable with --no-use-mmap for GPFS, SMB/CIFS, etc.",
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="force-mmap",
        dest="force_mmap",
        default=False,
        help="Force mmap mode even on low ulimit systems (for testing/debugging)",
    )

    # Warning suppression
    compiletools.utils.add_flag_argument(
        parser=cap,
        name="suppress-fd-warnings",
        dest="suppress_fd_warnings",
        default=False,
        help="Suppress file descriptor limit warnings",
    )

    compiletools.utils.add_flag_argument(
        parser=cap,
        name="suppress-filesystem-warnings",
        dest="suppress_filesystem_warnings",
        default=False,
        help="Suppress filesystem compatibility warnings",
    )
