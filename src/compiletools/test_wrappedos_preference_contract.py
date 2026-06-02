"""Lint-test: lock the ``wrappedos`` preference invariant from
``src/compiletools/CLAUDE.md`` ("``wrappedos`` preference and the documented
skip cases") into CI.

The contract (one sentence): every raw
``os.path.{realpath,isfile,isdir,getmtime,getsize}`` call in a production
module (non-``test_``, excluding ``wrappedos.py`` / ``testhelper.py`` /
``conftest.py``) must EITHER carry a skip-justification comment on the hit
line or in the contiguous comment block immediately above it (containing one
of the supported markers) OR be listed in the ``_WRAPPEDOS_EXEMPT`` allowlist
keyed by ``"basename:line"``, otherwise this test fails naming the offending
``file:line`` and the source line.

Why only those five functions: ``realpath`` / ``isfile`` / ``isdir`` /
``getmtime`` / ``getsize`` are the *correctness-sensitive* stat calls where the
cached ``compiletools.wrappedos`` wrappers can be the *wrong* answer (a lock
whose mtime changes under us, a build output that appears after the producing
rule ran, a relative path read after a ``chdir``). ``dirname`` / ``basename`` /
``join`` / ``normpath`` / ``isabs`` are pure string ops that appear
legitimately everywhere and are deliberately NOT scanned — linting them would
be pure noise.

The three documented skip cases (CLAUDE.md):
  1. lock / sidecar stats whose mtime / existence changes concurrently;
  2. build-output existence checks AFTER the producing rule ran (clean /
     realclean, post-build cache / cache-report / trim walks, diagnostics);
  3. relative-path inputs subject to ``chdir`` (and, equivalently cache-safe,
     ``realpath`` of an already-absolute string such as ``os.getcwd()`` or
     ``find_git_root()`` — CLAUDE.md says these are safe to skip).

Mirrors the grep-based house style of ``test_cas_dir_resolver_contract.py``:
the contract is static, so a file-text scan is enough and avoids importing
every entry point.
"""

import os
import re

# The five correctness-sensitive stat functions. Negative lookbehind on
# ``[A-Za-z0-9_.]`` so ``wrappedos.realpath(`` and attribute access never
# match — only a bare ``os.path.<fn>(`` call does.
_STAT_RE = re.compile(r"(?<![A-Za-z0-9_.])os\.path\.(realpath|isfile|isdir|getmtime|getsize)\s*\(")

# Modules that are part of the wrappedos machinery or are test-support and so
# are not subject to the "prefer wrappedos" rule.
_EXCLUDE_FILES = frozenset({"wrappedos.py", "testhelper.py", "conftest.py"})

# Comment substrings that mark an intentional, reviewed skip. These are the
# tokens the codebase actually uses inline (e.g. lock_utils.py:34
# "CRITICAL: Use os.path.getmtime, NOT cached version"). A match passes the
# lint if any of these appears on the hit line or in the contiguous run of
# comment lines immediately above it. Kept deliberately literal so a vague
# nearby comment can't rubber-stamp a real violation.
_JUSTIFICATION_MARKERS = (
    "CRITICAL",
    "NOT wrappedos",
    "not wrappedos",
    "NOT cached",
    "not cached",
    "os.path.realpath directly",
)

# Allowlist of reviewed, genuine skip sites keyed by ``"basename:line"``.
# Each entry is one of the three documented skip cases (or the cache-safe
# "realpath of an absolute string" sub-case). Sites that carry a natural
# inline justification comment go through the comment path instead and are NOT
# listed here. The trailing note records the category:
#   #1 = lock/sidecar concurrent stat
#   #2 = post-build / diagnostic existence walk
#   #3 = chdir-relative input  /  abs = realpath of an already-absolute string
#   str = the match is inside a string literal, not a real call
# A ``# REVIEW:`` prefix flags a provisional entry that a human should confirm.
_WRAPPEDOS_EXEMPT: frozenset[str] = frozenset(
    {
        # --- #3 / abs: realpath of an absolute string (cache-safe to skip) ---
        "apptools_argparse.py:625",  # abs: realpath(os.getcwd()) one-off, Caveat #3 documented
        "apptools_compiler.py:174",  # abs: realpath of absolute install_dir, compiler-introspection one-off
        "apptools_compiler.py:176",  # #3: isfile of derived candidate during compiler introspection
        "apptools_compiler.py:178",  # abs: realpath of absolute resolved compiler path
        "apptools_compiler.py:181",  # #3: isfile of derived candidate during compiler introspection
        "examples_registry.py:23",  # abs: realpath(__file__) module-init one-off
        "filesystem_utils.py:139",  # abs/#3: realpath before reading /proc/mounts; result cached by caller
        "check_venv.py:80",  # str: inside a subprocess "-c" code string, not a live call
        "check_venv.py:91",  # abs: realpath of venv src-root argument
        "check_venv.py:92",  # abs: realpath of subprocess-reported absolute path
        "check_venv.py:136",  # abs: realpath(compiletools.__file__) one-off venv check
        "git_sha_report.py:22",  # #1-like: realpath MUST be uncached to detect symlink divergence
        "git_utils.py:110",  # #2/#3: .git probing for git-root detection (foundational, pre-cache)
        "git_utils.py:115",  # #2/#3: .git probing for git-root detection
        "git_utils.py:117",  # #2/#3: .git HEAD probing for git-root detection
        "bazel_backend.py:120",  # #3: realpath of relative "BUILD.bazel" MUST stay uncached (chdir between generate()/execute in tests); docstring explains
        "bazel_backend.py:279",  # #3: include dir is relative, resolved against base_dir per call
        # --- #2: post-build / diagnostic existence walks ---
        "backend_pch.py:162",  # #2: PCH cache-dir stat in a stat-the-target helper (mtime-bearing)
        "build_backend.py:614",  # #2: clean() removes exe_dir if present
        "build_backend.py:616",  # #2: clean() removes obj_dir if present
        "build_backend.py:631",  # #2: realclean() removes exe_dir if present
        "build_backend.py:642",  # #2: realclean() prunes obj_dir if present
        "build_backend.py:652",  # #2: realclean() removes a built target if present
        "build_backend.py:685",  # #2: post-build walk of built executables
        "bazel_backend.py:1281",  # #2: post-build bazel-bin existence check
        "cache_report.py:230",  # #2: diagnostic scan of cas-objdir
        "cache_report.py:396",  # #2: diagnostic scan of cas-pchdir
        "cache_report.py:507",  # #2: diagnostic scan of cas-pcmdir
        "cache_report.py:631",  # #2: diagnostic scan of cas-exedir
        "cache_report.py:1118",  # #2: diagnostic "should I scan this dir" gate
        "trim_cache.py:175",  # #2: trim walk of cas-objdir
        "trim_cache.py:356",  # #2: trim walk of cas-pchdir
        "trim_cache.py:534",  # #2: trim walk of cas-pcmdir
        "trim_cache.py:701",  # #2: trim walk of cas-exedir
        "timing_report.py:99",  # #2: diagnostic scan of diagnostics_dir
        "timing_report.py:106",  # #2: diagnostic scan of invocation subdirs
        "ninja_backend.py:192",  # #1/#2: .ninja_log size read around the build
        "trace_backend.py:195",  # #2: output existence check after rule executed
        "trace_backend.py:210",  # #2: input existence check while assembling a post-run trace
        # --- #1: lock / sidecar concurrent stats ---
        "locking.py:501",  # #1: lockfile mtime for stale-lock age (changes concurrently)
        "locking.py:965",  # #1: target size guard inside the locked compile/link path
        "ct_lock_helper.py:127",  # #1: bytes-reused size read on a CAS hit under lock
        "trace_backend.py:565",  # #1: bytes-reused size read on a CAS hit under lock
        # --- #3: literal absolute / non-cacheable inputs ---
        "makefile_backend.py:173",  # abs: isfile("/bin/bash") literal absolute path
        "utils.py:565",  # #3: isdir of a commonpath-derived path that may not exist
    }
)


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for fname in os.listdir(src_dir):
        if fname.endswith(".py") and not fname.startswith("test_") and fname not in _EXCLUDE_FILES:
            yield os.path.join(src_dir, fname)


def _is_in_comment_or_string(text: str, pos: int) -> bool:
    """Best-effort: True if the match at ``pos`` is inside a ``# ...`` line
    comment. In-string matches are handled per-site via ``_WRAPPEDOS_EXEMPT``
    (a real tokenizer would be overkill for the handful of string-literal
    occurrences), mirroring the sibling ``test_cas_dir_resolver_contract``."""
    line_start = text.rfind("\n", 0, pos) + 1
    line_prefix = text[line_start:pos]
    return "#" in line_prefix.split('"')[0].split("'")[0]


def _has_justification(lines: list[str], hit_idx: int) -> bool:
    """True if a supported marker appears on the hit line or anywhere in the
    contiguous run of ``#`` comment lines immediately above it."""
    if any(mk in lines[hit_idx] for mk in _JUSTIFICATION_MARKERS):
        return True
    idx = hit_idx - 1
    while idx >= 0 and lines[idx].lstrip().startswith("#"):
        if any(mk in lines[idx] for mk in _JUSTIFICATION_MARKERS):
            return True
        idx -= 1
    return False


def test_raw_os_path_stat_calls_are_justified_or_allowlisted():
    """Every raw ``os.path.{realpath,isfile,isdir,getmtime,getsize}`` call in a
    production module must be comment-justified or in ``_WRAPPEDOS_EXEMPT``.

    Why this matters: the cached ``compiletools.wrappedos`` wrappers are the
    default for hot-path stats of stable absolute inputs. The only legitimate
    raw calls are the three documented skip cases (concurrent lock/sidecar
    stats, post-build/diagnostic existence walks, chdir-relative inputs). This
    lint freezes that prose invariant so a new raw call must be consciously
    classified (comment) or recorded (allowlist), not added silently.
    """
    failures = []
    for path in _production_python_files():
        basename = os.path.basename(path)
        with open(path) as fh:
            text = fh.read()
        lines = text.splitlines()
        for m in _STAT_RE.finditer(text):
            if _is_in_comment_or_string(text, m.start()):
                continue
            line_no = text[: m.start()].count("\n") + 1
            key = f"{basename}:{line_no}"
            if key in _WRAPPEDOS_EXEMPT:
                continue
            if _has_justification(lines, line_no - 1):
                continue
            failures.append(f"{key}: {lines[line_no - 1].strip()}")

    assert not failures, (
        "Raw os.path.{realpath,isfile,isdir,getmtime,getsize} calls with no "
        "skip-justification comment and no _WRAPPEDOS_EXEMPT entry:\n"
        + "\n".join(f"  {f}" for f in failures)
        + "\n\nFix one of:\n"
        "  1. Prefer the cached wrapper: compiletools.wrappedos.<fn>(...)\n"
        "  2. If this is a genuine skip case (concurrent lock/sidecar stat;\n"
        "     post-build/diagnostic existence walk; chdir-relative input;\n"
        "     realpath of an already-absolute string), add an inline comment\n"
        "     containing CRITICAL / NOT wrappedos / NOT cached, OR add the\n"
        "     'basename:line' to _WRAPPEDOS_EXEMPT with a category note.\n"
        "See src/compiletools/CLAUDE.md 'wrappedos preference and the "
        "documented skip cases'."
    )


def test_wrappedos_exempt_entries_refer_to_real_files():
    """Typo / staleness guard for ``_WRAPPEDOS_EXEMPT`` — same shape as
    ``test_resolver_exempt_entries_refer_to_real_files`` in
    ``test_cas_dir_resolver_contract.py``. Every allowlist key's file must
    exist (line-number drift is caught by the main lint going red)."""
    src_dir = os.path.dirname(__file__)
    missing = sorted(
        {
            entry.split(":", 1)[0]
            for entry in _WRAPPEDOS_EXEMPT
            if not os.path.exists(os.path.join(src_dir, entry.split(":", 1)[0]))
        }
    )
    assert not missing, f"_WRAPPEDOS_EXEMPT references non-existent files: {missing}"
