"""Lint-test: lock the ``wrappedos`` preference invariant from
``src/compiletools/CLAUDE.md`` ("``wrappedos`` preference and the documented
skip cases") into CI.

The contract (one sentence): every raw
``os.path.{realpath,isfile,isdir,getmtime,getsize}`` call in a production
module (non-``test_``, excluding ``wrappedos.py`` / ``testhelper.py`` /
``conftest.py``) must EITHER carry a skip-justification comment on the hit
line or in the contiguous comment block immediately above it (containing one
of the supported markers) OR be listed in the ``_WRAPPEDOS_EXEMPT`` allowlist,
otherwise this test fails naming the offending ``file:line`` and the source
line.

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

The allowlist is keyed by ``(basename, exact stripped source line)``, NOT by
line number: refactors that move code around (the splits that created the
``backend_*`` / ``apptools_*`` modules are a live example) shift line numbers
constantly, and a line-number key would go stale on every such edit. Keying on
the line *text* means an entry only needs revisiting when that line's code
actually changes — which is exactly when the skip rationale should be
re-checked. Trade-off: two byte-identical stat lines in the same file share one
entry (and one would-be-new identical call is silently covered); for these
defensive one-liners that is acceptable and, arguably, correct (same code =
same justification). Mirrors the grep-based house style of
``test_cas_dir_resolver_contract.py``.
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
# tokens the codebase actually uses inline (e.g. lock_utils.py
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

# Allowlist of reviewed, genuine skip sites keyed by
# ``(basename, exact stripped source line)``. Each entry is one of the three
# documented skip cases (or the cache-safe "realpath of an absolute string"
# sub-case). Sites that carry a natural inline justification comment go through
# the comment path instead and are NOT listed here. The trailing note records
# the category:
#   #1 = lock/sidecar concurrent stat
#   #2 = post-build / diagnostic existence walk
#   #3 = chdir-relative input  /  abs = realpath of an already-absolute string
#   str = the match is inside a string literal, not a real call
_WRAPPEDOS_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # --- #3 / abs: realpath of an absolute string (cache-safe to skip) ---
        # (apptools_argparse.py realpath(os.getcwd()) is justified inline via a
        #  "NOT cached" comment instead of an allowlist entry.)
        (
            "apptools_compiler.py",
            'gcc_root = os.path.realpath(os.path.join(install_dir, "..", "..", "..", ".."))',
        ),  # abs: realpath of absolute install_dir, compiler-introspection one-off
        (
            "apptools_compiler.py",
            "return candidate if os.path.isfile(candidate) else None",
        ),  # abs: isfile of derived absolute candidate during compiler introspection
        (
            "apptools_compiler.py",
            "bin_dir = os.path.dirname(os.path.realpath(resolved))",
        ),  # abs: realpath of absolute resolved compiler path
        (
            "examples_registry.py",
            "_PACKAGE_ROOT = os.path.dirname(os.path.realpath(__file__))",
        ),  # abs: realpath(__file__) module-init one-off
        (
            "filesystem_utils.py",
            "path = os.path.realpath(path)",
        ),  # abs/#3: realpath before reading /proc/mounts; result cached by caller
        (
            "check_venv.py",
            '"print(os.path.dirname(os.path.dirname(os.path.realpath(compiletools.__file__))))",',
        ),  # str: inside a subprocess "-c" code string, not a live call
        ("check_venv.py", "expected = os.path.realpath(expected_src_root)"),  # abs: realpath of venv src-root argument
        (
            "check_venv.py",
            "if os.path.realpath(actual) == expected:",
        ),  # abs: realpath of subprocess-reported absolute path
        (
            "check_venv.py",
            "expected = os.path.dirname(os.path.dirname(os.path.realpath(compiletools.__file__)))",
        ),  # abs: realpath(compiletools.__file__) one-off venv check
        (
            "git_sha_report.py",
            "if os.path.realpath(joined) != joined:",
        ),  # #3-adjacent: realpath MUST stay uncached -- the check (realpath(joined) != joined) IS the symlink-divergence detector; cold one-off sample
        (
            "git_utils.py",
            "if os.path.isfile(git_path):",
        ),  # #2/#3: .git probing for git-root detection (foundational, pre-cache)
        ("git_utils.py", "if os.path.isdir(git_path):"),  # #2/#3: .git probing for git-root detection
        ("git_utils.py", "if not os.path.isfile(head_path):"),  # #2/#3: .git HEAD probing for git-root detection
        (
            "bazel_backend.py",
            'return os.path.dirname(os.path.realpath(cls.build_filename())) or "."',
        ),  # #3: realpath of relative "BUILD.bazel" MUST stay uncached (chdir between generate()/execute in tests); docstring explains
        (
            "bazel_backend.py",
            "if not os.path.isdir(inc_abs):",
        ),  # #3: include dir is relative, resolved against base_dir per call
        # --- #2: post-build / diagnostic existence walks ---
        (
            "backend_pch.py",
            "target = pchdir if os.path.isdir(pchdir) else parent",
        ),  # #2: PCH cache-dir stat in a stat-the-target helper (mtime-bearing)
        ("build_backend.py", "if os.path.isdir(exe_dir):"),  # #2: clean()/realclean() removes exe_dir if present
        (
            "build_backend.py",
            "if obj_dir != exe_dir and os.path.isdir(obj_dir):",
        ),  # #2: clean()/realclean() removes obj_dir if present
        ("build_backend.py", "if os.path.isfile(target):"),  # #2: realclean() removes a built target if present
        (
            "build_backend.py",
            "if not (os.path.isfile(full) and os.access(full, os.X_OK)):",
        ),  # #2: post-build walk of built executables
        ("bazel_backend.py", "if not os.path.isdir(bazel_bin):"),  # #2: post-build bazel-bin existence check
        ("cache_report.py", "if not os.path.isdir(objdir):"),  # #2: diagnostic scan of cas-objdir
        ("cache_report.py", "if not os.path.isdir(pchdir):"),  # #2: diagnostic scan of cas-pchdir
        ("cache_report.py", "if not os.path.isdir(pcmdir):"),  # #2: diagnostic scan of cas-pcmdir
        ("cache_report.py", "if not os.path.isdir(exedir):"),  # #2: diagnostic scan of cas-exedir
        ("cache_report.py", "return os.path.isdir(path)"),  # #2: diagnostic "should I scan this dir" gate
        ("trim_cache.py", "if not os.path.isdir(objdir):"),  # #2: trim walk of cas-objdir
        ("trim_cache.py", "if not os.path.isdir(pchdir):"),  # #2: trim walk of cas-pchdir
        ("trim_cache.py", "if not os.path.isdir(pcmdir):"),  # #2: trim walk of cas-pcmdir
        ("trim_cache.py", "if not os.path.isdir(exedir):"),  # #2: trim walk of cas-exedir
        ("timing_report.py", "if not os.path.isdir(diagnostics_dir):"),  # #2: diagnostic scan of diagnostics_dir
        (
            "timing_report.py",
            "name for name in entries if INVOCATION_ID_RE.match(name) and os.path.isdir(os.path.join(diagnostics_dir, name))",
        ),  # #2: diagnostic scan of invocation subdirs
        ("ninja_backend.py", "log_offset = os.path.getsize(ninja_log)"),  # #1/#2: .ninja_log size read around the build
        (
            "trace_backend.py",
            "if output_hash is None and not os.path.isfile(rule.output):",
        ),  # #2: output existence check after rule executed
        ("trace_backend.py", "if os.path.isfile(p):"),  # #2: input existence check while assembling a post-run trace
        # --- #1: lock / sidecar concurrent stats ---
        (
            "locking.py",
            "age = time.time() - os.path.getmtime(self.lockfile_excl)",
        ),  # #1: lockfile mtime for stale-lock age (changes concurrently)
        (
            "locking.py",
            "if ar_appends and os.path.exists(target) and os.path.getsize(target) > 0:",
        ),  # #1: target size guard inside the locked compile/link path
        (
            "ct_lock_helper.py",
            "bytes_reused = os.path.getsize(target) if cas_hit and os.path.exists(target) else 0",
        ),  # #1: bytes-reused size read on a CAS hit under lock
        (
            "trace_backend.py",
            "bytes_reused = os.path.getsize(target) if cas_hit and os.path.exists(target) else 0",
        ),  # #1: bytes-reused size read on a CAS hit under lock
        # --- literal absolute / non-cacheable inputs ---
        ("makefile_backend.py", 'if os.path.isfile("/bin/bash"):'),  # abs: isfile("/bin/bash") literal absolute path
        (
            "utils.py",
            "if common_root and not os.path.isdir(common_root):",
        ),  # #2-like: isdir in a cold LDFLAGS-cycle error-message path; derived prefix may not exist
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
            stripped = lines[line_no - 1].strip()
            if (basename, stripped) in _WRAPPEDOS_EXEMPT:
                continue
            if _has_justification(lines, line_no - 1):
                continue
            failures.append(f"{basename}:{line_no}: {stripped}")

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
        "     (basename, exact source line) to _WRAPPEDOS_EXEMPT with a note.\n"
        "See src/compiletools/CLAUDE.md 'wrappedos preference and the "
        "documented skip cases'."
    )


def test_wrappedos_exempt_entries_are_live():
    """Staleness guard for ``_WRAPPEDOS_EXEMPT`` — same intent as
    ``test_resolver_exempt_entries_refer_to_real_files`` in
    ``test_cas_dir_resolver_contract.py``, but stronger: because entries are
    keyed by line *text*, this asserts each entry's file exists AND the exact
    line still appears in it. An entry that no longer matches any line means the
    code changed (or the entry has a typo) — re-verify the skip is still valid,
    then update the entry. This is what makes content-keying safe: a drifted
    entry fails here loudly instead of silently exempting nothing."""
    src_dir = os.path.dirname(__file__)
    stale = []
    for basename, line_text in _WRAPPEDOS_EXEMPT:
        path = os.path.join(src_dir, basename)
        if not os.path.exists(path):
            stale.append(f"{basename}: file does not exist")
            continue
        with open(path) as fh:
            present = {ln.strip() for ln in fh.read().splitlines()}
        if line_text not in present:
            stale.append(f"{basename}: no line matches {line_text!r}")
    assert not stale, (
        "_WRAPPEDOS_EXEMPT has stale entries (the file or line changed). "
        "Re-verify each skip is still valid, then update the entry:\n" + "\n".join(f"  {s}" for s in stale)
    )
