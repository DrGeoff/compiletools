"""Lint-test: every ``--otel-*`` ``add_argument`` lives inside
``compiletools.apptools.add_otel_export_arguments``.

Why: the OTel arg group must stay DRY across every ``ct-*`` entry point.
Hand-rolling a ``--otel-foo`` in a sibling tool would drift defaults,
env-var rules, and help text; future PRs (P3, P5) add new ``ct-*`` tools
that need the same surface.

Patterned on ``test_cas_dir_resolver_contract.py``; that file applies a
comment/string filter for false-positive suppression which is omitted
here because the ``--otel-*`` surface is small enough that no production
file currently mentions the pattern outside a real registration."""

import os
import re

_OTEL_ARG_RE = re.compile(r'add_argument\s*\(\s*["\']--otel-[A-Za-z0-9_-]+["\']')
# [^)]* matches newlines (it's a negated class, not `.`), so no re.DOTALL needed.
_FLAG_ARG_RE = re.compile(r'add_flag_argument\s*\([^)]*name\s*=\s*["\']otel-[A-Za-z0-9_-]+["\']')

# apptools.py is THE registrar; the contract is about every OTHER file.
_DEFINITION_FILES = frozenset({"apptools.py"})

# No exemptions at landing; add an entry only if a future tool genuinely
# needs to declare a one-off --otel-* arg outside the shared helper
# (and explain why in a comment alongside the entry).
_EXEMPT: frozenset[str] = frozenset()


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for fname in os.listdir(src_dir):
        if fname.endswith(".py") and not fname.startswith("test_"):
            yield os.path.join(src_dir, fname)


def test_no_otel_args_outside_apptools_helper():
    """Every ``--otel-*`` argparse registration must be inside
    ``apptools.add_otel_export_arguments``. Hand-rolling one elsewhere
    is the bug this lint catches."""
    failures = []
    for path in _production_python_files():
        basename = os.path.basename(path)
        if basename in _DEFINITION_FILES or basename in _EXEMPT:
            continue
        with open(path) as fh:
            text = fh.read()
        hits = list(_OTEL_ARG_RE.finditer(text)) + list(_FLAG_ARG_RE.finditer(text))
        for hit in hits:
            line = text[: hit.start()].count("\n") + 1
            failures.append(f"{basename}:{line}: {text[hit.start() : hit.end()][:80]}")
    assert not failures, (
        "Hand-rolled --otel-* registrations found outside "
        "apptools.add_otel_export_arguments:\n"
        + "\n".join(f"  {f}" for f in failures)
        + "\n\nFix: call compiletools.apptools.add_otel_export_arguments(cap) "
        "in the offending file's parseargs flow, and delete the inline "
        "add_argument."
    )


def test_exempt_entries_refer_to_real_files():
    """Typo guard for ``_EXEMPT``."""
    src_dir = os.path.dirname(__file__)
    missing = [name for name in _EXEMPT if not os.path.exists(os.path.join(src_dir, name))]
    assert not missing, f"_EXEMPT references non-existent files: {missing}"
