"""Lint-test: every consumer of ``add_cas_directory_arguments`` must
follow up with either ``apptools.parseargs(...)`` (which runs
``_commonsubstitutions`` and therefore the resolver) or an explicit
call to ``apptools.resolve_cas_directory_arguments(...)``.

This contract exists because the variant-suffix auto-append for the
four ``cas-*dir`` paths lives inside ``resolve_cas_directory_arguments``
(called from ``_commonsubstitutions``). Tools that bypass
``apptools.parseargs`` and call ``cap.parse_args(argv)`` directly will
see unsuffixed paths and silently read the wrong (parent) directory.

Mirrors the grep-based pattern of ``test_anchor_root_required.py``:
the contract is static, so a file-text check is enough and avoids
importing every entry point.
"""

import os
import re

import pytest


def _production_python_files():
    src_dir = os.path.dirname(__file__)
    for fname in os.listdir(src_dir):
        if fname.endswith(".py") and not fname.startswith("test_"):
            yield os.path.join(src_dir, fname)


# Files allowed to call add_cas_directory_arguments / add_output_directory_arguments
# without a paired parseargs or resolver. Each entry must be a genuinely
# non-resolving caller — a tool that registers the args for layered-config
# participation but never reads the parsed cas_*dir values, or an arg-registrar
# helper whose actual parsing happens in its caller.
#
# - namer.py: ``Namer.add_arguments`` is a static method that registers args on a
#   parser supplied by callers (ct-cake, the rest go through apptools.parseargs).
#   Namer itself does not parse — it just defines.
# - timing_report.py: registers add_output_directory_arguments to inherit
#   --bindir / --diagnostics-dir layering, but never reads args.cas_*dir.
_RESOLVER_EXEMPT: frozenset[str] = frozenset({"namer.py", "timing_report.py"})


# apptools itself defines these and chains them through _commonsubstitutions;
# the contract doesn't apply to the definition module.
_DEFINITION_FILES = frozenset({"apptools.py"})


_REGISTRAR_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:add_cas_directory_arguments|add_output_directory_arguments)\s*\("
)
_RESOLVER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:apptools\.parseargs|resolve_cas_directory_arguments)\s*\("
)


def _is_in_comment_or_string(text: str, pos: int) -> bool:
    """Best-effort check: skip matches inside a ``# ...`` line comment.

    Triple-quoted strings would require a real tokenizer; in practice
    the cas-dir helpers are never named inside docstrings outside
    apptools.py itself (which is exempt via ``_DEFINITION_FILES``)."""
    line_start = text.rfind("\n", 0, pos) + 1
    line_prefix = text[line_start:pos]
    return "#" in line_prefix.split('"')[0].split("'")[0]


def test_every_add_cas_directory_arguments_caller_resolves_or_parseargs():
    """Every non-apptools module that calls ``add_cas_directory_arguments``
    or ``add_output_directory_arguments`` must also reference
    ``apptools.parseargs(`` or ``resolve_cas_directory_arguments(`` in
    the same file.

    Why this matters: ``add_cas_directory_arguments`` only registers
    argparse defaults. The variant-suffix auto-append and the
    ``unsupplied``-sentinel fallback both live in
    ``apptools.resolve_cas_directory_arguments`` (called by
    ``_commonsubstitutions`` inside ``apptools.parseargs``). A tool
    that parses with bare ``cap.parse_args(argv)`` and skips both
    follow-ups will silently read unsuffixed cas-dir paths — the bug
    that made ``ct-cache-report`` report 0 entries against a
    populated cache when the user set ``cas-objdir = $HOME/cache/...``
    without a ``/<variant>`` suffix.
    """
    failures = []
    for path in _production_python_files():
        basename = os.path.basename(path)
        if basename in _DEFINITION_FILES or basename in _RESOLVER_EXEMPT:
            continue
        with open(path) as fh:
            text = fh.read()
        registrar_hits = [
            m for m in _REGISTRAR_RE.finditer(text) if not _is_in_comment_or_string(text, m.start())
        ]
        if not registrar_hits:
            continue
        resolver_hits = [
            m for m in _RESOLVER_RE.finditer(text) if not _is_in_comment_or_string(text, m.start())
        ]
        if not resolver_hits:
            first = registrar_hits[0]
            line = text[: first.start()].count("\n") + 1
            failures.append(f"{basename}:{line}")

    assert not failures, (
        "Files that call add_cas_directory_arguments / add_output_directory_arguments "
        "but do not also call apptools.parseargs( or resolve_cas_directory_arguments(:\n"
        + "\n".join(f"  {f}" for f in failures)
        + "\n\nFix: after `cap.parse_args(args=argv)`, add:\n"
        "    compiletools.apptools.resolve_cas_directory_arguments(args)\n"
        "Or route the entire parse through `compiletools.apptools.parseargs(cap, argv, ...)`."
    )


def test_resolver_exempt_entries_refer_to_real_files():
    """Typo guard for ``_RESOLVER_EXEMPT`` — same shape as
    ``test_pinned_cli_allowlist_is_documented`` in
    ``test_entry_point_surface.py``."""
    src_dir = os.path.dirname(__file__)
    missing = [name for name in _RESOLVER_EXEMPT if not os.path.exists(os.path.join(src_dir, name))]
    assert not missing, f"_RESOLVER_EXEMPT references non-existent files: {missing}"


@pytest.mark.parametrize("known", ["cache_report.py", "trim_cache_main.py"])
def test_known_diagnostic_tools_have_the_resolver_wired(known):
    """Belt-and-braces positive assertion: both diagnostic entry
    points that originally exhibited the bug DO call the resolver.
    Catches a reviewer accidentally reverting one of the two
    one-line wirings without also turning off the lint."""
    src_dir = os.path.dirname(__file__)
    with open(os.path.join(src_dir, known)) as fh:
        text = fh.read()
    assert "resolve_cas_directory_arguments" in text, (
        f"{known} no longer calls resolve_cas_directory_arguments; the "
        "variant-suffix bug will silently re-appear."
    )
