"""Lint-test: every file that calls ``apptools.add_otel_export_arguments``
must also call ``apptools.validate_otel_timing_pair``.

Why: ``--otel-export`` requires ``--timing`` to produce a non-empty span
tree. P1 will wire ``validate_otel_timing_pair`` to flip
``args.timing = True`` and hard-error on the explicit
``--otel-export --no-timing`` combo. Forgetting to call the validator
in a new ``ct-*`` entry point would let the empty-tree footgun resurface.

``ct-cache-report`` has no ``--timing`` concept; the validator would be
a no-op there, so it is exempted explicitly.

Applies no comment/string filter for the validator check: a
commented-out ``validate_otel_timing_pair(args)`` would satisfy
the lint. Acceptable today because production callers are few and
readable by inspection; revisit if the call corpus grows."""

import os
import re

_REGISTRAR_RE = re.compile(r"(?<![A-Za-z0-9_])add_otel_export_arguments\s*\(")
_VALIDATOR_RE = re.compile(r"(?<![A-Za-z0-9_])validate_otel_timing_pair\s*\(")

# apptools.py defines both; the contract doesn't apply to the definition module.
_DEFINITION_FILES = frozenset({"apptools.py"})

# Tools that register the OTel arg group but legitimately do NOT need the
# timing-pair validator. Add a comment alongside each entry explaining why.
#
# - cache_report.py: ct-cache-report emits cache-health metrics, has no
#   --timing concept; validate_otel_timing_pair would be a no-op for it.
#   (Allowlisted in anticipation of P3 landing the --otel-export wiring
#   in cache_report.py; harmless until then since it doesn't yet call
#   add_otel_export_arguments.)
_EXEMPT: frozenset[str] = frozenset({"cache_report.py"})


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for fname in os.listdir(src_dir):
        if fname.endswith(".py") and not fname.startswith("test_"):
            yield os.path.join(src_dir, fname)


def test_every_otel_registrar_caller_also_validates():
    """Every non-apptools, non-exempt module that calls
    ``add_otel_export_arguments`` must also call
    ``validate_otel_timing_pair`` in the same file."""
    failures = []
    for path in _production_python_files():
        basename = os.path.basename(path)
        if basename in _DEFINITION_FILES or basename in _EXEMPT:
            continue
        with open(path) as fh:
            text = fh.read()
        registrar_match = _REGISTRAR_RE.search(text)
        if not registrar_match:
            continue
        if not _VALIDATOR_RE.search(text):
            line = text[: registrar_match.start()].count("\n") + 1
            failures.append(f"{basename}:{line}")
    assert not failures, (
        "Files that call add_otel_export_arguments but not "
        "validate_otel_timing_pair:\n"
        + "\n".join(f"  {f}" for f in failures)
        + "\n\nFix: add compiletools.apptools.validate_otel_timing_pair(args) "
        "immediately after the apptools.parseargs(...) call."
    )


def test_exempt_entries_refer_to_real_files():
    """Typo guard for ``_EXEMPT``."""
    src_dir = os.path.dirname(__file__)
    missing = [name for name in _EXEMPT if not os.path.exists(os.path.join(src_dir, name))]
    assert not missing, f"_EXEMPT references non-existent files: {missing}"
