# Bug: `prebuild-script` does not accumulate across conf-file layers, contradicting its documentation

**Version:** 10.2.1 (also present in 10.2.0; the parsing code predates both)
**Component:** `_AccumulatingConfigFileParser` (`src/compiletools/apptools_argparse.py`)
**Severity:** medium — silent misconfiguration; a project-level conf file drops
globally-configured build hooks with no warning
**Also affected:** `postbuild-script` (same declaration pattern, same parser path)

## Summary

Both the README and the `--prebuild-script` help text promise that hook-script
entries accumulate across configuration layers. The conf-file parser does not
deliver this: when two conf files in the layer hierarchy each set
`prebuild-script`, the higher-priority file **replaces** the lower one instead
of appending to it. A project `ct.conf` that adds its own prebuild hook
silently drops any hook configured in `ct.conf.d/ct.conf` — there is no
warning, and the dropped script simply never runs.

## Documented behavior

`src/compiletools/README.ct-cake.rst:286-290`:

> Each option may be given multiple times. Like other `action="append"`
> options, the entries **accumulate** across all configuration layers
> (bundled < system < venv < user < project < cwd < env < CLI) — a
> project's `ct.conf` listing one script plus a variant `.conf`
> listing another yields both, in declaration order.

The same claim is shipped in the `--prebuild-script` help text
(`src/compiletools/cake.py:453-455`): *"accumulates across ct.conf layers
(bundled < system < user < project < variant < env < CLI)"*.

## Actual behavior

Only keys prefixed `append-` / `prepend-` accumulate across conf layers.
Every other duplicated key — including `prebuild-script` — is a plain dict
overwrite, so the last (highest-priority) conf file wins:

```python
# src/compiletools/apptools_argparse.py:1182-1192 (_AccumulatingConfigFileParser.parse)
if key.startswith(("append-", "prepend-")) and key in items:
    ...
    items[key] = existing
else:
    items[key] = value        # last-writer-wins for ALL other keys
```

`_ComposingArgumentParser._open_config_files` concatenates all conf files into
a single stream (highest priority last), so by the time configargparse sees
the key there is only one surviving value — the argparse `action="append"` on
the option never gets a second occurrence to append.

The behavior is also internally inconsistent: CLI values **do** accumulate
after conf-file values, because `_extract_cli_append_prepend` pops every
`argparse._AppendAction` option (not just `append-*`/`prepend-*` keys) from
argv and re-appends the CLI values after the conf values. So conf+CLI merges,
but conf+conf replaces.

## Why it matters

The natural deployment pattern is a repo-wide generator hook in
`ct.conf.d/ct.conf` (e.g. the generated-implementation-file pattern the README
itself recommends for embedding version info) plus occasional per-project
hooks. Any project that adds its own `prebuild-script` silently disables the
global generator for that project's builds. Depending on what the generator
maintains, the result is a link error at best, or — if a stale generated file
is left on disk from a previous build — a successfully linked binary carrying
stale generated content, with no diagnostic anywhere.

The current workaround is for every overriding project to re-list the global
hook in a single-line JSON list, which callers can only learn about by being
bitten.

## Reproduction / test

The test below fails on 10.2.1 exactly at the documented claim, with two
control tests pinning the behaviors that already work (so a fix can be
verified against all three). Drop it into `src/compiletools/` (or run from
anywhere with compiletools importable) and run with pytest.

```python
"""Repro test: ``prebuild-script`` does not accumulate across conf-file
layers, contradicting README.ct-cake.rst ("the entries **accumulate**
across all configuration layers") and the ``--prebuild-script`` help
text ("accumulates across ct.conf layers").

The two control tests pin the behaviors that DO work, so a fix can be
verified against all three.
"""

import configargparse
import pytest

import compiletools.apptools_argparse as aap


def _make_parser(config_files):
    """Parser with the same kwargs create_parser() uses for the real
    config-aware parser (include_config=True path)."""
    return aap._ComposingArgumentParser(
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter,
        auto_env_var_prefix="",
        default_config_files=[str(f) for f in config_files],
        args_for_setting_config_path=["-c", "--config"],
        ignore_unknown_config_file_keys=True,
        conflict_handler="resolve",
        config_file_parser_class=aap._AccumulatingConfigFileParser,
    )


@pytest.fixture
def conf_layers(tmp_path):
    """Two conf files, low priority first (mirrors a system-level
    ct.conf.d/ct.conf below a <project>/ct.conf in
    default_config_files order)."""
    low = tmp_path / "global.conf"
    high = tmp_path / "project.conf"
    return low, high


def test_prebuild_script_accumulates_across_conf_layers(conf_layers):
    """FAILS on 10.2.1: the project layer replaces the global layer."""
    low, high = conf_layers
    low.write_text("prebuild-script = ./gen_version_info.sh\n")
    high.write_text("prebuild-script = ./project_hook.sh\n")

    parser = _make_parser([low, high])
    parser.add_argument(
        "--prebuild-script", dest="prebuild_scripts", action="append", default=[]
    )
    args, _ = parser.parse_known_args([])

    # Documented order: lower layer first, higher layer after.
    assert args.prebuild_scripts == [
        "./gen_version_info.sh",
        "./project_hook.sh",
    ]


def test_control_append_prefix_accumulates_across_conf_layers(conf_layers):
    """PASSES: append-* keys accumulate across the same two layers."""
    low, high = conf_layers
    low.write_text("append-CPPFLAGS = -DLOW\n")
    high.write_text("append-CPPFLAGS = -DHIGH\n")

    parser = _make_parser([low, high])
    parser.add_argument(
        "--append-CPPFLAGS", dest="append_cppflags", action="append", default=[]
    )
    args, _ = parser.parse_known_args([])

    assert args.append_cppflags == ["-DLOW", "-DHIGH"]


def test_control_prebuild_script_accumulates_conf_plus_cli(conf_layers):
    """PASSES: a CLI value accumulates after the conf-file value."""
    low, _ = conf_layers
    low.write_text("prebuild-script = ./gen_version_info.sh\n")

    parser = _make_parser([low])
    parser.add_argument(
        "--prebuild-script", dest="prebuild_scripts", action="append", default=[]
    )
    args, _ = parser.parse_known_args(["--prebuild-script=./from_cli.sh"])

    assert args.prebuild_scripts == ["./gen_version_info.sh", "./from_cli.sh"]
```

Observed output against this checkout's venv (Python 3.13.5, compiletools
10.2.1):

```
test_prebuild_script_conf_layering.py::test_prebuild_script_accumulates_across_conf_layers FAILED
test_prebuild_script_conf_layering.py::test_control_append_prefix_accumulates_across_conf_layers PASSED
test_prebuild_script_conf_layering.py::test_control_prebuild_script_accumulates_conf_plus_cli PASSED

E       AssertionError: assert ['./project_hook.sh'] == ['./gen_versi...ject_hook.sh']
========================= 1 failed, 2 passed in 0.07s ==========================
```

The failing assertion is precisely the README's worked scenario: two layers
each contributing one script should "yield both, in declaration order" —
instead only the higher layer's script survives.

## How this went unnoticed

`src/compiletools/test_cake_hooks.py` exercises multiple prebuild scripts only
via direct assignment to `args.prebuild_scripts` and via the CLI — there is no
test that feeds `prebuild-script` through two conf-file layers. The first test
above fills that gap.

## Suggested fix

Either the code or the docs must move; shipping both as-is is the one
indefensible state.

**Preferred: make the code match the docs.** Extend the accumulation
predicate at `apptools_argparse.py:1182` to also accumulate a whitelist of
known append-action hook keys:

```python
_ACCUMULATING_KEYS = {"prebuild-script", "postbuild-script"}

if (key.startswith(("append-", "prepend-")) or key in _ACCUMULATING_KEYS) and key in items:
```

(A fully general action-aware check — accumulate any key whose registered
argparse action is `_AppendAction` — would be cleaner but requires giving
`_AccumulatingConfigFileParser.parse()` a back-reference to the parser's
actions; the string whitelist matches the existing prefix-based design.)

Everything downstream already handles the list form:
`convert_item_to_command_line_arg` emits one `--prebuild-script=<elem>` token
per list element, and multi-script execution order is already tested
(`test_cake_hooks.py::test_multiple_prebuild_scripts_run_in_declaration_order`).

**Compatibility note for the changelog:** any consumer currently using a
project-level `prebuild-script` to deliberately *suppress* a global hook will
silently regain that hook, and consumers that re-list the global hook as a
workaround will run it twice (harmless if the hook is idempotent, which the
README's recommended write-if-changed pattern is). If explicit suppression is
worth supporting, an explicit empty-list syntax (`prebuild-script = []`
clearing accumulated entries) is a follow-up option.

**If the override semantic is instead declared intentional:** correct
`README.ct-cake.rst:286-290`, the `--prebuild-script` help text
(`cake.py:453-455`), and the `--postbuild-script` help text
(`cake.py:470-472`) to state that conf layers replace rather than accumulate,
and consider a warning when a duplicated non-`append-`/`prepend-` key is
discarded — the silent drop is the damaging part.
