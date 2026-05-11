"""Lint test: every ct-* entry point in pyproject.toml exposes the
standard apptools-derived flag surface (``--version``, ``--help``,
``-?``, ``--man``) on its ``--help`` output.

This is the systemic anti-regression test for the "stop missing flags"
goal of ``compiletools.apptools``. ``add_base_arguments`` enforces the
minimum surface for every caller that goes through it, but nothing
detects a tool that hand-rolls ``argparse.ArgumentParser`` and
silently drops one of the standard flags. This test plugs that gap by
walking ``[project.scripts]`` in ``pyproject.toml``, importing each
entry point, invoking it with ``--help``, and asserting the help
output advertises the required flags.

Adding a new ct-* entry point that bypasses apptools without an
explicit allowlist entry will fail this test on the first CI run.
"""

from __future__ import annotations

import importlib
import signal
import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Tools whose CLI surface is intentionally minimal because they're invoked
# from generated build recipes (Makefile / Ninja / Shake / Slurm) with
# pinned argv shapes. Adding the apptools surface to them would break
# those recipes — they parse argv with a fixed contract. They DO carry
# ``--version`` (cheap and useful for diagnostics) but NOT the rest.
PINNED_CLI_TOOLS: frozenset[str] = frozenset(
    {
        "ct-cas-publish",
        "ct-lock-helper",
    }
)

# The minimum-help surface every other ct-* tool MUST advertise.
REQUIRED_HELP_TOKENS: tuple[str, ...] = (
    "--version",
    "--help",
    "-?",
    "--man",
)


def _entry_points() -> list[tuple[str, str]]:
    """Return ``[(script_name, target)]`` for every ``ct-*`` entry point.

    Read from ``pyproject.toml`` so the test pins to the public CLI
    surface — adding a new ct-* tool to the project automatically pulls
    it into this audit without anyone editing the test.
    """
    pyproject_bytes = (REPO_ROOT / "pyproject.toml").read_bytes()
    data = tomllib.loads(pyproject_bytes.decode())
    scripts = data.get("project", {}).get("scripts", {})
    return sorted((name, target) for name, target in scripts.items() if name.startswith("ct-"))


@pytest.mark.parametrize("script_name,target", _entry_points())
def test_entry_point_help_surface(
    script_name: str,
    target: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ct-X --help`` must mention every flag in ``REQUIRED_HELP_TOKENS``.

    Pinned-CLI tools (see ``PINNED_CLI_TOOLS``) are exempt from the
    common surface but must still advertise ``--version``.
    """
    module_name, attr = target.split(":")
    module = importlib.import_module(module_name)
    main = getattr(module, attr)

    # Some legacy mains read sys.argv directly instead of taking argv.
    # Set sys.argv as a fallback so they still see ``--help``.
    monkeypatch.setattr(sys, "argv", [script_name, "--help"])

    # Snapshot SIGINT/SIGTERM handlers — installing them BEFORE parse_args
    # leaks the entry point's handler into the caller's process for
    # --help/--version (both of which raise SystemExit before the caller
    # would normally restore them). Caught ct_lock_helper doing exactly
    # this; the assertion below keeps the regression from coming back.
    saved_sigint = signal.getsignal(signal.SIGINT)
    saved_sigterm = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as exc:
            try:
                main(["--help"])
            except TypeError:
                # main() doesn't take argv — already covered by sys.argv above.
                main()
    finally:
        # Always restore so a regression in one parametrized case doesn't
        # cascade into the next.
        signal.signal(signal.SIGINT, saved_sigint)
        signal.signal(signal.SIGTERM, saved_sigterm)

    assert exc.value.code == 0, (
        f"{script_name} --help exited {exc.value.code}; expected 0. "
        "If the tool fails to build its parser, fix that before adding "
        "it to PINNED_CLI_TOOLS."
    )

    new_sigint = signal.getsignal(signal.SIGINT)
    new_sigterm = signal.getsignal(signal.SIGTERM)
    assert new_sigint is saved_sigint and new_sigterm is saved_sigterm, (
        f"{script_name} --help mutated the caller's signal handlers "
        f"(SIGINT: {saved_sigint!r} → {new_sigint!r}, SIGTERM: "
        f"{saved_sigterm!r} → {new_sigterm!r}). Fix: wrap the work in "
        f"``with apptools.graceful_shutdown(handler, *signums):`` (see "
        f"apptools.py for the helper, and cake.py / ct_lock_helper.py / "
        f"locking._run_with_signal_forwarding / trace_backend.execute "
        f"for the canonical call sites)."
    )

    captured = capsys.readouterr()
    out = captured.out + captured.err  # argparse may write help to either

    if script_name in PINNED_CLI_TOOLS:
        # Pinned helpers only need --version (cheap diagnostic).
        assert "--version" in out, (
            f"{script_name} is in PINNED_CLI_TOOLS but its --help output "
            f"does not advertise --version. Pinned helpers must still carry "
            f"--version for build-recipe diagnostics.\n\nFull --help output:\n{out}"
        )
        return

    missing = [flag for flag in REQUIRED_HELP_TOKENS if flag not in out]
    assert not missing, (
        f"{script_name} --help is missing required flag(s): {missing}.\n"
        f"Likely fix: route the parser through ``apptools.create_parser`` and "
        f"call ``apptools.add_base_arguments`` (see ct-cake / ct-trim-cache "
        f"for the canonical pattern). If this tool is genuinely a pinned-CLI "
        f"build-recipe helper that cannot accept the standard surface, add it "
        f"to PINNED_CLI_TOOLS in this file with a one-line comment explaining "
        f"why.\n\nFull --help output:\n{out}"
    )


def test_pinned_cli_allowlist_is_documented() -> None:
    """Catch typos in PINNED_CLI_TOOLS by asserting every entry refers to a real ct-* entry point."""
    real = {name for name, _target in _entry_points()}
    bogus = PINNED_CLI_TOOLS - real
    assert not bogus, (
        f"PINNED_CLI_TOOLS contains entries that aren't actually ct-* entry "
        f"points in pyproject.toml: {sorted(bogus)}. Either fix the typo or "
        f"remove the stale allowlist entry."
    )
